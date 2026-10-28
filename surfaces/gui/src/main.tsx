import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
// import "./i18n";
import { initTheme } from "./theme";
import { initTextSize } from "./textSize";
import { initSkin } from "./components/deskmate/skins/skinStore";
import { platformOS } from "./tauri";
import "./tailwind.css";
import "./styles.css";

initTheme();
// 字号档位：index.html 已预置 data-text-size，这里再同步一次（localStorage 被改过/首帧脚本
// 被 CSP 拦掉时的双保险）。必须在 React 渲染前调用，避免一次字号跳动。
initTextSize();
// Apply the saved skin's CSS variables to <html> pre-React. The store module also applies
// itself on import, so this is belt-and-suspenders — keeps the boot path explicit & greppable.
initSkin();
// Platform hook for CSS (html[data-platform="windows"] scrollbar styling etc.).
document.documentElement.dataset.platform = platformOS();

// A file dropped OUTSIDE a drop target (the composer) must never navigate the webview to the
// file itself — the browser/WKWebView default. Drop targets stopPropagation-free preventDefault
// in their own handlers; these guards only catch the misses. (The desktop shell disables Tauri's
// native drag-drop interception so HTML5 drag events reach the DOM at all — see lib.rs.)
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
