// 内嵌 sidecar 页（Bot 群，将来还有创作圆桌）跑在 iframe 里，拿不到父文档的 CSS 变量。
// 与其让页面自己复刻 styles.css 的 9 套皮肤（上游一改，页面立刻说谎），不如把
// **计算后的** token 值原样送进去：styles.css 仍是单一真源，iframe 只是消费者。
//
// 变更检测用 MutationObserver 盯 <html> 的 data-*：主题（theme.ts）、字号档
// （textSize.ts）、皮肤（skinStore.ts）三套开关都写在那里，所以以后加皮肤不用动这里。
// iframe 与父文档谁先就绪没有保证，除了 onLoad 还接受页面的 ow-tokens-request 主动索取。
import { useEffect, useRef, useState } from "react";

// 只送设计 token。凭据、用户内容、内部状态一律不在这个列表里。
const TOKEN_KEYS = [
  "--paper", "--panel", "--ink", "--muted", "--faint", "--line", "--line-strong",
  "--accent", "--accent-soft", "--ok", "--ok-soft", "--ok-line", "--ok-dot",
  "--warn-ink", "--warn-soft", "--danger", "--danger-soft", "--teal-ink", "--teal-soft",
  "--solid", "--on-solid", "--scrim", "--mono",
  "--fs-label", "--fs-meta", "--fs-ui", "--fs-body", "--fs-heading", "--fs-title", "--fs-chat",
];

function readTokens() {
  const el = document.documentElement;
  const cs = getComputedStyle(el);
  const vars: Record<string, string> = {};
  for (const key of TOKEN_KEYS) {
    const v = cs.getPropertyValue(key).trim();
    if (v) vars[key] = v;
  }
  return {
    type: "ow-tokens" as const,
    theme: el.dataset.theme || "light",
    textSize: el.dataset.textSize || "default",
    vars,
  };
}

export function ThemedSidecarFrame({
  src,
  title,
  testId,
}: {
  src: string;
  title: string;
  testId?: string;
}) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  const [loaded, setLoaded] = useState(false);

  const push = () => {
    const win = ref.current?.contentWindow;
    if (!win) return;
    // sidecar 与 GUI 不同源（tauri://localhost vs http://127.0.0.1:<port>），
    // targetOrigin 只能给 "*"；载荷就是上面那份白名单 token，不含任何敏感值。
    win.postMessage(readTokens(), "*");
  };

  useEffect(() => {
    if (!loaded) return;
    push();
    const mo = new MutationObserver(push);
    mo.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-text-size", "data-skin-pre", "class", "style"],
    });
    return () => mo.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded]);

  // 页面自己起来时也会索取一次，避开 onLoad 与脚本注册的竞态
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (e.source === ref.current?.contentWindow && e.data?.type === "ow-tokens-request") push();
    };
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 换 sidecar 地址（端口变了 / 重连）等于换了个文档，需要重新握手
  useEffect(() => setLoaded(false), [src]);

  return (
    <iframe
      ref={ref}
      src={src}
      title={title}
      data-testid={testId}
      onLoad={() => setLoaded(true)}
    />
  );
}
