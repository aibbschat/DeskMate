import { useCallback, useEffect, useRef, useState, type PointerEvent, Component, type ReactNode } from "react";
import {
  announceInboxUnlock,
  finalizeAutomationRun,
  getArtifacts,
  getHealth,
  httpBase,
  getRecentWorkspaces,
  getSessionMessages,
  getSessions,
  announceAutomationsChanged,
  connectEvents,
  getSettings,
  getPersonas,
  getInbox,
  getUnattended,
  PERSONAS_CHANGED,
  resolveInboxItem,
  deleteSession,
  renameSession,
  runAutomation,
  setSessionFlags,
  setUnattended,
  Session,
  type InboxItem,
  type MessageSource,
  type Persona,
  type RecentWorkspace,
  type SurfaceVisibility,
  type WorkspaceCommandTrust,
  postInspire,
  speakDeskmateHandoff,
  startIntentDiscuss,
  finalizeIntentDiscuss,
  runIntentDiscussRound,
  cancelIntentDiscuss,
  announceMailboxChanged,
  promoteMailboxLetter,
  type HandoffItem,
  type MailboxLetter,
} from "./api";
import type {
  ApprovalDecision,
  Attachment,
  Item,
  SessionInfo,
  SessionUsage,
  TodoItem,
  WsEvent,
} from "./types";
import { isProjectScoped, shortPersonaName } from "./personaScope";
import { baseName } from "./paths";
import { itemsFromMessages } from "./itemsFromMessages";
import { addTurnUsage, emptyUsage, usageFromMessages } from "./usage";
import { streamMode } from "./streamGate";
import { parseTs } from "./time";
import { InboxItemCard } from "./components/InboxItemCard";
import { isTauri, platformOS, startWindowDrag } from "./tauri";
import { Icon } from "./components/Icon";
import historianImg from "./assets/avatars/historian.png";
import litEditorImg from "./assets/avatars/lit-editor.png";
import proofreaderImg from "./assets/avatars/proofreader.png";
import readerImg from "./assets/avatars/reader.png";
import publisherImg from "./assets/avatars/publisher.png";
import adaptationImg from "./assets/avatars/adaptation.png";
import genreImg from "./assets/avatars/genre.png";

const PERSONA_AVATAR: Record<string, string> = {
  historian: historianImg,
  "lit-editor": litEditorImg,
  proofreader: proofreaderImg,
  reader: readerImg,
  publisher: publisherImg,
  adaptation: adaptationImg,
  genre: genreImg,
};
import { Sidebar } from "./components/Sidebar";
import { ThinkingBlock, Transcript } from "./components/Transcript";
import { Composer } from "./components/Composer";
import { Markdown } from "./components/Markdown";
import { SearchModal } from "./components/SearchModal";
import { SessionIntro } from "./components/SessionIntro";
import { FolderGate } from "./components/FolderGate";
import { Onboarding } from "./components/Onboarding";
import { UpdateBanner } from "./components/UpdateBanner";
import { ScheduledView } from "./components/ScheduledView";
import { RightRail } from "./components/RightRail";
import { IntegrationsView } from "./components/IntegrationsView";
import { SkillStore } from "./components/SkillStore";
import { SettingsView } from "./components/SettingsView";
import { PersonaView } from "./components/PersonaView";
import { AuditView } from "./components/AuditView";
import { InboxView } from "./components/InboxView";
import { ApprovalCard } from "./components/ApprovalCard";
import { DirectoryRequestCard } from "./components/DirectoryRequestCard";
import { PlanCard } from "./components/PlanCard";
import { WorkspaceTrustPrompt } from "./components/WorkspaceTrustPrompt";
import { DeskMateOverlay } from "./components/deskmate/DeskMateOverlay";
import { DeskHome } from "./components/deskmate/DeskHome";
import { MailboxPage } from "./components/deskmate/MailboxPage";
import { DeskMateProactiveInline } from "./components/deskmate/DeskMateProactiveInline";
import { GardenProfileCenter } from "./components/garden/GardenProfileCenter";
import { EXPERTS, getExpert, type Expert } from "./components/deskmate/experts";
import { ExpertAvatar } from "./components/deskmate/ExpertAvatar";
import { ReportDialog } from "./components/deskmate/ReportDialog";

import { ExpertsCenter } from "./components/deskmate/ExpertsCenter";
import { CreationChatView } from "./components/CreationChatView";
import { TeamChatView } from "./components/TeamChatView";
import { BadgeWall } from "./components/deskmate/BadgeWall";
import { useTranslation } from "react-i18next";
import XiaojuAvatar from "./components/XiaojuAvatar";

class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: "monospace", color: "#f44", background: "#1a1a2e", minHeight: "100vh" }}>
          <h2>React Error</h2>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>{this.state.error.message}</pre>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: 11, color: "#888" }}>{this.state.error.stack}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}

const newId = () =>
  (crypto as any).randomUUID ? crypto.randomUUID().slice(0, 12) : Math.random().toString(36).slice(2, 14);

const SUGGESTIONS = [
  { ico: "⚙", text: "Run the test suite and summarize any failures." },
  { ico: "✦", text: "Read the project and give me a 5-bullet overview." },
  { ico: "↻", text: "Find and fix the failing build." },
];

// Tools whose success means a new/changed file should show up under Artifacts right away.
const FILE_WRITE_TOOLS = new Set(["write_file", "apply_patch", "apply_unified_diff", "replace_in_file"]);

// Models sometimes pass todo items as bare strings instead of {content, status} objects (the
// backend tool normalizes them the same way; the GUI reads the raw proposal args, so mirror it).
function normalizeTodos(raw: unknown): TodoItem[] {
  if (!Array.isArray(raw)) return [];
  const statuses = new Set(["pending", "in_progress", "done"]);
  return raw.map((entry: any) => {
    if (entry && typeof entry === "object") {
      const status = entry.status === "completed" ? "done" : entry.status; // common model alias
      return {
        content: String(entry.content ?? ""),
        status: statuses.has(status) ? status : "pending",
      };
    }
    return { content: String(entry ?? ""), status: "pending" as const };
  });
}

// Fallbacks used only before the persona list loads (the in-component, family-aware
// needsWorkspace/gatesWorkspace consult the real persona once available).
const needsWorkspaceFallback = (a: string) => a === "code" || a === "cowork";
const gatesWorkspaceFallback = (a: string) => a === "code";
const LAST_SESSION_KEY = "coworker:last-session-by-agent:v1";
const NAV_COLLAPSED_KEY = "coworker:nav-collapsed:v1";

type LastSession = { sessionId: string; workspace: string; updatedAt: number };

function readLastSessions(): Record<string, LastSession> {
  try {
    const raw = localStorage.getItem(LAST_SESSION_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function rememberLastSession(agent: string, sessionId: string, workspace: string | null) {
  if (!agent || !sessionId) return;
  try {
    const all = readLastSessions();
    all[agent] = { sessionId, workspace: workspace || "", updatedAt: Date.now() };
    localStorage.setItem(LAST_SESSION_KEY, JSON.stringify(all));
  } catch {
    /* localStorage may be unavailable; session restore is best effort. */
  }
}

function sessionTs(s: SessionInfo): number {
  return parseTs(s.updated_at);
}

function resumeTargetForAgent(agent: string, sessions: SessionInfo[]): LastSession | null {
  const remembered = readLastSessions()[agent];
  if (remembered?.sessionId) {
    const live = sessions.find((s) => s.session_id === remembered.sessionId && s.agent === agent);
    if (live || remembered.workspace) {
      return {
        sessionId: remembered.sessionId,
        workspace: live?.workspace ?? remembered.workspace ?? "",
        updatedAt: live ? sessionTs(live) : remembered.updatedAt,
      };
    }
  }
  const recent = sessions
    .filter((s) => s.agent === agent && s.session_id && !s.session_id.startsWith("__"))
    .sort((a, b) => sessionTs(b) - sessionTs(a))[0];
  return recent ? { sessionId: recent.session_id, workspace: recent.workspace || "", updatedAt: sessionTs(recent) } : null;
}

function fallbackWorkspace(current: string | null, projects: RecentWorkspace[]): string {
  if (current) return current;
  const existing = projects.find((p) => p.exists);
  return existing?.path || projects[0]?.path || "";
}

export function App() {
  const { t } = useTranslation();
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [branch, setBranch] = useState<string | null>(null);
  const [showGate, setShowGate] = useState(false);
  const [workspaceTrustRequest, setWorkspaceTrustRequest] =
    useState<WorkspaceCommandTrust | null>(null);
  // Web 部署（xj.aibbs.chat）无桌面壳、且云端 chat.db 未同步（会话无法跨端恢复），
  // 每次刷新都会落到默认 agent 而丢失小桔。故 web 端首屏默认进小桔（桌宠常驻），
  // 桌面端（Tauri）维持原 cowork 默认不变。
  const [agent, setAgent] = useState(isTauri() ? "cowork" : "deskmate");
  // 当前激活的专家（专家模式）。null = 普通模式，回复角色显示「助手」。
  const [currentExpert, setCurrentExpert] = useState<Expert | null>(null);
  // 编辑专家：打开文稿选择弹窗
  const [reportDialogOpen, setReportDialogOpen] = useState(false);
  // 专家模式：桌搭首页「召唤专家」→ 切换到专家 persona 的 Agent 会话。
  // 如果专家有关联的 personaId，创建新会话并切换 agent；否则仅设置 currentExpert。
  const startNewSessionRef = useRef<(forAgent?: string) => void>(() => {});
  useEffect(() => {
    const onSummon = (e: Event) => {
      const detail = (e as CustomEvent<{ expertId?: string }>).detail;
      const expert = detail?.expertId ? getExpert(detail.expertId) : undefined;
      if (expert?.personaId) {
        // 专家有独立 persona → 切换到该 Agent 会话
        startNewSessionRef.current(expert.personaId);
      }
      setCurrentExpert(expert ?? null);
    };
    window.addEventListener("deskmate:summon-expert", onSummon);
    return () => window.removeEventListener("deskmate:summon-expert", onSummon);
  }, []);
  const [model, setModel] = useState("gpt-5.6-sol");
  const [models, setModels] = useState<string[]>([]);
  const [modelLabels, setModelLabels] = useState<Record<string, string>>({});
  // {full model id → "free" | "paid"} (from the backend), drives the picker's two-tier split.
  const [modelTiers, setModelTiers] = useState<Record<string, string>>({});
  // {full model id → 厂商显示名}（2026-10-13）：picker 按「厂商 × 档位」分组折叠。
  const [modelVendors, setModelVendors] = useState<Record<string, string>>({});
  // {full model id → context window in tokens} from the curated matrix (verified only);
  // drives the composer usage chip's context-fill meter.
  const [modelContextWindows, setModelContextWindows] = useState<Record<string, number>>({});
  // Settings: show the composer's context-window fill bar. OFF by default (owner ask),
  // so an older backend without the field also shows the session total.
  const [contextBar, setContextBar] = useState(false);
  // Per-session token usage (OPE-42): rebuilt from the transcript on session load,
  // accumulated live from assistant_message events, reset with the transcript.
  const [usage, setUsage] = useState<SessionUsage>(emptyUsage());
  const [surfaces, setSurfaces] = useState<SurfaceVisibility>({ cowork: true, chat: false, code: false });
  const [mode, setMode] = useState("interactive");
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  // Transient "Compacting context…" indicator (OPE-27): set by the `compacting` event,
  // cleared by whatever the engine emits next — the summarizer call is otherwise a
  // multi-second silent stall mid-turn.
  const [compacting, setCompacting] = useState(false);
  const [items, setItems] = useState<Item[]>([]);
  const [streaming, setStreamingState] = useState("");
  // Ref mirror of `streaming`: the WS handler closure is built once per socket and can't read
  // fresh state — the interrupted/error flush below needs the live buffer at event time.
  const streamingRef = useRef("");
  const setStreaming = (value: string | ((s: string) => string)) => {
    streamingRef.current = typeof value === "function" ? value(streamingRef.current) : value;
    setStreamingState(streamingRef.current);
  };
  // The turn's live thinking text (reasoning_delta events) — same ref-mirror pattern.
  // Folded onto the assistant item when the message finalizes; cleared on turn_start.
  const [reasoningStream, setReasoningStreamState] = useState("");
  const reasoningRef = useRef("");
  const setReasoningStream = (value: string) => {
    reasoningRef.current = value;
    setReasoningStreamState(value);
  };
  // DeskMate perception: tool success/failure tallies used as a live confidence proxy.
  const dmToolTotal = useRef(0);
  const dmToolOk = useRef(0);
  const [todo, setTodo] = useState<TodoItem[]>([]);
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [projects, setProjects] = useState<RecentWorkspace[]>([]);
  const [sessionId, setSessionId] = useState<string>(newId());
  // Automation-run context (§ owner ask 2026-07-04): which task an open __run__ session belongs
  // to, driving the banner + "Back to runs". Best-effort — a run session without context still
  // shows a generic banner (detected by its __run__ id).
  const [runContext, setRunContext] = useState<{ id: string; title: string } | null>(null);
  // Which automation the Automations surface opens on (set by the banner's Back link
  // or a sidebar Scheduled-band click). Cleared on leaving the surface: a remembered
  // id going stale (e.g. the automation was deleted) reopened a dead detail —
  // "Loading…" forever (owner-hit 2026-07-20). Nav re-entry should land on the list.
  const [scheduledOpenId, setScheduledOpenId] = useState<string | null>(null);
  const [gateCreate, setGateCreate] = useState(false);
  // Which Settings section the full-page Settings surface opens on (§ Settings-as-page).
  const [settingsTab, setSettingsTab] = useState<"appearance" | "models" | "voice" | "personas">(
    "appearance",
  );
  const openSettings = (tab: "appearance" | "models" | "voice" | "personas" = "appearance") => {
    setSettingsTab(tab);
    setSurface("settings");
  };
  // Whether the default model's provider is actually configured (any provider). Drives the
  // composer's "No model connected" chip. Default true so we don't flash the chip before settings
  // load; corrected by loadSettings.
  const [modelReady, setModelReady] = useState(true);
  const [surface, setSurface] = useState<
    "session" | "scheduled" | "integrations" | "skillstore" | "audit" | "inbox" | "persona" | "settings" | "experts" | "roundtable" | "badges" | "profile" | "creation" | "teamchat" | "mailbox"
  >("session");
  const [teamId] = useState("");
  // A remembered Scheduled-detail target must not outlive the surface (see the
  // scheduledOpenId comment above): nav re-entry lands on the list, never a
  // possibly-deleted automation's dead detail.
  useEffect(() => {
    if (surface !== "scheduled") setScheduledOpenId(null);
  }, [surface]);
  // The persona whose detail page is showing (surface === "persona"); empty falls back to the
  // active session's persona. Phase 5 wires the grouped-nav gear + "Manage personas…" entry points.
  const [personaViewId, setPersonaViewId] = useState<string>("");
  // Where the persona page returns on "back": the active session, or Settings ▸ Personas when it
  // was opened from there (persona config now lives in Settings).
  const [personaViewReturn, setPersonaViewReturn] = useState<"session" | "settings">("session");
  const openPersona = (id: string, from: "session" | "settings" = "session") => {
    setPersonaViewReturn(from);
    setPersonaViewId(id);
    setSurface("persona");
  };
  // 专家中心：召唤某专家进入专家模式（复用既有的 deskmate:summon-expert 事件），
  // 并切回会话视图，使 Composer 专家条与「生成审阅报告」等能力立即可用。
  const summonExpert = (id: string) => {
    window.dispatchEvent(new CustomEvent("deskmate:summon-expert", { detail: { expertId: id } }));
    setSurface("session");
  };
  const [browserRefreshKey, setBrowserRefreshKey] = useState(0);
  const [railHidden, setRailHidden] = useState(false);
  // Left-nav collapse (⌘B): when collapsed the sidebar leaves the grid so content reclaims the
  // width; hovering the left edge peeks it back as a floating overlay. Persisted per-device.
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(NAV_COLLAPSED_KEY) === "1"; } catch { return false; }
  });
  const [navPeek, setNavPeek] = useState(false);
  // While an artifact preview is open we auto-collapse the nav (#3). Remember the pre-preview
  // collapse state so we can restore it on close — unless the user re-opened the nav meanwhile.
  const navBeforePreview = useRef<boolean | null>(null);
  const setNavCollapsedPersist = useCallback((v: boolean) => {
    setNavCollapsed(v);
    try { localStorage.setItem(NAV_COLLAPSED_KEY, v ? "1" : "0"); } catch { /* best effort */ }
  }, []);
  const toggleNav = useCallback(() => {
    setNavPeek(false);
    navBeforePreview.current = null; // a manual toggle takes control from the artifact auto-collapse
    setNavCollapsedPersist(!navCollapsed);
  }, [navCollapsed, setNavCollapsedPersist]);
  // #3: collapse the nav while a full artifact preview is open, restore it on close (unless the
  // user manually toggled meanwhile). The collapse is transient — it never overwrites the pref.
  const onArtifactPreview = useCallback((open: boolean) => {
    if (open) {
      if (navBeforePreview.current === null) navBeforePreview.current = navCollapsed;
      setNavPeek(false);
      setNavCollapsed(true);
    } else if (navBeforePreview.current !== null) {
      setNavCollapsed(navBeforePreview.current);
      navBeforePreview.current = null;
    }
  }, [navCollapsed]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleNav();
      }
      // ⌘, — the platform Settings shortcut (advertised in the account menu, §26).
      if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        e.preventDefault();
        setSurface("settings");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleNav]);
  // Count of files this Cowork conversation has produced — surfaces an "Artifacts (N)" button in
  // the topbar when the side panel is hidden, so produced files are never buried.
  const [artifactCount, setArtifactCount] = useState(0);
  // §32 deep link into the rail's Access section (the former Session-settings drawer): bumping
  // the key expands the section and scrolls it into view. Callers also un-hide the rail.
  const [accessKey, setAccessKey] = useState(0);
  const openAccess = () => {
    setRailHidden(false);
    setAccessKey((k) => k + 1);
  };
  // §34 (UX-016): clicking an artifact chip in the transcript must land somewhere visible —
  // RightRail opens the viewer; this just makes sure the rail isn't hidden.
  useEffect(() => {
    const show = () => setRailHidden(false);
    window.addEventListener("ocw-open-artifact", show);
    return () => window.removeEventListener("ocw-open-artifact", show);
  }, []);
  // The command-palette search, openable from the collapsed-sidebar topbar cluster (§22). The
  // expanded sidebar owns its own instance; this one exists so search never disappears with it.
  const [searchOpen, setSearchOpen] = useState(false);
  // A pending composer prefill (text + attachments) pushed from the session start panel.
  const [composerPrefill, setComposerPrefill] = useState<{ text: string; attachments?: Attachment[]; nonce: number }>();

  // Persona metadata drives workspace behavior by FAMILY, not by hardcoded id (so a DevOps/SecOps
  // code-family persona gates a folder like Code, and a knowledge persona starts orphan like Cowork).
  const [personas, setPersonas] = useState<Persona[] | null>(null);
  useEffect(() => {
    getPersonas().then(setPersonas).catch(() => {});
  }, []);
  const personaOf = (a: string) => personas?.find((p) => p.id === a);
  // 专家 persona 会话（好友栏点开 / NewSessionSplit 选中的非默认角色）：agent 已是该
  // persona id。默认四件套（cowork/chat/code/deskmate）有各自的专用 hero，不走专家名片。
  const BUILTIN_AGENT_IDS = new Set(["cowork", "chat", "code", "deskmate", "myhelper"]);
  const expertPersona =
    !BUILTIN_AGENT_IDS.has(agent) ? personaOf(agent) : undefined;

  // Pending Inbox items for the ACTIVE session — surfaced inline above the composer so an
  // unattended session's blocking question/approval can be answered in context (resolving the
  // same item the Inbox shows; first responder wins).
  const [sessionInbox, setSessionInbox] = useState<InboxItem[]>([]);
  // Whether the active session is Unattended — when true, the agent's prompts route to the Inbox,
  // so we suppress the inline live cards (the Inbox / answer-in-context path shows them instead).
  // A ref too, because the WS event handler closes over stale state.
  const [unattended, setUnattendedState] = useState(false);
  const unattendedRef = useRef(false);
  const markUnattended = useCallback((on: boolean) => {
    unattendedRef.current = on;
    setUnattendedState(on);
  }, []);
  // The Mode menu's "Send approvals to Inbox" toggle (§22 — the old InboxControl, folded in).
  const toggleUnattended = async (on: boolean) => {
    await setUnattended(sessionId, on);
    markUnattended(on);
    // First Unattended enable = Inbox machinery engaged → the account row's chip unlocks (§26).
    if (on) announceInboxUnlock();
  };
  const resolveSessionInbox = async (id: string, resolution: string) => {
    await resolveInboxItem(id, resolution);
    getInbox(sessionId, "pending").then(setSessionInbox).catch(() => setSessionInbox([]));
    refreshSessions(); // attention badge should drop right away
  };
  // Shows a working-area chip / project grouping. Persona's needs_workspace; fallback before load.
  const needsWorkspace = (a: string) => personaOf(a)?.needs_workspace ?? needsWorkspaceFallback(a);
  // MUST pick a folder before starting — project-scoped personas (git-bound Code, project-bound
  // Ops). Scratch/deliverable personas start orphan: the server auto-provisions a per-conversation
  // scratch dir and reports it in the `ready` event.
  const gatesWorkspace = (a: string) => {
    const p = personaOf(a);
    return p ? isProjectScoped(p) : gatesWorkspaceFallback(a);
  };

  // The desktop tray's "Settings" item dispatches this on the window.
  useEffect(() => {
    const open = () => openSettings("appearance");
    window.addEventListener("coworker:open-settings", open);
    return () => window.removeEventListener("coworker:open-settings", open);
  }, []);

  // "Run setup again" (from Settings) re-opens the wizard.
  useEffect(() => {
    const open = () => {
      setOnboarding(true);
    };
    window.addEventListener("coworker:open-onboarding", open);
    return () => window.removeEventListener("coworker:open-onboarding", open);
  }, []);

  const sessionRef = useRef<Session | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // A prompt to auto-send once the next session connects (used by "Run now").
  const pendingPromptRef = useRef<string | null>(null);
  // The in-flight manual run to finalize after its first turn ({taskId, runId, sessionId}).
  const activeRunRef = useRef<{ taskId: string; runId: string; sessionId: string } | null>(null);

  // Fetch ALL sessions + known projects so the sidebar can group them.
  const refreshSessions = useCallback(() => {
    getSessions().then(setSessions).catch(() => setSessions([]));
    getRecentWorkspaces().then(setProjects).catch(() => setProjects([]));
  }, []);

  // initial: adopt the server's seed workspace if any, else force the gate.
  // Retry health for a while: the desktop shell starts its sidecar in parallel, so the
  // server may not answer for a second or two. Only fall back to the gate once it's truly up.
  const [booting, setBooting] = useState(true);
  const [onboarding, setOnboarding] = useState(false);
  // True once we've resumed a prior conversation on boot (drives the splash wording).
  const [resumedExisting, setResumedExisting] = useState(false);
  // Latched: keep the boot splash up until the restored session is actually CONNECTED (not just
  // until `booting` clears), so an early click can't land on a session that's still settling.
  const [uiReady, setUiReady] = useState(false);

  // On boot with no seeded workspace, reopen the last thing the user had — most recent
  // conversation (restores its folder + agent + transcript), else the most recent project
  // folder. Only a true first run (nothing to resume) falls through to the folder gate.
  const resumeLastOrGate = async () => {
    let loadedSessions: SessionInfo[] = [];
    try {
      loadedSessions = (await getSessions()).filter((s) => s.session_id && !s.session_id.startsWith("__"));
      setSessions(loadedSessions);
      const sess = loadedSessions;
      const ts = (s: SessionInfo) => parseTs(s.updated_at);
      const last = [...sess].sort((a, b) => ts(b) - ts(a))[0];
      if (last) {
        setResumedExisting(true);
        if (last.agent) setAgent(last.agent);
        if (last.workspace) {
          setWorkspace(last.workspace);
          setBranch(null);
        }
        try {
          const messages = await getSessionMessages(last.session_id);
          setItems(itemsFromMessages(messages));
          setUsage(usageFromMessages(messages));
        } catch (err) {
          // 拉取历史失败：保留当前界面内容，避免启动期接口/令牌未就绪时瞬间清空会话
          console.error("[resume] 恢复会话历史失败，保留当前界面:", err);
        }
        setSessionId(last.session_id);
        setShowGate(false);
        return;
      }
    } catch {
      /* fall through */
    }
    try {
      const recents = await getRecentWorkspaces();
      setProjects(recents);
      // Only auto-adopt a recent folder for gated surfaces (Code). Cowork starts orphan.
      if (gatesWorkspace(agent)) {
        const ws = recents.find((w) => w.exists) || recents[0];
        if (ws) {
          setWorkspace(ws.path);
          setShowGate(false);
          return;
        }
      }
    } catch {
      /* fall through */
    }
    setShowGate(gatesWorkspace(agent)); // only Code forces a first-run folder gate
  };

  useEffect(() => {
    let cancelled = false;
    const attempt = (tries: number) => {
      getHealth()
        .then(async (h) => {
          if (cancelled) return;
          setModel(h.model);
          // First-run setup wizard (desktop): show until the user completes/dismisses it.
          if (isTauri()) {
            getSettings()
              .then((s) => !cancelled && !s.onboarded && setOnboarding(true))
              .catch(() => {});
          }
          // Settle the active session BEFORE clearing `booting` (which unblocks the connection
          // effect). resumeLastOrGate is async — if we cleared `booting` first, the throwaway
          // initial sessionId would connect against an empty/stale workspace and the server
          // would provision a junk per-conversation scratch dir for it before resume could
          // flip to the real session. Cowork ignores default_workspace (a Code concept).
          if (h.default_workspace && gatesWorkspace(agent)) setWorkspace(h.default_workspace);
          else await resumeLastOrGate();
          // The mount-time loadSettings races the sidecar boot and swallows its failure —
          // on a cold start that left "Loading models…" stuck until the user visited
          // Settings (owner-hit 2026-07-23). Health just answered, so this one lands.
          loadSettings();
          if (!cancelled) setBooting(false);
        })
        .catch(() => {
          if (cancelled) return;
          if (tries <= 0) {
            setBooting(false);
            setShowGate(true);
          } else {
            setTimeout(() => attempt(tries - 1), 500);
          }
        });
    };
    attempt(40); // ~20s of 500ms retries
    return () => {
      cancelled = true;
    };
  }, []);

  // Reveal the UI once boot has settled AND the restored session is connected (or we're showing
  // the folder gate). Latched, so later reconnects never flash the splash again.
  useEffect(() => {
    if (uiReady || booting) return;
    if (connected || showGate) setUiReady(true);
  }, [uiReady, booting, connected, showGate]);
  // Safety net: if the restored session never reports connected (backend slow/unreachable), reveal
  // the UI anyway. Boot already passed the health check, so a live connect is sub-second; this only
  // bites in the failure case, so keep it short.
  useEffect(() => {
    if (uiReady || booting) return;
    const t = setTimeout(() => setUiReady(true), 1500);
    return () => clearTimeout(t);
  }, [uiReady, booting]);

  // Each setter is guarded by a shallow-equality check (sameRef) because this now runs on a
  // timer: getSettings() rebuilds the full label/tier/window maps from the live catalogs on
  // every call, so returning brand-new object identities each poll would re-render every
  // model-aware component forever (the context meter, the picker, every Transcript bubble
  // that reads a label). Same content ⇒ no setState ⇒ no render.
  const sameRef = useRef({
    models: null as string[] | null,
    labels: null as Record<string, string> | null,
    tiers: null as Record<string, string> | null,
    vendors: null as Record<string, string> | null,
    windows: null as Record<string, number> | null,
  });
  const sameArr = (a: unknown[] | null, b: unknown[]) =>
    a != null && a.length === b.length && a.every((v, i) => v === b[i]);
  const sameRec = (
    a: Record<string, string | number> | null,
    b: Record<string, string | number>,
  ) => {
    if (a == null) return false;
    const ka = Object.keys(a);
    return ka.length === Object.keys(b).length && ka.every((k) => a[k] === b[k]);
  };

  const loadSettings = useCallback(
    () =>
      getSettings()
        .then((s) => {
          const p = sameRef.current;
          const models = s.models || [];
          if (!sameArr(p.models, models)) {
            p.models = models;
            setModels(models);
          }
          const labels = s.model_labels || {};
          if (!sameRec(p.labels, labels)) {
            p.labels = labels;
            setModelLabels(labels);
          }
          const tiers = s.model_tiers || {};
          if (!sameRec(p.tiers, tiers)) {
            p.tiers = tiers;
            setModelTiers(tiers);
          }
          const vendors = s.model_vendors || {};
          if (!sameRec(p.vendors, vendors)) {
            p.vendors = vendors;
            setModelVendors(vendors);
          }
          const windows = s.model_context_windows || {};
          if (!sameRec(p.windows, windows)) {
            p.windows = windows;
            setModelContextWindows(windows);
          }
          setContextBar(s.context_bar === true);
          setModelReady(s.model_ready);
          if (s.surfaces) setSurfaces(s.surfaces);
        })
        .catch(() => {}),
    [],
  );

  // Open Settings → Configure Models (from the composer's "No model connected" chip).
  const openModelSetup = () => openSettings("models");

  // Leaving the Settings page: pick up any model/surface changes for the composer (the modal used to
  // do this on close).
  useEffect(() => {
    if (surface !== "settings") loadSettings();
  }, [surface, loadSettings]);

  // Keep the picker's model list live (2026-10-09: "the composer's opencode free models never
  // update"). The backend mirrors the vendor catalog on a 6h TTL, but the GUI only ever asked
  // for settings at boot and on surface change — so a model delisted/newly listed upstream stayed
  // invisible until the user happened to visit Settings. 10 minutes is well inside the 6h TTL
  // (plenty for a refresh to land) while adding a negligible request rate.
  //
  // Not 15s like the session poll on purpose: getSettings() re-reads and re-parses the ~840KB
  // OpenRouter cache on every call, and that runs on the server's single-threaded event loop —
  // the App.tsx session-poll comment records exactly what over-polling did to it (REST every
  // ~2s, session switches queued behind). This payload has no liveness/badge requirement, so
  // there is nothing to gain from a fast interval.
  useEffect(() => {
    const t = setInterval(loadSettings, 10 * 60 * 1000);
    return () => clearInterval(t);
  }, [loadSettings]);

  useEffect(() => {
    refreshSessions();
    loadSettings(); // selectable models + which session surfaces are visible
  }, [refreshSessions, loadSettings]);

  // Poll the session list so the attention/liveness badges stay live and sessions created
  // out-of-band (unattended work, messaging, automations) appear without a manual refresh.
  // 15s (was 5s): the 5s poll fires getSessions() which iterates all sessions and calls
  // inbox.pending() + liveness + subscriptions for each one - a synchronous burst on the
  // single-threaded Python event loop. Combined with the 4s inbox poll, the server was
  // servicing a REST request every ~2s, and session-switch requests queued behind them.
  useEffect(() => {
    const t = setInterval(refreshSessions, 15000);
    return () => clearInterval(t);
  }, [refreshSessions]);

  // Persona toggles can archive sessions server-side (disable-archives, §18): refetch on the
  // personas-changed event so the sidebar section disappears immediately, not on the next poll.
  useEffect(() => {
    const onPersonas = () => refreshSessions();
    window.addEventListener(PERSONAS_CHANGED, onPersonas);
    return () => window.removeEventListener(PERSONAS_CHANGED, onPersonas);
  }, [refreshSessions]);

  // If the active surface isn't visible (hidden in Settings, or a resumed session landed on a
  // hidden surface), fall back to Cowork (always visible). Watches both agent and surfaces so it
  // corrects regardless of which settled last.
  useEffect(() => {
    if ((agent === "chat" && !surfaces.chat) || (agent === "code" && !surfaces.code)) {
      switchAgent("cowork");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, surfaces]);

  useEffect(() => {
    if (surface === "session") rememberLastSession(agent, sessionId, workspace);
  }, [surface, agent, sessionId, workspace]);

  // (re)connect when workspace, session, or agent changes
  useEffect(() => {
    if (booting) return; // wait until boot/resume settles the session before connecting
    if (gatesWorkspace(agent) && !workspace) return; // Code needs a folder (gate handles it)
    const handleEvent = (ev: WsEvent) => {
      const d = ev.data || {};
      // DeskMate perception: translate the session's real agent-state events into the
      // window CustomEvents the DeskMate overlay listens for. No-op unless the active
      // agent is "deskmate" (the overlay only mounts for that agent anyway).
      const dmPush = (
        activity: string,
        emotion: string,
        confidence?: number,
      ) => {
        if (agent !== "deskmate") return;
        window.dispatchEvent(
          new CustomEvent("deskmate:status", {
            detail: { activity, emotion, confidence },
          }),
        );
      };
      // An interrupted/errored turn never emits assistant_message, so its streamed partial
      // would otherwise live only in the ephemeral buffer until the next turn_start wipes it
      // (owner-hit 2026-07-22). Promote it to a durable transcript item — the engine persists
      // the same text server-side, so the live view and a session reload now agree.
      const flushPartialStream = () => {
        const partial = streamingRef.current;
        const thinking = reasoningRef.current;
        if (!partial && !thinking) return;
        setStreaming("");
        setReasoningStream("");
        setItems((p) => [
          ...p,
          {
            kind: "assistant",
            text: partial,
            ts: Date.now() / 1000,
            ...(thinking ? { reasoning: thinking } : {}),
          },
        ]);
      };
      // Any engine event after `compacting` means the summarizer finished (compacted /
      // silent no-op / failure prompt) — the transient must never outlive it.
      if (ev.type !== "compacting") setCompacting(false);
      switch (ev.type) {
        case "ready":
          setConnected(true);
          if (d.model) setModel(d.model);
          if (d.mode) setMode(d.mode);
          if (d.command_trust?.required) setWorkspaceTrustRequest(d.command_trust);
          // Cowork: adopt the server-provisioned scratch dir (only when we don't already have one).
          if (d.workspace) setWorkspace((cur) => cur || d.workspace);
          // DeskMate: load memory panel immediately on session connect so the
          // pet status panel shows SOUL/USER/MEMORY without waiting for a turn.
          if (agent === "deskmate") {
            import("./api").then(({ httpBase, authedFetch }) => {
              authedFetch(`${httpBase()}/v1/deskmate/memory`)
                .then((r) => (r.ok ? r.json() : null))
                .then((data) => {
                  if (data?.files?.length) {
                    window.dispatchEvent(
                      new CustomEvent("deskmate:memory-update", { detail: { files: data.files } }),
                    );
                  }
                })
                .catch(() => {});
            });
          }
          break;
        case "turn_start":
          setRunning(true);
          setStreaming("");
          setReasoningStream("");
          dmToolTotal.current = 0;
          dmToolOk.current = 0;
          window.__deskmateTurnActive = true;
          dmPush("正在理解你的请求…", "thinking");
          // Background-delivered turns (channel message, self-wake, durable resume) have no local
          // send(), so the triggering message isn't in `items` yet — surface it. A connector message
          // carries a structured `source` (§3.1) → render the rich card; otherwise a plain user item.
          // Foreground turns already appended it in send(); skip the duplicate.
          if (d.source?.connector) {
            const src = d.source as MessageSource;
            setItems((p) => {
              const last = p[p.length - 1];
              return last && last.kind === "connector" && last.source.ts === src.ts && last.source.text === src.text
                ? p
                : [...p, { kind: "connector", source: src }];
            });
          } else if (typeof d.input === "string" && d.input) {
            setItems((p) => {
              const last = p[p.length - 1];
              return last && last.kind === "user" && last.text === d.input
                ? p
                : [...p, { kind: "user", text: d.input as string, ts: Date.now() / 1000 }];
            });
          }
          break;
        case "assistant_delta":
          setStreaming((s) => s + (d.text || ""));
          dmPush("正在写回复…", "focused");
          break;
        case "reasoning_delta":
          setReasoningStream(reasoningRef.current + (d.text || ""));
          dmPush("正在思考 / 推理…", "thinking");
          break;
        case "assistant_message": {
          if (d.usage) setUsage((u) => addTurnUsage(u, d.usage));
          // The event's reasoning is authoritative (covers background-delivered turns);
          // the local buffer is the fallback for older servers.
          const reasoning = d.reasoning || reasoningRef.current;
          // R13 意图讨论态消息：meta.origin === "intent_handoff" → 渲染带「定稿/再改/不要」
          // 三按钮的 intent_discuss 卡片（opening 阶段只显示文字）。
          const meta = d.meta || {};
          if (meta.origin === "intent_handoff" && (d.text || "")) {
            setItems((p) => [
              ...p,
              {
                kind: "intent_discuss",
                text: String(d.text || ""),
                intent_id: String(meta.intent_id || ""),
                round: Number(meta.round || 0),
                phase: meta.phase === "opening" ? "opening" : "draft",
                ts: Date.now() / 1000,
              },
            ]);
            setStreaming("");
            setReasoningStream("");
            break;
          }
          // Report card: deskMate background report generation embeds `report` + `report_name`
          // on the assistant message — render the rich ReportViewer instead of plain text.
          if (d.report && d.report_name) {
            let report: any = d.report;
            if (typeof report === "string") {
              try { report = JSON.parse(report); } catch { report = null; }
            }
            if (report && typeof report === "object") {
              setItems((p) => [
                ...p,
                { kind: "report", name: String(d.report_name), report },
              ]);
            }
          } else if (d.text || reasoning) {
            setItems((p) => [
              ...p,
              {
                kind: "assistant",
                text: d.text || "",
                ts: Date.now() / 1000,
                ...(reasoning ? { reasoning } : {}),
              },
            ]);
          }
          setStreaming(""); // finalized into items (or empty tool-only turn)
          setReasoningStream("");
          break;
        }
        case "tool_proposed":
          if (d.name === "todo_write" && (d.arguments?.todos || d.arguments?.items))
            setTodo(normalizeTodos(d.arguments.todos ?? d.arguments.items));
          setItems((p) => [
            ...p,
            { kind: "tool", id: newId(), name: d.name, args: d.arguments, status: "…" },
          ]);
          break;
        case "permission_required":
          // Unattended → the backend parked it in the Inbox; don't also surface a live card.
          if (unattendedRef.current) break;
          setItems((p) => [
            ...p,
            {
              kind: "approval",
              name: d.name,
              args: d.arguments,
              reason: d.reason,
              category: d.category,
              standingTarget: d.standing_target || undefined,
            },
          ]);
          break;
        case "directory_requested":
          if (unattendedRef.current) break;
          setItems((p) => [
            ...p,
            { kind: "dirreq", reason: d.reason || "", path: d.path || "", writable: !!d.writable },
          ]);
          break;
        case "plan_proposed":
          if (unattendedRef.current) break;
          setItems((p) => [...p, { kind: "planreq", plan: d.plan || "" }]);
          break;
        case "question_requested":
          // ask_user in an attended session — answered inline (not routed to the Inbox).
          setItems((p) => [
            ...p,
            {
              kind: "question",
              question: d.question || "",
              options: d.options || [],
              allow_text: d.allow_text !== false,
              multi: !!d.multi,
            },
          ]);
          break;
        case "tool_started":
          dmPush(`正在调用工具：${d.name}`, "focused");
          break;
        case "tool_finished":
          setItems((p) =>
            updateLastTool(
              p,
              d.name,
              d.status,
              d.result_preview || d.reason,
              d.display?.hidden_by_filters,
              d.standing_rule,
            ),
          );
          dmToolTotal.current += 1;
          if (d.status === "ok") dmToolOk.current += 1;
          dmPush(
            d.status === "error" ? `工具出错：${d.name}` : `工具完成：${d.name}`,
            d.status === "error" ? "concerned" : "focused",
          );
          // Refresh the right rail when something it shows may have changed: browser state, or a
          // file write that should appear under Artifacts immediately (not only after the turn).
          if (String(d.name || "").startsWith("browser_") || FILE_WRITE_TOOLS.has(d.name)) {
            setBrowserRefreshKey((k) => k + 1);
          }
          break;
        case "turn_end":
          if (d.status === "max_iterations_exceeded")
            setItems((p) => [...p, { kind: "notice", tone: "warn", text: "Stopped: max iterations reached." }]);
          break;
        case "model_changed":
          // Mid-session switch (server-applied): update the header fact and drop the
          // persisted marker into the live transcript (replay renders it from history).
          if (d.model) setModel(d.model);
          setItems((p) => [...p, { kind: "notice", tone: "info", text: d.text || "Model switched" }]);
          break;
        case "compacting":
          setCompacting(true);
          break;
        case "compacted":
          // Auto-compaction marker (OPE-27): outbound-only — the transcript stays intact,
          // this divider just shows where the model's memory was summarized.
          setItems((p) => [...p, { kind: "notice", tone: "info", text: d.text || "Context compacted" }]);
          break;
        case "interrupted":
          flushPartialStream();
          setItems((p) => [...p, { kind: "notice", tone: "warn", text: "Interrupted." }]);
          break;
        case "error":
          flushPartialStream();
          setItems((p) => [
            ...p,
            { kind: "notice", tone: "warn", text: "Error: " + (d.error || "unknown"), retriable: true },
          ]);
          break;
        case "input_rejected":
          setItems((p) => [
            ...p,
            { kind: "notice", tone: "warn", text: d.error || "That message was rejected." },
          ]);
          break;
        case "turn_done":
          setRunning(false);
          refreshSessions();
          window.__deskmateTurnActive = false;
          {
            const conf = dmToolTotal.current > 0 ? dmToolOk.current / dmToolTotal.current : undefined;
            dmPush("待机", "idle", conf);
          }
          // Catch-all artifact refresh: files created via shell or on a brand-new session (whose
          // record only exists after the first save) appear once the turn completes.
          setBrowserRefreshKey((k) => k + 1);
          // Finalize a manual run after its first turn completes (mark it ok in history).
          {
            const ar = activeRunRef.current;
            if (ar && ar.sessionId === sessionId) {
              activeRunRef.current = null;
              finalizeAutomationRun(ar.taskId, ar.runId).catch(() => {});
            }
          }
          break;
        case "deskmate_suggestion":
          if (agent !== "deskmate") break;
          window.dispatchEvent(new CustomEvent("deskmate:suggestion", { detail: d }));
          break;
        case "deskmate_memory_update":
          if (agent !== "deskmate") break;
          window.dispatchEvent(
            new CustomEvent("deskmate:memory-update", { detail: d }),
          );
          break;
        case "deskmate_memory_learned":
          if (agent !== "deskmate") break;
          // 本轮自动抽取到的新记忆 → 让浮层展示「我刚记住了」提示，让「越用越懂你」可见
          window.dispatchEvent(
            new CustomEvent("deskmate:memory-learned", { detail: d }),
          );
          break;
        case "deskmate_context_injected":
          if (agent !== "deskmate") break;
          // 本轮向对话注入了相关记忆 → 让浮层低调提示「我在用记忆帮你」（非模态轻提示）
          window.dispatchEvent(
            new CustomEvent("deskmate:context-injected", { detail: d }),
          );
          break;
      }
    };

    const session = new Session(sessionId, workspace || "", agent, {
      onEvent: handleEvent,
      onOpen: () => {
        setConnected(true);
        // Auto-send the task prompt once a "Run now" session connects.
        const p = pendingPromptRef.current;
        if (p) {
          pendingPromptRef.current = null;
          setItems((prev) => [...prev, { kind: "user", text: p, ts: Date.now() / 1000 }]);
          sessionRef.current?.userMessage(p);
        }
      },
      onClose: () => setConnected(false),
    });
    sessionRef.current = session;
    return () => session.close();
    // NOTE: `workspace` is intentionally NOT a dependency. Every real workspace change
    // (pick folder, select/switch session, new session) is paired with a `sessionId`
    // change, so the socket still reconnects when it should. The one workspace-only change
    // is the `ready` handler adopting the server's provisioned Cowork scratch dir — listing
    // `workspace` here made that adoption tear down and rebuild the socket immediately after
    // first connect, dropping the user's first message (the "send twice" bug). The scratch
    // dir is deterministic from `sessionId` server-side, so skipping that reconnect is safe.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [booting, sessionId, agent, refreshSessions]);

  // Stream-following (FB-004): auto-scroll only while the user is AT the bottom, so scrolling
  // up to read during a streaming turn sticks. `atBottomRef` is the live truth (per scroll
  // event, no re-render); `following` mirrors it into state for the jump-to-latest pill.
  // Programmatic smooth-scrolls fire scroll events of their own — while one is in flight
  // (`autoScrollingRef`) they must not read as "the user scrolled up", or every stream tick
  // would disengage its OWN follow. The animation only moves down, so a decreasing scrollTop
  // mid-flight can only be the user taking over.
  const atBottomRef = useRef(true);
  const autoScrollingRef = useRef(false);
  const lastScrollTopRef = useRef(0);
  const [following, setFollowing] = useState(true);
  const scrollToBottom = () => {
    const el = scrollRef.current;
    if (!el) return;
    autoScrollingRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };
  const followLatest = () => {
    atBottomRef.current = true;
    setFollowing(true);
    scrollToBottom();
  };
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const atBottom = el.scrollHeight - top - el.clientHeight < 48;
    if (autoScrollingRef.current) {
      if (atBottom) autoScrollingRef.current = false; // landed
      else if (top >= lastScrollTopRef.current) {
        lastScrollTopRef.current = top; // still animating down — not the user
        return;
      } else autoScrollingRef.current = false; // moved UP mid-flight — user takeover
    }
    lastScrollTopRef.current = top;
    atBottomRef.current = atBottom;
    setFollowing(atBottom);
  };
  // A different session is a fresh viewport — never inherit a scrolled-up state. Declared
  // BEFORE the auto-scroll effect: when a session switch and its hydrated items land in one
  // commit, the reset must run first or the stale ref would skip the initial bottom-scroll.
  useEffect(() => {
    atBottomRef.current = true;
    setFollowing(true);
  }, [sessionId]);
  useEffect(() => {
    if (atBottomRef.current) scrollToBottom();
  }, [items, streaming]);

  // Track produced-file count for the topbar "Artifacts" affordance (works even when the rail is
  // hidden, where the rail itself doesn't fetch). Cowork only; refreshes on file writes/turn end.
  useEffect(() => {
    if (agent !== "cowork" || surface !== "session") {
      setArtifactCount(0);
      return;
    }
    getArtifacts(sessionId).then((a) => setArtifactCount(a.length)).catch(() => {});
  }, [agent, surface, sessionId, browserRefreshKey]);

  // Keep the active session's pending Inbox items fresh (answer-in-context card). Loads on session
  // change + after each turn, plus a slow poll so an unattended agent's new question surfaces.
  // 10s (was 4s): the 4s poll fires two REST calls (getInbox + getUnattended) that queue on
  // the Python event loop. When combined with the session-list poll, switching sessions
  // had to wait behind a pile of in-flight poll responses.
  useEffect(() => {
    if (surface !== "session") return;
    const load = () => {
      getInbox(sessionId, "pending").then(setSessionInbox).catch(() => setSessionInbox([]));
      getUnattended(sessionId).then(markUnattended).catch(() => markUnattended(false));
    };
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [surface, sessionId, browserRefreshKey, markUnattended]);

  const send = (text: string, attachments?: Attachment[]) => {
    setItems((p) => [...p, { kind: "user", text, attachments, ts: Date.now() / 1000 }]);
    // The visible model rides along with the message (single source of truth per turn).
    sessionRef.current?.userMessage(text, attachments, model);
    followLatest(); // sending always re-engages stream-following, wherever the user had scrolled
    // 普通聊天里聊写作时，小桔基于认知库主动递一条灵感（不阻塞主对话；冷却/语境由后端控）。
    void postInspire(text).then((insp) => {
      const sug = insp?.suggestion;
      if (insp?.triggered && sug) {
        setItems((p) => [
          ...p,
          { kind: "inspire", text: sug, refs: insp.refs, ts: Date.now() / 1000 },
        ]);
      }
    });
  };

  // 桌搭首页资讯卡片点击后把 query 直接发成用户消息，触发 AI 搜索并总结。
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.text && typeof detail.text === "string") {
        send(detail.text);
      }
    };
    window.addEventListener("deskmate:send-message", handler);
    return () => window.removeEventListener("deskmate:send-message", handler);
  }, [send]);

  // Resolving a LIVE prompt also resolves its parked Inbox mirror server-side, but the polled
  // `sessionInbox` copy stays "pending" for up to a poll cycle — long enough for the docked
  // answer-in-context card to flash the SAME request again right after the user answered it
  // (tester catch 2026-07-12: a Slack send "asked twice"). Drop the mirror optimistically;
  // the 4s poll restores anything genuinely still pending.
  const dropSessionInbox = (kind: string) =>
    setSessionInbox((cur) => cur.filter((it) => it.kind !== kind));
  const approve = (decision: ApprovalDecision) => {
    setItems((p) => resolveLastApproval(p, decision));
    dropSessionInbox("approval");
    sessionRef.current?.approve(decision);
  };
  const respondPlan = (approved: boolean, mode?: string, feedback?: string) => {
    setItems((p) => resolveLastPlan(p, approved ? "approved" : "rejected"));
    dropSessionInbox("plan");
    sessionRef.current?.respondPlan(approved, mode, feedback);
    if (approved && mode) setMode(mode); // the server flips the live engine to this mode
  };
  const respondDirectory = (granted: boolean, path?: string, writable?: boolean) => {
    setItems((p) => resolveLastDirReq(p, granted ? "granted" : "denied"));
    dropSessionInbox("directory");
    sessionRef.current?.respondDirectory(granted, path, writable);
  };
  const answerQuestion = (answer: string) => {
    setItems((p) => resolveLastQuestion(p, answer));
    dropSessionInbox("question");
    sessionRef.current?.respondQuestion(answer);
  };
  const prefillComposer = (text: string, attachments?: Attachment[]) =>
    setComposerPrefill((p) => ({ text, attachments, nonce: (p?.nonce ?? 0) + 1 }));
  // 「聊这个」让她说（§11.16 层三）：条目作为她的助手消息落本会话（server 端
  // compose + append + broadcast，WS 即时渲染）。失败退回短句柄 prefill——
  // 老后端/会话缺失时至少用户能拿着 #id 问她。
  const speakHandoff = async (item: HandoffItem): Promise<boolean> => {
    const ok = await speakDeskmateHandoff(sessionId, item.id);
    if (!ok) {
      const short = item.id.split(":").pop()?.slice(0, 6) ?? "";
      prefillComposer(`#${short} 这条怎么回事？`);
    }
    return ok;
  };
  // 信箱「进聊天」两条路（设计单 §5）：
  // ① 这封信已经有轻回信往来 → promote 成普通会话（解除隐藏标记、进侧栏），直接切进去
  //    用气泡样式续上原有的上下文；
  // ② 还没聊过 → 起一个干净的 deskmate 会话，把信的内容 prefill 进输入框开聊。
  const openChatFromMailbox = async (letter: MailboxLetter) => {
    if (letter.mailbox_session_id) {
      const res = await promoteMailboxLetter(letter.id);
      if (res.ok && res.session_id) {
        announceMailboxChanged(); // 让信箱列表刷出「看对话」态
        await selectSession(res.session_id, res.workspace, "deskmate");
        return;
      }
    }
    startNewSession("deskmate");
    const short = letter.id.split(":").pop()?.slice(0, 6) ?? "";
    prefillComposer(`${letter.content}\n（来自信箱 #${short}）`);
  };
  const interrupt = () => sessionRef.current?.interrupt();
  const retry = () => {
    // Optimistic running: turn_start confirms; a rejected retry still ends in turn_done.
    setRunning(true);
    sessionRef.current?.retry();
  };
  const changeMode = (m: string) => {
    setMode(m);
    sessionRef.current?.setMode(m);
  };
  const changeModel = (m: string) => {
    if (running) return; // the server refuses mid-turn rebinds — don't let the header lie
    setModel(m);
    sessionRef.current?.setModel(m);
  };

  const startNewSession = (forAgent?: string) => {
    const target = forAgent || agent;
    setSurface("session"); // return to the conversation view if we were on a sub-view
    setItems([]);
    setUsage(emptyUsage());
    setStreaming("");
    setTodo([]);
    setRunning(false);
    // "New session" under a browsed persona switches to it (expand≠switch: the header alone
    // doesn't switch; this explicit action does).
    if (target !== agent) {
      setAgent(target);
      if (gatesWorkspace(target)) {
        // Never inherit the previous persona's folder — it may be a scratch dir. Clearing it
        // also blocks the connection effect, so nothing can chat behind the open gate.
        setWorkspace(null);
        setBranch(null);
        setShowGate(true);
      } else setShowGate(false);
    }
    // Knowledge family: a new conversation starts fresh (orphan) — clear the workspace so the
    // server provisions a NEW scratch dir for the new session id. Code keeps its repo.
    if (!gatesWorkspace(target)) setWorkspace(null);
    setSessionId(newId());
  };
  // Keep the ref in sync so the summon-expert useEffect can call startNewSession
  // without depending on it (avoids re-registering the event listener every render).
  startNewSessionRef.current = startNewSession;
  // Inbox → session: the item carries its session's workspace/agent, so open it directly.
  // UX-026: 5s top-right toast when a SCHEDULED automation run starts (never for
  // manual Run-now — the user is already watching). Rides the app-wide /ws/events
  // stream; View run opens the run's live session.
  const [runToast, setRunToast] = useState<{
    title: string; sessionId: string; workspace: string; agent: string; time: string;
  } | null>(null);
  useEffect(() => {
    const stop = connectEvents((msg) => {
      if (msg.type !== "automation_run_started") return;
      const d = (msg.data ?? {}) as Record<string, string>;
      setRunToast({
        title: d.task_title || "Automation",
        sessionId: d.session_id || "",
        workspace: d.workspace || "",
        agent: d.agent || "cowork",
        time: new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
      });
      announceAutomationsChanged(); // the Scheduled band's badge is now stale
    });
    return stop;
  }, []);
  useEffect(() => {
    if (!runToast) return;
    const t = window.setTimeout(() => setRunToast(null), 5000);
    return () => window.clearTimeout(t);
  }, [runToast]);

  const openSessionFromInbox = (sid: string, ws: string, ag: string) => selectSession(sid, ws, ag);
  const selectSession = async (id: string, ws: string, ag: string) => {
    setSurface("session"); // selecting a conversation always returns to the conversation view
    setTodo([]);
    setStreaming("");
    setRunning(false);
    if (ag) setAgent(ag);
    if (!gatesWorkspace(ag)) setShowGate(false);
    if (ws && ws !== workspace) {
      setWorkspace(ws); // switch project to the session's folder
      setBranch(null);
    }
    setSessionId(id);
    try {
      const messages = await getSessionMessages(id);
      setItems(itemsFromMessages(messages));
      setUsage(usageFromMessages(messages));
    } catch {
      setItems([]);
      setUsage(emptyUsage());
    }
  };
  // R13 意图讨论流：用户点「执行」→ 启动讨论 → 小桔开口（开场白落会话）→ 切到聊天。
  // 复用当前 sessionId（DeskHome 只在 agent==="deskmate" 且空会话时渲染，故已在小桔会话里）。
  // fail-open：失败仅切到聊天，不阻塞用户继续打字。
  const startIntentDiscussion = async (intentId: string) => {
    setSurface("session");
    if (!sessionId) return;
    try {
      const res = await startIntentDiscuss(sessionId, intentId);
      if (res && res.ok && res.ledger_id) {
        // R13 修复（2026-10-08「提议卡点执行落不到聊天」）：start 端点已自带开场白
        // 投递（append+broadcast 都带 meta → 渲染成讨论卡，且空会话先兜底建记录），
        // 不再回调通用 speak——那条通道丢 meta 会渲染成普通气泡，且空会话 404。
        // 紧接着触发第一轮 run：她把 draft 做出来经 WS 推回（带「定稿/再改/不要」
        // 三按钮的新卡）。fire-and-forget：executor 走 LLM 较慢，不阻塞界面。
        void runIntentDiscussRound(sessionId, intentId, 1).catch(() => {});
      }
    } catch {
      // 静默：用户已在聊天视图，可继续手动操作。
    }
  };
  // R13 三按钮回调：定稿 / 再改 / 不要。fail-open：失败仅置 resolved 标记，不阻塞。
  // 改完 items 把对应 item 的 resolved 字段更新（按钮消失 + 状态徽标显示）。
  const finalizeIntentDiscussion = async (intentId: string) => {
    if (!sessionId) return;
    setItems((p) =>
      p.map((it) =>
        it.kind === "intent_discuss" && it.intent_id === intentId
          ? { ...it, resolved: "finalized" as const }
          : it,
      ),
    );
    try {
      await finalizeIntentDiscuss(sessionId, intentId);
    } catch {
      // 静默：UI 已置定稿态，后端失败不阻塞（用户可手动重试）。
    }
  };
  const reviseIntentDiscussion = async (intentId: string, round: number) => {
    if (!sessionId) return;
    setItems((p) =>
      p.map((it) =>
        it.kind === "intent_discuss" && it.intent_id === intentId
          ? { ...it, resolved: "revising" as const }
          : it,
      ),
    );
    try {
      // 跑下一轮：round+1，后端会产出新 draft 并通过 WS 推回（自动渲染新卡片）。
      await runIntentDiscussRound(sessionId, intentId, round + 1);
    } catch {
      // 静默：UI 已置改中态，后端失败不阻塞。
    }
  };
  const cancelIntentDiscussion = async (intentId: string) => {
    if (!sessionId) return;
    setItems((p) =>
      p.map((it) =>
        it.kind === "intent_discuss" && it.intent_id === intentId
          ? { ...it, resolved: "cancelled" as const }
          : it,
      ),
    );
    try {
      await cancelIntentDiscuss(intentId, sessionId);
    } catch {
      // 静默：UI 已置取消态，后端失败不阻塞。
    }
  };
  const switchAgent = async (name: string) => {
    setSurface("session");
    if (name === agent) return;
    rememberLastSession(agent, sessionId, workspace);
    const knownSessions = sessions.length ? sessions : await getSessions().catch(() => []);
    const knownProjects = projects.length ? projects : await getRecentWorkspaces().catch(() => []);
    const target = resumeTargetForAgent(name, knownSessions);

    setAgent(name);
    setItems([]);
    setUsage(emptyUsage());
    setStreaming("");
    setTodo([]);
    setRunning(false);

    // The live workspace is only a valid fallback for a gated persona if it came from
    // another gated persona — a knowledge persona's workspace is a scratch dir, and a
    // code-family session must never adopt one. (`agent` is still the previous persona here.)
    const inheritable = gatesWorkspace(agent) ? workspace : null;

    if (target) {
      // Code falls back to a recent folder; Cowork resumes its scratch (target.workspace) or
      // starts orphan ("" → server provisions). Chat has no workspace.
      const targetWorkspace = gatesWorkspace(name)
        ? target.workspace || fallbackWorkspace(inheritable, knownProjects)
        : needsWorkspace(name)
          ? target.workspace || ""
          : "";
      if (targetWorkspace && targetWorkspace !== workspace) {
        setWorkspace(targetWorkspace);
        setBranch(null);
      } else if (!targetWorkspace) {
        setWorkspace(null); // orphan cowork: clear so the next `ready` adopts a fresh scratch
      }
      if (!gatesWorkspace(name)) setShowGate(false);
      else if (targetWorkspace) setShowGate(false);
      else setShowGate(true);
      setSessionId(target.sessionId);
      try {
        const messages = await getSessionMessages(target.sessionId);
        setItems(itemsFromMessages(messages));
        setUsage(usageFromMessages(messages));
      } catch {
        setItems([]);
        setUsage(emptyUsage());
      }
      return;
    }

    const id = newId();
    const fallback = gatesWorkspace(name) ? fallbackWorkspace(inheritable, knownProjects) : "";
    if (fallback && fallback !== workspace) {
      setWorkspace(fallback);
      setBranch(null);
    } else if (!fallback && needsWorkspace(name)) {
      setWorkspace(null); // orphan cowork: server provisions a fresh scratch on connect
    }
    setSessionId(id);
    rememberLastSession(name, id, fallback);
    if (!gatesWorkspace(name)) setShowGate(false);
    else setShowGate(!fallback);
  };
  const chooseWorkspace = (path: string, b?: string | null) => {
    setWorkspace(path);
    setBranch(b ?? null);
    setShowGate(false);
    setGateCreate(false);
    setItems([]);
    setUsage(emptyUsage());
    setStreaming("");
    setTodo([]);
    setSessionId(newId());
    getRecentWorkspaces().then(setProjects).catch(() => {});
  };
  // "New project" lives under a project-scoped persona's accordion. Switch to that persona, start a
  // fresh session with no folder yet, and open the gate in create mode — so the gate's
  // surface==="session" && gatesWorkspace(agent) guard passes even if the active session was Chat/Cowork.
  const newProject = (forAgent?: string) => {
    const target = forAgent || agent;
    setSurface("session");
    setItems([]);
    setUsage(emptyUsage());
    setStreaming("");
    setTodo([]);
    setRunning(false);
    if (target !== agent) setAgent(target);
    setWorkspace(null);
    setBranch(null);
    setSessionId(newId());
    setGateCreate(true);
    setShowGate(true);
  };
  const renameConversation = async (id: string, title: string) => {
    const res = await renameSession(id, title);
    if (res.ok) refreshSessions();
  };
  const togglePinned = async (id: string, pinned: boolean) => {
    await setSessionFlags(id, { pinned });
    refreshSessions();
  };
  const toggleArchived = async (id: string, archived: boolean) => {
    await setSessionFlags(id, { archived });
    refreshSessions();
    // Archiving the open chat: leave it and start fresh (it moves to the Archived section).
    if (archived && id === sessionId) {
      setItems([]);
      setUsage(emptyUsage());
      setStreaming("");
      setTodo([]);
      setRunning(false);
      setSessionId(newId());
    }
  };
  const deleteConversation = async (id: string) => {
    const res = await deleteSession(id);
    if (!res.ok) return;
    refreshSessions();
    if (id === sessionId) {
      setItems([]);
      setUsage(emptyUsage());
      setStreaming("");
      setTodo([]);
      setRunning(false);
      setSessionId(newId());
    }
  };

  // "Run now": prepare a manual run, open its session, and auto-send the task so the agent
  // runs LIVE in the main view; finalize it in history once the first turn finishes.
  const openRunSession = (
    sessionId: string,
    ws: string,
    ag: string,
    task?: { id: string; title: string },
  ) => {
    setRunContext(task ?? null);
    setSurface("session");
    setShowGate(false);
    selectSession(sessionId, ws, ag);
  };
  const runTaskNow = async (taskId: string, title?: string) => {
    const r = await runAutomation(taskId);
    if (!r || !r.ok) return;
    pendingPromptRef.current = r.prompt;
    activeRunRef.current = { taskId, runId: r.run_id, sessionId: r.session_id };
    openRunSession(r.session_id, r.workspace, r.agent, { id: taskId, title: title || "" });
  };

  const idle = items.length === 0 && !streaming;
  const pendingApproval = [...items].reverse().find((i) => i.kind === "approval" && !i.resolved);
  const pendingDirReq = [...items].reverse().find((i) => i.kind === "dirreq" && !i.resolved);
  const pendingPlan = [...items].reverse().find((i) => i.kind === "planreq" && !i.resolved);
  const pendingQuestion = [...items].reverse().find((i) => i.kind === "question" && !i.resolved);
  // Facts subtitle (§22): the session's FIXED facts, not controls — model (+ the
  // workspace folder for project-scoped sessions). Renders only once the session has history;
  // until then the model is still choosable in the composer, so there's no locked fact to state.
  const hasHistory = items.length > 0;
  // Curated labels read "Claude Opus 4.8 · Anthropic" — the provider suffix is dropdown context,
  // noise in a facts line. Fall back to the raw id without its provider prefix.
  const modelDisplay =
    modelLabels[model]?.split(" · ")[0] ||
    (model.includes(":") ? model.split(":").slice(1).join(":") : model);
  // Persona name dropped for this release (owner ask 2026-07-22): personas are hidden,
  // so "Coworker" read as noise. The model (+ project folder) are the real fixed facts.
  // Exception: an EXPERT persona session (rail contacts / persona picker) — there the
  // persona IS the session's defining fact, so it leads the facts line.
  const subtitleParts = [modelDisplay];
  if (expertPersona) subtitleParts.unshift(shortPersonaName(expertPersona.name, expertPersona.id));
  if (isProjectScoped(personaOf(agent)) && workspace) subtitleParts.push(baseName(workspace));
  const activeInfo = sessions.find((s) => s.session_id === sessionId);
  const activeTitle = activeInfo?.title || "New session";

  const desktop = isTauri();
  // Dev-only: `?overlay=1` simulates the desktop overlay layout in the browser (adds the
  // tauri-overlay class + draws fake traffic lights at the real position) so the top-left can be
  // tuned in the preview without a DMG build. Never active in the real app (isTauri() short-circuits).
  const simOverlay = !desktop && new URLSearchParams(window.location.search).has("overlay");
  // Overlay layout is macOS-ONLY: Windows/Linux keep the native title bar, so the mac
  // compensations (traffic-light insets, lowered top strips) must not apply there —
  // they rendered as misalignments under Windows' native bar (caught 2026-07-21).
  const overlay = (desktop && platformOS() === "macos") || simOverlay;
  const beginWindowDrag = (event: PointerEvent) => {
    if (!desktop || event.button !== 0) return;
    startWindowDrag();
  };

  if (booting || !uiReady) {
    return (
      <div className={"app boot-splash" + (overlay ? " tauri-overlay" : "")}>
        {/* overlay (not desktop): ?overlay=1 previews the splash's top-left in the browser
            too — the wordmark/traffic-light alignment is exactly what it exists to tune. */}
        {overlay && (
          <div className="titlebar-drag" data-tauri-drag-region>
            <span className="titlebar-brand brand-wordmark">
              <Icon name="logo" size={13} className="mark" /> OpenWorker<span className="beta-tag">{import.meta.env.VITE_APP_VERSION}</span>
            </span>
          </div>
        )}
        {simOverlay && (
          <div className="sim-traffic-lights" aria-hidden="true">
            <span /><span /><span />
          </div>
        )}
        {/* The real OpenWorker mark (6-point star, same as the app/tray icon) — the old
            ✦ text glyph was a 4-point sparkle that read as another product's logo. */}
        <div className="boot-mark">
          <Icon name="logo" size={38} />
        </div>
        <div className="boot-text">
          {resumedExisting ? "Restoring your session…" : "Starting OpenWorker…"}
          <span className="beta-tag">{import.meta.env.VITE_APP_VERSION}</span>
        </div>
      </div>
    );
  }

  return (
    <ErrorBoundary>
    <div
      className={
        "app" +
        (overlay ? " tauri-overlay" : "") +
        (navCollapsed ? " nav-collapsed" : "") +
        (navCollapsed && navPeek ? " nav-peek" : "")
      }
    >
      {/* Dev-only fake traffic lights so ?overlay=1 previews the real desktop top-left. */}
      {simOverlay && (
        <div className="sim-traffic-lights" aria-hidden="true">
          <span /><span /><span />
        </div>
      )}
      {/* Desktop-only auto-update prompt (15s after boot, then every 30 min; inert in browser). */}
      <UpdateBanner />
      {/* UX-026: automation-start toast — quiet panel, neutral dot/drain, accent only
          on the action (rev 2); auto-dismisses with the 5s drain bar. */}
      {runToast && (
        <div
          className="fixed top-3 right-3 z-[45] w-[290px] bg-panel border border-line rounded-xl shadow-lg px-3.5 pt-3 pb-2.5"
          data-testid="automation-toast"
        >
          <div className="flex items-center gap-2 text-[12.5px] font-semibold">
            <span className="w-[7px] h-[7px] rounded-full bg-faint toast-pulse" />
            Automation started
          </div>
          <div className="text-[12.5px] text-muted mt-0.5 ml-[15px] truncate">
            {runToast.title} · {runToast.time} run
          </div>
          <div className="flex items-center justify-between ml-[15px] mt-1.5">
            <button
              className="text-[12.5px] text-accent font-medium"
              data-testid="toast-view-run"
              onClick={() => {
                selectSession(runToast.sessionId, runToast.workspace, runToast.agent);
                setRunToast(null);
              }}
            >
              View run ›
            </button>
            <button
              className="text-[12px] text-faint px-0.5"
              data-testid="toast-dismiss"
              title="Dismiss"
              onClick={() => setRunToast(null)}
            >
              ✕
            </button>
          </div>
          <div className="absolute left-3 right-3 bottom-1 h-[2px] rounded bg-line overflow-hidden">
            <span className="block h-full bg-faint toast-drain" />
          </div>
        </div>
      )}
      {/* When collapsed, a thin left-edge zone peeks the nav back as a floating overlay. */}
      {navCollapsed && (
        <div
          className="nav-hover-zone"
          onMouseEnter={() => setNavPeek(true)}
          aria-hidden="true"
        />
      )}
      {/* Explicit reveal affordance while collapsed (alongside hover-peek + ⌘B) — on every
          surface EXCEPT the session view, whose topbar carries the [sidebar][+][search] cluster
          instead (§22; no duplicate reveal buttons). */}
      {navCollapsed && !navPeek && surface !== "session" && (
        <button
          className="nav-reveal-btn"
          onClick={toggleNav}
          onMouseEnter={() => setNavPeek(true)}
          title="Show sidebar (⌘B)"
          aria-label="Show sidebar"
        >
          <Icon name="sidebar" size={16} />
        </button>
      )}
      {onboarding && (
        <Onboarding
          onDone={(next) => {
            setOnboarding(false);
            getHealth().then((h) => setModel(h.model)).catch(() => {});
            loadSettings(); // pick up a model connected during setup (clears the composer chip)
            if (next === "gallery") {
              // The specialists tip: land on Settings ▸ Personas, where the Gallery link lives.
              openSettings("personas");
            } else if (next === "automations") {
              // "Create your first automation" (§29) lands on the Automations quickstart.
              setSurface("scheduled");
            } else if (next === "work") {
              // "Start working" teaches by landing (§24, §32): a fresh session with the rail's
              // Access section expanded. Bump after the session switch settles.
              startNewSession();
              setTimeout(openAccess, 80);
            }
          }}
        />
      )}
      <Sidebar
        agent={agent}
        workspace={workspace || ""}
        surfaces={surfaces}
        sessions={sessions}
        projects={projects}
        activeSession={sessionId}
        onSwitchAgent={switchAgent}
        onNewSession={startNewSession}
        onSelectSession={selectSession}
        onNewProject={newProject}
        onRenameSession={renameConversation}
        onDeleteSession={deleteConversation}
        onArchiveSession={toggleArchived}
        onTogglePin={togglePinned}
        onManage={() => openSettings("appearance")}
        onOpenPersona={(id) => {
          openPersona(id, "session");
        }}
        onManagePersonas={() => openSettings("personas")}
        onOpenScheduled={() => setSurface("scheduled")}
        onOpenAutomation={(id) => {
          setScheduledOpenId(id);
          setSurface("scheduled");
        }}
        onOpenIntegrations={() => setSurface("integrations")}
        onOpenSkillStore={() => setSurface("skillstore")}
        onOpenAudit={() => setSurface("audit")}
        onOpenInbox={() => setSurface("inbox")}
        onOpenExperts={() => setSurface("experts")}
        onOpenRoundtable={() => setSurface("roundtable")}
        roundtableActive={surface === "roundtable"}
        onOpenCreation={() => setSurface("creation")}
        creationActive={surface === "creation"}
        onOpenProfile={() => setSurface("profile")}
        profileActive={surface === "profile"}
        onOpenMailbox={() => setSurface("mailbox")}
        mailboxActive={surface === "mailbox"}
        scheduledActive={surface === "scheduled"}
        integrationsActive={surface === "integrations"}
        skillStoreActive={surface === "skillstore"}
        auditActive={surface === "audit"}
        inboxActive={surface === "inbox"}
        expertsActive={surface === "experts"}
        collapsed={navCollapsed}
        onCollapse={toggleNav}
        onPeekLeave={() => setNavPeek(false)}
      />
      {surface === "scheduled" ? (
        <ScheduledView
          onOpenRun={openRunSession}
          onRunNow={runTaskNow}
          initialOpenId={scheduledOpenId}
        />
      ) : surface === "integrations" ? (
        <IntegrationsView />
      ) : surface === "skillstore" ? (
        <SkillStore />
      ) : surface === "settings" ? (
        <SettingsView
          key={settingsTab}
          initialTab={settingsTab}
          onOpenPersona={(id) => openPersona(id, "settings")}
        />
      ) : surface === "audit" ? (
        <AuditView />
      ) : surface === "inbox" ? (
        <InboxView onOpenSession={openSessionFromInbox} />
      ) : surface === "persona" ? (
        <PersonaView
          personaId={personaViewId || agent}
          onBack={() =>
            personaViewReturn === "settings" ? openSettings("personas") : setSurface("session")
          }
          onOpenIntegrations={() => setSurface("integrations")}
        />
      ) : surface === "experts" ? (
        <ExpertsCenter
          experts={EXPERTS}
          activeExpertId={currentExpert?.id ?? null}
          onSummon={summonExpert}
          onBack={() => setSurface("session")}
        />
      ) : surface === "roundtable" ? (
        <div className="roundtable-view">
          <iframe src={`${httpBase()}/roundtable/`} title="创作圆桌" />
        </div>
      ) : surface === "creation" ? (
        <CreationChatView onBack={() => setSurface("session")} />
      ) : surface === "teamchat" ? (
        <TeamChatView teamId={teamId} onClose={() => setSurface("creation")} />
      ) : surface === "badges" ? (
        <BadgeWall onBack={() => setSurface("session")} />
      ) : surface === "profile" ? (
        <GardenProfileCenter onBack={() => setSurface("session")} />
      ) : surface === "mailbox" ? (
        <MailboxPage
          onBack={() => setSurface("profile")}
          onOpenChat={(letter) => openChatFromMailbox(letter)}
        />
      ) : (
      <div className={"main" + (surface === "session" && agent !== "chat" && !railHidden ? " rail-open" : "")}>
        <div className="main-topbar">
          {/* Left: the contextual cluster — [sidebar] [+ new session] [search] — rendered ONLY
              while the sidebar is collapsed (§22; the expanded sidebar already owns those
              actions). Clicks must not start a window drag. */}
          <div className="main-topbar-side" onPointerDown={beginWindowDrag}>
            {navCollapsed && (
              <div
                className="flex items-center gap-1"
                data-testid="topbar-cluster"
                onPointerDown={(e) => e.stopPropagation()}
              >
                <button
                  className="topbar-icon-btn"
                  onClick={toggleNav}
                  aria-label="Show sidebar"
                  title="Show sidebar (⌘B)"
                >
                  <Icon name="sidebar" size={16} />
                </button>
                <button
                  className="topbar-icon-btn"
                  onClick={() => startNewSession()}
                  aria-label="New session"
                  title="New session"
                >
                  <Icon name="plus" size={16} />
                </button>
                <button
                  className="topbar-icon-btn"
                  onClick={() => setSearchOpen(true)}
                  aria-label="Search"
                  title="Search"
                >
                  <Icon name="search" size={16} />
                </button>
              </div>
            )}
            {/* §32: no session-settings row up here anymore — the §23 rest/hover/click glance
                machinery retired with the drawer. "What can this touch" lives permanently on
                the rail's Access section header; the panel toggle is the one entry. */}
          </div>
          {/* Center: title + facts subtitle (§22, amended: the ⋯ menu removed — the nav row's
              hover cluster owns pin/rename/archive/delete). The title stays: with the sidebar
              collapsed it is the only session identifier, and it anchors the subtitle. */}
          <div className="main-title" onPointerDown={beginWindowDrag}>
            <span
              className={"main-title-text" + (activeInfo ? "" : " title-ghost")}
              title={activeTitle}
            >
              {activeTitle}
            </span>
            {/* Plain facts, no affordance: the persona page it used to open is hidden for
                this release (owner ask 2026-07-22). */}
            {hasHistory && (
              <span className="title-sub" data-testid="session-subtitle">
                {subtitleParts.join(" · ")}
              </span>
            )}
          </div>
          {/* Right: session-settings icon (§23) + panel toggle. Model/mode/persona chrome is
              gone — the facts live in the subtitle, the controls in the composer (§22). */}
          <div className="main-topbar-side main-topbar-actions" onPointerDown={beginWindowDrag}>
            {agent === "cowork" && railHidden && artifactCount > 0 && (
              <button
                className="topbar-artifacts-btn"
                onMouseDown={(e) => e.stopPropagation()}
                onClick={() => setRailHidden(false)}
                title="Show files this conversation produced"
              >
                <Icon name="file" size={14} />
                <span>Artifacts</span>
                <span className="topbar-artifacts-count">{artifactCount}</span>
              </button>
            )}
            {/* §32: the panel toggle is the ONE session-panel entry, for every non-chat persona
                (the rail now carries Access, so code-family gets it too). */}
            {agent !== "chat" && (
              <button
                className="topbar-icon-btn"
                onMouseDown={(e) => e.stopPropagation()}
                onClick={() => setRailHidden((h) => !h)}
                aria-label={railHidden ? "Show side panel" : "Hide side panel"}
                title={railHidden ? "Show side panel" : "Hide side panel"}
              >
                <Icon name="sidebarRight" size={16} />
              </button>
            )}
          </div>
        </div>
        <div className={"main-workspace" + (railHidden ? " rail-hidden" : "")}>
          <div className="main-chat">
            {/* Automation-run context (owner ask 2026-07-04): a __run__ session looked like any
                other chat with no way back to the runs list. Lives INSIDE the chat column (which
                is padded to clear the absolute glass topbar — rendering above .main-workspace put
                it underneath the topbar; owner-reported CSS bug). */}
            {sessionId.startsWith("__run__") && (
              <div
                className="flex items-center gap-2 px-4 py-2 mb-1 rounded-lg text-[12.5px] border border-line bg-accentSoft/40"
                data-testid="run-banner"
              >
                <Icon name="clock" size={14} className="text-accent shrink-0" />
                <span className="truncate text-muted">
                  Scheduled run
                  {runContext?.title ? (
                    <>
                      {" — "}
                      <span className="text-ink font-medium">{runContext.title}</span>
                    </>
                  ) : null}{" "}
                  · started by an automation
                </span>
                <button
                  className="ml-auto shrink-0 text-accent font-medium hover:underline"
                  onClick={() => {
                    if (runContext) setScheduledOpenId(runContext.id);
                    setSurface("scheduled");
                  }}
                >
                  ← Back to runs
                </button>
              </div>
            )}
            <div className="main-scroll" ref={scrollRef} onScroll={handleScroll}>
              {idle ? (
                currentExpert ? (
                  <div className="expert-intro">
                    <ExpertAvatar expert={currentExpert} size={72} />
                    <h1 className="expert-intro-name">{currentExpert.displayName}</h1>
                    <p className="expert-intro-desc">{currentExpert.description}</p>
                    <div className="expert-intro-hint">
                      {currentExpert.manuscriptReview
                        ? "点右上角「提交书稿」即可开始深度审阅；选中章节后，审阅会在对话中分批完成。"
                        : "在下方输入框描述你的需求，开始与专家协作。"}
                    </div>
                  </div>
                ) : expertPersona ? (
                  // 专家 persona 会话的空态名片：头像 + 名字 + 一句话定位，让「点谁跟谁聊」
                  // 立刻可见（此前落到通用 hero「Let's build something.」，看起来像默认模式）。
                  <div className="expert-intro">
                    <img
                      className="persona-intro-avatar"
                      src={PERSONA_AVATAR[agent] || ""}
                      alt={expertPersona.name}
                    />
                    <h1 className="expert-intro-name">
                      {shortPersonaName(expertPersona.name, expertPersona.id)}
                    </h1>
                    <p className="expert-intro-desc">{expertPersona.tagline}</p>
                    <div className="expert-intro-hint">
                      在下方输入框直接开聊；需要其他视角时 @名字 可临时请专家补位。
                    </div>
                  </div>
                ) : agent === "cowork" ? (
                  <SessionIntro
                    sessionId={sessionId}
                    onOpenSessionSettings={openAccess}
                    onPrefill={prefillComposer}
                  />
                ) : agent === "deskmate" ? (
                  <DeskHome onIntentExecuted={startIntentDiscussion} />
                ) : (
                  <div className="hero">
                    <h1 className="greeting">
                      <span className="mark">✦</span>
                      {agent === "chat" ? "How can I help?" : "Let's build something."}
                    </h1>
                    {needsWorkspace(agent) && (
                      <div className="suggestions">
                        <div className="suggest-head">Try a task</div>
                        {SUGGESTIONS.map((s, i) => (
                          <div className="suggest" key={i} onClick={() => workspace && send(s.text)}>
                            <span className="ico">{s.ico}</span>
                            {s.text}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )
              ) : (
                <>
                  <Transcript
                    items={items}
                    onApprove={approve}
                    running={running}
                    onRetry={retry}
                    // §33 ref #3: sub-threshold streamed text renders INSIDE the live turn
                    // group (header when collapsed, quiet line when expanded) — never as a
                    // floating paragraph.
                    streamingText={streamMode(streaming, items, running) === "quiet" ? streaming : undefined}
                    expert={currentExpert}
                    onFinalizeIntent={finalizeIntentDiscussion}
                    onReviseIntent={reviseIntentDiscussion}
                    onCancelIntent={cancelIntentDiscussion}
                  />
                  {/* Live thinking (reasoning models): a quiet collapsed block that streams the
                      trace for anyone who expands it; folds into the answer's disclosure when
                      the message finalizes. */}
                  {running && reasoningStream && !streaming && (
                    <div className="transcript">
                      <ThinkingBlock text={reasoningStream} live />
                    </div>
                  )}
                  {/* Compaction runs between provider turns (nothing streams during it), so
                      the transient takes over the waiting slot with a specific label. */}
                  {running && compacting && <WaitingForAgent label="Compacting context…" />}
                  {running &&
                    !compacting &&
                    !reasoningStream &&
                    (!streaming || streamMode(streaming, items, running) === "hold") &&
                    !lastItemIsAssistant(items) && <WaitingForAgent />}
                  {streaming && streamMode(streaming, items, running) === "answer" && (
                    <div className="transcript">
                      <div className="bubble-assistant">
                        <div className="who">
                          <XiaojuAvatar className="who-avatar" />
                          <span>{currentExpert?.displayName ?? t("chat.assistant")}</span>
                        </div>
                        <Markdown text={streaming} />
                        <span className="stream-cursor">▍</span>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>

            {/* Scrolled up while the transcript is still growing → offer the way back down.
                Zero-height strip keeps the pill floating over the scroll area, above the
                composer, without reserving layout space. */}
            {!following && (running || !!streaming) && (
              <div className="relative h-0 z-10">
                <button
                  className="absolute bottom-3 left-1/2 -translate-x-1/2 flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-line bg-panel shadow-md text-[12px] text-muted hover:text-ink cursor-pointer whitespace-nowrap"
                  data-testid="jump-to-latest"
                  onClick={followLatest}
                >
                  <Icon name="chevronDown" size={13} />
                  Jump to latest
                </button>
              </div>
            )}

            {/* 活跃会话相关性注入（§11.4② 触发入口，本轮新增）：仅 deskmate 且非 idle 时挂载，
                把最近对话作上下文，聊到某主题才递对应待办（非浮层、轻提示卡片）。
                「聊这个」（§11.16 层三）＝让她说：onSpeak 走 speak 端点，她的消息落消息流。 */}
            {!idle && agent === "deskmate" && (
              <DeskMateProactiveInline items={items} onSpeak={speakHandoff} />
            )}

            <Composer
              mode={mode}
              model={model}
              models={models}
              modelLabels={modelLabels}
              modelTiers={modelTiers}
              modelVendors={modelVendors}
              running={running}
              connected={connected}
              modelReady={modelReady}
              onConnectModel={openModelSetup}
              onConfigureVoiceInput={() => openSettings("voice")}
              onSend={send}
              onInterrupt={interrupt}
              onModeChange={changeMode}
              onModelChange={changeModel}
              // 桌搭模式同样暴露权限选择菜单（讨论 / 需授权 / 完全访问）：它也会跑
              // run_shell 等需审批工具，但需要「完全访问」时不应被一步步点授权卡住。
              // workspace 本身仅为触发 ModeMenu 渲染（空串即视为已定义），桌搭无目录工作区。
              workspace={needsWorkspace(agent) || agent === "deskmate" ? (workspace || "") : undefined}
              unattended={unattended}
              onUnattendedChange={agent !== "chat" ? toggleUnattended : undefined}
              expert={currentExpert}
              onExitExpert={() => {
                setCurrentExpert(null);
                // 退出专家模式时切回桌搭
                if (agent !== "deskmate") startNewSessionRef.current("deskmate");
              }}
              onOpenReportDialog={
                currentExpert?.manuscriptReview
                  ? () => setReportDialogOpen(true)
                  : undefined
              }
              prefill={composerPrefill}
              resetKey={sessionId}
              usage={usage}
              contextWindow={modelContextWindows[model]}
              contextBar={contextBar}
              placeholder={
                agent === "code"
                  ? "Ask the coder to build, fix, or explain…  (drop or paste files)"
                  : agent === "chat"
                    ? "Ask anything…  (drop or paste files)"
                    : "Ask the coworker…  (drop or paste files)"
              }
              approvalSlot={
                // Live inline cards are for ATTENDED sessions only; when Unattended the prompt is
                // parked in the Inbox and surfaced via the answer-in-context card below.
                !unattended && pendingPlan?.kind === "planreq" ? (
                  <PlanCard item={pendingPlan} onRespond={respondPlan} />
                ) : !unattended && pendingDirReq?.kind === "dirreq" ? (
                  <DirectoryRequestCard item={pendingDirReq} onRespond={respondDirectory} />
                ) : !unattended && pendingApproval?.kind === "approval" ? (
                  <ApprovalCard item={pendingApproval} onApprove={approve} runTask={runContext} compact />
                ) : !unattended && pendingQuestion?.kind === "question" ? (
                  // Live ask_user in an attended session — answer inline (reuses the Inbox card UI).
                  <InboxItemCard
                    item={{
                      id: "live-question",
                      session_id: sessionId,
                      kind: "question",
                      title: pendingQuestion.question,
                      body: "",
                      state: "pending",
                      resolution: null,
                      inbox: "default",
                      created_at: "",
                      resolved_at: null,
                      options: pendingQuestion.options,
                      allow_text: pendingQuestion.allow_text,
                      multi: pendingQuestion.multi,
                    }}
                    onResolve={(_id, answer) => answerQuestion(answer)}
                    compact
                  />
                ) : sessionInbox[0] ? (
                  // Unattended session blocked on an Inbox item — answer it in context.
                  <InboxItemCard item={sessionInbox[0]} onResolve={resolveSessionInbox} compact />
                ) : undefined
              }
            />
                  </div>
          <RightRail
            active={surface === "session" && agent !== "chat" && !railHidden}
            sessionId={sessionId}
            refreshKey={browserRefreshKey}
            toolNames={items.filter((i) => i.kind === "tool").map((i: any) => i.name)}
            todo={todo}
            running={running}
            onPreviewChange={onArtifactPreview}
            showArtifacts={agent === "cowork"}
            personaId={agent}
            projectScoped={isProjectScoped(personaOf(agent))}
            workspace={workspace || undefined}
            branch={branch}
            scratchPrimary={agent === "cowork"}
            openAccessKey={accessKey}
            onOpenIntegrations={() => setSurface("integrations")}
            personas={personas ?? undefined}
            onOpenPersonaSession={(id) => startNewSession(id)}
          />
        </div>
      </div>
      )}

      {/* Search from the collapsed-sidebar topbar cluster (the sidebar's own instance is
          unreachable while it's collapsed). */}
      {searchOpen && (
        <SearchModal
          sessions={sessions}
          personas={personas ?? undefined}
          onSelect={(id, ws, ag) => {
            setSearchOpen(false);
            selectSession(id, ws, ag);
          }}
          onClose={() => setSearchOpen(false)}
        />
      )}

      {showGate && surface === "session" && gatesWorkspace(agent) && (
        <FolderGate
          create={gateCreate}
          onChoose={chooseWorkspace}
          onCancel={
            workspace
              ? () => {
                  setShowGate(false);
                  setGateCreate(false);
                }
              : undefined
          }
        />
      )}
      {workspaceTrustRequest && (
        <WorkspaceTrustPrompt
          request={workspaceTrustRequest}
          onClose={() => setWorkspaceTrustRequest(null)}
        />
      )}

      {/* DeskMate 浮动覆盖层（粒子角色 + 状态面板 + 建议弹窗 + 记忆编辑器） */}
      <DeskMateOverlay agent={agent} sessionId={sessionId} idle={idle} activeExpert={currentExpert} hidden={surface === "profile"} />

      {reportDialogOpen && (
        <ReportDialog
          onClose={() => setReportDialogOpen(false)}
          onDone={(info) => {
            setReportDialogOpen(false);
            // 把文稿信息组装成自然语言指令发给 Agent Loop。
            // 已存书稿 / 本地路径只需书名或路径，Agent 的 load_manuscript_file 会按名/路径调出，
            // 不必把冗余 JSON 暴露给用户（已存书稿尤其如此，JSON 里的 name 与上文重复）。
            const parts: string[] = ["请审阅以下文稿："];
            if (info.source === "manuscript") {
              parts.push(`来源：已存书稿《${info.name}》`);
            } else if (info.source === "text") {
              parts.push(`来源：粘贴文本（${(info.text?.length ?? 0).toLocaleString()} 字）`);
            } else if (info.source === "file") {
              parts.push(`来源：上传文件 ${info.filename}`);
            } else {
              parts.push(`来源：本地路径 ${info.path}`);
            }
            if (info.chapter_indices && info.chapter_indices.length > 0) {
              const idxs = [...info.chapter_indices].sort((a, b) => a - b);
              const titles = info.chapters
                ?.filter((c) => idxs.includes(c.idx))
                .map((c) => `第${c.idx}章 ${c.title}`);
              parts.push(`章节（共 ${idxs.length} 章）：${titles?.join("、") ?? idxs.join(", ")}`);
              parts.push(`（审阅章节索引：${idxs.join(", ")}）`);
            }
            if (info.source === "text" && info.text) {
              // 粘贴文本：把全文附在末尾供 Agent 直接审阅（这是审阅所需内容，无法省略）。
              parts.push(`\n书稿全文如下，请基于以下内容审阅：\n${info.text}`);
            } else if (info.source === "file") {
              // 上传文件：以 JSON 透传 base64（Agent 的 load_manuscript_file 可解码），保持原机制。
              const payload: Record<string, unknown> = {
                source: "file",
                filename: info.filename,
                content_base64: info.content_base64,
                chapter_indices: info.chapter_indices,
              };
              parts.push(`\n\`\`\`json\n${JSON.stringify(payload, null, 2)}\n\`\`\``);
            }
            send(parts.join("\n"));
          }}
        />
      )}
    </div>
    </ErrorBoundary>
  );
}

function lastItemIsAssistant(items: Item[]): boolean {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind === "notice") continue;
    return item.kind === "assistant";
  }
  return false;
}

function WaitingForAgent({ label }: { label?: string }) {
  return (
    <div className="waiting-transcript">
      <div className="waiting-row" aria-live="polite">
        <span className="waiting-spinner" />
        <span>{label || "Waiting for agent..."}</span>
      </div>
    </div>
  );
}

function updateLastTool(
  items: Item[],
  name: string,
  status: string,
  preview?: string,
  hidden?: number,
  standingRule?: string,
): Item[] {
  const copy = [...items];
  for (let i = copy.length - 1; i >= 0; i--) {
    const it = copy[i];
    if (it.kind === "tool" && it.name === name && it.status === "…") {
      copy[i] = {
        ...it,
        status,
        preview,
        ...(hidden ? { hidden } : {}),
        ...(standingRule ? { standingRule } : {}),
      };
      break;
    }
  }
  return copy;
}

function resolveLastApproval(items: Item[], decision: ApprovalDecision): Item[] {
  const copy = [...items];
  for (let i = copy.length - 1; i >= 0; i--) {
    const it = copy[i];
    if (it.kind === "approval" && !it.resolved) {
      copy[i] = { ...it, resolved: decision };
      break;
    }
  }
  return copy;
}

function resolveLastDirReq(items: Item[], resolved: "granted" | "denied"): Item[] {
  const copy = [...items];
  for (let i = copy.length - 1; i >= 0; i--) {
    const it = copy[i];
    if (it.kind === "dirreq" && !it.resolved) {
      copy[i] = { ...it, resolved };
      break;
    }
  }
  return copy;
}

function resolveLastPlan(items: Item[], resolved: "approved" | "rejected"): Item[] {
  const copy = [...items];
  for (let i = copy.length - 1; i >= 0; i--) {
    const it = copy[i];
    if (it.kind === "planreq" && !it.resolved) {
      copy[i] = { ...it, resolved };
      break;
    }
  }
  return copy;
}

function resolveLastQuestion(items: Item[], answer: string): Item[] {
  const copy = [...items];
  for (let i = copy.length - 1; i >= 0; i--) {
    const it = copy[i];
    if (it.kind === "question" && !it.resolved) {
      copy[i] = { ...it, resolved: answer };
      break;
    }
  }
  return copy;
}
