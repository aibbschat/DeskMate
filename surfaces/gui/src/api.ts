import type { GroupedQuestion, QuestionOption, SessionInfo, WsEvent } from "./types";

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
const isBrowserPage = (): boolean =>
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
const creationBase = (): string => `${httpBase()}/roundtable`;
// Same origin-derived reasoning as httpBase: on a hosted page the socket must go to the
// page's own host (the proxy forwards /ws), never to the visitor's 127.0.0.1.
const wsBase = (): string => {
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
const runtimeConfig = (): Record<string, string> =>
  (globalThis as any).__COWORKER_RUNTIME_CONFIG__ || {};

const apiToken = (): string =>
  (globalThis as any).__COWORKER_API_TOKEN__ ||
  runtimeConfig().__COWORKER_API_TOKEN__ ||
  (typeof __COWORKER_DEV_TOKEN__ === "string" ? __COWORKER_DEV_TOKEN__ : "");

// All local REST calls pass through this module, so a module-local wrapper applies launch
// authentication without asking every endpoint helper to remember the security header.
const fetch = (
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> => {
  const headers = new Headers(init.headers);
  const token = apiToken();
  if (token) headers.set("X-OpenWorker-Token", token);
  return globalThis.fetch(input, { ...init, headers });
};

const openWebSocket = (url: string): WebSocket => {
  const token = apiToken();
  return token
    ? new WebSocket(url, ["openworker", token])
    : new WebSocket(url);
};

export interface Health {
  status: string;
  default_workspace: string | null;
  model: string;
}

export interface RecentWorkspace {
  path: string;
  name: string;
  exists: boolean;
}

export interface WorkspaceCommandTrust {
  workspace: string;
  requested_commands: string[];
  trusted: boolean;
  required: boolean;
  exists?: boolean;
}

export async function getHealth(): Promise<Health> {
  const res = await fetch(`${httpBase()}/v1/health`);
  return res.json();
}

export async function getRecentWorkspaces(): Promise<RecentWorkspace[]> {
  const res = await fetch(`${httpBase()}/v1/workspaces/recent`);
  return (await res.json()).workspaces ?? [];
}

/** Ask the LOCAL sidecar to open the OS folder picker — the browser GUI can't obtain absolute
 * paths from web file dialogs. Blocks until the user picks or cancels; null on cancel/unavailable. */
export async function pickFolderViaServer(): Promise<string | null> {
  try {
    const res = await fetch(`${httpBase()}/v1/workspaces/pick`, { method: "POST" });
    const d = await res.json();
    return d.ok && d.path ? d.path : null;
  } catch {
    return null;
  }
}

/** Ask the LOCAL sidecar to open the OS file picker (single file — 创作流水线「浏览…」
 * 选原著文件用)。浏览器 GUI 拿不到绝对路径，必须走 sidecar 原生对话框。 */
export async function pickNovelFileViaServer(): Promise<string | null> {
  try {
    const res = await fetch(`${httpBase()}/v1/files/pick`, { method: "POST" });
    const d = await res.json();
    return d.ok && d.path ? d.path : null;
  } catch {
    return null;
  }
}

export async function openWorkspace(
  path: string,
  create = false,
): Promise<{
  path: string;
  ok: boolean;
  error?: string;
  git_branch?: string | null;
  command_trust?: WorkspaceCommandTrust;
}> {
  const res = await fetch(`${httpBase()}/v1/workspaces/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, create }),
  });
  return res.json();
}

/** UX-029 "Start in a temporary folder": create the conversation's temp dir at send time
 * (git-init'd for code-family work). Idempotent. */
export async function createTempWorkspace(
  sessionId: string,
  git = true,
): Promise<{ ok: boolean; path?: string; git?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/workspaces/temp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, git }),
  });
  return res.json();
}

/** UX-029 "Save as project…": move a session's temporary folder to a real location.
 * Callers reconnect afterwards so the engine rebinds to the new path. */
export async function saveSessionAsProject(
  sessionId: string,
  path: string,
): Promise<{ ok: boolean; path?: string; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/save-as-project`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    },
  );
  return res.json();
}

export async function getTrustedWorkspaces(): Promise<WorkspaceCommandTrust[]> {
  const res = await fetch(`${httpBase()}/v1/workspaces/trusted`);
  return (await res.json()).workspaces ?? [];
}

export async function setWorkspaceTrusted(
  path: string,
  trusted: boolean,
): Promise<{ ok: boolean; error?: string } & WorkspaceCommandTrust> {
  const res = await fetch(`${httpBase()}/v1/workspaces/trust`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, trusted }),
  });
  return res.json();
}

export async function getSessions(workspace?: string): Promise<SessionInfo[]> {
  const q = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await fetch(`${httpBase()}/v1/sessions${q}`);
  return (await res.json()).sessions ?? [];
}

// A structured connector-delivered inbound message (§3.1). Attached to the user message it framed,
// for display only — the model still sees the framed `content`; this drives the ConnectorMessageCard.
export interface MessageSource {
  connector: string; // platform id, e.g. "slack"
  kind: "channel" | "dm";
  channel_id: string; // e.g. "C0BD7KZ1AH5"
  channel_name: string; // resolved; may equal the id (e.g. "#ocw-test")
  sender_id: string;
  sender_name: string; // resolved; may equal the id
  ts: number; // epoch seconds
  text: string; // the RAW message (what the card shows)
  // Board wakes only (connector === "board"): the digest as structured rows, so
  // the BoardWakeCard renders collapsed summaries instead of re-parsing prose.
  board?: { rows: BoardWakeRow[] };
}

// One digest event on a board wake. `note` is a UI-clamped excerpt of a hand-off
// comment (the full text lives on the board).
export interface BoardWakeRow {
  kind: "assigned" | "claimed" | "moved" | "filed" | "comment" | "chat" | string;
  item?: number | null;
  title?: string;
  actor?: string;
  to?: string;
  note?: string;
}

// A transcript message from GET /v1/sessions/{id}/messages. Kept permissive (open shape) because
// itemsFromMessages reads several role-specific fields; `source` is the optional connector sidecar.
export interface ConversationMessage {
  role: string;
  content?: any;
  tool_calls?: any[];
  tool_call_id?: string;
  source?: MessageSource;
  // Token counts for the round-trip that produced an assistant message
  // ({model, input, output, cache_read, cache_write}); absent on older servers.
  usage?: import("./types").TurnUsage;
  [key: string]: any;
}

export async function getSessionMessages(sessionId: string): Promise<ConversationMessage[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${sessionId}/messages`);
  return (await res.json()).messages ?? [];
}

export async function renameSession(sessionId: string, title: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  return res.json();
}

export async function setSessionFlags(
  sessionId: string,
  flags: { pinned?: boolean; archived?: boolean },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

export async function deleteSession(sessionId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  return res.json();
}

// Agent teams (OPE-96): the session's board — items on the workspace-keyed space.
export interface BoardItem {
  id: number;
  title: string;
  description: string;
  criteria: string;
  state: "open" | "in_progress" | "blocked" | "review" | "done" | "canceled" | string;
  assignee: string;
  creator: string;
  refs: string[];
  links: { kind: string; item: number }[];
  // Blocked rows only: the latest blocker comment, clamped ("need tfvars…").
  blocker?: string;
}

export interface Board {
  space: string | null;
  name: string;
  items: BoardItem[];
}

export interface JournalCase {
  case: string;
  entries: number;
  last_ts: string;
}

export async function getBoard(sessionId: string): Promise<Board> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/board`);
  return res.json();
}

// One event in an item's merged timeline (the detail pane renders the item's
// whole story: filed → assigned/claimed → moves → comments, with attachments).
export interface BoardTimelineEvent {
  seq: number;
  ts: string;
  actor: string;
  kind: "created" | "assigned" | "claimed" | "moved" | "comment" | string;
  to?: string;
  assignee?: string;
  body?: string;
  refs?: string[];
}

export type BoardItemDetail = BoardItem & { timeline?: BoardTimelineEvent[] };

export async function getBoardItem(
  sessionId: string,
  id: number,
): Promise<BoardItemDetail | { error: string }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/board/item?id=${id}`,
  );
  return res.json();
}

// Attachment bytes → an object URL for <img>. The module fetch wrapper carries
// the sidecar token, which a bare <img src> cannot.
export async function fetchBoardAttachment(
  sessionId: string,
  stored: string,
): Promise<string | null> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/board/attachment?name=${encodeURIComponent(stored)}`,
  );
  if (!res.ok) return null;
  return URL.createObjectURL(await res.blob());
}

// A pure note on an item — never changes state; the assignee hears it via its feed.
export async function boardComment(
  sessionId: string,
  item: number,
  body: string,
): Promise<{ ok?: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/board/comment`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item, body }),
    },
  );
  return res.json();
}

export async function boardTransition(
  sessionId: string,
  item: number,
  to: string,
  comment = "",
): Promise<BoardItem | { error: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/board/transition`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item, to, comment }),
  });
  return res.json();
}

export interface ChatMessage {
  seq: number;
  ts: string;
  author: string;
  author_role: "user" | "lead" | "worker" | string;
  text: string;
  mentions: string[];
}

export interface TeamChat {
  enabled: boolean;
  team_id?: string;
  members: { name: string; persona: string; role: string }[];
  messages: ChatMessage[];
}

export async function getTeamChat(teamId: string): Promise<TeamChat> {
  const res = await fetch(`${httpBase()}/v1/teams/${encodeURIComponent(teamId)}/chat`);
  return res.json();
}

export async function postTeamChat(teamId: string, text: string): Promise<ChatMessage | { error: string }> {
  const res = await fetch(`${httpBase()}/v1/teams/${encodeURIComponent(teamId)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  return res.json();
}

export async function getJournalCases(): Promise<JournalCase[]> {
  const res = await fetch(`${httpBase()}/v1/teams/journal`);
  return (await res.json()).cases ?? [];
}

export interface ArtifactInfo {
  path: string; // workspace-relative (the display/API identifier)
  abs_path?: string; // absolute — what "Copy path" copies
  name: string;
  kind: "markdown" | "html" | "image" | "code" | "text" | string;
  size: number;
  modified_at: number;
  // Which rail surface opened it — drives the viewer's breadcrumb ("Artifacts" vs
  // "Files"). Absent = artifacts (UX-037).
  origin?: "artifacts" | "files";
}

export interface ArtifactContent {
  ok: boolean;
  error?: string;
  path: string;
  kind: string;
  content?: string;
  data_url?: string;
  truncated?: boolean;
  // kind === "folder": a directory listing (models sometimes link a whole package dir).
  entries?: { name: string; dir: boolean; size: number }[];
}

export async function getArtifacts(sessionId: string): Promise<ArtifactInfo[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts`);
  return (await res.json()).artifacts ?? [];
}

export async function readArtifact(sessionId: string, path: string): Promise<ArtifactContent> {
  const q = new URLSearchParams({ path });
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/read?${q.toString()}`);
  return res.json();
}

/** Show the artifact in the OS file manager ("reveal") or open it with its default app ("open"). */
export async function revealArtifact(
  sessionId: string,
  path: string,
  mode: "reveal" | "open" = "reveal",
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/artifacts/reveal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, mode }),
  });
  return res.json();
}

// -- session roots (orphan Cowork: scratch + added folders) -------------------
export interface RootInfo {
  path: string;
  writable: boolean;
  label: string;
  primary: boolean;
  exists: boolean;
}

export async function getRoots(sessionId: string): Promise<RootInfo[]> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`);
  return (await res.json()).roots ?? [];
}

export async function addRoot(
  sessionId: string,
  path: string,
  writable: boolean,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, writable }),
  });
  return res.json();
}

export async function removeRoot(
  sessionId: string,
  path: string,
): Promise<{ ok: boolean; error?: string; roots?: RootInfo[] }> {
  const q = new URLSearchParams({ path });
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/roots?${q.toString()}`,
    { method: "DELETE" },
  );
  return res.json();
}

// -- MCP servers --------------------------------------------------------------
export interface McpServer {
  name: string;
  enabled: boolean;
  transport: string;
  requires_approval: boolean;
  // "connected" | "configured" | "disabled" | and for auth:"oauth" servers:
  // "needs_auth" (no tokens yet) | "authorizing" (browser sign-in in flight)
  status: string;
  auth?: "oauth" | null;
  // http server whose anonymous connect hit a 401/403 — offer OAuth sign-in.
  auth_hint?: boolean;
  // Epoch seconds of the last successful explicit Test (persisted server-side).
  last_test_at?: number | null;
  last_error?: string | null;
  tool_count: number | null;
  config: Record<string, any>;
}

export async function getMcpServers(): Promise<McpServer[]> {
  const res = await fetch(`${httpBase()}/v1/mcp`);
  return (await res.json()).servers ?? [];
}

export async function addMcpServer(name: string, config: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, config }),
  });
  return res.json();
}

export async function patchMcpServer(name: string, changes: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteMcpServer(name: string) {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}`, { method: "DELETE" });
  return res.json();
}

export async function getMcpTools(
  name: string,
): Promise<{ ok: boolean; error?: string; tools: { name: string; description: string }[] }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/tools`);
  return res.json();
}

export async function reloadMcp() {
  const res = await fetch(`${httpBase()}/v1/mcp/reload`, { method: "POST" });
  return res.json();
}

/** Connect one MCP server now. For OAuth servers this opens the system browser;
 * poll getMcpServers() for the status flip (authorizing → connected / needs_auth). */
export async function connectMcp(name: string): Promise<{ ok: boolean; started?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/connect`, {
    method: "POST",
  });
  return res.json();
}

/** Drop the connection and forget the stored OAuth tokens. */
export async function signoutMcp(name: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/mcp/${encodeURIComponent(name)}/signout`, {
    method: "POST",
  });
  return res.json();
}

// -- connectors ---------------------------------------------------------------
export interface ConnectorField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
}

// A message from a sender not (yet) on the allow-list — parked instead of dropped (§19).
export interface ParkedMessage {
  id: string;
  platform: string;
  chat_id: string;
  chat_name: string | null;
  user_id: string;
  user_name: string | null;
  chat_type: string;
  text: string;
  ts: number;
  team_id?: string | null; // workspace (managed Slack relay); null on manual Socket Mode
}

// One connected Slack workspace (managed relay is multi-workspace; ids are workspace-scoped,
// so each workspace carries its OWN allow-list).
export interface SlackWorkspace {
  team_id: string;
  account: string;
  domain?: string; // slack.com subdomain — unique even when display names collide
  allowed_users: string[];
  allow_all: boolean;
  allowed_user_names?: Record<string, string | null>;
  approval_owner_ids?: string[];
  approval_owner_names?: Record<string, string | null>;
  // Who installed this workspace (authed_user) — pre-added to the allow-list on
  // connect (UX-027); the GUI marks their chip "you" and keys the setup card copy.
  installer_user_id?: string;
  installer_name?: string;
}

// One connected GitHub App installation (managed relay is multi-installation;
// sender logins are global but each installation keeps its OWN allow-list).
export interface GithubInstallation {
  installation_id: string;
  account_login: string; // the org/user the App is installed on
  account_type: string; // "Organization" | "User"
  repo_selection: string; // "all" | "selected"
  github_login: string; // the connecting user's own login
  allowed_users: string[]; // sender logins allowed to trigger work
  allow_all: boolean;
}

// One connected HubSpot portal (multi-portal: `hubspot:portal:<hub_id>` profiles).
export interface HubSpotPortal {
  hub_id: string;
  name: string;
  sandbox: boolean;
  default: boolean;
  managed: boolean;
  access: "read" | "write" | ""; // consent tier granted ("" = manual token, unknown)
}

// One connected Google account (multi-account: `gmail:account:<email>` /
// `google_calendar:account:<email>` profiles — same shape for both).
export interface GmailAccount {
  email: string;
  default: boolean;
  managed: boolean;
  scopes: string;
  needs_reauth: boolean;
}

// "Never show agents" — enforced locally in the tool layer; agents see silent
// omissions, the user sees counts on tool cards + Activity rows.
export interface GmailFilters {
  senders: string[];
  labels: string[];
}

// One account of a generic multi-account connector (`<name>:account:<id>`
// profiles — Notion workspaces, PostHog projects, …). Gmail/Calendar predate
// the generic layer and keep their email-keyed shape above.
export interface AccountRow {
  account_id: string;
  name: string; // display identity captured at connect (workspace name, email, …)
  default: boolean;
  managed: boolean;
}

export interface Connector {
  name: string;
  title: string;
  icon: string;
  blurb: string;
  // Pre-connect detail page copy (UX-DECISIONS §38): optional About paragraph
  // (empty → group omitted) + honest Access bullets.
  about?: string;
  access?: string[];
  auth: string;
  two_way: boolean;
  // Chat-platform capability, narrower than two_way: sessions can subscribe to channels.
  channels: boolean;
  available: boolean;
  fields: ConnectorField[];
  instructions: string[];
  connected: boolean;
  account: string | null;
  enabled: boolean;
  brand_color: string; // hex brand color, e.g. "#611f69" (fallback gray "#6b7280")
  logo: string; // stable logo id keyed into the frontend registry (empty → fallback glyph)
  aliases?: string[]; // extra typeahead terms ("calendar" surfaces Outlook)
  mcp?: boolean; // MCP-backed one-click (vendor-hosted MCP + local OAuth — no cloud sign-in)
  allowed_users: string[]; // the allow-list (managed inline in the Connectors tab)
  allowed_user_names?: Record<string, string | null>; // id → display name (people directory)
  approval_owner_ids?: string[]; // Manual Slack: humans allowed to resolve approvals
  approval_owner_names?: Record<string, string | null>;
  recent?: RecentSender[]; // recently-seen senders on a connected two-way connector
  unauthorized?: ParkedMessage[]; // parked messages from unallowed senders (§19)
  tools: ConnectorTool[];
  managed: boolean; // one-click managed OAuth available (needs cloud sign-in)
  managed_paused?: boolean; // one-click temporarily off (e.g. Google CASA pending) — badge "Coming soon"
  managed_profile: boolean; // current profile came from managed OAuth (vs manual paste)
  mode?: string; // "relay" for the managed cloud path; "" for manual/token connect
  workspaces?: SlackWorkspace[]; // Slack only: connected workspaces (managed relay)
  // Gmail/Calendar: email-keyed rows; generic account connectors (notion,
  // attio, posthog, …): AccountRow. The detail pages narrow by connector.
  accounts?: GmailAccount[] | AccountRow[];
  filters?: GmailFilters; // Gmail only: "Never show agents" senders/labels
  portals?: HubSpotPortal[]; // HubSpot only: connected portals (multi-portal)
  hidden_fields?: string[]; // HubSpot only: properties stripped from agent reads
  installations?: GithubInstallation[]; // GitHub only: App installations (managed relay)
}

// --- OpenWorker Cloud (optional sign-in; manual token paste always works) ---

export interface CloudStatus {
  signed_in: boolean;
  account: string;
  user_id: string;
  telemetry_enabled?: boolean; // Phase 5 opt-out; signed-out users send nothing regardless
}

/** Flip the product-telemetry preference (local; only meaningful when signed in). */
export async function setCloudTelemetry(
  enabled: boolean,
): Promise<{ ok: boolean; telemetry_enabled?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/cloud/telemetry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export async function getCloudStatus(): Promise<CloudStatus> {
  const res = await fetch(`${httpBase()}/v1/cloud/status`);
  return res.json();
}

export async function cloudLogin(): Promise<{ ok: boolean }> {
  // The sidecar opens the system browser; the GUI just polls status after.
  const res = await fetch(`${httpBase()}/v1/cloud/login`, { method: "POST" });
  return res.json();
}

/** Poll cloud status until the browser sign-in lands (or the bound runs out).
 *
 * Fast 500ms polls for the first 20s — the moment the user finishes in the
 * browser they're staring at the app waiting for it to flip, and a 2s interval
 * reads as "sign-in is slow" (owner complaint, 2026-07-16) — then relaxes to 2s
 * for the long tail (~2min total). Calls `onDone` with the signed-in status, or
 * null when it timed out. Returns a cancel function (call on unmount). */
export function waitForCloudSignIn(
  onDone: (s: CloudStatus | null) => void,
): () => void {
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let polls = 0;
  const tick = async () => {
    polls += 1;
    const s = await getCloudStatus().catch(() => null);
    if (cancelled) return;
    if (s?.signed_in) return onDone(s);
    if (polls >= 90) return onDone(null); // 40×500ms + 50×2s ≈ 2min
    timer = setTimeout(tick, polls < 40 ? 500 : 2000);
  };
  timer = setTimeout(tick, 500);
  return () => {
    cancelled = true;
    if (timer) clearTimeout(timer);
  };
}

export async function cloudLogout(): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/cloud/logout`, { method: "POST" });
  return res.json();
}

export async function connectManaged(
  name: string,
  options?: { access?: "read" | "write" },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect-managed`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // `access` names a broker-defined consent tier (hubspot read | write).
      // GitHub needs no flow choice: the broker is authorize-first — one connect
      // links an existing App installation or redirects on to the install page.
      body: JSON.stringify({
        ...(options?.access ? { access: options.access } : {}),
      }),
    },
  );
  return res.json();
}

/** One-click connect for an MCP-backed connector (monday, asana, jira): the sidecar
 * opens the vendor's sign-in in the browser (local OAuth, no cloud account needed);
 * poll getConnectors until the card flips to connected. */
export async function connectMcpBacked(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/mcp-connect`,
    { method: "POST" },
  );
  return res.json();
}

export interface ConnectorTool {
  name: string;
  label: string;
  kind: "read" | "write" | string;
  description: string;
  enabled: boolean;
  requires_approval: boolean;
}

export async function getConnectors(): Promise<Connector[]> {
  const res = await fetch(`${httpBase()}/v1/connectors`);
  return (await res.json()).connectors ?? [];
}

export async function connectConnector(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; account?: string; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fields }),
  });
  return res.json();
}

export async function disconnectConnector(name: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disconnect`, {
    method: "POST",
  });
  return res.json();
}

export async function updateConnectorTools(
  name: string,
  enabled: Record<string, boolean>,
): Promise<{ ok: boolean; error?: string; tools?: Record<string, boolean> }> {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/tools`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  return res.json();
}

export interface AuditEvent {
  id: number;
  timestamp: string;
  session_id: string;
  agent: string;
  workspace: string;
  connector: string;
  tool: string;
  stage: string;
  status: string;
  approval: string;
  args: Record<string, any>;
  result_preview: string;
  reason: string;
  resource: string;
}

export async function getAudit(params: {
  limit?: number;
  session_id?: string;
  connector?: string;
  tool?: string;
} = {}): Promise<AuditEvent[]> {
  const q = new URLSearchParams();
  if (params.limit) q.set("limit", String(params.limit));
  if (params.session_id) q.set("session_id", params.session_id);
  if (params.connector) q.set("connector", params.connector);
  if (params.tool) q.set("tool", params.tool);
  const res = await fetch(`${httpBase()}/v1/audit${q.toString() ? "?" + q.toString() : ""}`);
  return (await res.json()).events ?? [];
}

export interface BrowserState {
  open: boolean;
  url: string;
  title: string;
  status: string;
  last_action: string;
  last_result: string;
  last_error: string;
  screenshot_data_url: string;
  updated_at: string | null;
  controls: any[];
}

export async function getBrowserState(): Promise<BrowserState> {
  const res = await fetch(`${httpBase()}/v1/browser/state`);
  return res.json();
}

export async function takeBrowserScreenshot(): Promise<BrowserState & { ok?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/screenshot`, { method: "POST" });
  return res.json();
}

export async function closeBrowser(): Promise<{ ok?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/browser/close`, { method: "POST" });
  return res.json();
}

// -- settings (model API key, default model, onboarding) ----------------------
export interface SurfaceVisibility {
  cowork: boolean; // always true
  chat: boolean;
  code: boolean;
}

export interface ModelSettings {
  provider: string;
  model: string;
  models: string[];
  has_key: boolean;
  model_ready: boolean; // can the default model's provider actually run (any provider)?
  source: "env" | "store" | null;
  onboarded: boolean;
  surfaces: SurfaceVisibility;
  scratch_base: string;
  secrets_path: string;  // OS-native on-disk location the server reports (not hardcoded)
  // Sidebar layout preference (§7): "flat" = the persona accordions / today's list; "grouped" =
  // bounded per-persona cards. Defaults to "flat" (absent → flat) so the GUI is robust to an older
  // backend that hasn't shipped the field yet.
  nav_layout?: "flat" | "grouped";
  // Sidebar: sessions shown per group before "Show more" (default 5, 1–50).
  sessions_peek?: number;
  // Composer: show the context-window fill bar (default FALSE; absent → the chip shows
  // the session total). The usage popover keeps both numbers regardless.
  context_bar?: boolean;
  // Auto-Approve mode (spec §1.5): the feature flag that offers the reviewer mode, and its
  // shadow-eval sibling. Both default FALSE and are absent on older backends — the composer
  // hides the Auto-Approve mode entry unless auto_approve is explicitly true.
  auto_approve?: boolean;
  auto_approve_shadow?: boolean;
  // Curated-matrix display names ({full id → "GLM-5.2 · via Together"}); custom models absent.
  model_labels?: Record<string, string>;
  // {full id → context window in tokens}, verified matrix entries only — drives the
  // composer's context-fill meter (absent id → the meter hides). Optional for older backends.
  model_context_windows?: Record<string, number>;
  // Token savings (PDF attachments): fallback for models without native PDF support,
  // and attach-time thresholds. Optional so the GUI is robust to an older backend.
  pdf_fallback?: "text" | "images";
  pdf_max_pages?: number; // default 20, 1–100
  pdf_max_mb?: number; // default 10, 1–10
  // Auto-compaction of long histories (OPE-27): trigger = min(threshold% × context
  // window, cap tokens); model pins the summarizer ("" → the session's own model).
  // Optional so the GUI is robust to an older backend.
  compaction_threshold_pct?: number; // default 0.8, 0.10–0.95
  compaction_cap_tokens?: number; // default 250000
  compaction_model?: string;
  // 我们的治理/沙箱设置（后端 coworker/config.py 同源）
  execution_level?: "off" | "auto" | "strict";
  sandbox_enabled?: boolean;
  sandbox_network?: boolean;
  sandbox_deny_paths?: string[];
  // {full model id → "free" | "paid"} — 与后端 manager.py 一致（dict[str, str]），
  // 用于 composer 选择器按 免费/付费 两段分组。注意：官方 v0.2.0 此处为 Record<string, string[]>，
  // 我们后端语义为单模型→单 tier 字符串，故保持 Record<string, string>。
  model_tiers?: Record<string, string>;
}

export interface PdfSettings {
  pdf_fallback: "text" | "images";
  pdf_max_pages: number;
  pdf_max_mb: number;
}

/** Persist the Token-savings PDF settings (fallback mode + attach thresholds). */
export async function setPdfSettings(
  patch: Partial<PdfSettings>,
): Promise<{ ok: boolean; error?: string } & Partial<PdfSettings>> {
  const res = await fetch(`${httpBase()}/v1/settings/pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

export interface CompactionSettings {
  compaction_threshold_pct: number;
  compaction_cap_tokens: number;
  compaction_model: string;
}

/** Persist the auto-compaction overrides (threshold %, token cap, summarizer model). */
export async function setCompactionSettings(
  patch: Partial<CompactionSettings>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/compaction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

/** Local page/size probe for a PDF data URL — the composer's attach-time threshold check. */
export async function inspectPdf(
  dataUrl: string,
): Promise<{ ok: boolean; pages?: number; bytes?: number; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/attachments/inspect-pdf`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
  return res.json();
}

/** Persist whether the composer shows the context-window fill bar. */
export async function setContextBar(
  shown: boolean,
): Promise<{ ok: boolean; context_bar?: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/context-bar`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ context_bar: shown }),
  });
  return res.json();
}

type AutoApproveResult = {
  ok: boolean;
  auto_approve?: boolean;
  auto_approve_shadow?: boolean;
  error?: string;
};

/** Toggle the Auto-Approve feature flag (spec §1.5); applies to the next session build. */
export async function setAutoApprove(on: boolean): Promise<AutoApproveResult> {
  const res = await fetch(`${httpBase()}/v1/settings/auto-approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_approve: on }),
  });
  return res.json();
}

/** Toggle shadow evaluation (Part 6 step 3): the reviewer records but never decides. */
export async function setAutoApproveShadow(on: boolean): Promise<AutoApproveResult> {
  const res = await fetch(`${httpBase()}/v1/settings/auto-approve-shadow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_approve_shadow: on }),
  });
  return res.json();
}

/** Persist how many sessions a sidebar group shows before "Show more". */
export async function setSessionsPeek(
  n: number,
): Promise<{ ok: boolean; sessions_peek?: number; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/sessions-peek`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessions_peek: n }),
  });
  return res.json();
}

export async function setScratchBase(
  path: string,
): Promise<{ ok: boolean; error?: string; scratch_base?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/scratch-base`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return res.json();
}

export async function setSurfaces(
  flags: { chat?: boolean; code?: boolean },
): Promise<{ ok: boolean; surfaces: SurfaceVisibility }> {
  const res = await fetch(`${httpBase()}/v1/settings/surfaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(flags),
  });
  return res.json();
}

/** Persist the sidebar layout preference (flat ↔ grouped-by-persona); read back from getSettings. */
export async function setNavLayout(
  layout: "flat" | "grouped",
): Promise<{ ok: boolean; nav_layout?: "flat" | "grouped"; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/nav-layout`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nav_layout: layout }),
  });
  return res.json();
}

// Fired after a cloud sign-in/out completes so the account row (§26) refreshes without
// waiting for the next window focus.
export const CLOUD_CHANGED = "coworker:cloud-changed";
export function announceCloudChanged() {
  window.dispatchEvent(new CustomEvent(CLOUD_CHANGED));
}

// Fired the first time Inbox machinery is engaged (an item parks, or a session goes
// Unattended) — the account row's inbox chip unlocks stickily on it (§26).
export const INBOX_UNLOCK = "coworker:inbox-unlock";
export function announceInboxUnlock() {
  window.dispatchEvent(new CustomEvent(INBOX_UNLOCK));
}

// -- Personas -----------------------------------------------------------------

// Fired after any persona mutation (enable/disable/install/delete) so always-mounted
// consumers (the sidebar's new-session picker) refetch instead of going stale.
export const PERSONAS_CHANGED = "coworker:personas-changed";
function announcePersonasChanged() {
  window.dispatchEvent(new CustomEvent(PERSONAS_CHANGED));
}

export interface Persona {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  requires_folder: boolean; // folder gate — drives project-scoping
  builtin: boolean;
  tools: string[];
  enabled: boolean;
  surfaced: boolean;
  default: boolean;
  // Distribution flag (ships:false = internal builds only) + settings-page group.
  ships?: boolean;
  group?: string; // "general" | "security"
  version?: string;
  installed_at?: string;

  // DeskMate 圆桌标记（builtin 7 专家为 true）：右侧「圆桌」好友栏按此过滤。
  roundtable?: boolean;
  roundtable_role?: string;
  roundtable_market?: string;
  // 我们的自定义字段（圆桌/项目作用域）
  needs_workspace?: boolean;
  family?: string;
  workspace?: string; // "git" | "project" | "deliverable" | "none" — drives project-scoping
}

export interface PersonaConsent {
  id: string;
  name: string;
  description: string;
  tools: string[];
  risk: string[];
  // "all" (general builtins) or the declared allowlist — [] means no connector access.
  connectors: "all" | string[];
  mcp: string[];
  messaging: boolean;
  recommended_mode: string;
  recommended_models: string[];
  recommends?: { kind: string; ref: string; reason: string; tier: string }[];
  version?: string;
  replaces?: { version: string; installed_at: string; capabilities_grew: boolean } | null;
  source: string | null;
  builtin: boolean;
}

export async function getPersonas(): Promise<Persona[]> {
  const res = await fetch(`${httpBase()}/v1/personas`);
  return (await res.json()).personas;
}

/** Personas plus the build flag: `internal` builds may show unshipped coworkers + the Gallery. */
export async function getPersonasIndex(): Promise<{ personas: Persona[]; internal: boolean }> {
  const res = await fetch(`${httpBase()}/v1/personas`);
  const body = await res.json();
  return { personas: body.personas ?? [], internal: !!body.internal };
}

export async function updatePersona(
  id: string,
  body: { enabled?: boolean; surfaced?: boolean; default?: boolean },
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok !== false) announcePersonasChanged();
  return out;
}

/** Uninstall a non-builtin persona (its snapshot + state). Local; works signed out. */
export async function deletePersona(
  id: string,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// A curated persona card from the cloud gallery (metadata only — the manifest
// is fetched server-side at install and runs through the normal consent flow).
export interface GalleryPersona {
  slug: string;
  version: number;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  family: string;
  workspace: string;
  publisher: string;
  recommended_connectors: string[];
  risk_summary: string;
  featured?: boolean; // publisher-flagged for the gallery's featured carousel
}

export async function getCloudGallery(): Promise<{
  ok: boolean;
  personas: GalleryPersona[];
  error?: string;
}> {
  const res = await fetch(`${httpBase()}/v1/cloud/gallery`);
  return res.json();
}

// Solo page for one gallery coworker. `capabilities` is the desktop's own
// consent summary derived from the manifest (same parser as install), so the
// page shows exactly what installing would ask the user to approve.
export interface GalleryDetail {
  ok: boolean;
  error?: string;
  card?: GalleryPersona & { pitch_markdown: string };
  capabilities?: {
    tools: string[];
    risk: string[];
    connectors: boolean;
    mcp: string[];
    messaging: boolean;
    recommended_mode: string;
    recommended_models: string[];
  };
  recommends?: { kind: string; ref: string; reason: string; tier: string }[];
}

export async function getCloudGalleryDetail(slug: string): Promise<GalleryDetail> {
  const res = await fetch(`${httpBase()}/v1/cloud/gallery/${encodeURIComponent(slug)}`);
  return res.json();
}

/** Sharing v1 (OPE-7): zip a coworker's bundle into `dir`; the zip is the import format. */
export async function exportPersona(
  id: string,
  dir: string,
): Promise<{ ok: boolean; path?: string; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dir }),
  });
  return res.json();
}

export async function installPersona(
  body: { dir?: string; git_url?: string; gallery_slug?: string; zip_b64?: string; filename?: string },
): Promise<{ ok: boolean; consent?: PersonaConsent[]; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Persona detail + connection defaults (§5) --------------------------------
// A persona's declared recommendation (manifest `recommends`): a connector or MCP server it works
// best with, with a reason + tier (core/optional). `connected` is annotated server-side from the
// connector list so the detail page can show connect state without a second round-trip.
export interface PersonaRecommendation {
  kind: string; // "connector" | "mcp" | …
  ref: string; // connector id (e.g. "github") or mcp/server name
  reason: string;
  tier: string; // "core" | "optional"
  connected: boolean;
}

// A persona-default connection (the middle of the §4 hierarchy): for a connected connector, whether
// new sessions of this persona get it enabled by default.
export interface PersonaDefaultConnection {
  connector: string; // connector id
  enabled: boolean; // persona-default on/off
  connected: boolean; // is the account actually connected (else the toggle is disabled)
}

export interface PersonaDetail {
  id: string;
  name: string;
  icon: string;
  tagline: string;
  description: string;
  media: string[]; // bundle media/ screenshots, served via /v1/personas/{id}/media/{name}
  builtin: boolean;
  group: string;
  enabled: boolean; // persona on/off (shown in the picker)
  surfaced: boolean;
  default: boolean;
  tools: string[];
  recommended_models: string[];
  default_permission_mode: string;
  requires_folder: boolean; // folder gate (workspace-scratch-design.md)
  recommends: PersonaRecommendation[];
  default_connections: PersonaDefaultConnection[];
}

/** Fetch one bundle screenshot with launch auth and hand back an object URL. */
export async function getPersonaMediaUrl(id: string, name: string): Promise<string> {
  const res = await fetch(
    `${httpBase()}/v1/personas/${encodeURIComponent(id)}/media/${encodeURIComponent(name)}`,
  );
  if (!res.ok) throw new Error(`media ${name}: ${res.status}`);
  return URL.createObjectURL(await res.blob());
}

export async function getPersonaDetail(id: string): Promise<PersonaDetail> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}`);
  return res.json();
}

/** Set a persona-default connection (new sessions of this persona get it on/off by default). */
export async function setPersonaConnection(
  id: string,
  connector: string,
  enabled: boolean,
): Promise<{ ok: boolean; default_connections?: PersonaDefaultConnection[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled }),
  });
  return res.json();
}

/** Enable/disable the persona (whether it surfaces in the new-session picker). */
export async function setPersonaEnabled(
  id: string,
  enabled: boolean,
): Promise<{ ok: boolean; personas?: Persona[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/personas/${encodeURIComponent(id)}/enable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  const out = await res.json();
  if (out.ok) announcePersonasChanged();
  return out;
}

// -- Per-session connections (Sources bar + drawer, §6) -----------------------
// An effective-enabled connector for a session, with a short human detail (e.g. "#ocw-test · DMs").
// `enabled` reflects the session override/persona default so the drawer toggle shows correct state.
export interface SessionConnectedConnector {
  connector: string;
  enabled: boolean;
  detail: string;
}

// A persona-recommended connector not yet connected (drives the `⚠ N` attention count).
export interface SessionRecommendedConnector {
  connector: string;
  reason: string;
  tier: string;
  connected: boolean;
}

export interface SessionConnections {
  connected: SessionConnectedConnector[];
  recommended: SessionRecommendedConnector[];
  attention: number; // ⚠ count = recommended connectors not yet connected
}

/** `persona` = the active persona hint — required for brand-new sessions (no server-side
 * record yet), otherwise the view resolves to the default persona's defaults/recommends. */
export async function getSessionConnections(
  sessionId: string,
  persona?: string,
): Promise<SessionConnections> {
  const q = persona ? `?persona=${encodeURIComponent(persona)}` : "";
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections${q}`,
  );
  return res.json();
}

/**
 * Set a per-session connection override (mute/unmute a connector for THIS session). Pass
 * `clear: true` to drop the override and inherit the persona default again.
 */
export async function setSessionConnection(
  sessionId: string,
  connector: string,
  enabled: boolean,
  clear = false,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/connections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, enabled, ...(clear ? { clear: true } : {}) }),
  });
  return res.json();
}

// -- Skills (SKILLS-SPEC §4) ----------------------------------------------------
// Scope = folder location: "global" (every session) or "project" (one workspace).
// The session endpoints resolve the effective menu (Settings disables + session mutes).

export interface SkillRow {
  name: string;
  description: string;
  instructions: string;
  scope: "global" | "project";
  source: string; // "local" | "uploaded"
  enabled: boolean;
  path: string;
  files?: number; // bundled resources beyond SKILL.md (§6 — rich skills are visible)
}

export interface SessionSkillRow {
  name: string;
  description: string;
  scope: "global" | "project";
  enabled: boolean; // false = muted for this session only
}

export interface SkillUploadPreview {
  ok: boolean;
  error?: string;
  token?: string;
  name?: string;
  description?: string;
  instructions?: string;
  files?: string[];
}

const skillUrl = (path = "") => `${httpBase()}/v1/skills${path}`;
const jsonPost = (body: unknown, method = "POST") => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export async function listSkills(workspace?: string): Promise<SkillRow[]> {
  const qs = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await fetch(skillUrl(qs));
  return (await res.json()).skills ?? [];
}

export async function createSkill(body: {
  name: string;
  description: string;
  instructions: string;
  scope?: "global" | "project";
  workspace?: string;
}): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(skillUrl(), jsonPost(body));
  return res.json();
}

export async function updateSkill(
  name: string,
  patch: { description?: string; instructions?: string; enabled?: boolean; workspace?: string },
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(skillUrl(`/${encodeURIComponent(name)}`), jsonPost(patch, "PATCH"));
  return res.json();
}

export async function revealSkill(name: string): Promise<{ ok: boolean; error?: string }> {
  // §6 "Show folder": the backend opens the skill's folder in the OS file manager.
  const res = await fetch(skillUrl(`/${encodeURIComponent(name)}/reveal`), jsonPost({}));
  return res.json();
}

export async function deleteSkill(
  name: string,
  workspace?: string,
): Promise<{ ok: boolean; error?: string }> {
  const qs = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await fetch(skillUrl(`/${encodeURIComponent(name)}${qs}`), { method: "DELETE" });
  return res.json();
}

export async function moveSkill(
  name: string,
  scope: "global" | "project",
  workspace?: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(skillUrl(`/${encodeURIComponent(name)}/move`), jsonPost({ scope, workspace }));
  return res.json();
}

export async function stageSkillUpload(
  dataB64: string,
  filename = "",
): Promise<SkillUploadPreview> {
  const res = await fetch(skillUrl("/upload"), jsonPost({ data_b64: dataB64, filename }));
  return res.json();
}

export async function confirmSkillUpload(
  token: string,
  scope: "global" | "project" = "global",
  workspace?: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(skillUrl("/upload/confirm"), jsonPost({ token, scope, workspace }));
  return res.json();
}


export async function sessionSkills(
  sessionId: string,
  workspace?: string,
): Promise<SessionSkillRow[]> {
  const qs = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/skills${qs}`,
  );
  return (await res.json()).skills ?? [];
}

export async function setSessionSkill(
  sessionId: string,
  skill: string,
  enabled: boolean,
  opts: { clear?: boolean; workspace?: string } = {},
): Promise<{ skills?: SessionSkillRow[]; ok?: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/skills`,
    jsonPost({
      skill,
      enabled,
      ...(opts.clear ? { clear: true } : {}),
      ...(opts.workspace ? { workspace: opts.workspace } : {}),
    }),
  );
  return res.json();
}

// -- DeskMate curator (P0-7: 自控清理 + 一键还原) ----------------------------
export interface DeskmateCuratorStatus {
  auto_archive_enabled: boolean;
  total: number;
  merge_candidates: { a: string; b: string; similarity: number }[];
  archive_candidates: { name: string; uses: number; stale_days: number }[];
  auto_archive_candidates: { name: string; uses: number; stale_days: number }[];
  archived_now: number;
  archived_names: string[];
  report_path: string;
}

export async function getDeskmateSkillsCurator(): Promise<DeskmateCuratorStatus> {
  const res = await fetch(`${httpBase()}/v1/deskmate/skills/curator`);
  return res.json();
}

export async function restoreDeskmateSkill(
  name: string,
): Promise<{ status: string; message?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/deskmate/skills/restore`,
    jsonPost({ name }),
  );
  return res.json();
}

// -- Inbox + Unattended -------------------------------------------------------
export interface InboxItem {
  id: string;
  session_id: string;
  kind: "approval" | "question" | "notification" | "directory" | "plan";
  title: string;
  body: string;
  state: "pending" | "resolved";
  resolution: string | null;
  inbox: string;
  created_at: string;
  resolved_at: string | null;
  visibility?: "inline" | "inbox";
  // Question metadata (ask_user): quick-reply choices + a free-text escape. Options may be rich
  // {label, description, recommended, preview} objects (OPE-51); `questions` is the grouped form
  // (stepper), whose resolution is a JSON object string keyed by header-or-question.
  options?: QuestionOption[];
  allow_text?: boolean;
  multi?: boolean;
  header?: string;
  questions?: GroupedQuestion[];
  // Kind-specific payload (directory: {path, writable}; …).
  data?: Record<string, any>;
  // Originating-session context (server-joined) so the Inbox is self-contained.
  session_title?: string;
  session_agent?: string | null;
  session_workspace?: string | null;
  session_exists?: boolean;
}

export async function getInbox(sessionId?: string, state?: string): Promise<InboxItem[]> {
  const q = new URLSearchParams();
  if (sessionId) q.set("session_id", sessionId);
  if (state) q.set("state", state);
  const res = await fetch(`${httpBase()}/v1/inbox?${q.toString()}`);
  return (await res.json()).items;
}

export async function resolveInboxItem(
  id: string,
  resolution: string,
): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/inbox/${encodeURIComponent(id)}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution }),
  });
  return res.json();
}

// -- channel subscriptions (view-only) ----------------------------------------
export interface Subscription {
  session_id: string;
  session_title: string;
  agent: string;
  channel: string;
  channel_name?: string | null; // resolved display name ("ocw-test"); address stays the id
  routing_target: string | null;
  collision: boolean; // inbound subscription == outbound Inbox routing on the same channel
}

export interface RecentChannel {
  channel: string;
  name?: string | null; // resolved display name, e.g. "ocw-test" (falls back to the address)
  last_from: string | null;
  last_text: string | null;
}

export async function getSubscriptions(): Promise<Subscription[]> {
  const res = await fetch(`${httpBase()}/v1/subscriptions`);
  return (await res.json()).subscriptions ?? [];
}

// -- inbox routing (where Unattended approvals/questions get mirrored) ---------
export interface InboxBinding {
  name: string;
  channel: string | null; // platform, e.g. "slack" (null = in-app Inbox only)
  target: string; // chat_id, e.g. "C0BEJNCQQ8Y"
}

export async function getInboxRouting(): Promise<InboxBinding[]> {
  const res = await fetch(`${httpBase()}/v1/inbox/routing`);
  return (await res.json()).bindings ?? [];
}

export async function setInboxBinding(
  name: string,
  channel: string | null,
  target: string,
): Promise<{ ok: boolean; bindings?: InboxBinding[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/inbox/routing/binding`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, channel, target }),
  });
  return res.json();
}

export interface UnroutedItem {
  source: string;
  sender: string;
  text: string;
  reason: string;
  ts: number;
}

export async function getUnrouted(): Promise<UnroutedItem[]> {
  const res = await fetch(`${httpBase()}/v1/unrouted`);
  return (await res.json()).items ?? [];
}

export async function getRecentChannels(): Promise<RecentChannel[]> {
  const res = await fetch(`${httpBase()}/v1/channels/recent`);
  return (await res.json()).channels ?? [];
}

export async function subscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; channel?: string; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/subscriptions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function unsubscribeChannel(
  sessionId: string,
  channel: string,
): Promise<{ ok: boolean; removed?: boolean }> {
  const res = await fetch(`${httpBase()}/v1/subscriptions/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, channel }),
  });
  return res.json();
}

export async function getUnattended(sessionId: string): Promise<boolean> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
  );
  return (await res.json()).unattended;
}

export async function setUnattended(
  sessionId: string,
  unattended: boolean,
): Promise<{ ok: boolean; unattended: boolean }> {
  const res = await fetch(
    `${httpBase()}/v1/sessions/${encodeURIComponent(sessionId)}/unattended`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ unattended }),
    },
  );
  return res.json();
}

// Auto-Approve metering (§1.7): per-session reviewer counts from the durable audit rows.
export interface ReviewerBucket {
  checks: number;
  allow: number;
  deny: number;
  unsure: number;
  tokens_in: number;
  tokens_out: number;
  // Cached-prefix share of the input, billed at ~10%. Without it the badge only ever
  // showed the FRESH tokens — a fraction of what a check really processes.
  cache_read: number;
  cache_write: number;
}
export interface ReviewerStats {
  live: ReviewerBucket;   // the mode actually deciding (Mode.AUTO_APPROVE)
  shadow: ReviewerBucket; // shadow evaluation: recorded next to the human's own decisions
}

export async function getReviewerStats(sessionId: string): Promise<ReviewerStats> {
  const res = await fetch(`${httpBase()}/v1/sessions/${sessionId}/reviewer-stats`);
  return res.json();
}

export async function getSettings(): Promise<ModelSettings> {
  const res = await fetch(`${httpBase()}/v1/settings`);
  return res.json();
}

export async function setModelKey(
  apiKey: string,
): Promise<{ ok: boolean; error?: string; has_key?: boolean; source?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/model-key`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
  return res.json();
}

export async function setDefaultModel(
  model: string,
): Promise<{ ok: boolean; error?: string; model?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/default-model`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function addModel(model: string): Promise<ModelSettings & { ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/settings/models/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function removeModel(model: string): Promise<ModelSettings & { ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/settings/models/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  return res.json();
}

export async function setOnboarded(value: boolean): Promise<{ ok: boolean; onboarded: boolean }> {
  const res = await fetch(`${httpBase()}/v1/settings/onboarded`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  return res.json();
}

// -- Memory (MEMORY-SPEC §5.3/§6: the memory screen, user rules, toast Undo) ----

export interface MemoryEntry {
  id: number;
  scope: string;
  content: string;
  summary: string;
  created_at: string;
}

export interface MemorySettings {
  enabled: boolean;
  user_rules: string;
  auto_inject?: boolean;
}

// Fired whenever memory changes from OUTSIDE the memory screen — today the agent
// saving or editing one mid-conversation. The screen only loads its list on mount, so
// without this it sits there stale and the user reads "Nothing yet" seconds after a
// save actually landed (owner-hit 2026-07-28).
export const MEMORY_CHANGED = "coworker:memory-changed";
export function announceMemoryChanged() {
  window.dispatchEvent(new CustomEvent(MEMORY_CHANGED));
}

export async function getMemory(): Promise<MemoryEntry[]> {
  const res = await fetch(`${httpBase()}/v1/memory`);
  return (await res.json()).memory ?? [];
}

export async function updateMemory(
  id: number,
  content: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/memory/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  return res.json();
}

export async function deleteMemory(id: number): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/memory/${id}`, { method: "DELETE" });
  return res.json();
}

export async function deleteAllMemory(): Promise<{ ok: boolean; deleted: number }> {
  const res = await fetch(`${httpBase()}/v1/memory`, { method: "DELETE" });
  return res.json();
}

export async function getMemorySettings(): Promise<MemorySettings> {
  const res = await fetch(`${httpBase()}/v1/memory/settings`);
  return res.json();
}

export async function setMemorySettings(
  patch: Partial<MemorySettings>,
): Promise<MemorySettings> {
  const res = await fetch(`${httpBase()}/v1/memory/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

// -- model providers (OpenAI, Ollama, …) --------------------------------------
export interface ProviderField {
  key: string;
  label: string;
  secret: boolean;
  required: boolean;
  help: string;
  placeholder: string;
  default?: string; // pre-filled editable value (e.g. an OpenAI-compatible vendor's endpoint)
  // Non-empty → segmented choice, not a text input. tag = tiny badge ("Easiest");
  // desc = one-liner atop the method panel; command = copyable terminal command.
  choices?: { value: string; label: string; tag?: string; desc?: string; command?: string }[];
  show_when?: Record<string, string> | null; // render only while these fields hold these values
}

export interface ProviderInfo {
  name: string;
  title: string;
  needs_key: boolean;
  fields: ProviderField[];
  configured: boolean;
  values: Record<string, string>; // non-secret stored values (e.g. base_url), for prefilling
  suggested_models: string[]; // bare model-name suggestions for the "add model" datalist
  recommended_model: string | null; // pre-filled default for this provider (e.g. qwen3-coder:30b)
  blurb?: string; // one-line note under the title ("Uses X's OpenAI-compatible API…")
  key_set_at?: string | null; // ISO date the key was last (re)saved — absent for env-only config
  last_used_at?: number | null; // epoch secs the provider last served a completion
  // OAuth providers (auth === "oauth"): browser sign-in instead of a key form.
  auth?: string | null;
  signed_in?: boolean;
  account?: string | null; // signed-in account label (email or id)
  authorizing?: boolean;
  last_error?: string | null;
}

// -- ChatGPT-subscription provider sign-in (OAuth; tokens never reach the GUI) ------
export interface CodexAuthStatus {
  signed_in: boolean;
  account?: string | null;
  authorizing: boolean;
  last_error?: string | null;
  authorize_url?: string | null;
}

export async function codexSignin(): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/providers/openai-codex/signin`, { method: "POST" });
  return res.json();
}

export async function codexAuthStatus(): Promise<CodexAuthStatus> {
  const res = await fetch(`${httpBase()}/v1/providers/openai-codex/status`);
  return res.json();
}

export async function codexSignout(): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/providers/openai-codex/signout`, { method: "POST" });
  return res.json();
}

export async function getProviders(): Promise<ProviderInfo[]> {
  const res = await fetch(`${httpBase()}/v1/providers`);
  return res.json();
}

export async function setProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string; provider?: string; recommended_model?: string | null }> {
  const res = await fetch(`${httpBase()}/v1/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Forget a provider's stored config (Settings ▸ Models "Remove key…"). */
export async function removeProvider(name: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/providers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  return res.json();
}

/** Live read-only credential check (does NOT save the key). Triggered by the user's "Test" click. */
export async function verifyProvider(
  name: string,
  fields: Record<string, string>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/providers/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, fields }),
  });
  return res.json();
}

/** Client-side provider guess from an API key's shape (mirrors the server's detect_provider). */
export function detectProvider(apiKey: string): string | null {
  const key = (apiKey || "").trim();
  if (!key) return null;
  if (key.startsWith("sk-ant-")) return "anthropic";
  if (key.startsWith("sk-or-")) return "openrouter";
  if (key.startsWith("AIza")) return "gemini";
  if (key.startsWith("sk-") || key.startsWith("sk_")) return "openai";
  return null;
}

// -- super-agent --------------------------------------------------------------
export interface RecentSender {
  user_id: string;
  user_name: string | null;
  chat_id: string;
  chat_type: string;
  target: string;
  authorized: boolean;
  team_id?: string | null; // workspace (managed relay); null on manual Socket Mode
}

// -- direct-message routing ---------------------------------------------------
export async function getDmRoute(): Promise<string | null> {
  const res = await fetch(`${httpBase()}/v1/messaging/dm-route`);
  return (await res.json()).dm_session ?? null;
}

export async function setDmRoute(sessionId: string): Promise<{ ok: boolean; dm_session: string | null }> {
  const res = await fetch(`${httpBase()}/v1/messaging/dm-route`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  return res.json();
}

// -- automations (scheduled tasks) --------------------------------------------
export interface Automation {
  id: string;
  title: string;
  instructions: string;
  schedule: string;
  schedule_raw?: { kind: string; cron?: string | null; fire_at?: string | null; timezone?: string };
  workspace: string;
  agent: string;
  enabled: boolean;
  next_run: number | null;
  last_run: number | null;
  last_status: string | null;
  run_count: number;
  notify_on_completion: boolean;
  // UX-023 sidebar badges: runs started since the user last opened this automation's
  // detail; `unseen_failed` = the newest unseen run errored (danger tint).
  unseen_runs?: number;
  unseen_failed?: boolean;
  seen_runs_at?: number;
  // Standing scoped approvals (§25): target-bound rules this automation may exercise
  // without asking. `entry` is the raw record entry — the revoke handle; `target` is
  // null for legacy name-only entries.
  always_allowed: { entry: string; tool: string; target: string | null }[];
}

export interface AutomationRun {
  run_id: string;
  task_id: string;
  session_id: string;
  started_at: number;
  finished_at: number | null;
  status: string;
  result_text: string | null;
  artifacts: string[];
  error: string | null;
  trigger: string;
}

export async function getAutomations(): Promise<Automation[]> {
  const res = await fetch(`${httpBase()}/v1/automations`);
  return (await res.json()).tasks ?? [];
}

// Fired after any automation mutation the sidebar should reflect immediately
// (mark-seen, create, delete) — its poll covers the rest.
export const AUTOMATIONS_CHANGED = "coworker:automations-changed";
export function announceAutomationsChanged() {
  window.dispatchEvent(new CustomEvent(AUTOMATIONS_CHANGED));
}

/** App-wide event stream (/ws/events): session-independent server pushes — today
 * automation_run_started (the UX-026 toast). Quietly reconnects while the app is
 * open; the returned cleanup stops it for good. */
export function connectEvents(
  onEvent: (msg: { type: string; data?: Record<string, unknown> }) => void
): () => void {
  let ws: WebSocket | null = null;
  let timer: number | null = null;
  let closed = false;
  const open = () => {
    if (closed) return;
    ws = openWebSocket(`${wsBase()}/ws/events`);
    ws.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch {
        /* malformed frame — ignore */
      }
    };
    ws.onclose = () => {
      if (!closed) timer = window.setTimeout(open, 5000);
    };
  };
  open();
  return () => {
    closed = true;
    if (timer !== null) window.clearTimeout(timer);
    ws?.close();
  };
}

/** Advance the automation's seen mark — clears its unseen-runs badge (UX-023). */
export async function markAutomationSeen(id: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${httpBase()}/v1/automations/${id}/seen`, { method: "POST" });
  return res.json();
}

export async function createAutomation(payload: {
  title: string;
  instructions: string;
  cron?: string;
  fire_at?: string;
  timezone?: string;
  // §25 standing grants (the creating surface rendered them; submit IS the consent).
  // Only target-bound write entries survive server-side validation.
  permissions?: { tool: string; target: string; access: "read" | "write" }[];
}): Promise<{ ok: boolean; error?: string; task?: Automation }> {
  const res = await fetch(`${httpBase()}/v1/automations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json();
}

export async function getAutomation(id: string): Promise<{ task: Automation; runs: AutomationRun[] }> {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`);
  return res.json();
}

export async function updateAutomation(id: string, changes: Record<string, any>) {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return res.json();
}

export async function deleteAutomation(id: string) {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}`, { method: "DELETE" });
  return res.json();
}

export interface PreparedRun {
  ok: boolean;
  error?: string;
  run_id: string;
  session_id: string;
  workspace: string;
  agent: string;
  prompt: string;
}

/** Prepare a live manual run: returns the session to open + the opening prompt to send. */
export async function runAutomation(id: string): Promise<PreparedRun> {
  const res = await fetch(`${httpBase()}/v1/automations/${encodeURIComponent(id)}/run`, { method: "POST" });
  return res.json();
}

/** Mark a manual run complete after its first turn finished. */
export async function finalizeAutomationRun(id: string, runId: string) {
  const res = await fetch(
    `${httpBase()}/v1/automations/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/finalize`,
    { method: "POST" },
  );
  return res.json();
}

export async function allowUser(
  name: string,
  userId: string,
  teamId?: string | null,
  displayName?: string,
) {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/allow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      ...(teamId ? { team_id: teamId } : {}),
      // Directory picks carry the display name so the chip is readable at once.
      ...(displayName ? { name: displayName } : {}),
    }),
  });
  return res.json();
}

// One workspace member from the roster (people picker; users:read, cached locally).
export interface SlackMember {
  id: string;
  name: string;
  handle: string;
  guest: boolean;
}

// One channel from the workspace roster. Private channels appear only where the
// bot is a member (Slack API constraint); is_member=false → "invite @OpenWorker" hint.
export interface SlackChannelEntry {
  id: string;
  name: string;
  is_private: boolean;
  is_member: boolean;
}

/** Workspace member roster for the people picker (teamId "default" = manual Socket Mode). */
export async function getSlackDirectory(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; members?: SlackMember[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/directory?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Channel roster for the channel typeahead (name → id resolution). */
export async function getSlackChannels(
  teamId: string,
  q = "",
): Promise<{ ok: boolean; error?: string; channels?: SlackChannelEntry[] }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/channels?q=${encodeURIComponent(q)}`,
  );
  return res.json();
}

/** Resolve a parked unauthorized message (§19): dismiss / allow / allow_deliver. */
export async function resolveUnauthorized(
  name: string,
  itemId: string,
  action: "dismiss" | "allow" | "allow_deliver",
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(name)}/unauthorized/${encodeURIComponent(itemId)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    },
  );
  return res.json();
}

export async function disallowUser(name: string, userId: string, teamId?: string | null) {
  const res = await fetch(`${httpBase()}/v1/connectors/${encodeURIComponent(name)}/disallow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(teamId ? { user_id: userId, team_id: teamId } : { user_id: userId }),
  });
  return res.json();
}

export async function addSlackApprovalOwner(
  userId: string,
  displayName?: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/approval-owners/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_id: userId,
      ...(displayName ? { name: displayName } : {}),
    }),
  });
  return res.json();
}

export async function removeSlackApprovalOwner(
  userId: string,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/approval-owners/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  return res.json();
}

/** Stop relaying one managed Slack workspace (the app stays installed in Slack). */
export async function disconnectSlackWorkspace(teamId: string): Promise<{ ok: boolean; error?: string; remaining_workspaces?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/slack/workspaces/${encodeURIComponent(teamId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Gmail mailbox; the default pointer moves to the next account. */
export async function disconnectGmailAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGmailDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/gmail/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE Google Calendar account; the default pointer moves to the next one. */
export async function disconnectGcalAccount(email: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setGcalDefaultAccount(email: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/google_calendar/accounts/${encodeURIComponent(email)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE account of a generic multi-account connector (notion, attio,
 * posthog, …); the default pointer moves to the next account. */
export async function disconnectAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string; remaining_accounts?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setDefaultAccount(connector: string, accountId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/${encodeURIComponent(connector)}/accounts/${encodeURIComponent(accountId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the "Never show agents" lists (senders and/or labels; omit to keep). */
export async function setGmailFilters(filters: { senders?: string[]; labels?: string[] }): Promise<{ ok: boolean; filters?: GmailFilters; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/gmail/filters`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(filters),
  });
  return res.json();
}

// GitHub relay health, the Slack three-layer shape: shared relay socket /
// cloud sign-in / per-installation token health (+ missed-event counts).
export interface GithubStatus {
  ok: boolean;
  mode: string;
  relay: { state: string; reconnects: number; last_event_at: number | null; last_error: string };
  signed_in: boolean;
  installs: Record<string, { token_ok: boolean }>;
  missed: Record<string, number>;
}

export async function getGithubStatus(): Promise<GithubStatus> {
  const res = await fetch(`${httpBase()}/v1/connectors/github/status`);
  return res.json();
}

/** Stop relaying ONE GitHub App installation to this computer. */
export async function disconnectGithubInstallation(installationId: string): Promise<{ ok: boolean; error?: string; remaining_installs?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/github/installations/${encodeURIComponent(installationId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

/** Drop ONE HubSpot portal; the default pointer moves to the next portal. */
export async function disconnectHubSpotPortal(hubId: string): Promise<{ ok: boolean; error?: string; remaining_portals?: number }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/disconnect`,
    { method: "POST" },
  );
  return res.json();
}

export async function setHubSpotDefaultPortal(hubId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${httpBase()}/v1/connectors/hubspot/portals/${encodeURIComponent(hubId)}/default`,
    { method: "POST" },
  );
  return res.json();
}

/** Replace the hidden-fields denylist (properties stripped from agent reads). */
export async function setHubSpotHiddenFields(fields: string[]): Promise<{ ok: boolean; hidden_fields?: string[]; error?: string }> {
  const res = await fetch(`${httpBase()}/v1/connectors/hubspot/hidden-fields`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden_fields: fields }),
  });
  return res.json();
}

/** Slack health, three honest layers: relay socket / cloud sign-in / per-team tokens. */
export interface SlackStatus {
  mode: string; // "relay" | "" (manual/off)
  relay: {
    state: "live" | "reconnecting" | "offline";
    reconnects: number;
    last_event_at: number | null;
    last_error: string;
  };
  signed_in: boolean;
  teams: Record<string, { token_ok: boolean }>;
}

export async function getSlackStatus(): Promise<SlackStatus> {
  const res = await fetch(`${httpBase()}/v1/connectors/slack/status`);
  return res.json();
}

export type Handlers = {
  onEvent: (event: WsEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
};

/**
 * 会话 WebSocket 的保活与重连参数。
 *
 * 2026-10-05：web端「浏览器一直开着 → 过一段时间 → 发送键变灰、小桔不回复」，
 * 刷新浏览器即好。根因是 Session 只建一次 socket、断开后无任何恢复：服务端
 * `/ws/session/{id}` 是 `while True: receive_json()` 无限等待，不会主动断，但中间的
 * Caddy/代理空闲超时、云端服务重启、笔记本休眠/换网都会把这条长连接掐掉。
 * 一旦断开，`onclose` 把 `connected` 置回 false 而永不恢复 → Composer's send button
 * 永久 `disabled={!props.connected}`；快捷键虽能调`userMessage`，但 `send()` 遇 CLOSED
 * 既不发也不入队 → 消息静默丢弃（这正是"按快捷键也没反应"的原因）。
 *
 * 修法：周期性 ping 保活（防中间层空闲回收）+ 指数退避自动重连（覆盖代理超时/重启），
 * 并让断线期间的用户消息进 outbox 排队，重连成功后由 `flush()` 补发。
 */
const WS_PING_INTERVAL_MS = 25_000;
const WS_RECONNECT_BASE_MS = 500;
const WS_RECONNECT_MAX_MS = 15_000;

export class Session {
  // Assigned inside connect() (constructor delegates to it) → `!` because TS can't see
  // through the indirection and would otherwise demand an initializer here.
  private ws!: WebSocket;
  // Payloads sent before the socket finished opening, replayed on `onopen`. Belt-and-suspenders
  // against the first message being dropped if the user sends in the connect window.
  private outbox: object[] = [];
  // --- 保活/重连 state (2026-10-05) ---
  private closedByUs = false;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryAttempt = 0;

  constructor(sessionId: string, workspace: string, agent: string, handlers: Handlers) {
    const q = `?workspace=${encodeURIComponent(workspace)}&agent=${encodeURIComponent(agent)}`;
    this.url = `${wsBase()}/ws/session/${sessionId}${q}`;
    this.handlers = handlers;
    this.connect();

    // 保活 ping：应用层无消息时每 25s 发一个空 ping（服务端对未知 type 静默忽略），
    // 让中间的 Caddy/代理不会因空闲把这条长连接回收掉。socket 一断就停，重连后重启。
    this.pingTimer = setInterval(() => {
      if (this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          /* 发送失败交给 onclose → 重连路径处理 */
        }
      }
    }, WS_PING_INTERVAL_MS);
  }

  private url!: string;
  private handlers!: Handlers;

  /** 建（重）连。每次重连都重新绑定全部 handler，outbox 保留待补发。 */
  private connect() {
    this.ws = openWebSocket(this.url);
    this.ws.onmessage = (e) => {
      try {
        this.handlers.onEvent(JSON.parse(e.data));
      } catch {
        /* malformed frame — ignore */
      }
    };
    this.ws.onopen = () => {
      this.retryAttempt = 0; // 连上即重置退避，下一次断线从base 重新爬
      this.flush();
      this.handlers.onOpen?.();
    };
    this.ws.onclose = () => {
      this.handlers.onClose?.();
      // 主动 close()（切会话/卸载）不重连，否则会泄漏一条幽灵 socket。
      if (!this.closedByUs) this.scheduleReconnect();
    };
  }

  /** 指数退避重连，封顶 WS_RECONNECT_MAX_MS，持续尝试直到成功或被 close()。 */
  private scheduleReconnect() {
    if (this.retryTimer) return; // 已有重连在排队
    const delay = Math.min(
      WS_RECONNECT_BASE_MS * 2 ** this.retryAttempt,
      WS_RECONNECT_MAX_MS,
    );
    this.retryAttempt += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (this.closedByUs) return;
      try {
        this.connect();
      } catch {
        this.scheduleReconnect(); // 建连即抛（如 URL 非法/无网络）→ 继续排下一轮
      }
    }, delay);
  }

  private flush() {
    if (this.ws.readyState !== WebSocket.OPEN) return;
    const pending = this.outbox;
    this.outbox = [];
    for (const p of pending) this.ws.send(JSON.stringify(p));
  }

  private send(payload: object) {
    if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
    // Still connecting: queue and flush on open rather than silently dropping.
    else if (this.ws.readyState === WebSocket.CONNECTING) this.outbox.push(payload);
    // CLOSED/CLOSING（2026-10-05 修）：断线后的用户消息**必须入队**。旧实现直接落空，
    // 于是「刷新前按快捷键发送」静默丢失（发送键已灰、消息进黑洞）。重连成功后
    // flush() 会补发；同时 scheduleReconnect() 正在把 socket 拉回来。
    else this.outbox.push(payload);
  }

  /** `model` = the composer's CURRENT selection, carried on every message so the turn uses
   * exactly what the user sees — immune to set_model races across reconnects (a new cowork
   * session always reconnects once to adopt its scratch dir, which could drop a queued
   * set_model and leave the engine on a stale/resumed model; found 2026-07-04). */
  userMessage(text: string, attachments?: unknown[], model?: string, skill?: string) {
    this.send({
      type: "user_message",
      text,
      ...(model ? { model } : {}),
      ...(attachments?.length ? { attachments } : {}),
      // Force-run (SKILLS-SPEC §4.1): the composer's /skill pick rides as its own field;
      // the server validates it against the session's effective menu and frames the turn.
      ...(skill ? { skill } : {}),
    });
  }

  approve(decision: string) {
    this.send({ type: "approval", decision });
  }

  /** §8.4 "Allow anyway": register a ONE-SHOT exact-action approval for a reviewer-denied
   *  tool call. The caller follows up with a normal user message so the agent retries. */
  allowAnyway(name: string, args: any) {
    this.send({ type: "allow_anyway", name, arguments: args ?? {} });
  }

  // Reply to a `request_directory` prompt: grant a folder (with access level) or decline.
  respondDirectory(granted: boolean, path?: string, writable?: boolean) {
    this.send({ type: "directory_response", granted, ...(path ? { path } : {}), writable: !!writable });
  }

  // Reply to a `request_tool` prompt: install the pinned build, or skip the check.
  respondTool(approved: boolean) {
    this.send({ type: "tool_response", approved });
  }

  // Reply to a `propose_plan` prompt: approve (choosing the execution mode) or reject with feedback.
  respondPlan(approved: boolean, mode?: string, feedback?: string) {
    this.send({
      type: "plan_response",
      approved,
      ...(mode ? { mode } : {}),
      ...(feedback ? { feedback } : {}),
    });
  }

  respondTeam(approved: boolean, feedback?: string, enableChat?: boolean) {
    this.send({
      type: "team_response",
      approved,
      ...(feedback ? { feedback } : {}),
      ...(enableChat !== undefined ? { enable_chat: enableChat } : {}),
    });
  }

  respondItems(approved: boolean, feedback?: string) {
    this.send({
      type: "items_response",
      approved,
      ...(feedback ? { feedback } : {}),
    });
  }

  // Answer a live `ask_user` prompt (attended sessions; unattended ones answer via the Inbox).
  respondQuestion(answer: string) {
    this.send({ type: "question_response", answer });
  }

  interrupt() {
    this.send({ type: "interrupt" });
  }

  // Re-run a turn that ended in a provider error — no new user message; the server
  // guards on the history tail so a stray frame is a no-op.
  retry() {
    this.send({ type: "retry" });
  }

  setMode(mode: string) {
    this.send({ type: "set_mode", mode });
  }

  setModel(model: string) {
    this.send({ type: "set_model", model });
  }

  close() {
    // 2026-10-05：置位 closedByUs 并清掉保活/重连定时器 —— 否则本会话被 close() 后，
    // 仍在排队的 retryTimer 会把一条幽灵 socket 拉起来（切会话/卸载时泄漏）。
    this.closedByUs = true;
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    // Detach before closing: this socket's async `close` event may land AFTER the
    // successor session's `open` (observed when switching into an automation-run
    // session), and a torn-down socket must not clobber the new one's connected state.
    this.ws.onopen = null;
    this.ws.onmessage = null;
    this.ws.onclose = null;
    this.ws.close();
  }
}

// -- project bindings (pass 20 / UX-044) ---------------------------------------

export interface ProjectMenu {
  kind: "memory" | "board";
  bound: string | null;
  derived: { kind: "git" | "folder"; label: string; full: string; key: string } | null;
  named: { name: string; key: string }[];
}

export async function getProjectMenu(sessionId: string, kind: "memory" | "board"): Promise<ProjectMenu> {
  const r = await fetch(`${httpBase()}/v1/sessions/${sessionId}/project-menu?kind=${kind}`);
  return r.json();
}

export async function setProjectBinding(
  sessionId: string,
  kind: "memory" | "board",
  name: string | null,
): Promise<{ ok: boolean; error?: string }> {
  const r = await fetch(`${httpBase()}/v1/sessions/${sessionId}/bindings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, name }),
  });
  return r.json();
}

export async function nameCurrentProject(
  sessionId: string,
  kind: "memory" | "board",
  name: string,
): Promise<{ ok: boolean; error?: string }> {
  const r = await fetch(`${httpBase()}/v1/sessions/${sessionId}/project-name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, name }),
  });
  return r.json();
}

export const authedFetch = (
  input: RequestInfo | URL,
  init: RequestInit = {},
  opts?: { timeoutMs?: number },
): Promise<Response> => {
  const headers = new Headers(init.headers);
  const token = apiToken();
  if (token) headers.set("X-OpenWorker-Token", token);
  let signal = init.signal as AbortSignal | undefined;
  let abortTimer: ReturnType<typeof setTimeout> | undefined;
  if (opts?.timeoutMs && !signal) {
    const ac = new AbortController();
    abortTimer = setTimeout(() => ac.abort(), opts.timeoutMs);
    signal = ac.signal;
  }
  return globalThis
    .fetch(input, { ...init, headers, signal })
    .finally(() => {
      if (abortTimer) clearTimeout(abortTimer);
    });
};

export interface ProofreadItem {
  id: string;
  excerpt: string;
  type: string;
  suggestion: string;
  reason: string;
}

export interface ProofreadResult {
  letter: string;
  items: ProofreadItem[];
  report?: string;
  chapters?: { idx: number; title: string; char_count: number }[];
  current_chapter?: number | null;
  total_items?: number;
}

export interface ManuscriptChapter {
  idx: number;
  title: string;
  char_count: number;
  text: string;
}

export interface StyleProfile {
  summary?: string;
  features?: Record<string, string>;
}

export async function calibrateWritingStyle(opts: {
  path?: string;
  content_base64?: string;
  filename?: string;
}): Promise<{ ok: boolean; profile: StyleProfile; chars: number }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/style-profile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as any));
    throw new Error(e.error || `风格校准失败 (${res.status})`);
  }
  return res.json();
}

export async function getWritingStyle(): Promise<{ profile: StyleProfile | null }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/style-profile`);
  return res.json();
}

/** 溯源 + 遗忘可见（P0-4 + 8.3）：拉取最近「注入了哪些记忆」与「忘了哪些记忆」。 */
export async function getDeskmateMemoryTrace(
  kind: "both" | "inject" | "forgot" = "both",
  limit = 10,
): Promise<{ inject?: TraceRecord[]; forgot?: TraceRecord[] }> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/memory/trace?kind=${kind}&limit=${limit}`,
    );
    if (!res.ok) return {};
    return res.json();
  } catch {
    return {}; // 静默降级：trace 展示是锦上添花，不阻塞记忆面板
  }
}

/** P0-5 记忆去重：USER.md 近重复率与条目数（8.0 红线：前端可感可看）。 */
export async function getDeskmateUserDedup(): Promise<{
  dup_rate?: number;
  entries?: number;
}> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/memory/user-dedup`);
    if (!res.ok) return {};
    return res.json();
  } catch {
    return {};
  }
}

export interface DeskmateMemoryLayering {
  global_entries?: number;
  project_entries?: number;
  project_names?: string[];
  book_sections_left?: string[];
  layered?: boolean;
  stale_rate?: number;
  stale_days?: number;
  stale_entries?: number;
}

export async function getDeskmateMemoryLayering(): Promise<DeskmateMemoryLayering> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/memory/layering`);
    if (!res.ok) return {};
    return res.json();
  } catch {
    return {};
  }
}

// -- P1-1 条目级可控记忆（Replika 式：用户可查看 / 逐条遗忘小桔的结构化记忆）--
export interface LearnedMemoryItem {
  id: number;
  ts: string | null;
  fact: string;
  category: string;
  status: string; // "active" | "forgotten" | …
  valid_until: string | null;
  valid_from: string | null; // P1-2 双时态：事实生效起点
  source?: string; // 来源标记（"live" / "legacy" / "local_intake:<path>" 等）
}

export interface ProfileMemoryItem {
  id: string;
  layer: string;
  dimension: string;
  topic: string;
  content: string;
  status: string; // "active" | "forgotten" | …
  last_updated: string | null;
  valid_from: string | null; // P1-2 双时态：自何时持有
  valid_until: string | null; // NULL = 至今有效
}

/** P1-2 双时态：拉取某 ISO 时点有效的认知画像快照。 */
export async function getDeskmateProfileItemsAsOf(
  at: string,
): Promise<{ ok: boolean; at: string; items: ProfileMemoryItem[] }> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/memory/profile-items/at?at=${encodeURIComponent(at)}`,
    );
    if (!res.ok) return { ok: false, at, items: [] };
    const d = await res.json();
    return { ok: !!d.ok, at: d.at ?? at, items: d.items ?? [] };
  } catch {
    return { ok: false, at, items: [] };
  }
}

/** 拉取小桔「学到的记忆」条目（learned_history），含 id/status。 */
export async function getDeskmateLearnedMemories(
  limit = 300,
): Promise<LearnedMemoryItem[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/memory/learned?limit=${limit}`);
    if (!res.ok) return [];
    const d = await res.json();
    return d.items ?? [];
  } catch {
    return [];
  }
}

/** 用户主动遗忘某条学到的记忆（标记 forgotten，检索自动剔除）。 */
export async function forgetDeskmateLearnedMemory(itemId: number): Promise<boolean> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/memory/learned/${itemId}`,
      { method: "DELETE" },
    );
    if (!res.ok) return false;
    const d = await res.json();
    return !!d.ok;
  } catch {
    return false;
  }
}

/** 拉取小桔「认知条目」（profile_items：偏好/价值观/世界观等），含 id/layer/status。 */
export async function getDeskmateProfileItems(
  limit = 300,
): Promise<ProfileMemoryItem[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/memory/profile-items?limit=${limit}`);
    if (!res.ok) return [];
    const d = await res.json();
    return d.items ?? [];
  } catch {
    return [];
  }
}

/** 用户主动遗忘某条认知条目（物理删除使遗忘生效 + 留痕）。 */
export async function deleteDeskmateProfileItem(itemId: string): Promise<boolean> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/memory/profile-item/${encodeURIComponent(itemId)}`,
      { method: "DELETE" },
    );
    if (!res.ok) return false;
    const d = await res.json();
    return !!d.ok;
  } catch {
    return false;
  }
}

/** P1-8：读取 growth snapshot（含 trust_controls_proactive 状态）。 */
export async function getDeskmateGrowth(): Promise<Record<string, unknown>> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/growth`);
    if (!res.ok) return {};
    return res.json();
  } catch {
    return {};
  }
}

// ---------------------------------------------------------------------------
// P1-3：计划任务（auto-task）用户可配置面板（对标 Spark Skills / Replika「你在忙什么」）
// ---------------------------------------------------------------------------

export interface AutoTaskRecentEntry {
  date: string;
  target: string;
  summary: string;
  action: string;
}

export interface AutoTaskItem {
  task_type: string;
  task_name: string;
  category: string;
  description: string;
  enabled: boolean;
  created_at: string;
  last_run_at: string;
  run_count: number;
  recent: AutoTaskRecentEntry[];
}

/** 拉取小桔的「计划任务」列表（含每条最近执行摘要）。 */
export async function getDeskmateAutoTasks(
  limit = 3,
): Promise<AutoTaskItem[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/auto-tasks?limit=${limit}`);
    if (!res.ok) return [];
    const d = await res.json();
    return d.tasks ?? [];
  } catch {
    return [];
  }
}

/** 切换某计划任务的启用/暂停状态。返回最新的 enabled 值；任务不存在返回 null。 */
export async function toggleDeskmateAutoTask(
  taskType: string,
  enabled: boolean,
): Promise<boolean | null> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/auto-tasks/${encodeURIComponent(taskType)}/toggle`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    );
    if (!res.ok) return null;
    const d = await res.json();
    if (d.ok === false) return null;
    return d.enabled === undefined ? enabled : !!d.enabled;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// P2-3：本地文件 / 笔记摄入管线（对标 Khoj）—— 用户主动指定目录，扫描预览，
// 勾选确认后沉淀进小桔的「学到的记忆」（learned_history）。作者稿（如《国子监秘闻录》）
// 可读、可摄入、可讨论——读稿协助（建人物圣经 / 看进度 / 查一致性）是用户授权的在界行为，
// 走 novel_memory 的 guarded_assistance_read 通道；唯一的硬红线是原稿正文绝不直接改写
// （guarded_write 守住）。
// ---------------------------------------------------------------------------

export interface IngestCandidate {
  path: string;
  name: string;
  size: number;
  mtime: number;
  excerpt: string;
}

/** 读取已配置的摄入目录列表。 */
export async function getIngestDirs(): Promise<string[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/ingest/dirs`);
    if (!res.ok) return [];
    const d = await res.json();
    return d.dirs ?? [];
  } catch {
    return [];
  }
}

/** 保存摄入目录列表（后端去重 + 取绝对路径）。 */
export async function saveIngestDirs(dirs: string[]): Promise<string[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/ingest/dirs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dirs }),
    });
    if (!res.ok) return dirs;
    const d = await res.json();
    return d.dirs ?? dirs;
  } catch {
    return dirs;
  }
}

/** 扫描给定目录，返回候选笔记预览（不写入记忆）。 */
export async function scanIngestDirs(dirs: string[]): Promise<IngestCandidate[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/ingest/scan`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dirs }),
    });
    if (!res.ok) return [];
    const d = await res.json();
    return d.candidates ?? [];
  } catch {
    return [];
  }
}

/** 把勾选的笔记文件沉淀进记忆（用户已在 UI 确认）。 */
export async function ingestFiles(
  paths: string[],
): Promise<{ ingested: number; updated: number; skipped: number; errors: any[] }> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/ingest`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ paths }),
    });
    if (!res.ok) return { ingested: 0, updated: 0, skipped: 0, errors: [] };
    return await res.json();
  } catch {
    return { ingested: 0, updated: 0, skipped: 0, errors: [] };
  }
}

// ---------------------------------------------------------------------------
// 私人资料通道（P2-3 演化）：本地资料按分类灌库，复用史料 FTS 引擎。
// 与史料通道逻辑隔离——这是萧哥的私人资料，不当史料引用。分类=库
// （private-<分类名>.db），丢进 history 目录即被自动扫到，随时加分类零代码。
// 扫描为轻量预览（只列文件，不读内容），内容读取推迟到勾选灌库时。
// ---------------------------------------------------------------------------

export interface PrivateCandidate {
  path: string;
  name: string;
  size: number;
  mtime: number;
  ext: string;
}

/** 列出已有私人资料分类（private-*.db 的去前缀名）。 */
export async function getPrivateCategories(): Promise<string[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/private/categories`);
    if (!res.ok) return [];
    const d = await res.json();
    return d.categories ?? [];
  } catch {
    return [];
  }
}

/** 轻量扫描给定目录，返回候选文件（不读内容，不写入库）。 */
export async function scanPrivateDirs(dirs: string[]): Promise<PrivateCandidate[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/private/scan`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ dirs }),
    });
    if (!res.ok) return [];
    const d = await res.json();
    return d.candidates ?? [];
  } catch {
    return [];
  }
}

/** 把勾选的文件按分类灌库到 private-<category>.db（用户已在 UI 勾选确认）。 */
export async function ingestPrivate(
  paths: string[],
  category: string,
): Promise<{ ingested: number; updated: number; skipped: number; errors: any[] }> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/private/ingest`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ paths, category }),
    });
    if (!res.ok) return { ingested: 0, updated: 0, skipped: 0, errors: [] };
    return await res.json();
  } catch {
    return { ingested: 0, updated: 0, skipped: 0, errors: [] };
  }
}

/** 清理某分类下已删源文件的向量（策略 B：懒惰清理，下次调用时触发）。 */
export async function cleanPrivate(category: string): Promise<number> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/private/clean`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ category }),
    });
    if (!res.ok) return 0;
    const d = await res.json();
    return d.removed ?? 0;
  } catch {
    return 0;
  }
}

/** P1-8：设置信任感是否调制主动打扰阈值。 */
export async function setDeskmateGrowthProactive(control: boolean): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/growth/proactive`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ control }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** v1.9.0 意志层：读取 will_settings（enabled + 5 个阈值）。 */
export interface WillSettings {
  enabled: boolean;
  thresholds: {
    emotional_proactive_frequency_max: number;
    cognitive_threshold_delta_pos: number;
    cognitive_threshold_delta_neg: number;
    memory_budget_ratio: number;
    tone_prompt_max_len: number;
  };
  source?: Record<string, string>;
}

export async function getDeskmateWillSettings(): Promise<WillSettings | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/will/settings`);
    if (!res.ok) return null;
    // 后端返回 {ok: bool, settings: {...}} 包装层（app.py:2420/2446）；兼容裸 settings 对象。
    const data = await res.json();
    return data && typeof data === "object" && "settings" in data
      ? (data.settings as WillSettings)
      : (data as WillSettings);
  } catch {
    return null;
  }
}

/** v1.9.0 意志层：保存 will_settings（部分字段 PUT）。 */
export async function setDeskmateWillSettings(patch: Partial<WillSettings>): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/will/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(patch),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** v1.9.0 意志层：查看每条调制参数来源（调试用）。 */
export async function getDeskmateWillInspect(): Promise<Record<string, unknown>> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/will/inspect`);
    if (!res.ok) return {};
    return res.json();
  } catch {
    return {};
  }
}

// -- L2 自主层（替你行动） ----------------------------------------------------------
// 注意：后端 status()/tasks 返回**平铺结构**，无 {ok, settings} 包装层，前端直接取字段。

export interface AgencyTask {
  id: string;
  ts: string;
  action_type: string;
  risk: string;
  status: string;
  topic: string;
  summary: string;
  detail: string;
  count: number;
  // R13（2026-10-08）：后端 to_dict() 已返回 extra dict。意图提案被放行后，
  // extra.execution = {discuss_pending: true, intent_id} 用来驱动前端跳聊天。
  extra?: Record<string, unknown>;
}

/** L2 自主层状态：enabled / 白名单动作 / 主题域 / 最近任务 / 待裁决提议卡。 */
export interface AgencyStatus {
  enabled: boolean;
  kill_switch_env?: string;
  whitelist_actions: string[];
  topics: string[];
  recent_tasks: AgencyTask[];
  pending_proposals: AgencyTask[];
}

export async function getDeskmateAgencyStatus(): Promise<AgencyStatus | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/agency/status`);
    if (!res.ok) return null;
    return (await res.json()) as AgencyStatus;
  } catch {
    return null;
  }
}

// -- LLM 通道组（2026-10-06，设计 §二）----------------------------------
// 两个总开关：付费组（火山→小米→hy Token Plan）/ 免费组（opencode 免费模型）。
// 组内自动切+ 手动切组；这份状态跨端共享（熔断表进 SYNC_TABLES）。

export interface LlmChannelGroup {
  total: number;
  available: string[];
  open: string[];
}
export interface LlmChannelsStatus {
  paid: LlmChannelGroup;
  free: LlmChannelGroup;
  memory_group: "paid" | "free";
}
export interface LlmChannelAuditRow {
  ts: string;
  event: string;
  channel: string;
  kind: string;
  detail: string;
  until: string;
}

/** 两组状态：各有哪些候选、哪些在熔断、后台任务用哪组。 */
export async function getLlmChannels(): Promise<LlmChannelsStatus | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/llm-channels`);
    if (!res.ok) return null;
    return (await res.json()) as LlmChannelsStatus;
  } catch {
    return null;
  }
}

/** 手动切后台任务（记忆抽取/理解加工）走哪组：paid | free。 */
export async function setLlmMemoryGroup(
  group: "paid" | "free",
): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/llm-channels/memory-group`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 最近的通道审计（本地簿记、不同步）——「为什么换了通道」一眼可见。 */
export async function getLlmChannelAudit(
  limit = 20,
): Promise<LlmChannelAuditRow[] | null> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/llm-channels/audit?limit=${limit}`,
    );
    if (!res.ok) return null;
    const data = (await res.json()) as { rows?: LlmChannelAuditRow[] };
    return data.rows ?? [];
  } catch {
    return null;
  }
}

/** 开/ 关 L2 自主层总开关（写 l2_settings.json）。 */
export async function setDeskmateAgencyEnabled(enabled: boolean): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/agency/enabled`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 任务面板：最近任务 + 待裁决提议卡。 */
export async function getDeskmateAgencyTasks(limit = 20): Promise<{
  tasks: AgencyTask[];
  pending_proposals: AgencyTask[];
}> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/agency/tasks?limit=${limit}`);
    if (!res.ok) return { tasks: [], pending_proposals: [] };
    return (await res.json()) as { tasks: AgencyTask[]; pending_proposals: AgencyTask[] };
  } catch {
    return { tasks: [], pending_proposals: [] };
  }
}

export interface ScreenObservation {
  ts: string;
  scene: string;
  app: string;
  activity: string;
  attention: string;
  mood: string;
  confidence: number;
  persisted?: boolean;
}

export async function getScreenObservations(limit = 10): Promise<ScreenObservation[]> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/perception/screen?limit=${limit}`);
    if (!res.ok) return [];
    const data = (await res.json()) as { observations?: ScreenObservation[] };
    return Array.isArray(data.observations) ? data.observations : [];
  } catch {
    return [];
  }
}

export async function observeCurrentScreen(): Promise<
  { ok: true; observation: ScreenObservation } | { ok: false; error: string }
> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/perception/screen/observe`, {
      method: "POST",
    });
    const data = await res.json().catch(() => ({ error: "屏幕观察无响应" }));
    if (!res.ok) return { ok: false, error: data.error || `请求失败（${res.status}）` };
    return data as { ok: true; observation: ScreenObservation };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "屏幕观察失败" };
  }
}

/**
 * 看一眼屏幕（修复版）：优先走 Tauri 原生命令 `peek_screen`。该命令会先把小桔窗口隐藏、
 * 等焦点回到你正在用的软件（如 Word）再截图，彻底修复「点按钮时屏幕已切到小桔、截到的
 * 永远是小桔自己」的逻辑缺陷。无 Tauri 环境（浏览器 / 测试）回退到直连 HTTP 端点。
 */
export async function peekScreen(): Promise<
  { ok: true; observation: ScreenObservation } | { ok: false; error: string }
> {
  const tauri = (globalThis as any).__TAURI__;
  if (tauri?.core?.invoke) {
    try {
      const data = await tauri.core.invoke("peek_screen");
      if (data && data.ok) {
        return { ok: true, observation: data.observation as ScreenObservation };
      }
      return { ok: false, error: (data && data.error) || "屏幕观察无响应" };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : "屏幕观察失败" };
    }
  }
  return observeCurrentScreen();
}

/** 裁决高风险提议卡。decision: executed | rejected | never
 *
 * R13（2026-10-08）：返回 {ok, discuss_pending?, intent_id?}——意图提案被放行后，
 * 后端 _execute_intent_proposal 在 rec.extra.execution 写 discuss_pending=true + intent_id，
 * 前端拿这俩信号跳聊天调 /discuss/start。非意图类（write_file 等）只看 ok。 */
export async function resolveDeskmateAgencyProposal(
  id: string,
  decision: "executed" | "rejected" | "never",
): Promise<{
  ok: boolean;
  discuss_pending?: boolean;
  intent_id?: string;
}> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/agency/propose/resolve`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id, decision }),
    });
    if (!res.ok) return { ok: false };
    return (await res.json()) as { ok: boolean; discuss_pending?: boolean; intent_id?: string };
  } catch {
    return { ok: false };
  }
}

/** 立刻跑一次 L2 tick（不等后台周期）。 */
export async function runDeskmateAgencyTick(): Promise<Record<string, unknown> | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/agency/tick`, { method: "POST" });
    if (!res.ok) return null;
    return (await res.json()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

export interface TraceRecord {
  ts?: string;
  query?: string;
  n?: number;
  refs?: Array<{ fp: string; section?: string; date?: string; summary?: string }>;
  action?: "archived" | "deleted";
  section?: string;
  date?: string;
  summary?: string;
}

export async function proofreadDraft(opts: {
  text?: string;
  path?: string;
  content_base64?: string;
  filename?: string;
  chapter?: number | string;
  sessionId?: string;
  title?: string;
}): Promise<ProofreadResult> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/proofread`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as any));
    throw new Error(e.error || `批改失败 (${res.status})`);
  }
  return res.json();
}

export async function listManuscriptChapters(opts: {
  text?: string;
  path?: string;
  content_base64?: string;
  filename?: string;
}): Promise<ManuscriptChapter[]> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/chapters`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as any));
    throw new Error(e.error || `章节识别失败 (${res.status})`);
  }
  const d = await res.json();
  return d.chapters ?? [];
}

export interface ManuscriptInfo {
  name: string;
  chapter_count: number;
  saved_at: string;
}

export async function saveManuscript(name: string, chapters: ManuscriptChapter[]): Promise<void> {
  await authedFetch(`${httpBase()}/v1/deskmate/manuscript`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, chapters }),
  });
}

export async function listManuscripts(): Promise<ManuscriptInfo[]> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/manuscripts`, { method: "GET" });
  if (!res.ok) return [];
  const d = await res.json().catch(() => ({ manuscripts: [] }));
  return d.manuscripts ?? [];
}

export async function loadManuscript(name: string): Promise<ManuscriptChapter[]> {
  const res = await authedFetch(
    `${httpBase()}/v1/deskmate/manuscript/${encodeURIComponent(name)}`,
    { method: "GET" },
  );
  if (!res.ok) throw new Error(`读取书稿失败 (${res.status})`);
  const d = await res.json();
  return d.chapters ?? [];
}

export interface ReviewReportIssue {
  type: string;
  excerpt: string;
  comment: string;
}

export interface ReviewReportChapter {
  idx: number;
  title: string;
  summary: string;
  analysis: string;
  issues: ReviewReportIssue[];
}

export interface ReviewReportOverall {
  summary: string;
  scores: Record<string, number>;
  strengths: string[];
  weaknesses: string[];
  character_consistency: string[];
  recommendations: string[];
}

export interface ReviewReport {
  title: string;
  generated_at: string;
  overall: ReviewReportOverall;
  per_chapter: ReviewReportChapter[];
  meta: { chapter_count: number; detailed_count: number; truncated: boolean; bible?: string[] };
}

export interface ReviewReportSummary {
  name: string;
  title: string;
  generated_at: string;
  chapter_count: number;
}

export async function generateReport(opts: {
  name?: string;
  text?: string;
  path?: string;
  folder?: string;
  content_base64?: string;
  filename?: string;
  chapter_indices?: number[];
  sessionId?: string;
}): Promise<{ ok: boolean; task_id: string }> {
  const { sessionId, ...rest } = opts;
  const body: Record<string, unknown> = { ...rest };
  if (sessionId) body.session_id = sessionId;
  const res = await authedFetch(`${httpBase()}/v1/deskmate/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as any));
    throw new Error(e.error || `生成报告失败 (${res.status})`);
  }
  return res.json();
}

export async function getReportProgress(
  taskId: string,
): Promise<{ status: string; progress: { completed: number; total: number; current_title: string } }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/report/progress/${encodeURIComponent(taskId)}`);
  if (!res.ok) {
    const e = await res.json().catch(() => ({} as any));
    throw new Error(e.error || `查询进度失败 (${res.status})`);
  }
  return res.json();
}

export function getReportDocxUrl(name: string): string {
  return `${httpBase()}/v1/deskmate/report/${encodeURIComponent(name)}/docx`;
}

export async function downloadReportDocx(name: string): Promise<void> {
  const res = await authedFetch(
    `${httpBase()}/v1/deskmate/report/${encodeURIComponent(name)}/docx`,
  );
  if (!res.ok) {
    throw new Error(`下载 Word 失败（${res.status}）`);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}.docx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export async function listReports(): Promise<ReviewReportSummary[]> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/reports`, { method: "GET" });
  if (!res.ok) return [];
  const d = await res.json().catch(() => ({ reports: [] }));
  return d.reports ?? [];
}

export interface SkillEntry {
  id: string;
  name: string;
  description: string;
  category: string;
  icon: string;
  version: string;
  tags: string[];
  author: string;
  homepage: string;
  config_type: string;
  config_key_name: string;
  config_key_hint: string;
  config_env_key: string;
  status: "not_installed" | "installed" | "configured";
}

export async function getSkills(): Promise<{ skills: SkillEntry[] }> {
  const res = await authedFetch(`${httpBase()}/v1/skill-store`);
  return res.json();
}

export async function installSkill(skillId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await authedFetch(`${httpBase()}/v1/skill-store/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_id: skillId }),
  });
  return res.json();
}

export async function configureSkill(
  skillId: string,
  config: Record<string, string>,
): Promise<{ ok: boolean; error?: string }> {
  const res = await authedFetch(`${httpBase()}/v1/skill-store/configure`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_id: skillId, config }),
  });
  return res.json();
}

export async function uninstallSkill(skillId: string): Promise<{ ok: boolean; error?: string }> {
  const res = await authedFetch(`${httpBase()}/v1/skill-store/${encodeURIComponent(skillId)}`, {
    method: "DELETE",
  });
  return res.json();
}

export interface PluginValidateResult {
  ok: boolean;
  name?: string;
  servers?: string[];
  personas?: string[];
  errors?: string[];
  error?: string;
}

export interface PluginInstallResult {
  ok: boolean;
  name?: string;
  servers?: string[];
  personas?: string[];
  errors?: string[];
  error?: string;
}

export async function validatePlugin(body: { path?: string; payload?: string }): Promise<PluginValidateResult> {
  const res = await authedFetch(`${httpBase()}/v1/plugins/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export async function installPlugin(body: { path?: string; payload?: string }): Promise<PluginInstallResult> {
  const res = await authedFetch(`${httpBase()}/v1/plugins/install`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export interface GovernanceSettings {
  execution_level: "off" | "auto" | "strict";
  sandbox_enabled: boolean;
  sandbox_network: boolean;
  sandbox_deny_paths: string[];
}

export async function setGovernance(patch: Partial<GovernanceSettings>): Promise<
  { ok: boolean; error?: string } & Partial<GovernanceSettings>
> {
  const res = await authedFetch(`${httpBase()}/v1/settings/governance`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return res.json();
}

// ---- 创作流水线（线路 B 原生 teams 执行 + 线路 A 圆桌终审）----
export interface CreationItem {
  id: number;
  title: string;
  state: string;
  criteria: string;
  comments: number;
  /** 分集模式下该 item 所属集号（1..n）；单集/大纲为 null。 */
  episode?: number | null;
}
export interface NovelIndexStatus {
  status: "none" | "pending" | "ready" | "failed";
  novel_ref?: string;
  total_chars?: number;
  n?: number;
  degraded?: boolean;
  bible_chars?: number;
  chapters?: { idx: number; title: string; chars: number }[];
  error?: string;
}
export interface CreationSnapshot {
  pid: string;
  kind: string;
  concept: string;
  team_id: string;
  novel_ref: string;
  items: CreationItem[];
  phase: string;
  docx: Record<string, string>;
  novel_index?: NovelIndexStatus;
  related_chapters?: number[];
  /** 透明化 · 结构化制作要求与分集信息 */
  style?: string[];
  pacing?: string;
  episodes?: number;
  episode_range?: number[];
  /** 前情上下文档位（加集续写衔接）："summary" | "summary_recent" */
  prior_context?: string;
  include_review_notes?: boolean;
  /** 用户打回时填写的修改意见（按 stage 存：outline / screen / screen:N），重写时注入 prompt */
  user_notes?: Record<string, string>;
}
export interface FinalProposal {
  item_kind: string;
  topic: string;
  manuscript_text: string;
  criteria: string;
  prior_review: string;
  all_experts: { id: string; name: string; role?: string }[];
  recommended: string[];
  /** 分集模式下被审集号。 */
  episode?: number | null;
}
export interface FinalReviewEvent {
  type: string;
  [k: string]: any;
}

export async function createPipeline(
  kind: "outline" | "screenplay",
  concept: string,
  executor: "stub" | "llm" = "stub",
  novelRef = "",
  opts: {
    style?: string[];
    pacing?: string;
    episodes?: number;
    episodeRange?: number[];
  } = {},
): Promise<{
  pid: string;
  kind: string;
  team_id: string;
  ids: Record<string, number>;
  concept: string;
  novel_index: NovelIndexStatus;
  style?: string[];
  pacing?: string;
  episodes?: number;
  episode_range?: number[];
}> {
  const res = await authedFetch(`${creationBase()}/api/creation/pipeline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind,
      concept,
      executor,
      novel_ref: novelRef,
      style: opts.style ?? [],
      pacing: opts.pacing ?? "",
      episodes: opts.episodes ?? 1,
      episode_range: opts.episodeRange ?? [],
    }),
  });
  await _rejectIfNotOk(res);
  return res.json();
}

export async function getPipeline(pid: string): Promise<CreationSnapshot> {
  const res = await authedFetch(`${creationBase()}/api/creation/pipeline/${encodeURIComponent(pid)}`);
  return res.json();
}

/** 加集续写：做完前 N 集后接着做后续集（风格/节奏/前情自动继承，大纲须已定稿）。 */
export async function extendEpisodes(
  pid: string,
  episodeRange: number[],
  opts: { priorContext?: string; includeReviewNotes?: boolean } = {},
): Promise<{
  pid: string;
  added: number[];
  episodes: number;
  episode_range: number[];
  prior_context: string;
  include_review_notes: boolean;
}> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/extend-episodes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      episode_range: episodeRange,
      prior_context: opts.priorContext ?? null,
      include_review_notes: opts.includeReviewNotes ?? null,
    }),
  });
  if (!res.ok) {
    const j = await res.json().catch(() => ({}));
    throw new Error(j.error || `extend-episodes failed: ${res.status}`);
  }
  return res.json();
}

/** 产出预览：取指定 item 的草稿文本（透明化 · 看得见产出长什么样）。 */
export async function getItemContent(
  pid: string,
  itemId: number,
): Promise<{ pid: string; item_id: number; content: string; episode?: number | null }> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/item/${itemId}`);
  return res.json();
}

/** 原著索引状态（章节目录完整版，独立接口避免每次快照携带大目录）。 */
export async function getNovelIndex(pid: string): Promise<NovelIndexStatus> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/novel-index`);
  return res.json();
}

/** 按主题挑候选相关章节（bigram 匹配，返回标题+开头预览供勾选）。 */
export async function pickNovelChapters(
  pid: string,
  topic = "",
  k = 3,
): Promise<{ chapters: { idx: number; title: string; chars: number; head: string }[]; topic: string }> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/novel-pick`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ topic, k }),
  });
  return res.json();
}

/** 指定创作参考章节（1..n）；空数组恢复自动匹配。 */
export async function setRelatedChapters(pid: string, indices: number[]): Promise<{ related: number[] }> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/related-chapters`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ indices }),
  });
  return res.json();
}

/** 非 2xx 时从响应体提取后端可读错误（error/detail/message），抛给上层友好展示。 */
async function _rejectIfNotOk(res: Response): Promise<void> {
  if (res.ok) return;
  let msg = `请求失败 (HTTP ${res.status})`;
  try {
    const text = await res.text();
    try {
      const j = JSON.parse(text);
      msg = j.error || j.detail || j.message || text;
    } catch {
      msg = text || msg;
    }
  } catch {
    /* 忽略读取失败，保留默认文案 */
  }
  throw new Error(msg);
}

/** 生成型 step 的聚合版本：消费完整 SSE 流，返回最终快照（供非流式调用方复用）。
 * 服务端这些 step 已是 text/event-stream，不再返回 JSON 快照。 */
async function _runStepToSnapshot(
  pid: string,
  step: "draft-outline" | "draft-screen" | "review-outline" | "review-screen",
  opts: { episode?: number | null } = {},
): Promise<CreationSnapshot> {
  let snap: CreationSnapshot | undefined;
  for await (const ev of streamStep(pid, step, opts)) {
    if (ev.type === "done" && ev.snapshot) snap = ev.snapshot;
    else if (ev.type === "error") throw new Error(ev.error ?? "操作失败");
  }
  if (!snap) throw new Error("连接中断，未能收到结果");
  return snap;
}

export const draftOutline = (pid: string) => _runStepToSnapshot(pid, "draft-outline");
export const reviewOutline = (pid: string) => _runStepToSnapshot(pid, "review-outline");
export const draftScreen = (pid: string, episode?: number) =>
  _runStepToSnapshot(pid, "draft-screen", { episode: episode ?? null });
export const reviewScreen = (pid: string, episode?: number) =>
  _runStepToSnapshot(pid, "review-screen", { episode: episode ?? null });

async function _postLead(pid: string, step: string, approved: boolean, episode?: number, userNote?: string | null): Promise<CreationSnapshot> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/${step}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, episode: episode ?? null, user_note: userNote ?? null }),
  });
  await _rejectIfNotOk(res);
  return res.json();
}

export const leadOutline = (pid: string, approved: boolean, userNote?: string | null) =>
  _postLead(pid, "lead-outline", approved, undefined, userNote);
export const leadScreen = (pid: string, approved: boolean, episode?: number, userNote?: string | null) =>
  _postLead(pid, "lead-screen", approved, episode, userNote);

export async function finalReviewProposal(
  pid: string,
  itemKind: "outline" | "screenplay",
  genreHint?: string,
  episode?: number,
): Promise<FinalProposal> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/final-review-proposal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_kind: itemKind, genre_hint: genreHint ?? null, episode: episode ?? null }),
  });
  return res.json();
}

export async function finalReviewApply(
  pid: string,
  itemKind: "outline" | "screenplay",
  verdict: boolean,
  episode?: number,
): Promise<{ status: string; docx: Record<string, string>; pid: string; item_kind: string }> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/final-review-apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ item_kind: itemKind, verdict, episode: episode ?? null }),
  });
  return res.json();
}

export async function exportCreationDocx(
  kind: "outline" | "screenplay",
  title: string,
  markdown: string,
): Promise<{ ok: boolean; path?: string; degraded?: boolean; error?: string }> {
  const res = await authedFetch(`${creationBase()}/api/creation/export-docx`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, title, markdown }),
  });
  return res.json();
}

/** SSE 流式终审：POST /api/creation/{pid}/final-review，逐事件吐出圆桌研讨流。 */
export async function* streamFinalReview(
  pid: string,
  itemKind: "outline" | "screenplay",
  selectedExperts: string[],
  genreHint?: string,
  episode?: number,
): AsyncGenerator<FinalReviewEvent> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/final-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      item_kind: itemKind,
      selected_experts: selectedExperts,
      genre_hint: genreHint ?? null,
      episode: episode ?? null,
    }),
  });
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (line) {
        try {
          yield JSON.parse(line.slice(5).trim());
        } catch {
          /* ignore malformed */
        }
      }
    }
  }
}

/** 生成型 step 的 SSE 进度事件（写大纲/写剧本/审核）。 */
export interface CreationStepEvent {
  type: "stage" | "note" | "done" | "error";
  stage?: string;
  label?: string;
  status?: string;
  message?: string;
  progress?: number;
  snapshot?: CreationSnapshot;
  error?: string;
}

/** SSE 流式推进生成型 step：POST /api/creation/{pid}/{step}，逐事件吐出进度。
 * 事件：stage（阶段开始）→ note*（中间进度）→ done（带快照）| error。 */
export async function* streamStep(
  pid: string,
  step: "draft-outline" | "draft-screen" | "review-outline" | "review-screen",
  opts: { episode?: number | null } = {},
): AsyncGenerator<CreationStepEvent> {
  const isScreen = step === "draft-screen" || step === "review-screen";
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/${step}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: isScreen ? JSON.stringify({ episode: opts.episode ?? null }) : undefined,
  });
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (line) {
        try {
          yield JSON.parse(line.slice(5).trim());
        } catch {
          /* ignore malformed */
        }
      }
    }
  }
}

// ---- 创作对话流（chat_flow：把流水线放入原生对话流） ----

/** 创作对话消息（与后端 chat_flow.py 的 _msg 对应）。 */
export interface CreationChatMessage {
  id: string;
  type: "user" | "system" | "progress" | "artifact" | "review" | "milestone" | "expert" | "expert_card" | "roundtable" | "help" | "error" | "inspire";
  ts: string;
  text?: string;
  title?: string;
  message?: string;
  label?: string;
  progress?: number;
  kind?: "outline" | "screenplay" | "complete";
  episode?: number;
  approved?: boolean;
  criteria?: string;
  expert?: string;
  tagline?: string;
  events?: Array<{ message: string; progress?: number }>;
}

export interface InspireResult {
  triggered: boolean;
  suggestion?: string;
  refs?: string[];
  reason?: string;
}

// -- 主动产出台账：对话式对接开场白（P0 本地开聊挂载点） ----------------------
export interface HandoffItem {
  id: string;
  source: string;
  kind: string;
  content: string;
  evidence: string;
  understanding: string;
  status: string;
  confidence: number;
  created_at: string;
  source_node: string;
}
export interface HandoffOpening {
  text: string;
  items: HandoffItem[];
}
/** 拉取小桔主动产出台账的「对话式开场白」。无 pending 返回 null（静默降级，不阻塞首页）。 */
export async function getDeskmateHandoff(): Promise<HandoffOpening | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/proactive/handoff`);
    if (!res.ok) return null;
    const data = (await res.json()) as HandoffOpening;
    if (!data || !data.items || data.items.length === 0) return null;
    return data;
  } catch {
    return null;
  }
}
/** 标记某条主动产出的处理状态（只前进不后退）。
 * snoozed＝「稍后」：3 天退避后自动复活轻递（修⑥），不再用 acknowledged 承接
 *（那等于永久拉黑，与按钮字面义相悖）。 */
export async function ackDeskmateHandoff(
  id: string,
  status: "acknowledged" | "dismissed" | "snoozed" = "acknowledged",
): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/proactive/handoff/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, status }),
    });
    return res.ok;
  } catch {
    return false;
  }
}
// -- 信箱 · 送达层（2026-10-09）：proactive_ledger 的门面（presentation layer） --
// 信箱只持信件元数据；轻回信走「隐藏 mailbox 会话」复用同一引擎（前端用 Session 类连 WS）。
// 与江河设计 §九 正交，独立于 P3 自主闭环；Mac-only 不同步（决策5）。
// 准入（哪些台账行算「一封信」）在服务端 ledger.is_mailbox_letter：系统遥测不入。
export interface MailboxLetter {
  id: string;
  source: string;
  kind: string;
  content: string;
  evidence: string;
  understanding: string;
  status: string;
  read_at: string | null;
  read: boolean;
  created_at: string;
  /** 该信的轻回信会话 id（隐藏中或已提升都返）。 */
  mailbox_session_id: string | null;
  /** 往来已被「进聊天」提升成普通会话（侧栏可见，信箱留引用）。 */
  thread_promoted?: boolean;
  /** 已读超 SHELF_DAYS 天仍未处理 → 「已搁置」归档态（设计单 §7 TTL 衰减）。 */
  stale?: boolean;
  /** 归档原因（只在 ?status=archived 时返）：稍后退避 / 不用了 / 已办。 */
  shelf_reason?: string;
  acknowledged_at?: string | null;
  origin?: string;
  intent_id?: string | null;
}

export interface MailboxView {
  ok: boolean;
  unread_count: number;
  /** 左栏三档计数（未读/已读/归档），与 items 同一准入口径。 */
  counts?: { unread: number; read: number; archived: number };
  items: MailboxLetter[];
}

/** 拉取信箱信件列表 + 未读数（proactive_ledger 的 pending/snoozed 视图态项）。
 *  status="unread" 只要未读，"archived" 要归档（处置过的）；
 *  默认 all = 收件箱（未读 + 已读，前端按 read 标记分栏，不再多要一趟接口）。 */
export async function getMailbox(
  status: "all" | "unread" | "archived" = "all"
): Promise<MailboxView> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/mailbox?status=${status}`);
    if (!res.ok) return { ok: false, unread_count: 0, items: [] };
    return (await res.json()) as MailboxView;
  } catch {
    return { ok: false, unread_count: 0, items: [] };
  }
}

/** 标已读（read_at + 回流情感层「用户看见了」，设计单 §7 要害）。 */
export async function readMailboxLetter(id: string): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/mailbox/${encodeURIComponent(id)}/read`, {
      method: "POST",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 稍后（3 天退避后自动复活轻递，复用 ledger 状态机 snoozed）。 */
export async function snoozeMailboxLetter(id: string): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/mailbox/${encodeURIComponent(id)}/snooze`, {
      method: "POST",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 不用了（终态，复用 ledger 状态机 dismissed）。 */
export async function dismissMailboxLetter(id: string): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/mailbox/${encodeURIComponent(id)}/dismiss`, {
      method: "POST",
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 轻回信：确保一封「隐藏 mailbox 会话」存在，返回其 session_id。
 *  前端再用 Session 类连 /ws/session/{id} 复用同一引擎；信件样式渲染往来。 */
export async function replyMailboxLetter(id: string): Promise<{ ok: boolean; session_id: string | null }> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/mailbox/${encodeURIComponent(id)}/reply`, {
      method: "POST",
    });
    const d = (await res.json()) as { ok?: boolean; session_id?: string | null };
    return { ok: !!d.ok, session_id: d.session_id ?? null };
  } catch {
    return { ok: false, session_id: null };
  }
}

/** 「进聊天」：把这封信的隐藏往来提升成普通会话（侧栏可见、气泡样式续聊）。
 *  返 workspace 供 App 直接 selectSession。 */
export async function promoteMailboxLetter(
  id: string
): Promise<{ ok: boolean; session_id: string | null; workspace: string }> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/mailbox/${encodeURIComponent(id)}/promote`,
      { method: "POST" }
    );
    const d = (await res.json()) as {
      ok?: boolean;
      session_id?: string | null;
      workspace?: string;
    };
    return { ok: !!d.ok, session_id: d.session_id ?? null, workspace: d.workspace ?? "" };
  } catch {
    return { ok: false, session_id: null, workspace: "" };
  }
}

// Fired when the mailbox changes (read/snooze/dismiss/reply) so the sidebar bell and
// DeskHome pointer refresh their unread counts without polling.
export const MAILBOX_CHANGED = "coworker:mailbox-changed";
export function announceMailboxChanged() {
  window.dispatchEvent(new CustomEvent(MAILBOX_CHANGED));
}

/** 「聊这个」让她先说（§11.16 层三）：把条目作为她的助手消息落入本会话。
 * true=已落会话（GUI 经 WS assistant_message 即时渲染，reload 后也在）；
 * false=端点失败（会话不存在/台账条目缺失），调用方可退回短句柄 prefill。 */
export async function speakDeskmateHandoff(
  sessionId: string,
  id: string,
): Promise<boolean> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/proactive/handoff/speak`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, session_id: sessionId }),
      },
    );
    return res.ok;
  } catch {
    return false;
  }
}

// --------------------------------------------------------------------------
// R13 意图讨论流（4 个端点封装）——把意图执行从「一次性静默」改成「跨会话回合讨论」。
// 状态机：点执行 → start（小桔开口）→ run（出 draft 递聊天）→ 用户点评 →
//         再改（run round+1）/ 定稿（finalize）/ 不要（cancel）
// --------------------------------------------------------------------------

/** R13 启动意图讨论：设 discuss_state、写开场白 ledger，**端点自送开场白**（append +
 * broadcast 都带 meta{origin=intent_handoff, phase=opening}，渲染成讨论卡；空会话先
 * 兜底建 deskmate 会话记录）。返回 {ok, ledger_id, text}——前端成功后紧接着调
 * runIntentDiscussRound(sessionId, intentId, 1) 触发第一轮 draft。
 * blocking_intent_id 非空 = 同 session 已有讨论中的意图（多并发护栏）。
 * null = 端点失败。 */
export async function startIntentDiscuss(
  sessionId: string,
  intentId: string,
): Promise<{
  ok: boolean;
  ledger_id: string;
  text: string;
  blocking_intent_id?: string;
} | null> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/agency/intent/discuss/start`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent_id: intentId, session_id: sessionId }),
      },
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** R13 跑一轮讨论：调 executor 出 draft、递到聊天（带 meta 标记意图讨论态）。
 * 前端在「再改」按钮时调。null = 端点失败。 */
export async function runIntentDiscussRound(
  sessionId: string,
  intentId: string,
  round: number,
): Promise<{ ok: boolean; ledger_id: string; round: number } | null> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/agency/intent/discuss/run`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent_id: intentId, session_id: sessionId, round }),
      },
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** R13 定稿：落最终版 intent_outputs、写总结 lesson、speak 定稿确认。
 * 前端在「定稿」按钮时调。null = 端点失败。 */
export async function finalizeIntentDiscuss(
  sessionId: string,
  intentId: string,
): Promise<{ ok: boolean; output_id: string; lesson_written: boolean } | null> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/agency/intent/discuss/finalize`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent_id: intentId, session_id: sessionId }),
      },
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

/** R13 取消讨论：intent.status=dropped、清 discuss_state。
 * 前端在「不要」按钮时调。 */
export async function cancelIntentDiscuss(
  intentId: string,
  sessionId?: string,
): Promise<boolean> {
  try {
    const res = await authedFetch(
      `${httpBase()}/v1/deskmate/agency/intent/discuss/cancel`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ intent_id: intentId, session_id: sessionId || "" }),
      },
    );
    return res.ok;
  } catch {
    return false;
  }
}
/** 活跃会话相关性注入（§11.4② 触发入口）：把最近对话作为 session_context 传入，
 * 后端按 bigram 重叠取最相关的主动产出 1–2 条；零关联返回 null（静默，不阻塞聊天）。
 * 只递今日尚未递过的新条目，不追重首页/之前会话。 */
export async function getDeskmateHandoffContext(
  sessionContext: string,
): Promise<HandoffOpening | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/proactive/handoff`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_context: sessionContext }),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as HandoffOpening;
    if (!data || !data.items || data.items.length === 0) return null;
    return data;
  } catch {
    return null;
  }
}

/** 「记住这个」→ 写主动产出台账（source=mobile_voice，修⑧/P1）。
 * 手机 PWA 与桌面 GUI 同走 app.py 的 /v1/deskmate/proactive/capture；true=已落台账。 */
export async function captureProactive(
  kind: string,
  content: string,
  evidence?: string,
): Promise<boolean> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/proactive/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, content, evidence: evidence ?? "" }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** 普通聊天里聊写作时，小桔基于认知库主动递一条创作灵感（POST /v1/deskmate/inspire）。 */
export async function postInspire(text: string, concept?: string): Promise<InspireResult | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/inspire`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, concept: concept ?? "" }),
    });
    if (!res.ok) return null;
    return (await res.json()) as InspireResult;
  } catch {
    return null;
  }
}

/** 写作顾问（P1-5）：用她 worldview 层的看法，评用户的情节/框架/书评。 */
export interface WritingAdvisorResult {
  ok: boolean;
  kind: string;
  advice: string;
  worldview_used: boolean;
  reason?: string;
}

/** 把草稿交给小桔，让她用「自己的世界观镜头」读后给看法（POST /v1/deskmate/cognitive/writing-advisor）。 */
export async function writingAdvisor(
  draft: string,
  kind: "plot" | "framework" | "review" | "general" = "general",
): Promise<WritingAdvisorResult | null> {
  try {
    const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/writing-advisor`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ draft, kind }),
    });
    if (!res.ok) return null;
    return (await res.json()) as WritingAdvisorResult;
  } catch {
    return null;
  }
}

/** 获取创作对话历史。 */
export async function getChatHistory(pid: string): Promise<CreationChatMessage[]> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/chat`);
  const data = await res.json();
  return data.messages ?? [];
}

/** SSE 流式发消息：POST /api/creation/{pid}/chat，逐事件返回 chat_message。 */
export async function* streamChat(pid: string, text: string): AsyncGenerator<CreationChatMessage> {
  const res = await authedFetch(`${creationBase()}/api/creation/${encodeURIComponent(pid)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const line = chunk.split("\n").find((l) => l.startsWith("data:"));
      if (line) {
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "chat_message" && ev.message) {
            yield ev.message as CreationChatMessage;
          } else if (ev.type === "error") {
            yield { id: "err", type: "error", ts: new Date().toISOString(), text: ev.message } as CreationChatMessage;
          }
        } catch {
          /* ignore malformed */
        }
      }
    }
  }
}

/** 圆桌专家列表（GET /roundtable/api/personas），返回 roundtable: true 的 7 专家。 */
export async function getRoundtablePersonas(): Promise<Persona[]> {
  const res = await authedFetch(`${creationBase()}/api/personas`);
  return res.json();
}

/* ------------------------------------------------------------------ */
/*  深度访谈（cognitive interview）—— ③b 闭环前端接入                    */
/* ------------------------------------------------------------------ */

export interface InterviewQuestion {
  id: string;
  topic: string;
  question: string;
  context: string;
  related_item_id: string | null;
  related_gap_id: string | null;
  priority: number;
  mode: "clarify" | "explore" | "deepen";
}

export interface InterviewStartResult {
  session_id: string | null;
  questions: InterviewQuestion[];
  total: number;
  note?: string;
}

export interface InterviewAnswerResult {
  ok: boolean;
  session_id: string;
  answered: number;
  total: number;
  session_done: boolean;
  open_gaps: number;
}

/** 发起一轮深度访谈（POST /v1/deskmate/cognitive/interview/start）。 */
export async function startCognitiveInterview(limit = 3): Promise<InterviewStartResult> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }),
  });
  return res.json();
}

/** 提交一个访谈回答（POST /v1/deskmate/cognitive/interview/answer）。 */
export async function answerCognitiveInterview(
  sessionId: string,
  questionId: string,
  response: string,
): Promise<InterviewAnswerResult> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, question_id: questionId, response }),
  });
  return res.json();
}

/** 用户点「稍后再说」→ 显式忽略当前挂起访谈，后端暂停该层补送一段时间。 */
export async function dismissCognitiveInterview(
  layer = "cognitive",
): Promise<{ ok: boolean; dismissed: boolean; layer: string }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/dismiss`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layer }),
  });
    return res.json();
}

/** 跳过单题（POST /v1/deskmate/cognitive/interview/skip）。 */
export async function skipCognitiveInterviewQuestion(
  sessionId: string,
  questionId: string,
  layer = "cognitive",
): Promise<{ ok: boolean; session_done: boolean; handled: number; total: number }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/skip`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, question_id: questionId, layer }),
  });
  return res.json();
}

/** 拒答整轮（POST /v1/deskmate/cognitive/interview/decline）。 */
export async function declineCognitiveInterview(
  sessionId: string,
  layer = "cognitive",
): Promise<{ ok: boolean; session_id: string; layer: string }> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/decline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, layer }),
  });
  return res.json();
}

/** 查询是否应主动发起访谈（GET /v1/deskmate/cognitive/interview/should）。 */
export async function cognitiveInterviewShould(): Promise<{
  should_interview: boolean;
  open_gaps: number;
}> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/should`);
  return res.json();
}

/* ------------------------------------------------------------------ */
/*  认知验证（Turing test）—— ValidationEngine 前端接入                 */
/* ------------------------------------------------------------------ */

export interface ValidationScenario {
  id: string;
  domain: string;
  scenario: string;
  question: string;
  related_items: string[];
}

export interface ValidationResult {
  scenario: ValidationScenario;
  evaluation?: {
    overall_score: number;
    dimension_scores: Record<string, number>;
    divergence_points: string[];
    feedback: string;
    _engine?: string;
  };
  applied?: boolean;
  note?: string;
}

/** 生成一个验证情境（POST /v1/deskmate/cognitive/validation/scenario）。 */
export async function createValidationScenario(): Promise<{
  scenario: ValidationScenario;
  note?: string;
}> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/validation/scenario`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return res.json();
}

/** 提交 AI 回答 + 用户回答，评估相似度并反哺画像（POST /v1/deskmate/cognitive/validation/evaluate）。
 *  必须回传作答时拿到的那个情境：后端靠 scenario_id / related_items 定位该改哪条画像，
 *  否则会重新随机生成情境，把反馈写到另一条目上（反哺张冠李戴）。 */
export async function submitValidationEvaluation(
  aiResponse: string,
  userResponse: string,
  scenario?: ValidationScenario | null,
): Promise<ValidationResult> {
  const res = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/validation/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ai_response: aiResponse,
      user_response: userResponse,
      scenario_id: scenario?.id ?? "",
      related_items: scenario?.related_items ?? [],
      scenario: scenario?.scenario ?? "",
      domain: scenario?.domain ?? "",
    }),
  });
  return res.json();
}
