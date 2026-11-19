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
  // 2026-10-09 事故自检①：vite build 也会加载 .env.local / .env.production* 等文件——
  // 曾有人在这些文件里写 VITE_COWORKER_HTTP=http://127.0.0.1:8765（本意只给 npm run dev），
  // 结果该值被 define 成字面量烘进部署 bundle，httpBase 的浏览器同源分支整条变死代码，
  // 云端所有 HTTP API 打到访客本机（全息屏/个人中心全空）。本地开发的 env 只许放
  // .env.development.local（mode=development 才加载）。构建时发现即报错。
  if (command === "build") {
    for (const envFile of [".env", ".env.local", ".env.production", ".env.production.local"]) {
      const p = path.resolve(__dirname, envFile);
      if (fs.existsSync(p) && /VITE_COWORKER_HTTP\s*=/.test(fs.readFileSync(p, "utf8"))) {
        throw new Error(
          `[env-guard] ${envFile} 定义了 VITE_COWORKER_HTTP，vite build 会把它烘焙进 bundle ` +
            `（部署后云端 API 全挂）。本地开发请改放 .env.development.local，` +
            `或删除该行（桌面端走运行时注入，无需它）。`,
        );
      }
    }
  }
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
        // 2026-11-19 PR9：主包 752KB / styles 541KB 触发 Vite 500KB 告警。
        // 把稳定的第三方库拆成独立 chunk（vendor-react / vendor-md / vendor-i18n），
        // 业务代码改动时这些 chunk 的哈希不变，浏览器可长期缓存。
        output: {
          manualChunks(id: string) {
            if (!id.includes("node_modules")) return undefined;
            if (/node_modules\/(react|react-dom|scheduler|react\/)/.test(id)) return "vendor-react";
            if (/node_modules\/(react-markdown|remark-|mdast-|micromark|unified|unist-|vfile|hast-|mdast-util)/.test(id)) return "vendor-md";
            if (/node_modules\/(i18next|react-i18next)/.test(id)) return "vendor-i18n";
            return undefined;
          },
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
    plugins: [
      react(),
      // 2026-10-09 事故自检②：产物级断言兜底。httpBase 的浏览器同源分支被 minifier 折叠后
      // 会失去 `?"":` 三元特征（env-guard 漏网时第二道防线）。
      {
        name: "assert-httpbase-not-folded",
        closeBundle() {
          const dir = path.resolve(__dirname, "dist/assets");
          if (!fs.existsSync(dir)) return;
          for (const f of fs.readdirSync(dir)) {
            if (!f.endsWith(".js")) continue;
            const s = fs.readFileSync(path.join(dir, f), "utf8");
            const i = s.indexOf("__COWORKER_HTTP__");
            if (i < 0) continue;
            const body = s.slice(i, i + 250);
            if (!body.includes('?"":')) {
              throw new Error(
                `[assert-httpbase-not-folded] dist/assets/${f} 中 httpBase 的浏览器同源分支疑似被折叠` +
                  `（附近 250 字符无 ?"": 三元特征）。最常见原因 = env 文件定义了 VITE_COWORKER_HTTP。`,
              );
            }
            return; // 只检查第一个命中（httpBase 定义处）
          }
        },
      },
    ],
  };
});
