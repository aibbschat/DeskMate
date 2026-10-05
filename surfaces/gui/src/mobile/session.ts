/**
 * 手机端会话选取（纯函数，可单测）。
 *
 * 契约（设计稿 §三.4）：session_id 由**客户端**铸造，服务端在 `turn_start` 才懒落库，
 * 所以「每次冷启动都新建一条」会在 Recents 里长出一堆空会话。手机端冷启动必须先
 * 续聊最近一条 deskmate 会话，只有确实没有时才新建。
 */
import type { SessionInfo } from "../types";

/** 与桌面 App.tsx 的 newId() 同形：12 位、可读、够短。 */
export function newSessionId(): string {
  return typeof crypto !== "undefined" && typeof (crypto as any).randomUUID === "function"
    ? crypto.randomUUID().slice(0, 12)
    : Math.random().toString(36).slice(2, 14);
}

/** 该会话是否值得在手机端续聊：小桔的、未归档、且真有过消息（排除空壳）。 */
function resumable(s: SessionInfo, agent: string): boolean {
  if (s.agent !== agent) return false;
  if (s.archived) return false;
  // messages===0 的行是「开过但没说话」的壳；服务端懒落库后一般不会出现，这里兜底。
  if (typeof s.messages === "number" && s.messages === 0) return false;
  return true;
}

/**
 * 挑一条最近的小桔会话来续聊；没有则返回 null（调用方再 mint 新 id）。
 * 排序：置顶优先 → updated_at 新优先。等值时按 session_id 稳定排，避免同一输入两次
 * 调用选出不同条（测试与真机重进都要可预期）。
 */
export function pickSession(sessions: SessionInfo[], agent = "deskmate"): SessionInfo | null {
  const pool = (sessions || []).filter((s) => resumable(s, agent));
  if (!pool.length) return null;
  const ts = (s: SessionInfo) => {
    const t = s.updated_at ? Date.parse(s.updated_at) : NaN;
    return Number.isFinite(t) ? t : 0;
  };
  return [...pool].sort((a, b) => {
    const pin = Number(!!b.pinned) - Number(!!a.pinned);
    if (pin !== 0) return pin;
    const dt = ts(b) - ts(a);
    if (dt !== 0) return dt;
    return String(a.session_id).localeCompare(String(b.session_id));
  })[0];
}

/**
 * 断线时该给用户看哪句话（设计稿 §三.3：「连不上服务器」与「凭据不对」必须分开报）。
 * 1008 = 服务端 _websocket_authenticated 拒绝（token 不对/没带），不是网络故障。
 */
export function connFaultLabel(code?: number): { title: string; hint: string } {
  if (code === 1008)
    return { title: "凭据不对", hint: "这台设备的 runtime-config.json 与服务器不匹配，去桌面端重开一次或重新部署" };
  return { title: "连不上她的服务器", hint: "网络或云端未就绪，会自动重试" };
}

/**
 * 401 分两类，**必须分开报**（2026-10-05 云端实测取证）：
 *  - `site`：Caddy 的站点口令没过 —— 响应头 `Www-Authenticate: Basic realm="restricted"`、
 *    Content-Length: 0、无 body。这**不是**部署问题；提示"重新部署前端"会把人带偏
 *    （萧哥真机就被我这句话误导过）。
 *  - `token`：站点口令过了，但同源 runtime-config.json 里的应用 token 不对/没拿到 ——
 *    后端回的是 JSON `{"error":"missing or invalid OpenWorker sidecar token"}`。
 */
export type AuthFault = null | "site" | "token" | "net";

export function classify401(res: { status: number; headers: Headers }): Exclude<AuthFault, null> {
  const wa = res.headers.get("www-authenticate") || "";
  return /basic/i.test(wa) ? "site" : "token";
}

export function faultCopy(f: AuthFault): { title: string; hint: string } | null {
  switch (f) {
    case "site":
      return {
        title: "站点口令没过",
        hint: "这台设备还没通过 xj.aibbs.chat 的账号密码（或刚失效）。在浏览器里输一次口令，再点重试即可——不需要重新部署",
      };
    case "token":
      return { title: "凭据不对", hint: "服务器上的 runtime-config.json 与后端 token 不匹配——只有这种情况才需重新部署前端" };
    case "net":
      return { title: "连不上她的服务器", hint: "网络或云端未就绪，会自动重试；这期间你打的字会排队补发" };
    default:
      return null;
  }
}
