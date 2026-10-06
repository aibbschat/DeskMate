/**
 * 「装到主屏」这件事的可读状态 + 应用内入口（2026-10-06，安卓真机反馈）。
 *
 * 为什么要写在应用里：Chromium 的自动安装提示（地址栏右侧那个下载箭头、或底部小条）
 * 出现得没规律、也不解释「为什么没有」。**可安装 ≠ 会被提示**，所以必须自己抓
 * `beforeinstallprompt`（监听在 `mobile.html` 内联脚本里，见那里的注释——事件只发一次，
 * 晚挂监听就永久错过）并给一个明确的按钮。
 *
 * 顺带把 manifest 自检做进来：安卓装不上的病灶是 manifest 的 Content-Type 整个没有
 * （Caddy 的 mime 表里没 `.webmanifest`），补上 `application/manifest+json` 之后真机回读
 * 就是这一行报的「可以装」。这一行自检的意义：这类坏法**页面照常能用、浏览器不报错**，
 * 只是不出安装入口，而 iOS 压根不看 manifest——所以必须有个能在手机上 5 秒读出来的地方，
 * 不能再靠连 DevTools 或猜。
 */

export type InstallState = "installed" | "prompt" | "embedded" | "ios" | "android" | "other";

export interface InstallEnv {
  standalone: boolean;
  canPrompt: boolean;
  ua: string;
  /** 设备是否有触点（iPadOS 13+ 的 Safari 把 UA 报成 Macintosh，只能靠这个认） */
  touch?: boolean;
}

/** 浏览器菜单里那个动作在不同内核里叫法不一样，文案要给全，别让人找不到。 */
export function installStateOf(env: InstallEnv): InstallState {
  if (env.standalone) return "installed";
  if (env.canPrompt) return "prompt";
  const ua = String(env.ua || "").toLowerCase();
  if (/micromessenger|wechat|wxwork/.test(ua)) return "embedded";
  if (/iphone|ipad|ipod/.test(ua) || (/macintosh/.test(ua) && env.touch === true)) return "ios";
  if (/android/.test(ua)) {
    return /chrome|crios|edg|samsungbrowser|huaweibrowser|miuibrowser|oppobrowser|vivobrowser/.test(ua)
      ? "android"
      : "other";
  }
  return "other";
}

/** 自检页/门口那一行用的短标签（用户看得懂的说法，不是内部枚举名）。 */
export const INSTALL_STATE_LABEL: Record<InstallState, string> = {
  installed: "已装（现在就是独立窗口）",
  prompt: "可安装（浏览器已给弹窗）",
  embedded: "微信内置，装不了",
  ios: "iOS：走分享菜单添加",
  android: "安卓 Chromium：等弹窗或走菜单",
  other: "当前浏览器不认 manifest 安装",
};

export function installCopyOf(state: InstallState): { title: string; hint: string; action: "prompt" | null } {
  switch (state) {
    case "installed":
      return { title: "已经在这台手机上了", hint: "从桌面图标进就是她自己的窗口，不带浏览器地址栏", action: null };
    case "prompt":
      return { title: "可以装了", hint: "点下面这个按钮，桌面会出现「小桔」图标；装完打开就是她，不带地址栏", action: "prompt" };
    case "embedded":
      return {
        title: "这里装不了",
        hint: "微信内置浏览器不支持装到主屏。用「在浏览器打开」跳到系统浏览器，再回这一页点安装",
        action: null,
      };
    case "ios":
      return {
        title: "iPhone 这样装",
        hint: "Safari 底部「分享」→「添加到主屏幕」。装完的窗口不带地址栏，但她不在前台时不会自己找你（第一批没做推送）",
        action: null,
      };
    case "android":
      return {
        title: "浏览器没给安装提示",
        hint:
          "右上角菜单里找「安装应用」（Edge 在「应用」→「安装此网站为应用」，三星浏览器在「菜单」底部）。" +
          "实在找不到就选「添加到主屏幕」——那个一定有，区别只是打开时还带浏览器界面。" +
          "两样都没有的话换 Chrome 再进一次（个别安卓浏览器没做安装）",
        action: null,
      };
    default:
      return { title: "装到主屏", hint: "用 Chrome / Edge 打开这个网址，右上角菜单里选「安装应用」", action: null };
  }
}

export interface ManifestProbe {
  url: string;
  status: number | null;
  type: string;
  ok: boolean;
  note: string;
}

/** JSON 类 MIME 才算合格（Chromium 的判据：manifest+json / json / text/json）。 */
export function manifestOk(status: number | null, type: string): boolean {
  return status === 200 && /json/i.test(String(type || ""));
}

export function probeManifestFrom(probe: { url: string; status: number | null; type: string; err?: string }): ManifestProbe {
  const ok = !probe.err && manifestOk(probe.status, probe.type);
  const note = probe.err
    ? `读不到：${probe.err}`
    : probe.status !== 200
      ? `拿不到（HTTP ${probe.status}）→ 安卓不会出现安装入口`
      : !manifestOk(probe.status, probe.type)
        ? `Content-Type 是「${probe.type || "（空）"}」，不是 JSON 类 → 安卓会判它无效、不出安装入口`
        : "正常";
  return { url: probe.url, status: probe.status, type: probe.type, ok, note };
}

/** 真去取一次 `<link rel="manifest">` 指向的文件，只看状态与 Content-Type，不解析内容。 */
export async function probeManifest(): Promise<ManifestProbe> {
  const link = typeof document !== "undefined" ? document.querySelector('link[rel="manifest"]') : null;
  const href = link?.getAttribute("href") || "";
  if (!href) return probeManifestFrom({ url: "—", status: null, type: "", err: "页面没有挂 manifest" });
  const url = new URL(href, location.href).href;
  try {
    const r = await fetch(url, { cache: "no-store" });
    return probeManifestFrom({ url, status: r.status, type: r.headers.get("content-type") || "" });
  } catch (e: any) {
    return probeManifestFrom({ url, status: null, type: "", err: String(e?.message || e) });
  }
}

/**
 * 为什么这里**没有** Service Worker（别下次又当成缺口补上）：
 * 可安装判据里没有 SW 这一项（MDN「Making PWAs installable」2026-09 版：HTTPS + manifest 的
 * name/short_name、192 与 512 图标、start_url、display 就够；SW 只决定"离线可用"和那个更漂亮
 * 的自动提示）。设计稿 §六.5 / D5 当初也拍了不注册：这是自更新站点，SW 缓存会把旧版钉在手机上。
 * ⚠ 这条结论**没在本地验过**：Playwright 驱动的被控 Chrome 无论怎么配都不发
 * beforeinstallprompt（试了有/无 Content-Type、有/无 SW 三种，全 0 次），所以判据只能到手机上读
 * ——就是这一屏的「装到主屏」那一行。别再浪费时间想做本地自动化验证。
 */

/** 现在是不是已经以独立应用形态在跑（standalone / iOS 的 apple-managed 模式）。 */
export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  const mq = typeof window.matchMedia === "function" ? window.matchMedia("(display-mode: standalone)") : null;
  return !!(mq?.matches || (navigator as any).standalone === true);
}

/** 给纯判断用的环境快照（有触点 = iPadOS 把 UA 伪装成 Mac 的那条路）。 */
export function installEnv(): InstallEnv {
  return {
    standalone: isStandalone(),
    canPrompt: !!((globalThis as any).__COWORKER_BIP__),
    ua: typeof navigator !== "undefined" ? navigator.userAgent : "",
    touch: typeof globalThis !== "undefined" && "ontouchend" in globalThis,
  };
}
