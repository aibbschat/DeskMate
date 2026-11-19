// HTTP 基础设施（从 api.ts 机械抽取，PR16/#33）：endpoint 解析 / 鉴权 fetch 包装 / WebSocket。
// 所有 REST 域模块共用此处的 fetch（自动加 X-OpenWorker-Token），行为与抽取前一致。
declare const __COWORKER_DEV_TOKEN__: string;

// Endpoint resolution order: runtime-injected globals (Tauri sets window.__COWORKER_HTTP__
// for its dynamically-chosen sidecar port) → Vite env → an environment-appropriate default.
//
// The default must NOT be a hardcoded http://127.0.0.1:8765 for hosted builds. A web build
// served over http(s) (e.g. xj.aibbs.chat behind Caddy/nginx, which reverse-proxies /v1, /ws,
// /roundtable, /tasks, /message:* to the sidecar on 127.0.0.1:8765) injects neither global nor
// Vite env, so the old default made the browser dial the *visitor's own* 127.0.0.1:8765 —
// every request failed (personal center empty, no model list, chat stuck on "模型加载中"),
// and no amount of hard-refreshing could recover it. Same-origin ("") is the correct default
// for a hosted page because it is exactly what the reverse proxy is there to serve.
export const isBrowserPage = (): boolean =>
  typeof window !== "undefined" && typeof document !== "undefined";

// Empty string = same origin. The Tauri shell always injects __COWORKER_HTTP__, and browser
// "npm run dev" sets VITE_COWORKER_HTTP, so this only affects hosted builds that want the
// reverse proxy in front of them.
// ⚠️ 2026-10-09 事故教训：曾因 surfaces/gui/.env.local 写了 VITE_COWORKER_HTTP=http://127.0.0.1:8765，
// 而 vite build 也会加载 .env.local → 该值被 define 成字面量烘进 bundle → 浏览器同源分支整条
// 变死代码 → 云端所有 HTTP API 打到访客本机（全息屏/个人中心全空）。约定：本地开发用的
// VITE_COWORKER_HTTP 只许写在 .env.development.local（build 的 mode=production 不加载），
// 且 vite.config.ts 的 closeBundle 自检会拦截误烘焙。
export const httpBase = (): string =>
  (globalThis as any).__COWORKER_HTTP__ ||
  (import.meta as any).env?.VITE_COWORKER_HTTP ||
  (isBrowserPage() ? "" : "http://127.0.0.1:8765");
// 创作圆桌（deskmate.creation）在真实 sidecar（主 coworker app）中以 /roundtable 前缀挂载
// （coworker/server/app.py: app.mount("/roundtable", roundtable_app)）。所有创作接口必须带此前缀，
// 否则在真实环境命中 404（返回 {error:"not found"} 而非抛异常，前端静默失败 → “点开始创作没反应”）。
export const creationBase = (): string => `${httpBase()}/roundtable`;
// Same origin-derived reasoning as httpBase: on a hosted page the socket must go to the
// page's own host (the proxy forwards /ws), never to the visitor's 127.0.0.1.
export const wsBase = (): string => {
  const injected =
    (globalThis as any).__COWORKER_WS__ ||
    (import.meta as any).env?.VITE_COWORKER_WS;
  if (injected) return injected;
  if (isBrowserPage() && typeof window !== "undefined" && window.location) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + window.location.host;
  }
  return "ws://127.0.0.1:8765";
};
// Token resolution order: runtime-injected global → runtime-config.json (hosted deploy) →
// the dev token file (browser dev only).
//
// The sidecar requires X-OpenWorker-Token on every call. It is a RUNTIME value, never a build
// parameter: local and cloud must ship the same bundle, so the token is supplied by whichever
// host serves the app — the desktop shell sets window.__COWORKER_API_TOKEN__ via the webview
// initialization script, a hosted deployment serves runtime-config.json next to index.html.
// Baking it in at build time would make local and cloud artifacts irreproducible and force a
// rebuild for every token rotation. See docs/DEPLOY-PARITY.md.
//
// Reading the already-fetched config object keeps this synchronous, so the very first request
// (which can fire before index.html's async fetch resolves) still authenticates.
export const runtimeConfig = (): Record<string, string> =>
  (globalThis as any).__COWORKER_RUNTIME_CONFIG__ || {};

export const apiToken = (): string =>
  (globalThis as any).__COWORKER_API_TOKEN__ ||
  runtimeConfig().__COWORKER_API_TOKEN__ ||
  (typeof __COWORKER_DEV_TOKEN__ === "string" ? __COWORKER_DEV_TOKEN__ : "");

// All local REST calls pass through this module, so a module-local wrapper applies launch
// authentication without asking every endpoint helper to remember the security header.
export const fetch = (
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> => {
  const headers = new Headers(init.headers);
  const token = apiToken();
  if (token) headers.set("X-OpenWorker-Token", token);
  return globalThis.fetch(input, { ...init, headers });
};

export const openWebSocket = (url: string): WebSocket => {
  const token = apiToken();
  return token
    ? new WebSocket(url, ["openworker", token])
    : new WebSocket(url);
};

// 共享 JSON 请求 init 构造（多域共用，PR16/#33）。
export const jsonPost = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
