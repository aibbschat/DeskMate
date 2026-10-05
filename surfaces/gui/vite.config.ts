import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// 应用版本：以 tauri.conf.json 为单一数据源，构建时注入到 import.meta.env.VITE_APP_VERSION，
// 使侧边栏品牌标能显示真实版本号（如 1.2.0），无需在两处手动同步。
function readAppVersion(): string {
  try {
    const conf = JSON.parse(
      fs.readFileSync(path.resolve(__dirname, "src-tauri/tauri.conf.json"), "utf-8"),
    );
    if (conf?.version) return String(conf.version);
  } catch {
    /* 读不到则用占位值，构建不因此中断 */
  }
  return "0.0.0";
}
const APP_VERSION = readAppVersion();

// `base: "./"` makes built asset URLs relative, so the bundle loads from the `tauri://`
// origin in the desktop shell (absolute `/assets` 404s there); a server-hosted build is
// unaffected. Dev runs on a fixed port (1420) with strictPort so the Tauri webview always
// loads the vite instance Tauri itself spawns (a drifting port would make the window load a
// stale/other server). `tauri.conf.json` devUrl must match this.
export default defineConfig(({ command }) => {
  let devToken = "";
  if (command === "serve") {
    const state =
      process.env.COWORKER_STATE_DIR ||
      (process.platform === "win32"
        ? path.join(process.env.APPDATA || os.homedir(), "coworker")
        : path.join(os.homedir(), ".config", "coworker"));
    try {
      devToken = fs.readFileSync(path.join(state, "sidecar-8765.token"), "utf8").trim();
    } catch {
      // The Tauri dev shell injects its in-memory token at runtime. Plain browser dev
      // shows the normal startup retry until the standalone server/token file exists.
    }
  }
  return {
    base: "./",
    plugins: [react()],
    // 双入口：桌面 index.html + 手机端 mobile.html。
    // 一次 `npm run build` 同时产出两端，dist/ 仍是一个目录 —— 守住 docs/DEPLOY-PARITY.md
    // 的铁律（只构建一次、本地与云端同一份字节），scripts/deploy_web.sh 一行都不用改。
    // ⚠ mobile.html 必须留在 dist **根**（产物即 dist/mobile.html）：`base:"./"` 让资源走
    // 相对路径，若塞进 /m/ 子目录，页面里的 ./assets/x.js 会解析成 /m/assets/x.js → 404。
    // 想要短 URL 就在 Caddy 里 `redir /m /mobile.html`——用 redir 不用 rewrite，
    // rewrite 会保留 /m/ 地址，从而踩同一个相对路径坑。
    build: {
      rollupOptions: {
        input: {
          main: path.resolve(__dirname, "index.html"),
          mobile: path.resolve(__dirname, "mobile.html"),
        },
      },
    },
    server: { port: 1420, strictPort: true },
    define: {
      // Never bake environment values (token/URL/port) into a deployed bundle: local and cloud
      // must ship the same artifact, so anything environment-specific is resolved at RUNTIME
      // (window.__COWORKER_* globals, or same-origin paths behind the reverse proxy).
      // See docs/DEPLOY-PARITY.md.
      __COWORKER_DEV_TOKEN__: JSON.stringify(devToken),
      "import.meta.env.VITE_APP_VERSION": JSON.stringify(APP_VERSION),
    },
    // Tauri CLI looks for these; harmless for the browser build.
    clearScreen: false,
    envPrefix: ["VITE_", "TAURI_"],
  };
});
