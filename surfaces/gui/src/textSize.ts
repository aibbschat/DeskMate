// 全局字号档位（上游 UX-048 follow-up）。三档：小 / 默认 / 大，按设备存 localStorage，
// 通过 <html data-text-size> 让 styles.css 里的 --fs-* token 整体换挡。
//
// 与 theme.ts 同构：index.html 在首帧前应用同一个 key，避免"先默认字号再变"的跳动。
//
// 为什么是 token 而不是 zoom：app 的文字尺寸原本散落在 ~1000 处 text-ui 硬编码里，
// zoom 会连布局宽度一起放大（侧栏变胖、聊天区被挤）。token 只换字号、布局不动 —— 这是
// 「字大一号」而不是「界面放大」，读长文/日记时这才是想要的。
import { useEffect, useState } from "react";

export type TextSize = "small" | "default" | "large";

const KEY = "openwork-text-size";
const PREF_EVENT = "openwork:text-size";

export function getTextSize(): TextSize {
  try {
    const v = localStorage.getItem(KEY);
    return v === "small" || v === "large" ? v : "default";
  } catch {
    // 隐私模式等读不到 localStorage —— 退回默认，不影响本次会话
    return "default";
  }
}

function apply(size: TextSize) {
  // default 不挂属性：styles.css 的 :root 默认值就是出厂档，未设置时渲染与历史版本逐像素一致。
  if (size === "default") delete document.documentElement.dataset.textSize;
  else document.documentElement.dataset.textSize = size;
}

export function setTextSize(size: TextSize) {
  try {
    if (size === "default") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, size);
  } catch {
    /* 存不下也要让本次会话生效 */
  }
  apply(size);
  window.dispatchEvent(new CustomEvent(PREF_EVENT));
}

/** 启动时调一次（main.tsx / mobile/main.tsx）。 */
export function initTextSize() {
  apply(getTextSize());
}

/** 设置页控件的 hook —— 别处改了这个偏好也能同步。 */
export function useTextSize(): [TextSize, (s: TextSize) => void] {
  const [size, setSize] = useState<TextSize>(getTextSize);
  useEffect(() => {
    const sync = () => setSize(getTextSize());
    window.addEventListener(PREF_EVENT, sync);
    return () => window.removeEventListener(PREF_EVENT, sync);
  }, []);
  return [size, setTextSize];
}
