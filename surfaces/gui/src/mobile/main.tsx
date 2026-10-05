/**
 * 手机端启动入口（第二构建入口，产物 `dist/mobile.html`）。
 *
 * 顺序有讲究：
 *  1. 先 initTheme/initSkin —— 皮肤变量落到 <html>，否则首帧配色是出厂中性色；
 *  2. 再同步 theme-color —— 让手机状态栏跟着皮肤走（manifest 里那个只是兜底默认值）；
 *  3. 样式导入顺序：tailwind.css → styles.css（复用桌面组件的全局样式，含 .transcript/.md）
 *     → mobile.css（**最后**，窄屏覆写必须赢）。
 */
import React from "react";
import ReactDOM from "react-dom/client";
// i18n 必须显式初始化：桌面是靠 App.tsx 静态 import SettingsView 时顺带 side-effect 初始化的，
// 手机端不引 SettingsView，少了这一行 Transcript 就会把 `chat.assistant` 这种键名当名字渲染。
import "../i18n";
import { initTheme } from "../theme";
import { initSkin } from "../components/deskmate/skins/skinStore";
import "../tailwind.css";
import "../styles.css";
import "./mobile.css";
import { MobileApp } from "./App";

initTheme();
initSkin();

/** 状态栏色跟随当前皮肤的 --accent（换肤/切深浅色都重取）。 */
function syncThemeColor() {
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  if (!accent) return;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", accent);
}
syncThemeColor();
new MutationObserver(syncThemeColor).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["data-theme", "data-skin-pre"],
});

// iOS 首次加主屏后，standalone 启动时把地址栏残留的滚动位置归零
window.addEventListener("load", () => window.scrollTo(0, 0), { once: true });

const boot = document.getElementById("boot");
if (boot) boot.remove();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MobileApp />
  </React.StrictMode>,
);
