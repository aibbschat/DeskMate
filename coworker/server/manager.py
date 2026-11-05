"""Session manager — owns engines (one per session), stores, and the provider.

Each session is bound to a workspace folder (Code requires one). Storage is a single DB
under a data dir (global for the real server, per-workspace for tests), so recents and
sessions span folders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ..agent import build_engine
from ..agents import get_agent
from ..connections import (
    PersonaConnectionStore,
    SessionConnectionStore,
    effective as effective_connections,
)
from ..inbox import InboxStore, args_preview
from ..inbox_routing import InboxRouting
from ..personas import PersonaRegistry
from ..personas.registry import set_registry as set_persona_registry
from ..selfwake import WakeStore
from ..mentions import MentionSessionStore
from ..subscriptions import ChannelBuffer, SubscriptionStore
from ..unrouted import UnroutedStore
from ..unattended import UnattendedRegistry
from ..audit import AuditStore
from ..config import load_config, workspace_allowed_commands
from ..conversations import ConversationStore, title_from
from ..engine import ApprovalOutcome, Approver, TurnEngine
from ..roots import RootDir
from ..workspace_trust import WorkspaceTrustStore
from ..automation import Schedule, ScheduledTask, Scheduler, TaskRun, TaskStore
from ..connectors import (
    Gateway,
    MessageSource,
    connect_connector,
    connector_list,
    disconnect_connector,
    experimental_enabled,
    load_settings,
    make_adapter,
    set_experimental_enabled,
    slack_split,
    update_connector_tools,
)
from ..connectors.browser_automation import (
    browser_close_session,
    browser_state,
    browser_take_screenshot,
)
from ..connectors.parked import ParkedStore
from ..mcp import (
    MCPManager,
    build_callables,
    delete_global_server,
    load_mcp_servers,
    patch_global_server,
    put_global_server,
    read_global,
)
from ..memory import MemorySettingsStore, MemoryStore, Scope, SQLiteMemoryStore
from ..permissions import EXECUTION_LEVELS, Mode
from ..agents import list_agents as _list_agents
from ..providers import (
    ProviderClient,
    ProviderRouter,
    descriptor_configured,
    get_descriptor,
    provider_descriptors,
    verify_provider_key,
)
from ..secrets import SecretStore, state_dir
from ..sessions import SessionRecord
from ..teams import Actor as TeamActor
from ..teams import BoardError as TeamsBoardError
from ..teams import JournalStore, Role as TeamRole, TeamStore, board_tools, journal_tools
from ..teams.store import WORKER_WAITING
from ..teams.tools import decide_worker_call_tool, local_clock
from ..projects import (
    project_key,
    project_label,
    project_presence,
    resolve_board_space,
    resolve_memory_key,
)
from ..teams.chat import ChatStore
from ..teams.registry import TeamRegistry, TeamWorker
from ..teams.attachments import AttachmentStore
from ..teams.tokens import BoardTokens
from ..skills import (
    SessionSkillStore,
    SkillLoader,
    SkillStore,
    effective_skills,
)

_SCOPES = {s.value for s in Scope}

logger = logging.getLogger("coworker.manager")


def _grants_of(engine) -> dict[str, Any]:
    """The engine's session-scoped "Always allow" approvals, in persistable shape."""
    tools = sorted(getattr(engine.permissions, "session_allow_tools", None) or ())
    commands = sorted(getattr(engine.permissions, "session_allow_commands", None) or ())
    readonly = bool(getattr(engine.permissions, "session_readonly", False))
    out: dict[str, Any] = {}
    if tools or commands or readonly:
        out = {"tools": tools, "commands": commands}
        if readonly:
            out["readonly"] = True
    return out


def _grant_offered(outcome, request) -> bool:
    """Whether a persistent grant is legitimately offered for this tool — the server-side
    mirror of what the approval card actually renders (`ApprovalCard.tsx`).

    - ALWAYS_TOOL is tool-wide and argument-unbounded, so it is withheld from run_shell (the
      command-scoped grant is the narrower option), from save_skill (every skill proposal
      gets its own review), from anything that reaches off the machine — connectors and
      MCP tools alike, where "always allow send_message" would cover every future recipient —
      and from URL-carrying egress (§1.9): "always allow web_fetch" would cover every future
      destination, and the domain-scoped grant is the one the card offers. Fixed-destination
      egress (web_search: no url argument) keeps it — tool-wide IS provider-wide there.
    - ALWAYS_COMMAND only means anything for the shell tool.
    - ALWAYS_DOMAIN only means anything for a tool carrying a url.
    """
    from ..engine import ApprovalOutcome
    from ..risk import RiskClass, classify

    name = getattr(request, "tool_name", "")
    metadata = getattr(request, "metadata", None)
    args = getattr(request, "arguments", None) or {}
    risk = classify(name, metadata)

    if outcome is ApprovalOutcome.ALWAYS_COMMAND:
        return risk is RiskClass.EXEC
    if outcome is ApprovalOutcome.ALWAYS_DOMAIN:
        return risk is RiskClass.EGRESS and bool(args.get("url"))
    if outcome is ApprovalOutcome.ALWAYS_TOOL:
        if risk in (RiskClass.EXEC, RiskClass.EXTERNAL):
            return False
        if risk is RiskClass.EGRESS and args.get("url"):
            return False
        if getattr(metadata, "category", "") == "connector":
            return False
        return name != "save_skill"
    return True


def _approval_body(request) -> str:
    """Approval card body: the tool's reason (if any) plus a compact preview of its args, so a
    mirrored 'Run `write_file`?' shows the path/content rather than just the tool name.
    """
    reason = (getattr(request, "reason", "") or "").strip()
    preview = args_preview(getattr(request, "arguments", None))
    return "\n".join(p for p in (reason, preview) if p)


def _stable_error(error: str) -> str:
    """An error string with per-process noise removed, for change detection only:
    hex object addresses and long digit runs (pids, ports, timestamps) vary between
    identical failures across relaunches."""
    stable = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", error or "")
    return re.sub(r"\d{4,}", "N", stable)


class SessionManager:
    def __init__(
        self,
        *,
        workspace: Optional[str | Path] = None,  # default/seed workspace (e.g. --cwd)
        data_dir: Optional[str | Path] = None,
        model: str = "gpt-5.6-sol",
        mode: Mode = Mode.INTERACTIVE,
        provider: Optional[ProviderClient] = None,
    ) -> None:
        self.default_workspace = (
            str(Path(workspace).expanduser().resolve()) if workspace else None
        )
        self.model = model
        self.mode = mode
        self.provider = provider

        if data_dir is not None:
            base = Path(data_dir).expanduser()
        elif self.default_workspace is not None:
            base = Path(self.default_workspace) / ".coworker"
        else:
            base = state_dir()
        base.mkdir(parents=True, exist_ok=True)
        # 提前设置 _data_base，使下方 ProviderRouter 构造即可读取 prefs（dynamic_routing 开关）。
        self._data_base = base

        self.memory_store: MemoryStore = SQLiteMemoryStore(base / "coworker.db")
        # MEMORY-SPEC §4.3/§6: the on/off switch + the user's standing rules. Settings-
        # level, outside the memory table; read at engine build time.
        self.memory_settings = MemorySettingsStore(base / "memory-settings.json")
        # P0-1: 启动即把持久化记忆注入开关镜像到环境变量（默认开启）。
        self.memory_settings.apply_auto_inject_env()
        self.audit_store = AuditStore(base / "coworker.db")
        self.session_store = ConversationStore(base)
        self.session_store.canonicalize_workspaces()  # collapse /tmp vs /private/tmp etc.
        if self.default_workspace:
            self.session_store.touch_workspace(self.default_workspace)
        self._engines: dict[str, TurnEngine] = {}
        # Sessions whose workspace was promoted mid-turn (workspace-scratch-design.md §5):
        # evicted from the engine cache at the next mark_idle so the following turn
        # rebuilds fully anchored on the new workspace.
        self._promotion_rebuild: set[str] = set()
        self._running_sessions: set[str] = (
            set()
        )  # sessions with an in-flight turn (busy)
        # Sessions with an auto-title LLM call in flight (FB-010) — one call at a time.
        self._autotitle_inflight: set[str] = set()
        self._autotitle_tasks: set[asyncio.Task] = set()
        self._autotitle_attempts: dict[str, int] = {}
        # Opener-count signature of the last attempt: titling fires at TURN START (owner
        # catch 2026-08-24 — waiting for an agentic turn to COMPLETE left sessions
        # untitled for however long the scan ran), and the completion hook still covers
        # background turns; this guard keeps the two trigger points from burning
        # duplicate attempts on the same openers.
        self._autotitle_sig: dict[str, int] = {}
        self.workspace_trust = WorkspaceTrustStore()
        self.secrets = SecretStore()
        # No explicit provider injected → route by the model's `provider:` prefix (OpenAI default,
        # Ollama, …). Tests inject a provider directly and bypass the router. The same router is
        # shared by every engine and the `/v1/chat/completions` proxy.
        if self.provider is None:
            self.provider = ProviderRouter(
                self.secrets,
                default_provider="openai",
                on_use=self._note_provider_use,
                dynamic_routing=bool(self._load_prefs().get("dynamic_routing", False)),
            )
        self.mcp = MCPManager(secrets=self.secrets)
        # OAuth MCP servers with a sign-in in flight / their last connect error —
        # feeds list_mcp's status so the GUI can show "authorizing…" and failures.
        self._mcp_authorizing: set[str] = set()
        self._mcp_errors: dict[str, str] = {}
        # ChatGPT-subscription provider sign-in in flight / its last error — feeds
        # the providers list + status route so the GUI can show "authorizing…".
        self._codex_authorizing = False
        self._codex_error: Optional[str] = None
        # http servers whose anonymous connect came back 401/403 — the failure is
        # "needs sign-in", so the GUI offers the OAuth switch instead of a raw error.
        self._mcp_auth_hints: set[str] = set()
        # Servers that failed to connect while preparing a session's tools —
        # drained once by the WS handler to append a transcript notice.
        self._mcp_session_failures: dict[str, list[str]] = {}
        self.gateway: Optional[Gateway] = None
        self._data_base = base
        # Desktop/UI prefs (default model, onboarding state) — not secrets; a plain JSON file.
        self._prefs = self._load_prefs()
        if self._prefs.get("default_model"):
            self.model = self._prefs["default_model"]
        # Seed the PDF-fallback module global from prefs so engines see the user's
        # choice from the first turn (set_pdf_settings keeps it in sync after).
        from ..pdf_support import set_fallback_mode

        set_fallback_mode(self.pdf_settings()["pdf_fallback"])
        # Per-session live-view registry: every socket open on a session id gets the turn's events,
        # whoever drives the turn (foreground user_message, channel delivery, self-wake, resume).
        # Delivery itself is socket-independent — this only governs *live visibility*.
        self._session_clients: dict[str, set[Any]] = {}
        # App-wide event sockets (/ws/events): session-independent pushes — today the
        # automation-run-started toast (UX-026); badges could ride it later.
        self._event_clients: set[Any] = set()
        # Automation: scheduled tasks store + the tick scheduler (started in the lifespan).
        # The scheduler also resumes self-wake'd sessions each tick (extra_tick).
        self.task_store = TaskStore(base / "automation.db")
        self.scheduler = Scheduler(
            self.task_store,
            self._run_scheduled_task,
            extra_tick=self._combined_extra_tick,
        )
        # Cached DeskMate proactive engine (created once, reused across ticks).
        # Without this, _combined_extra_tick would construct a new ProactiveEngine
        # every 30s, discarding DisturbManager state (consecutive-ignore counters,
        # accepted/sent history) and wasting ~50ms on init + module imports.
        self._deskmate_proactive_engine = None
        # Agent teams (官方 Teams/Triage Lead 框架, v0.2.0): 两个 append-only stores +
        # 一个 record discipline。journal 按 case 键（知识长于 board/team），board log 按
        # space 作用域，assignment 喂 journal-case grants。verbs 在 persona 的 team: trait 后
        # 按 session 注册；registry 持有 roster（lead/worker session per board），wake plumbing 遍历之。
        self.journal_store = JournalStore(base / "journal.db")
        self.team_store = TeamStore(base / "teams.db", journal=self.journal_store)
        self.chat_store = ChatStore(base / "chat.db")
        self.teams = TeamRegistry(base / "teams.json")
        # Self-heal on start: a listed member whose session record is gone is a ghost —
        # `assign` still validated it, the wake went to a session with no engine, and the
        # dispatched work silently evaporated (2026-10-31: two rounds lost). Rosters
        # orphaned before detach_session existed are cleaned up here instead of needing a
        # hand-edit of teams.json. Never fatal: a probe problem must not block startup.
        try:
            for gone in self.teams.prune_missing_sessions(
                lambda sid: self.session_store.load(sid) is not None
            ):
                logger.warning(
                    "pruned ghost team member: %s '%s' (session %s) from team %s%s",
                    gone["role"],
                    gone["actor"],
                    gone["session_id"],
                    gone["team_id"],
                    " — team dropped" if gone["team_removed"] else "",
                )
        except Exception:  # noqa: BLE001
            logger.exception("team roster prune failed")
        # External board clients (OPE-100): join tokens bind actor+role; the
        # `/v1/board` API resolves them and the store enforces authority.
        self.board_tokens = BoardTokens(base / "board-tokens.json")
        # Work-item attachments (OPE-105): content-addressed blobs next to the
        # board; the log carries only `attachment://` refs.
        self.attachment_store = AttachmentStore(base / "attachments")
        self._team_inflight: set[str] = set()
        # Lead-side coalescing (upstream TEAM_BATCH_SECONDS): quiet rows wait for a
        # debounce window so ten comments arriving together are one model turn.
        self._team_batch_deadlines: dict[str, float] = {}
        self._team_batch_handles: dict[str, Any] = {}
        # Activity receipts already folded into a persisted incoming turn — their
        # wake ids and board cursors may now be acknowledged.
        self._activity_acknowledged: set[str] = set()
        # Lead-session last-turn timestamps for the check-in backstop (monotonic-ish
        # wall clock; restart resets the clock rather than firing a wake storm).
        self._team_last_alive: dict[str, float] = {}
        # Team watchdog (synced from upstream OpenWorker, PR #792): the lead is alerted
        # once per *distinct* stalled state so it isn't woken every tick; the stale
        # state is the tuple returned by _stalled_team_state.
        self._team_watchdog_alerted: dict[str, tuple] = {}
        # Stalled-worker auto-revive re-ping counter, keyed by worker session id. A
        # stalled in_progress worker is nudged back to work up to the cap, then
        # auto-blocked for the lead to take over (prevents silent multi-hour idles).
        self._team_reping: dict[str, int] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None        # Personas: registry + lifecycle state under this manager's data dir. Installed as the
        # process singleton so agents.get_agent resolves persona ids (incl. third-party) here.
        self.personas = PersonaRegistry(state_path=base / "personas.json")
        set_persona_registry(self.personas)
        # Inbox (cross-session human-attention queue), routing (named inboxes + Slack/Telegram
        # bindings), the Unattended toggle, and self-wake records.
        self.inbox = InboxStore(base / "inbox.json")
        self.inbox_routing = InboxRouting(base / "inbox_routing.json")
        self.unattended = UnattendedRegistry(base / "unattended.json")
        self.wakes = WakeStore(base / "wakes.json")
        # Channel subscriptions (inbound): persisted (session_id, channel) records + a ring buffer
        # of recently-seen channel messages for get_channel_messages.
        self.subscriptions = SubscriptionStore(base / "subscriptions.json")
        self.channel_buffer = ChannelBuffer(state_path=base / "channels.json")
        # Mention router (§31): thread target → the session that owns that Slack thread.
        # Also the durable source of the thread's standing send_message grant (re-seeded
        # onto the engine in get_engine).
        self.mention_sessions = MentionSessionStore(base / "mention_threads.json")
        # Unauthorized inbound messages, parked instead of dropped (one-step allow-and-deliver).
        self.parked = ParkedStore(base / "parked.json")
        # People directory: "platform:user_id" → display name, noted from every inbound
        # (authorized or parked) so allow-list chips read "Rohit Prsad", not "U07JK…".
        self._people_path = base / "people.json"
        try:
            self._people: dict[str, str] = json.loads(self._people_path.read_text())
        except (OSError, ValueError):
            self._people = {}
        # Seed from already-parked messages (they carry resolved names) so an allow made from
        # an old parked item still gets a named chip.
        for it in self.parked.list():
            if it.get("user_name"):
                self._people.setdefault(
                    f"{it['platform']}:{it['user_id']}", it["user_name"]
                )
        # Connection hierarchy (UI-REFRESH §4): per-persona default connector on/off (seeded from the
        # manifest, then user-editable) + per-session overrides. Resolved into the session's effective
        # connector set, which gates inbound delivery and the engine's connector tools.
        self.persona_connections = PersonaConnectionStore(
            base / "persona_connections.json"
        )
        self.session_connections = SessionConnectionStore(
            base / "session_connections.json"
        )
        # Skills (SKILLS-SPEC §4): folder-backed CRUD + per-session mutes. The effective menu
        # gates the engine's skill catalog the same way effective_connectors gates connector
        # tools — one resolver feeds the catalog injection, the rail, and the composer popup.
        self.skill_store = SkillStore()
        self.session_skills = SessionSkillStore(base / "session_skills.json")
        # Dead-letter: inbound messages with no destination + background-turn failures, so neither
        # vanishes silently (a debugging/visibility surface, not a redelivery queue).
        self.unrouted = UnroutedStore(base / "unrouted.json")
        # 主动闭环执行器（Task2）：注册后，deskmate after_action 收尾时识别到的关联未竟
        # 会以一次性 AUTO 任务落地执行（复用权限引擎的路径作用域 + CRITICAL 拒杀）。
        try:
            from deskmate.proactivity.executor import register_close_loop_executor

            register_close_loop_executor(self._submit_close_loop_task)
        except Exception as _e:
            import logging as _lg

            _lg.getLogger(__name__).warning("注册主动闭环执行器失败：%s", _e)

    # -- workspaces -------------------------------------------------------------
    def open_workspace(self, path: str, *, create: bool = False) -> dict[str, Any]:
        resolved = Path(path).expanduser()
        if resolved.exists() and not resolved.is_dir():
            return {"path": str(resolved), "ok": False, "error": "not a directory"}
        if not resolved.exists():
            if not create:
                return {
                    "path": str(resolved),
                    "ok": False,
                    "error": "folder does not exist",
                }
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return {"path": str(resolved), "ok": False, "error": str(exc)}
        resolved = resolved.resolve()
        self.session_store.touch_workspace(str(resolved))
        return {
            "path": str(resolved),
            "ok": True,
            "git_branch": _git_branch(resolved),
            "command_trust": self.workspace_command_trust(resolved),
        }

    def workspace_command_trust(self, path: str | Path) -> dict[str, Any]:
        if not str(path).strip():
            return {
                "workspace": "",
                "requested_commands": [],
                "trusted": False,
                "required": False,
            }
        canonical = WorkspaceTrustStore.canonical(path)
        commands = (
            workspace_allowed_commands(canonical)
            if Path(canonical).is_dir()
            else []
        )
        trusted = self.workspace_trust.is_trusted(canonical)
        return {
            "workspace": canonical,
            "requested_commands": commands,
            "trusted": trusted,
            "required": bool(commands and not trusted),
        }

    def _mcp_workspace_trusted(self, workspace: Optional[str | Path]) -> bool:
        """Whether workspace `.coworker/mcp.json` may be loaded (#213).

        Same consent boundary as repository ``allowed_commands``: an untrusted
        clone must not define stdio processes that spawn at session open.
        """
        return bool(workspace and self.workspace_trust.is_trusted(workspace))

    def set_workspace_trust(
        self, path: str | Path, *, trusted: bool
    ) -> dict[str, Any]:
        if not str(path).strip():
            return {"ok": False, "error": "workspace path is required"}
        candidate = Path(path).expanduser()
        if trusted and not candidate.is_dir():
            return {"ok": False, "error": "workspace is not a directory"}
        canonical = self.workspace_trust.set_trusted(candidate, trusted)
        effective = load_config(
            canonical, workspace_trusted=trusted
        ).allowed_commands
        # Apply trust/revocation immediately to live sessions rooted at this exact path.
        for engine in self._engines.values():
            engine_workspace = str(
                (getattr(engine, "audit_context", {}) or {}).get("workspace", "")
            )
            if engine_workspace and WorkspaceTrustStore.canonical(
                engine_workspace
            ) == canonical:
                engine.permissions.allowed_commands = list(effective)
        return {
            "ok": True,
            **self.workspace_command_trust(canonical),
        }

    def trusted_workspaces(self) -> list[dict[str, Any]]:
        return [
            {
                **self.workspace_command_trust(path),
                "exists": Path(path).is_dir(),
            }
            for path in self.workspace_trust.list()
        ]

    def recent_workspaces(self) -> list[dict[str, Any]]:
        """Recent real projects for the folder gate. Per-conversation scratch dirs are
        excluded — they're workspaces to the session store, but never something a user
        should re-open as a 'project'."""
        scratch = self.scratch_base().resolve()
        out = []
        for path in self.session_store.recent_workspaces():
            p = Path(path)
            try:
                if p.resolve().is_relative_to(scratch):
                    continue
            except OSError:
                pass
            out.append({"path": path, "name": p.name, "exists": p.is_dir()})
        return out

    DEFAULT_SCRATCH_BASE = "~/OpenWorker"

    def scratch_base(self) -> Path:
        """Common area for per-conversation scratch directories. Configurable via prefs;
        the env override keeps tests (and any sandboxed run) out of the real home dir —
        universal scratch means every session provisions here, not just orphan ones."""
        base = (
            self._prefs.get("scratch_base")
            or os.environ.get("COWORKER_SCRATCH_BASE")
            or self.DEFAULT_SCRATCH_BASE
        )
        return Path(base).expanduser()

    def _provision_scratch(self, session_id: str) -> str:
        """Create (idempotently) and return this conversation's scratch directory."""
        d = self.scratch_base() / session_id
        d.mkdir(parents=True, exist_ok=True)
        return str(d.resolve())

    _SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

    def is_temp_workspace(self, path: Optional[str]) -> bool:
        """True when `path` is a per-conversation temporary directory (lives under the
        scratch base). The GUI uses this to label the folder "Temporary folder" instead
        of exposing its raw path."""
        if not path:
            return False
        try:
            return (
                Path(path).expanduser().resolve().is_relative_to(self.scratch_base().resolve())
            )
        except OSError:
            return False

    def provision_temp_workspace(self, session_id: str, *, git: bool = True) -> dict[str, Any]:
        """UX-029 "Start in a temporary folder": create the conversation's temporary
        directory at SEND time (not connect) and, for code-family work, make git ready.
        Idempotent — re-sending against an existing dir is a no-op."""
        if not self._SESSION_ID_RE.match(session_id or "") or session_id in {".", ".."}:
            return {"ok": False, "error": "invalid session id"}
        path = self._provision_scratch(session_id)
        if git and not (Path(path) / ".git").is_dir():
            try:
                subprocess.run(
                    ["git", "init", "-q"],
                    cwd=path,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # no git on PATH → still a usable folder, just not a repo
        return {"ok": True, "path": path, "git": (Path(path) / ".git").is_dir()}

    def save_temp_as_project(self, session_id: str, dest: str) -> dict[str, Any]:
        """UX-029 "Save as project…": move a session's temporary folder to a real
        location and rebind the session there. The cached engine is dropped so the next
        connect rebuilds against the new path — callers must reconnect after this."""
        if not dest or not dest.strip():
            return {"ok": False, "error": "no destination folder"}
        record = self.session_store.load(session_id)
        src = record.workspace if record and record.workspace else None
        if not src:
            engine = self._engines.get(session_id)
            executor = getattr(engine, "executor", None) if engine else None
            src = str(executor.cwd) if executor else None
        if not src or not self.is_temp_workspace(src) or not Path(src).is_dir():
            return {"ok": False, "error": "this session is not in a temporary folder"}
        if self.is_running(session_id):
            return {"ok": False, "error": "wait for the current task to finish first"}
        d = Path(dest).expanduser()
        if d.exists():
            if not d.is_dir() or any(d.iterdir()):
                return {"ok": False, "error": "destination must be a new or empty folder"}
            d.rmdir()  # shutil.move into an existing dir would nest src inside it
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, str(d))
        except OSError as e:
            return {"ok": False, "error": f"could not move the folder: {e}"}
        new_path = str(d.resolve())
        if record:
            record.workspace = new_path
            self.session_store.save(record)
        self._engines.pop(session_id, None)
        self.session_store.touch_workspace(new_path)
        return {"ok": True, "path": new_path}

    def resolve_workspace(self, requested: Optional[str]) -> Optional[str]:
        if requested:
            p = Path(requested).expanduser()
            if p.is_dir():
                return str(p.resolve())
            return None
        return self.default_workspace

    # -- engines ----------------------------------------------------------------
    def engine_workspace(
        self, session_id: str, *, workspace: Optional[str] = None, agent: str = "code"
    ) -> Optional[str]:
        """The workspace `get_engine` would bind — for prepping MCP tools beforehand."""
        record = self.session_store.load(session_id)
        if record:
            return record.workspace or None
        return self.resolve_workspace(workspace)

    def get_engine(
        self,
        session_id: str,
        *,
        workspace: Optional[str] = None,
        agent: str = "code",
        approver: Optional[Approver] = None,
        extra_tools: Optional[list[Any]] = None,
        directory_requester: Optional[Any] = None,
        plan_approver: Optional[Any] = None,
        question_asker: Optional[Any] = None,
        tool_requester: Optional[Any] = None,
        team_approver: Optional[Any] = None,
        items_approver: Optional[Any] = None,
    ) -> Optional[TurnEngine]:
        engine = self._engines.get(session_id)
        if engine is not None:
            if approver is not None:
                engine.approver = approver
            if directory_requester is not None:
                engine.directory_requester = directory_requester
            if plan_approver is not None:
                engine.plan_approver = plan_approver
            if question_asker is not None:
                engine.question_asker = question_asker
            if tool_requester is not None:
                engine.tool_requester = tool_requester
            if team_approver is not None:
                engine.team_approver = team_approver
            if items_approver is not None:
                engine.items_approver = items_approver
            return engine

        # Resolve the owning automation task (if this session is a run of one) up front, so the
        # writable-root set below can fold in its deliverable folders (Desktop / Documents /
        # Downloads + declared write targets) — the same set _build_task_engine grants the AUTO
        # scheduled path. Without this, a manual "Run now" session only had the workspace as a
        # writable root and could not drop its article on the Desktop.
        owning_task = self.task_store.task_for_run_session(session_id)
        record = self.session_store.load(session_id)
        is_new_session = record is None
        agent_name = (record.agent if record else agent) or "code"
        ag = get_agent(agent_name)

        if record:
            ws = record.workspace or None
            model, mode, messages = record.model, Mode(record.mode), record.messages
        else:
            ws = self.resolve_workspace(workspace)
            model, mode, messages = self.model, self.mode, None

        if not ws or not Path(ws).is_dir():
            # Sessions without a folder start "orphan": auto-provision a per-conversation
            # scratch directory (generalizes MyHelper's auto-workspace). Folder-gated
            # personas (requires_folder) still demand a real directory picked by the user.
            if not ag.requires_folder:
                ws = self._provision_scratch(session_id)
            else:
                return None

        if ws:
            self.session_store.touch_workspace(ws)
        # Universal scratch (workspace-scratch-design.md §4): EVERY session is multi-root
        # with a per-conversation scratch dir. Orphan sessions run ON their scratch
        # (ws == scratch, primary). Sessions on a real folder — gated personas, or a
        # temp-workspace pick that later became a project — keep that folder primary and
        # gain scratch as a second writable root, so deliverables/temp files have a home
        # that never dirties the user's repo. request_directory rides on roots, so it now
        # registers everywhere.
        roots = None
        if ws:
            extra = [
                r
                for r in ((record.extra_roots if record else []) or [])
                if Path(str(r.get("path", ""))).is_dir()
            ]
            if self.is_temp_workspace(ws):
                roots = [{"path": ws, "writable": True, "label": "scratch"}, *extra]
            elif self._SESSION_ID_RE.match(session_id or "") and session_id not in {".", ".."}:
                roots = [
                    {"path": ws, "writable": True, "label": "workspace"},
                    {
                        "path": self._provision_scratch(session_id),
                        "writable": True,
                        "label": "scratch",
                    },
                    *extra,
                ]
            else:
                roots = [{"path": ws, "writable": True, "label": "workspace"}, *extra]
            team = (record.team if record else {}) or {}
            if team.get("role") == "worker":
                # Shared team filesystem, with a worker-owned directory for worktrees.
                # Provisioned FROM THE LEAD'S ID, not from its session record: a creation
                # pipeline leads through a synthetic `creation:<pid>:lead` key that has no
                # record, and skipping the root there meant the worker could never hand a
                # file to its lead — the write was rejected by root scoping before the
                # worker lane ever ran. The directory is the same one the lane approves.
                lead_sid = str(team.get("lead_session") or "")
                if lead_sid and "/" not in lead_sid and lead_sid not in {".", ".."}:
                    for path, label in (
                        (self._provision_scratch(lead_sid), "team scratch"),
                        (self._provision_scratch(session_id), "worker scratch"),
                    ):
                        if not any(
                            Path(r["path"]).resolve() == Path(path).resolve() for r in roots
                        ):
                            roots.append({"path": path, "writable": True, "label": label})
        # A task-run session (manual "Run now" over WS, or durable resume) folds in the task's
        # deliverable folders as writable roots, exactly like the AUTO scheduled path in
        # _build_task_engine. System folders outside the workspace stay out of the root set, so
        # writes still can't escape to arbitrary locations — only to where the task is allowed.
        if owning_task is not None:
            task_roots = self._scheduled_write_roots(owning_task)
            if roots is None:
                roots = [{"path": str(ws), "writable": True, "label": "workspace"}] if ws else []
            seen = {
                str(r.get("path") if isinstance(r, dict) else getattr(r, "path", ""))
                for r in roots
            }
            for r in task_roots:
                key = str(getattr(r, "path", ""))
                if key and key not in seen:
                    seen.add(key)
                    roots.append(r)
        engine = build_engine(
            agent=ag,
            workspace=ws,
            model=model,
            mode=mode,
            provider=self.provider,
            # Memory off (§4.3) = stop LEARNING, not amnesia: saved facts still inject
            # and stay usable, only the write tools go. Read at build time; running
            # sessions finish under the mode they started with.
            memory_store=self.memory_store,
            memory_workspace=self._memory_key_for(record, ws),
            memory_off=not self.memory_settings.enabled,
            # LIVE, not a snapshot: turning saving off mid-conversation must take
            # effect at once (owner-hit 2026-07-28 — a running session kept saving).
            memory_saving_enabled=lambda: self.memory_settings.enabled,
            # Callable, not a snapshot: editing your instructions in Settings applies
            # to conversations already open (same reason as the saving switch).
            user_rules=lambda: self.memory_settings.user_rules,
            on_memory_saved=self._memory_saved_notifier(session_id),
            messages=messages,
            extra_tools=[
                *(extra_tools or []),
                *self._team_tools_for(session_id, ag, record, ws),
            ]
            or None,
            secrets=self.secrets,
            task_store=self.task_store,
            wake_store=self.wakes,
            session_id=session_id,
            audit_sink=self.audit_store.append,
            roots=roots,
            # WS sessions pass mode-aware callbacks (attended → live prompt, unattended → Inbox).
            # Background / self-wake / durable-resume runs have no live socket → default to the
            # Inbox-based callbacks so a rebuilt engine can still get approvals/answers (and, on
            # resume, the already-resolved item returns immediately).
            approver=approver or self.inbox_approver(session_id, agent),
            directory_requester=directory_requester
            or self.inbox_directory_requester(session_id, agent),
            plan_approver=plan_approver or self.inbox_plan_approver(session_id, agent),
            question_asker=question_asker
            or self.inbox_question_asker(session_id, agent),
            tool_requester=tool_requester,
            # The team gates work on ANY turn (headless boxes are driven by background
            # deliveries): the queue-backed handlers are the default.
            team_approver=team_approver
            or self.inbox_team_approver(session_id, agent_name),
            items_approver=items_approver
            or self.inbox_items_approver(session_id, agent_name),
            subscription_store=self.subscriptions,
            channel_buffer=self.channel_buffer,
            routing_targets=self._routing_targets(session_id, agent),
            # Per-session connection hierarchy: expose only effective-enabled connectors' tools.
            connector_filter=self.effective_connectors(session_id, agent_name),
            # User governance/sandbox settings (Settings → Safety & sandbox), applied to
            # this new session's engine.
            **self.governance_settings(),
            # Per-session skill menu, LIVE (SKILLS-SPEC §3): a callable so load_skill sees
            # disables/new skills immediately; the catalog snapshot is taken at build.
            skill_filter=lambda sid=session_id, w=ws, a=agent_name: (
                self.effective_skill_names(sid, w, agent=a)
            ),
            # Persona-carried skills (OPE-58): the bundle's skills/ dir joins the loader
            # so its skills are readable, not just listed.
            extra_skill_dirs=(
                [d] if (d := self.persona_skill_scope(agent_name)[0]) is not None else None
            ),
            # Auto-Approve (spec §1.5): prefs-backed, so the Settings toggle takes effect on
            # the next session build without a config.toml edit.
            auto_approve=self.auto_approve(),
            auto_approve_shadow=self.auto_approve_shadow(),
            # Reasoning effort (default/low/medium/high) forwarded to provider.stream — the
            # engine passes **settings through; providers that don't model effort ignore it
            # (safely whitelist-filtered). Applies to the next session build.
            model_settings=self.model_settings_for_session(),        )
        # An automation run rebuilt here (manual "Run now" over WS, durable resume) still
        # carries its task's standing allowances — the rules live on the task record.
        if owning_task is not None:
            self._seed_task_permissions(engine, owning_task)
        # A mention-spawned session (§31) keeps its in-thread reply pre-approved across
        # rebuilds/restarts — the grant is re-derived from the durable thread map.
        for thread_target in self.mention_sessions.targets_for(session_id):
            engine.permissions.task_rules.setdefault("send_message", set()).add(
                thread_target
            )
        if record is not None and record.grants:
            self._apply_grants(engine, record.grants)
        # Auto-compaction (OPE-27): restore the persisted view boundary and wire the live
        # Settings getter — post-construction, so build_engine's signature stays put.
        if record is not None and record.compaction:
            from ..compaction import CompactionState

            engine.compaction_state = CompactionState.from_dict(record.compaction)
        engine.compaction_settings = self.compaction_settings
        # Team delegation: a lead's decide_worker_call proxy re-resolves the original worker
        # action under harness authority (never from the lead's note). Set per-session so
        # only team leads can exercise it (worker_review_context checks team ownership).
        engine.delegated_approval = lambda args: self.worker_review_context(session_id, args)
        # Team worker lane (owner ruling 2026-10-31, option A): a pre-spawned worker gets
        # its own per-session output directory auto-approved, so dispatched work actually
        # comes back instead of parking on an approval nobody is there to click. Only the
        # worker role gets this — the lead and every human-facing session are unaffected.
        # Worker lane is derived from the DURABLE session record first, roster second:
        # a worker spawned by another process/registry instance (the deskmate creation
        # pipeline keeps its own TeamsClient over the same teams.json) must still get
        # its auto-approved scratch, or dispatched work parks on an approval nobody
        # is there to click. `record.team` is written by the harness only.
        member = self.teams.for_worker_session(session_id)
        worker_info = (record.team if record is not None else {}) or {}
        if member is not None or str(worker_info.get("role") or "") == "worker":
            # Path, not str, and ALREADY RESOLVED: this is assigned onto a live engine
            # (no `__post_init__` re-run), and the decision path compares it against
            # `PermissionEngine._candidate()`, which resolves symlinks. `scratch_base()`
            # does not — so a scratch base that sits behind a symlink (e.g. /tmp →
            # /private/tmp, or an external volume reached through a link) would make
            # every own-directory write miss the lane and park on an approval nobody is
            # there to click. `_provision_scratch` resolves, so both sides agree.
            engine.permissions.worker_scope = Path(self._provision_scratch(session_id))
            # The team's shared scratch (the lead's own scratch dir, handed to every
            # worker as a writable root in `_session_roots`) must be writable without
            # a prompt too: an unattended team has nobody to click Allow, so a worker
            # that saves hand-off files there would park forever. Same lane rule —
            # only this team's shared dir, still no shell, still no writes elsewhere.
            lead_session = (
                member[0].lead_session
                if member is not None
                else str(worker_info.get("lead_session") or "")
            )
            if lead_session:
                # Path, not str: this is assigned onto a live engine (no
                # __post_init__ re-run), and the decision path compares Paths.
                engine.permissions.worker_shared_scopes = (
                    Path(self._provision_scratch(lead_session)),
                )
        # Team lead lane (owner ruling 2026-10-31): the approver must not need
        # approving. A lead's decide_worker_call is how it answers a worker's parked
        # approval; upstream marks that tool consequential on purpose, which recursed
        # into a 39-minute deadlock on the first live expert run (the lead's allow
        # sat in the user's inbox while three worker approvals idled behind it).
        # Roster is the source of truth — for_lead_session, mirroring the worker
        # lane's for_worker_session — so a resumed/rebuilt lead engine (durable
        # resume) keeps the lane, and a disbanded team's ex-lead loses it.
        if self.teams.for_lead_session(session_id) is not None:
            engine.permissions.team_lead = True
        self._engines[session_id] = engine
        # 自动化运行标记：本会话若是某个定时/手动任务的运行实例，它跑出的 web_search 等
        # 工具结果不能算作萧盛影响（否则检索转储会冒充满「内部·萧盛」标）。owning_task 在
        # get_engine 开头已解析（task_for_run_session）。实时对话会话 owning_task 为 None。
        engine.is_automation = owning_task is not None
        if is_new_session:
            self._emit_session_created(session_id, agent_name)
        return engine

    def _emit_session_created(self, session_id: str, persona_id: str) -> None:
        """Phase 5 telemetry, fired once per brand-new session on a background thread
        (never blocks session start). cloud.emit_session_created is a hard no-op when
        signed out or opted out, and sends only content-free facts."""
        import threading

        from .. import cloud
        from ..config import load_config

        entry = self.personas.get(persona_id)
        # Wire fields kept stable; both now carry the workspace shape ("folder" = gated
        # primary folder, "scratch" = starts on the per-session scratch dir).
        kind = ("folder" if entry.requires_folder else "scratch") if entry else ""
        family = kind
        workspace_kind = kind

        def _send() -> None:
            try:
                cloud.emit_session_created(
                    self.secrets,
                    load_config(),
                    session_id=session_id,
                    persona_id=persona_id,
                    persona_family=family,
                    workspace_kind=workspace_kind,
                )
            except Exception:
                logger.debug("telemetry emit_session_created failed", exc_info=True)

        threading.Thread(target=_send, daemon=True).start()

    def _routing_targets(self, session_id: str, agent: str) -> list[str]:
        """The channel address(es) this session's Inbox routes OUT to — used to warn when a
        subscription (inbound) collides with Inbox routing (outbound) on the same channel.
        """
        binding = self.inbox_routing.binding_for(
            self.inbox_routing.route_for(session_id, agent)
        )
        return [f"{binding.channel}:{binding.target}"] if binding.channel else []

    # -- connection hierarchy (UI-REFRESH §4) -----------------------------------
    def _persona_of(self, session_id: str, persona_id: Optional[str] = None) -> str:
        if persona_id:
            return persona_id
        # The live engine is the freshest truth — a brand-new session has no record row
        # until its first send, but its socket already knows the persona.
        engine = self._engines.get(session_id)
        live = getattr(engine, "agent_name", None) if engine is not None else None
        if live:
            return live
        record = self.session_store.load(session_id)
        return (record.agent if record else None) or self.personas.default_id()

    def _persona_connector_grant(self, persona_id: str) -> Optional[set[str]]:
        """The persona's declared connector allowlist (OPE-93). None = unrestricted (the
        `all` sentinel of general builtins); a set = only these ids can ever be effective
        for its sessions — the empty set means no connector access at all."""
        entry = self.personas.get(persona_id)
        if entry is None or entry.manifest is None:
            # Builder-based builtins (Chat/Code/Cowork/Ops) predate the allowlist: their
            # `connectors` trait gates TOOLS only, while their sessions legitimately use
            # the drawer/inbound path (channel bindings). No manifest → no restriction.
            return None
        declared = entry.manifest.connectors
        if declared is True:
            return None
        return set(declared or ())

    def effective_connectors(
        self, session_id: str, persona_id: Optional[str] = None
    ) -> set[str]:
        """The connectors effectively enabled for this session (§4.1): connected AND not muted by
        the session override / persona default AND within the persona's declared grant (OPE-93).
        Drives the engine's connector-tool gating and the inbound delivery gate; seeds the
        persona defaults from the manifest on first read using the full connected set.
        """
        persona = self._persona_of(session_id, persona_id)
        connected = {c["name"] for c in connector_list(self.secrets) if c["connected"]}
        entry = self.personas.get(persona)
        manifest = entry.manifest if entry else None
        persona_defaults = self.persona_connections.defaults_for(
            persona, manifest, connected=connected
        )
        session_overrides = self.session_connections.get(session_id)
        effective = set(
            effective_connections(
                connected=connected,
                persona_defaults=persona_defaults,
                session_overrides=session_overrides,
            )
        )
        grant = self._persona_connector_grant(persona)
        return effective if grant is None else effective & grant

    def _inbound_connector_allowed(self, session_id: str, connector: str) -> bool:
        """Whether an inbound message on `connector` should be DELIVERED to `session_id` (§4.3).

        Uses the SAME effective set as the engine's connector-tool gating so the inbound gate and the
        tool gate can never disagree (a muted connector is muted both ways, from the first message).
        """
        return connector in self.effective_connectors(session_id)

    # -- persona + session connection surfaces (UI-REFRESH §5/§6) ----------------
    def _connected_connectors(self) -> set[str]:
        """The account-connected connector names (the first layer of the §4 hierarchy)."""
        return {c["name"] for c in connector_list(self.secrets) if c["connected"]}

    def _persona_default_connections(
        self, persona_id: str, manifest, connected: set[str]
    ) -> list[dict[str, Any]]:
        """The persona's default connector map (seeded from the manifest's connector recommends on
        first read, then user-editable) as a list, each annotated with account-connectedness.
        """
        defaults = self.persona_connections.defaults_for(
            persona_id, manifest, connected=connected
        )
        return [
            {"connector": c, "enabled": bool(enabled), "connected": c in connected}
            for c, enabled in defaults.items()
        ]

    def persona_detail(self, persona_id: str) -> Optional[dict[str, Any]]:
        """Identity + capabilities + recommends(+connected) + default connections for one persona
        (UI-REFRESH §5). Returns None for an unknown id (the route maps that to an error).
        """
        entry = self.personas.get(persona_id)
        if entry is None:
            return None
        manifest = entry.manifest
        connected = self._connected_connectors()
        recommends = [
            {
                "kind": rec.kind,
                "ref": rec.ref,
                "reason": rec.reason,
                "tier": rec.tier,
                "connected": rec.ref in connected,
            }
            for rec in (manifest.recommends if manifest else [])
        ]
        media_dir = self.personas.media_dir(persona_id)
        media = (
            sorted(
                f.name
                for f in media_dir.iterdir()
                if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            )
            if media_dir
            else []
        )
        return {
            "id": entry.id,
            "name": entry.name,
            "icon": entry.icon,
            "tagline": entry.tagline,
            "description": manifest.description if manifest else "",
            "media": media,
            "builtin": entry.builtin,
            "group": entry.group,
            "enabled": self.personas.is_enabled(entry.id),
            "surfaced": self.personas.is_surfaced(entry.id),
            "default": entry.id == self.personas.default_id(),
            "tools": list(entry.tools),
            "recommended_models": list(manifest.recommended_models) if manifest else [],
            "default_permission_mode": (
                manifest.default_permission_mode if manifest else "interactive"
            ),
            "requires_folder": entry.requires_folder,
            "recommends": recommends,
            "default_connections": self._persona_default_connections(
                persona_id, manifest, connected
            ),
        }

    def set_persona_connection(
        self, persona_id: str, connector: str, enabled: bool
    ) -> dict[str, Any]:
        """Set a persona-default connector on/off (UI-REFRESH §5). Seeds the manifest defaults
        first so the stored row stays complete (the edit overlays the full seed rather than
        collapsing the row to this one connector), then returns the refreshed default_connections
        so the client can re-render without a second GET."""
        entry = self.personas.get(persona_id)
        if entry is None:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        manifest = entry.manifest
        connected = self._connected_connectors()
        self.persona_connections.defaults_for(persona_id, manifest, connected=connected)
        self.persona_connections.set(persona_id, connector, bool(enabled))
        return {
            "ok": True,
            "default_connections": self._persona_default_connections(
                persona_id, manifest, connected
            ),
        }

    def set_persona_enabled(self, persona_id: str, enabled: bool) -> dict[str, Any]:
        """Flip a persona's enabled flag. Disabling also archives its real (unarchived,
        non-internal) sessions — disable means "put this coworker and its history away", so
        the persona's sidebar section disappears with it (owner call, 2026-07-04). Re-enabling
        never unarchives: that would overwrite the user's archive state; history returns one
        click at a time via the Show-archived disclosure. Raises KeyError for unknown ids.
        """
        self.personas.set_enabled(persona_id, enabled)
        archived = 0
        if not enabled:
            for r in self.session_store.list():
                if (
                    r.agent == persona_id
                    and not r.archived
                    and not r.session_id.startswith("__")
                ):
                    self.session_store.set_flags(r.session_id, archived=True)
                    archived += 1
        return {"ok": True, "archived_sessions": archived}

    def _connection_detail(
        self, session_id: str, connector: str, info: Optional[dict[str, Any]]
    ) -> str:
        """A short human description of WHY a connector is live for a session: the chat ids it's
        subscribed to on that platform, plus "DMs" if this is the designated DM session. Channel
        *names* would need the live adapter's resolve cache (not cheap here), so we show the chat
        ids; with no subscription/DM tie we fall back to the connector's title."""
        prefix = f"{connector}:"
        parts = [
            s.channel.split(":", 1)[1]
            for s in self.subscriptions.for_session(session_id)
            if s.channel.startswith(prefix)
        ]
        if self.dm_session() == session_id:
            parts.append("DMs")
        if parts:
            return " · ".join(parts)
        return (info or {}).get("title") or connector

    def session_connections_view(
        self, session_id: str, persona_id: Optional[str] = None
    ) -> dict[str, Any]:
        """The per-session connections drawer payload (UI-REFRESH §6): every account-connected
        connector with its effective on/off state (muted ones stay VISIBLE as off — a §4.2 toggle
        must never make a row vanish), the persona's connector recommends that aren't yet
        account-connected, and the attention count (= those unconnected recommends).

        ``persona_id`` is the caller's hint (the GUI knows the active persona). It matters for a
        brand-new session: no SessionRecord exists until the first turn persists, so without the
        hint the view would resolve to the DEFAULT persona and show its defaults/recommends —
        the owner's 2026-07-03 finding (a fresh Project Manager session rendered cowork's view).
        """
        persona = self._persona_of(session_id, persona_id)
        entry = self.personas.get(persona)
        manifest = entry.manifest if entry else None
        connectors = connector_list(self.secrets)
        by_name = {c["name"]: c for c in connectors}
        connected_names = {c["name"] for c in connectors if c["connected"]}
        # OPE-93 (owner-hit 2026-08-15): the drawer must show the persona's world, not the
        # account's. An undeclared connector is not a mutable source of this session — it
        # was rendering as toggled-ON while the engine (correctly) refused its tools.
        grant = self._persona_connector_grant(persona)
        if grant is not None:
            connected_names &= grant
        effective = self.effective_connectors(session_id, persona)
        connected = [
            {
                "connector": name,
                "enabled": name in effective,
                "detail": self._connection_detail(session_id, name, by_name.get(name)),
            }
            for name in sorted(connected_names)
        ]
        recommended = [
            {
                "connector": rec.ref,
                "reason": rec.reason,
                "tier": rec.tier,
                "connected": False,
            }
            for rec in (manifest.recommends if manifest else [])
            if rec.kind == "connector" and rec.ref not in connected_names
        ]
        return {
            "connected": connected,
            "recommended": recommended,
            "attention": sum(1 for r in recommended if not r["connected"]),
        }

    def inbox_question_asker(self, session_id: str, agent: str):
        """The Unattended `ask_user` handler: turn the agent's question into an Inbox item and
        suspend until a human answers it (from the Inbox, or inline when they open the session).
        Also the default for background/self-wake runs (no live socket). Mirrors to a bound channel
        like the approver does."""

        # B1 / 出勤三档：``auto`` 档（PoC 环境变量开关，默认关闭）不进信箱、
        # 不 await 任何人，按「最小破坏」固定代答并落审计。D3 保持 BYPASS 后，
        # 这条是唤醒／后台续跑唯一的挂起点；attended / inbox 完全走下面的原逻辑。
        try:
            from deskmate.autonomy.attendance import (
                build_auto_question_asker,
                current_attendance,
                should_auto_answer,
            )

            if should_auto_answer(current_attendance()):
                def _audit_auto_answer(entry: dict) -> None:
                    try:
                        self.audit_store.append(entry)
                    except Exception:
                        logger.info("attendance=auto 代答审计写入失败: %s", entry)

                return build_auto_question_asker(_audit_auto_answer)
        except Exception:
            logger.warning("出勤三档模块不可用，保持原有信箱问答", exc_info=True)

        async def ask(
            args: dict[str, Any], tool_call_id: Optional[str] = None
        ) -> dict[str, Any]:
            from ..tools.ask import answer_result, question_item_fields

            fields = question_item_fields(args)
            if fields is None:
                return {"answer": "", "error": "no question"}
            inbox_name = self.inbox_routing.route_for(session_id, agent)
            item = self.inbox.add_question(
                session_id,
                inbox=inbox_name,
                tool_call_id=tool_call_id,
                **fields,
            )
            if (
                item.state != "pending"
            ):  # durable resume re-raised an already-answered prompt
                return answer_result(item.questions, item.resolution)
            self.persist_session(session_id)  # the pending tool call is now on disk
            await self.mirror_inbox_item(item)
            answer = await self.inbox.wait(item.id)
            return answer_result(item.questions, answer)

        return ask

    def note_worker_waiting(
        self, session_id: str, tool_name: str, *, prompt_id: str = "", preview: str = ""
    ) -> None:
        """A team worker parked on a tool approval (the lead is Manual): tell the lead
        through the board — `worker_waiting` is on its wake allowlist (§11.6). The wake
        digest names the call and the prompt id so the lead can answer it with
        `decide_worker_call`; the human can always answer the prompt directly too.
        No-op for non-team sessions (checked here, so callers may invoke unconditionally)."""
        found = self.teams.for_worker_session(session_id)
        if found is None:
            return
        team, worker = found
        item_id = None
        try:
            candidates = [
                it
                for it in self.team_store.list_items(team.space, self._user_actor())
                if it.get("assignee") == worker.actor and it.get("state") == "in_progress"
            ]
            # A session is not a task identity. Never pick the first candidate.
            if len(candidates) == 1:
                item_id = candidates[0]["id"]
        except Exception:  # noqa: BLE001
            item_id = None
        try:
            self.team_store.append_event(
                team.space,
                WORKER_WAITING,
                TeamActor(
                    id=worker.actor,
                    role=TeamRole.WORKER,
                    persona=worker.persona,
                    session_id=session_id,
                ),
                item_id=item_id,
                payload={
                    "tool": tool_name,
                    "session_id": session_id,
                    "prompt_id": prompt_id,
                    "preview": preview[:300],
                },
            )
        except Exception:  # noqa: BLE001 — a notice must never break the approval
            logger.exception("worker_waiting event for %s failed", session_id)
        self.kick_team_tick()

    def inbox_approver(self, session_id: str, agent: str):
        """Inbox-based approver — the default for no-socket runs (background, self-wake, durable
        resume). On resume the item already exists + is resolved, so wait returns at once.
        """

        async def approve(request):
            item = self.inbox.add_approval(
                session_id,
                f"Run `{request.tool_name}`?",
                body=_approval_body(request),
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=getattr(request, "tool_call_id", None),
                data=self.approval_prompt_data(session_id, request),
            )
            if item.state == "pending":
                self.persist_session(session_id)
                # §11.6: a worker parked under a Manual lead — tell its lead via the
                # board (background turns are how workers run; the socket approver
                # does the same for attended ones). note_worker_waiting is a no-op
                # for non-team sessions, so this is safe to call unconditionally.
                self.note_worker_waiting(
                    session_id,
                    request.tool_name,
                    prompt_id=item.id,
                    preview=_approval_body(request)[:300],
                )
                await self.mirror_inbox_item(item)
            resolution = await self.inbox.wait(item.id)
            return self.approval_outcome(resolution, request, session_id)

        return approve

    def inbox_directory_requester(self, session_id: str, agent: str):
        async def request(args, tool_call_id=None):
            item = self.inbox.add_directory(
                session_id,
                "Grant access to a folder?",
                body=str(args.get("reason", "")),
                inbox=self.inbox_routing.route_for(session_id, agent),
                data={
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                    "primary": bool(args.get("primary", False)),
                },
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("granted"):
                return {"granted": False, "reason": "the user declined the request"}
            path = (resp.get("path") or args.get("path") or "").strip()
            if not path:
                return {"granted": False, "error": "no directory was provided"}
            writable = bool(resp.get("writable", args.get("writable", False)))
            if bool(args.get("primary", False)):
                promo = await asyncio.to_thread(self.promote_workspace, session_id, path)
                if promo.get("ok"):
                    return {
                        "granted": True,
                        "path": promo["path"],
                        "writable": True,
                        "primary": True,
                        "note": (
                            "This folder is now the session's workspace. For the rest "
                            "of this turn, address it by absolute path."
                        ),
                    }
                res = self.add_root(session_id, path, writable)
                if not res.get("ok"):
                    return {
                        "granted": False,
                        "error": promo.get("error", "could not promote"),
                    }
                return {
                    "granted": True,
                    "path": path,
                    "writable": writable,
                    "primary": False,
                    "note": promo.get("error", "")
                    + " — granted as an additional folder instead",
                }
            res = self.add_root(session_id, path, writable)
            if not res.get("ok"):
                return {
                    "granted": False,
                    "error": res.get("error", "could not grant access"),
                }
            return {"granted": True, "path": path, "writable": writable}

        return request

    def inbox_plan_approver(self, session_id: str, agent: str):
        async def approve(args, tool_call_id=None):
            item = self.inbox.add_plan(
                session_id,
                "Approve the plan?",
                body=str(args.get("plan", "")),
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user rejected the plan",
                }
            return {"approved": True, "mode": resp.get("mode") or "interactive"}

        return approve

    def inbox_team_approver(self, session_id: str, agent: str):
        """The staffing gate for ANY turn — background deliveries included (drill
        finding 2026-09-05: an event-driven lead on a headless box got "team staffing
        isn't available in this surface" and could never staff). Parks "Create this
        team?" in the durable wait queue, waits, and on approval PRE-SPAWNS the team."""

        async def approve(args, tool_call_id=None):
            members = [m for m in (args.get("members") or []) if isinstance(m, dict)]
            roster = "\n".join(
                f"- {m.get('persona', '?')}"
                + (f" · {m['model']}" if m.get("model") else "")
                + (f" — {m['reason']}" if m.get("reason") else "")
                for m in members
            )
            item = self.inbox.add_plan(
                session_id,
                "Create this team?",
                body=roster,
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user declined this roster",
                }
            enable_chat = bool(
                resp["enable_chat"]
                if "enable_chat" in resp
                else args.get("enable_chat", False)
            )
            return self.create_team(session_id, members, enable_chat=enable_chat)

        return approve

    def inbox_items_approver(self, session_id: str, agent: str):
        """The decomposition gate. TEAMS-flavored sibling of plan_approver: park a
        durable Inbox item, wait, and on approval create the items."""

        async def approve(args, tool_call_id=None):
            items = [i for i in (args.get("items") or []) if isinstance(i, dict)]
            body = "\n".join(
                f"- {i.get('title', '?')} — Done when: {i.get('criteria', '?')}"
                for i in items
            )
            item = self.inbox.add_plan(
                session_id,
                "Approve the proposed work items?",
                body=body,
                inbox=self.inbox_routing.route_for(session_id, agent),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                self.persist_session(session_id)
                await self.mirror_inbox_item(item)
            resp = _parse_inbox_json(await self.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user declined the split",
                }
            return self.board_create_items(session_id, items)

        return approve

    def persist_session(self, session_id: str) -> None:
        """Save the cached engine's thread (so a prompt's pending tool call survives a crash)."""
        engine = self._engines.get(session_id)
        if engine is not None:
            self.save(session_id, engine)

    def reconcile_obsolete_prompts(self, session_id: str = "") -> int:
        """Retire only tool-bound prompts whose call already has a result.

        A repaired interrupted call has a result stub too. A genuinely pending
        call has no result and remains available for durable resume.
        """
        pending = self.inbox.pending(session_id or None)
        answered_by_session = {}
        retired = 0
        for item in pending:
            if not item.tool_call_id:
                continue
            if item.session_id not in answered_by_session:
                engine = self._engines.get(item.session_id)
                record = self.session_store.load(item.session_id) if engine is None else None
                messages = (
                    engine.messages
                    if engine is not None
                    else (record.messages if record else [])
                )
                answered_by_session[item.session_id] = {
                    m.get("tool_call_id") for m in messages if m.get("role") == "tool"
                }
            if item.tool_call_id in answered_by_session[item.session_id]:
                if self.inbox.resolve(item.id, "interrupted", by="system:obsolete-prompt"):
                    retired += 1
        return retired

    async def resolve_inbox(self, item_id: str, resolution: str, by: str = "") -> bool:
        """Resolve an Inbox item from any surface (REST / Slack button / channel reply). If the
        asking agent is still suspended live, that await handles it. Otherwise the process restarted
        (or the engine was evicted) while blocked → durably resume: rebuild the engine from the
        saved thread and continue the turn. `by` = the deciding person when known."""
        item = self.inbox.get(item_id)
        if item is not None:
            self.reconcile_obsolete_prompts(item.session_id)
        pending_before = {p.id: p for p in self.inbox.pending()}
        ok = self.inbox.resolve(item_id, resolution, by=by)
        if not ok or item is None:
            return ok
        # 原对话里的问人卡：答完立刻在对话里留一句「已记录」，让用户看见自己答上了。
        try:
            if (
                item.kind == "question"
                and getattr(item, "visibility", "") == "inline"
                and not getattr(item, "tool_call_id", None)
            ):
                _ack = f"（已记录你的回答：{resolution}）"
                if self._append_origin_message(item.session_id, _ack):
                    await self.broadcast_session(
                        item.session_id,
                        {"type": "assistant_message", "data": {"text": _ack}},
                    )
        except Exception:
            logger.debug("问人卡确认消息写入失败（fail-open）", exc_info=True)
        # Store resolution may atomically deny the original worker call and retire sibling
        # proxies too (§11.6 cascade). Resume all affected idle sessions after a restart, not
        # just the lead whose card was clicked. Live waiters are released by the store.
        resumed_sessions = set()
        resumes = []
        for resolved in pending_before.values():
            if (
                resolved.state == "resolved"
                and resolved.session_id not in resumed_sessions
                and not self.is_running(resolved.session_id)
            ):
                resumed_sessions.add(resolved.session_id)
                resumes.append(self._durable_resume(resolved))
        if resumes:
            await asyncio.gather(*resumes)
        return ok

    async def _durable_resume(self, item) -> None:
        if not getattr(item, "tool_call_id", None):
            return  # nothing to reconstruct (legacy item) — best-effort: leave it
        # 任务 run 的挂起（问人/审批）在重启后继续时，也必须包回 TaskRun：
        # 否则她自己续完、结果只留在会话文件里，没有任何人知道。
        task = None
        try:
            task = self.task_store.task_for_run_session(item.session_id)
        except Exception:
            task = None
        if task is not None:
            try:
                await self._run_scheduled_task(
                    task, "resume", session_id=item.session_id, resume=True
                )
                return
            except Exception:
                logger.exception(
                    "任务 run 的重启续跑失败，退回普通 turn: %s", item.session_id
                )
        engine = self.get_engine(item.session_id)
        if engine is None or not hasattr(engine, "resume"):
            return
        self.mark_running(item.session_id)
        try:
            async for _event in engine.resume():
                pass
            self.save(item.session_id, engine)
        finally:
            self.mark_idle(item.session_id)

    # -- MCP --------------------------------------------------------------------
    async def prepare_mcp_tools(
        self,
        session_id: str,
        *,
        workspace: Optional[str] = None,
        agent: str = "code",
        mcp_refs: Optional[set[str]] = None,
    ) -> list[Any]:
        """Connect enabled MCP servers (global + workspace) and return their tool callables.

        Called from the async WS handler before `get_engine`; no-op if the engine is already
        built (its MCP tools are attached). Servers that fail to connect are skipped.

        ``mcp_refs`` (optional) is the set of ``<server>:<tool>`` pairs a persona explicitly
        references via ``tools: [mcp:<server>:<tool>, …]``. When provided, each server's
        callable set is *restricted* to the referenced tools (a persona borrows only what it
        declared) — otherwise the server's full tool surface is exposed (the default for
        catalogs/personas that don't pin specific tools).
        """
        if session_id in self._engines:
            return []
        from ..connectors.descriptors import get_descriptor
        from ..connectors.tool_defs import (
            approval_for_tool,
            mcp_tool_defs,
            tool_enabled,
        )

        from ..mcp import oauth as mcp_oauth

        ws = self.engine_workspace(session_id, workspace=workspace, agent=agent)
        loop = asyncio.get_running_loop()
        effective: Optional[set[str]] = None  # computed lazily, once
        out: list[Any] = []
        # Persona `mcp:` wiring (OPE-58 sibling stub): a persona that declares an `mcp:`
        # list SCOPES its sessions to those servers — the consent screen already presents
        # that list as what the persona uses, so honoring it keeps consent truthful. It
        # only ever shrinks: the user's enabled/configured/authed gates all still apply,
        # and a persona with no list changes nothing. Connector-backed servers keep their
        # own per-persona connector gating instead.
        persona_mcp = self.persona_mcp_scope(agent)
        for server in load_mcp_servers(
            ws,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(ws),
        ):
            if not server.enabled:
                continue
            if server.auth == "oauth" and not mcp_oauth.has_tokens(
                server.name, self.secrets
            ):
                # NEVER start an interactive OAuth flow from a turn: a token-less
                # server here would open a browser and block every session for the
                # full flow timeout (owner-hit 2026-07-20 — a failed one-click's
                # leftover config froze all new sessions). Flows start only from an
                # explicit connect in Settings/Connectors.
                continue
            descriptor = get_descriptor(server.name)
            backed = descriptor is not None and bool(descriptor.mcp_url)
            if backed:
                # Connector-backed server: obey the same gates as connector tools —
                # the session's effective connector set and the per-tool toggles.
                # The descriptor's PIN is authoritative over whatever the config
                # file says (drift can only ever shrink the surface).
                if effective is None:
                    effective = self.effective_connectors(session_id, agent)
                if server.name not in effective:
                    continue
                prefix = f"mcp__{server.name}__"
                server.include_tools = [
                    t.name.removeprefix(prefix)
                    for t in mcp_tool_defs(server.name)
                    if tool_enabled(self.secrets, server.name, t.name)
                ]
            elif persona_mcp is not None and server.name not in persona_mcp:
                # Raw servers outside the persona's declared scope stay off its sessions.
                continue
            try:
                conn = await self.mcp.ensure(server)
                self._mcp_errors.pop(server.name, None)
                # Recovery resets the notice dedupe: if this server breaks again
                # later, the next session gets a fresh transcript notice.
                self._clear_mcp_notified(server.name)
            except Exception as exc:
                if mcp_oauth.is_auth_required(exc):
                    # Stored tokens no longer refresh (vendor rotated/expired
                    # them) — the non-interactive connect refused to open a
                    # browser. Record it so the MCP page shows WHY the server is
                    # dark; the session just runs without its tools.
                    self._mcp_errors[server.name] = (
                        "sign-in required — reconnect this server from its page"
                    )
                    logger.info(
                        "mcp %s needs re-auth; skipped for this session", server.name
                    )
                else:
                    # Bad command / crashed child / unreachable url — the session
                    # still runs without the tools, but the failure must not be
                    # silent (three-for-three silent failures in the 2026-08-20
                    # drill): record it for the MCP page and the session notice.
                    msg = str(exc) or exc.__class__.__name__
                    tail = self.mcp.last_stderr(server.name)
                    if tail:
                        msg = f"{msg} — {tail}"
                    self._mcp_errors[server.name] = msg[:500]
                    logger.warning(
                        "mcp %s failed to connect: %s", server.name, msg[:500]
                    )
                # Transcript notice on state CHANGE, not state (owner ruling
                # 2026-08-21): a continuously-broken server stamps only the first
                # session after it breaks (or breaks differently) — the Connectors
                # page carries the standing error. Personas that DECLARE the server
                # in their manifest keep the every-session notice: for them the
                # missing tools are material every time (the 2026-08-20 drill case).
                declared = persona_mcp is not None and server.name in persona_mcp
                if declared or self._should_notify_mcp_failure(
                    server.name, self._mcp_errors.get(server.name, "")
                ):
                    self._mcp_session_failures.setdefault(session_id, []).append(
                        server.name
                    )
                continue
            callables = build_callables(
                server,
                conn.tools,
                lambda tool, args, name=server.name: self.mcp.call(name, tool, args),
                loop,
            )
            if backed:
                # Per-tool approval from the pinned read/write classification
                # (server-level requires_approval is off for backed servers);
                # anything unclassified stays approval-gated — fail closed.
                for fn in callables:
                    fn.__aisuite_tool_metadata__.requires_approval = approval_for_tool(
                        fn.__aisuite_tool_metadata__.name, default=True
                    )
            # Persona-level tool pinning: if the persona declared specific
            # `mcp:<server>:<tool>` refs, only those callables surface — the persona
            # borrows exactly what it asked for, never the server's full toolset.
            if mcp_refs is not None:
                allowed = {
                    ref.split(":", 1)[1]
                    for ref in mcp_refs
                    if ref.startswith(server.name + ":")
                }
                if allowed:  # persona pinned this server → restrict to declared tools
                    callables = [
                        fn
                        for fn in callables
                        if self._mcp_tool_local_name(fn) in allowed
                    ]
            out.extend(callables)
        return out

    @staticmethod
    def _mcp_tool_local_name(fn: Any) -> str:
        """The bare MCP tool name behind a callable, decoded from its registered
        `mcp__<server>__<tool>` name (see ``mcp.tools.tool_name``)."""
        name = getattr(fn, "__name__", "")
        if name.startswith("mcp__"):
            # last segment after the server part is the local tool name
            parts = name.split("__")
            return parts[-1] if len(parts) >= 3 else name
        return name

    def _should_notify_mcp_failure(self, name: str, error: str) -> bool:
        """True once per failure episode: the first session after `name` starts
        failing (or its error text changes) notices; unchanged-broken stays quiet.
        Persisted in prefs so an app relaunch doesn't re-stamp the same complaint.
        Compared on a NORMALIZED error: stderr often embeds per-process values
        (0x… object addresses, pids), which made "the same" failure look new on
        every relaunch and re-stamp every session (owner-hit 2026-08-21)."""
        stable = _stable_error(error)
        notified = self._prefs.setdefault("mcp_notified_errors", {})
        if notified.get(name) == stable:
            return False
        notified[name] = stable
        self._save_prefs()
        return True

    def _clear_mcp_notified(self, name: str) -> None:
        if self._prefs.get("mcp_notified_errors", {}).pop(name, None) is not None:
            self._save_prefs()

    def pop_mcp_failures(self, session_id: str) -> list[tuple[str, Optional[str]]]:
        """Drain (name, error) for servers that failed while preparing this session's
        tools — consumed once by the WS handler to append a transcript notice."""
        names = self._mcp_session_failures.pop(session_id, [])
        return [(n, self._mcp_errors.get(n)) for n in names]
    def list_mcp(self) -> list[dict[str, Any]]:
        """Servers from the global config + connection status (does not connect)."""
        from ..mcp import oauth as mcp_oauth

        from ..connectors.descriptors import get_descriptor

        out = []
        for name, raw in read_global().items():
            d = get_descriptor(name)
            if d is not None and d.mcp_url:
                # Connector-backed server: surfaced on the Connectors page (its
                # connect/disconnect lifecycle lives there), not in the MCP tab.
                continue
            connected = name in self.mcp._conns
            is_oauth = str(raw.get("auth", "")).lower() == "oauth"
            if connected:
                status = "connected"
            elif not raw.get("enabled", True):
                status = "disabled"
            elif name in self._mcp_authorizing:
                status = "authorizing"
            elif is_oauth and not mcp_oauth.has_tokens(name, self.secrets):
                status = "needs_auth"
            elif name in self._mcp_errors and not is_oauth:
                # Startup/connection failure (stdio crash, unreachable url) — the
                # drill class. OAuth servers keep their softer statuses: acquiring
                # tokens supersedes a stale sign-in error (the GUI still prints
                # last_error under the row either way).
                status = "error"
            else:
                status = "configured"
            out.append(
                {
                    "name": name,
                    "enabled": bool(raw.get("enabled", True)),
                    "transport": (
                        "sse"
                        if str(raw.get("type", "")).lower()
                        in {"sse", "streamable-http"}
                        else "http"
                        if (raw.get("url") or str(raw.get("type", "")).lower() == "http")
                        else "stdio"
                    ),
                    "requires_approval": bool(raw.get("requires_approval", True)),
                    "auth": "oauth" if is_oauth else None,
                    "status": status,
                    "auth_hint": name in self._mcp_auth_hints,
                    "last_test_at": self._prefs.get("mcp_last_test", {}).get(name),
                    "last_error": self._mcp_errors.get(name),
                    "tool_count": (
                        len(self.mcp._conns[name].tools) if connected else None
                    ),
                    "config": _redact(raw),
                }
            )
        return out

    def begin_mcp_connect(self, name: str) -> None:
        """Flag `authorizing` BEFORE the background connect task starts. The GUI's
        fast poll keys off this status; the first refresh used to outpace the task,
        so a failing Test showed nothing until the lazy 5s tick (owner-hit
        2026-08-21 — the button looked dead). Known names only, so an unknown
        server can't wedge the flag (connect_mcp only clears it on a match)."""
        if name in read_global():
            self._mcp_authorizing.add(name)

    async def connect_mcp(self, name: str) -> dict[str, Any]:
        """Connect one server NOW — for OAuth servers this may open the browser and wait
        for the loopback callback, so callers run it as a background task and watch
        list_mcp for the status flip."""
        from ..mcp import oauth as mcp_oauth

        for server in load_mcp_servers(
            self.default_workspace,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(self.default_workspace),
        ):
            if server.name != name:
                continue
            self._mcp_authorizing.add(name)
            self._mcp_errors.pop(name, None)
            self._mcp_auth_hints.discard(name)
            try:
                # The ONE place a browser sign-in may start: an explicit connect.
                # verify (not ensure): an already-live server gets a real round-trip
                # and a refreshed tool list instead of a cached yes.
                conn = await self.mcp.verify(server, interactive=True)
                # The Connectors row says "Ready · tested ⟨when⟩" — the claim must
                # survive an app restart, so it lives in prefs, not memory.
                self._prefs.setdefault("mcp_last_test", {})[name] = int(time.time())
                self._save_prefs()
                self._clear_mcp_notified(name)
                return {"ok": True, "tools": len(conn.tools)}
            except Exception as exc:
                if (
                    server.transport == "http"
                    and server.auth != "oauth"
                    and mcp_oauth.is_http_auth_error(exc)
                ):
                    # Anonymous probe of a guarded server (the add-by-URL flow):
                    # the answer is sign-in, not a raw 401 dump.
                    self._mcp_auth_hints.add(name)
                    msg = "authentication required — sign in to connect"
                else:
                    msg = str(exc) or exc.__class__.__name__
                    tail = self.mcp.last_stderr(name)
                    if tail:
                        msg = f"{msg} — {tail}"
                self._mcp_errors[name] = msg[:500]
                return {"ok": False, "error": self._mcp_errors[name]}
            finally:
                self._mcp_authorizing.discard(name)
        self._mcp_authorizing.discard(name)  # begin_mcp_connect flagged a name we never matched
        return {"ok": False, "error": f"unknown MCP server: {name}"}

    async def mcp_connect_connector(self, name: str) -> dict[str, Any]:
        """One-click connect for an MCP-BACKED connector (descriptor.mcp_url): seed
        the global server entry pinned to the curated allowlist, run the browser
        OAuth flow, and mark the connector profile `mode: "mcp"` on success."""
        from ..connectors.descriptors import get_descriptor
        from ..connectors.tool_defs import mcp_pinned_tools

        d = get_descriptor(name)
        if d is None or not d.mcp_url:
            return {"ok": False, "error": f"{name} has no MCP connect path"}
        put_global_server(
            name,
            {
                "url": d.mcp_url,
                "auth": "oauth",
                # Server-level approval off: writes gate per-tool via the pinned
                # read/write classification (prepare_mcp_tools); unknown vendor
                # tools never load at all (include_tools).
                "requires_approval": False,
                "include_tools": mcp_pinned_tools(name),
                "enabled": True,
            },
        )
        result = await self.connect_mcp(name)
        if result.get("ok"):
            profile = self.secrets.get(f"{name}:default") or {}
            self.secrets.put(
                f"{name}:default", {**profile, "mode": "mcp", "enabled": True}
            )
        else:
            # A failed connect must take its seeded config with it: an enabled
            # oauth entry with no tokens lingers forever (nothing owns it once
            # the descriptor's mcp_url is gone) and re-arms at every session
            # start — the owner-hit asana leftover, 2026-07-20.
            delete_global_server(name)
        return result

    async def signout_mcp(self, name: str) -> dict[str, Any]:
        """Drop the live connection (if any) and forget the stored OAuth tokens."""
        from ..mcp import oauth as mcp_oauth

        conn = self.mcp._conns.get(name)
        if conn is not None:
            conn.shutdown.set()
        self._mcp_errors.pop(name, None)
        removed = mcp_oauth.sign_out(name, self.secrets)
        return {"ok": True, "had_tokens": removed}

    def add_mcp(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Register a server (one-click connect). Writes the global config.

        Connection probing intentionally lives on the ASYNC path
        (``connect_mcp`` / ``mcp_tools``), which run on the server's main event
        loop — a synchronous probe here would spin a second loop and clash with
        ``MCPManager``'s shared ``asyncio.Lock`` (cross-loop Future errors). The
        UI wires add → connect, so a broken command/url surfaces at install time
        via ``connect_mcp``'s error, not silently on the next session.
        """
        put_global_server(name, config)
        return {"ok": True, "name": name}

    def patch_mcp(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        ok = patch_global_server(name, changes)
        return {"ok": ok, "name": name}

    def delete_mcp(self, name: str) -> dict[str, Any]:
        ok = delete_global_server(name)
        if ok:
            # A later re-add under the same name starts clean, not pre-failed —
            # and not pre-trusted (the old entry's test says nothing about the new).
            self._mcp_errors.pop(name, None)
            self._mcp_auth_hints.discard(name)
            if self._prefs.get("mcp_last_test", {}).pop(name, None) is not None:
                self._save_prefs()
            self._clear_mcp_notified(name)
            # Removing a server must not leave its connection running until the next
            # restart, nor its OAuth tokens + DCR registration in the secret store —
            # "Remove" is the user saying this server is GONE (owner review 2026-08-21).
            # The route runs in a threadpool; the shutdown event belongs to the loop.
            conn = self.mcp._conns.get(name)
            if conn is not None:
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(conn.shutdown.set)
                else:
                    conn.shutdown.set()
            from ..mcp import oauth as mcp_oauth

            mcp_oauth.sign_out(name, self.secrets)
        return {"ok": ok, "name": name}

    async def mcp_tools(self, name: str) -> dict[str, Any]:
        """Connect one server and list its tools (name + description)."""
        for server in load_mcp_servers(
            self.default_workspace,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(self.default_workspace),
        ):
            if server.name == name:
                try:
                    conn = await self.mcp.ensure(server)
                except Exception as exc:
                    return {"name": name, "ok": False, "error": str(exc), "tools": []}
                return {
                    "name": name,
                    "ok": True,
                    "tools": [
                        {"name": t.name, "description": getattr(t, "description", "")}
                        for t in conn.tools
                    ],
                }
        return {"name": name, "ok": False, "error": "unknown server", "tools": []}

    async def reload_mcp(self) -> dict[str, Any]:
        """Hot-swap live MCP connections with fresh config — WITHOUT dropping the
        others, and WITHOUT taking the tool surface offline if one server fails.

        Replaces the old ``aclose`` (which nuked every connection and forced all
        sessions to rebuild from scratch). Now each server is build-before-swapped
        individually: a bad/down entry keeps its previous live connection instead
        of blinking the whole surface dark.
        """
        from ..mcp.config import load_mcp_servers

        servers = load_mcp_servers(
            self.default_workspace,
            secrets=self.secrets,
            workspace_trusted=self._mcp_workspace_trusted(self.default_workspace),
        )
        await self.mcp.reload_all(servers)
        return {"ok": True}

    async def hot_reload_mcp(
        self, session_id: str, *, agent: str = "code"
    ) -> dict[str, Any]:
        """Reload MCP AND push the new tool set into an *already-running* session.

        This is the roundtable/agent hot-plug: after a server is added/removed in
        Settings, the running session's engine gets the new MCP callables appended
        (existing tools are preserved; dup names overwrite) — no engine rebuild, no
        restart, the user doesn't lose their place. Servers that fail to connect are
        skipped (the session just doesn't gain those tools).

        Returns ``{"ok", "added"}`` where ``added`` is the count of new callables.
        """
        engine = self._engines.get(session_id)
        if engine is None:
            # No live session — nothing to hot-patch; the next session picks up
            # the new config via the normal prepare_mcp_tools path.
            return {"ok": True, "added": 0, "note": "no live session"}
        from ..catalog import is_mcp_ref, parse_mcp_ref

        mcp_refs: set[str] = set()
        try:
            entry = self.personas.get(agent)
            if entry is not None:
                for t in getattr(entry, "tools", []) or []:
                    if is_mcp_ref(t):
                        try:
                            srv, tool = parse_mcp_ref(t)
                        except ValueError:
                            continue
                        mcp_refs.add(f"{srv}:{tool}")
        except Exception:  # persona lookup is best-effort
            mcp_refs = set()
        new_tools = await self.prepare_mcp_tools(
            "__hot_reload__",  # bypass the `_engines` short-circuit
            workspace=self.engine_workspace(session_id),
            agent=agent,
            mcp_refs=mcp_refs or None,
        )
        existing = set(engine.registry.names())
        added = 0
        for fn in new_tools:
            if getattr(fn, "__name__", None) in existing:
                # Overwrite-in-place: drop the stale spec so the new one wins.
                existing.discard(getattr(fn, "__name__"))
        engine.registry.register_all(new_tools)
        added = sum(1 for fn in new_tools if getattr(fn, "__name__", None))
        # De-dupe any overwritten names that left a stale spec (register_all kept
        # the last write, so names() is already unique — this is just accounting).
        return {"ok": True, "added": added}

    # -- connectors -------------------------------------------------------------
    def list_connectors(self) -> list[dict[str, Any]]:
        # Enrich two-way connectors with the live gateway's recently-seen senders, so the Connectors
        # tab can manage the allow-list inline (each recent sender flagged authorized or not).
        connectors = connector_list(self.secrets)
        for c in connectors:
            if not (c.get("two_way") and c.get("connected")):
                continue
            allowed = set(c.get("allowed_users") or [])
            # Per-workspace allow-lists (managed relay) — a sender is judged against
            # ITS workspace's list; the flat list only governs team-less (socket) events.
            team_allowed = {
                w["team_id"]: set(w.get("allowed_users") or [])
                for w in (c.get("workspaces") or [])
            }
            recent = self.gateway.recent_senders(c["name"]) if self.gateway else []
            for r in recent:
                team = r.get("team_id")
                pool = team_allowed.get(team, set()) if team else allowed
                r["authorized"] = r.get("user_id") in pool
                # Backfill from the people directory (an event may predate name scopes).
                r["user_name"] = r.get("user_name") or self._people.get(
                    f"{c['name']}:{r.get('user_id')}"
                )
            c["recent"] = recent
            # Parked unauthorized messages (§19) — the connector page resolves them inline.
            c["unauthorized"] = self.parked.list(c["name"])
            # Allow-list display names from the people directory (ids stay the source of truth).
            c["allowed_user_names"] = {
                u: self._people.get(f"{c['name']}:{u}")
                for u in (c.get("allowed_users") or [])
            }
            c["approval_owner_names"] = {
                u: self._people.get(f"{c['name']}:{u}")
                for u in (c.get("approval_owner_ids") or [])
            }
            for w in c.get("workspaces") or []:
                w["allowed_user_names"] = {
                    u: self._people.get(f"{c['name']}:{u}")
                    for u in (w.get("allowed_users") or [])
                }
                w["approval_owner_names"] = {
                    u: self._people.get(f"{c['name']}:{u}")
                    for u in (w.get("approval_owner_ids") or [])
                }
        return connectors

    def connect_connector(
        self, name: str, fields: dict[str, Any], *, acknowledged: bool = False
    ) -> dict[str, Any]:
        # validates the token by a live API call (sync httpx) — run off the event loop
        return connect_connector(self.secrets, name, fields, acknowledged=acknowledged)

    def set_experimental_connectors(self, value: bool) -> dict[str, Any]:
        return set_experimental_enabled(self.secrets, value)

    def disconnect_connector(self, name: str) -> dict[str, Any]:
        # MCP-backed profile: drop the live server connection before the tokens go.
        conn = self.mcp._conns.get(name)
        if conn is not None:
            conn.shutdown.set()
        return disconnect_connector(self.secrets, name)

    def update_connector_tools(
        self, name: str, enabled: dict[str, Any]
    ) -> dict[str, Any]:
        return update_connector_tools(self.secrets, name, enabled)

    def list_audit(
        self,
        *,
        limit: int = 100,
        session_id: Optional[str] = None,
        connector: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.audit_store.list(
            limit=limit, session_id=session_id, connector=connector, tool=tool
        )

    def browser_state(self) -> dict[str, Any]:
        return browser_state()

    def browser_screenshot(self) -> dict[str, Any]:
        return browser_take_screenshot()

    def browser_close(self) -> dict[str, Any]:
        return browser_close_session()

    # ------------------------------------------------------------- agent teams (OPE-96)

    # ------------------------------------------------- project identity (pass 20)

    def _memory_key_for(self, record, ws: Optional[str]) -> Optional[str]:
        """Binding > git > path, with the one-time path→git re-key on the way."""
        binding = ((record.bindings if record else {}) or {}).get("memory")
        return resolve_memory_key(
            ws,
            binding=binding,
            names=self.session_store.names(),
            memory_store=self.memory_store,
        )

    def _space_for(self, record, ws: Optional[str]) -> Optional[str]:
        """Board-space twin of _memory_key_for — same ladder, board collision rule."""
        binding = ((record.bindings if record else {}) or {}).get("board")
        return resolve_board_space(
            ws,
            binding=binding,
            names=self.session_store.names(),
            team_store=self.team_store,
        )

    def _board_space(self, session_id: str) -> Optional[str]:
        record = self.session_store.load(session_id)
        workspace = (record.workspace if record else None) or self.default_workspace
        return self._space_for(record, workspace) if workspace else None

    def _user_actor(self) -> TeamActor:
        return TeamActor(id="user", role=TeamRole.USER)

    def board_attachment(self, session_id: str, stored: str) -> tuple[bytes, str]:
        """Read an attachment referenced by the session's board as the user."""
        space = self._board_space(session_id)
        if space is None:
            raise TeamsBoardError("attachment not found")
        self.team_store.require_attachment_access(
            space, self._user_actor(), stored
        )
        path = self.attachment_store.path_for(stored)
        return path.read_bytes(), self.attachment_store.mime_for(stored)

    def board_item_detail(self, session_id: str, item_id: int) -> dict[str, Any]:
        """One item in full, with its TIMELINE — creations, assignments,
        transitions, and comments merged chronologically (the detail pane renders
        the item's whole story; the store is an event log, so this is just its
        honest projection). Acts as the user."""
        space = self._board_space(session_id)
        if space is None:
            return {"error": "no board for this session"}
        try:
            item = self.team_store.get_item(
                space, int(item_id), actor=self._user_actor()
            )
        except TeamsBoardError as error:
            return {"error": str(error)}
        timeline: list[dict[str, Any]] = []
        for event in self.team_store.events(space, item_id=int(item_id)):
            payload = event.get("payload") or {}
            row: dict[str, Any] = {
                "seq": event["seq"],
                "ts": event["ts"],
                "actor": event["actor"],
            }
            if event["kind"] == "item_created":
                row["kind"] = "created"
            elif event["kind"] == "item_assigned":
                row["kind"] = "claimed" if payload.get("claimed") else "assigned"
                row["assignee"] = payload.get("assignee") or ""
            elif event["kind"] == "item_transitioned":
                row["kind"] = "moved"
                row["to"] = payload.get("to") or ""
                if payload.get("comment"):
                    row["body"] = payload["comment"]
                if payload.get("refs"):
                    row["refs"] = payload["refs"]
            elif event["kind"] == "item_commented":
                row["kind"] = "comment"
                row["body"] = payload.get("body") or ""
                if payload.get("refs"):
                    row["refs"] = payload["refs"]
            else:
                continue
            timeline.append(row)
        item["timeline"] = timeline
        return item

    def board_comment(self, session_id: str, item_id: int, body: str) -> dict[str, Any]:
        """A pure note from the user on an item — never changes state (owner
        doctrine 2026-08-17); the assignee hears it through its feed."""
        space = self._board_space(session_id)
        if space is None:
            return {"error": "no board for this session"}
        try:
            event = self.team_store.comment(
                space, self._user_actor(), int(item_id), body
            )
        except (TeamsBoardError, ValueError) as error:
            return {"error": str(error)}
        self.kick_team_tick()  # the assignee's feed has news
        return {"ok": True, "seq": event["seq"]}

    def session_board(self, session_id: str) -> dict[str, Any]:
        """The session's board: items grouped by the workspace-keyed space. Empty
        (space=None) when the workspace has no items — the rail hides itself."""
        space = self._board_space(session_id)
        if space is None:
            return {"space": None, "name": "", "items": []}
        items = self.team_store.list_items(space, self._user_actor())
        if not items:
            return {"space": None, "name": "", "items": []}
        # Blocked rows carry the blocker as a plain fact ("blocked: need tfvars") —
        # the latest blocked-transition comment, resolved here so the list stays
        # one round-trip.
        for item in items:
            if item["state"] != "blocked":
                continue
            for event in reversed(
                self.team_store.events(space, item_id=item["id"])
            ):
                payload = event.get("payload") or {}
                if event["kind"] == "item_transitioned" and payload.get("to") == "blocked":
                    if payload.get("comment"):
                        item["blocker"] = self._clamp(payload["comment"], 120)
                    break
        # "Waiting on the human" is a projection, not a state: an item is waiting
        # when its assignee has a pending inbox request (permission prompt, question)
        # or went quiet mid-turn. Folded in here so one round-trip answers "what
        # needs me?" — the Team View's work tab groups on exactly this.
        from ..teams.summary import all_events, waiting_items

        waits = waiting_items(all_events(self.team_store, space), self.inbox, items)
        for item in items:
            if item["id"] in waits:
                item["waiting"] = waits[item["id"]]
        return {"space": space, "name": Path(space).name, "items": items}

    def board_transition(
        self, session_id: str, item: int, to: str, comment: str = ""
    ) -> dict[str, Any]:
        space = self._board_space(session_id)
        if space is None:
            return {"error": "this session has no board"}
        try:
            return self.team_store.transition(
                space, self._user_actor(), int(item), to, comment=comment
            )
        except (TeamsBoardError, ValueError) as error:
            return {"error": str(error)}

    def board_create_items(
        self, session_id: str, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The decomposition gate's approved action: create the proposed items as
        the LEAD (its identity is the creator; the user's approval is the gate that
        let this run). Validates everything up front so a bad batch creates nothing."""
        record = self.session_store.load(session_id)
        if record is None or not record.workspace:
            return {"approved": False, "error": "the session has no workspace"}
        space = self._space_for(record, record.workspace)
        actor = TeamActor(
            id=f"{record.agent}:{session_id[:8]}",
            role=TeamRole.LEAD,
            persona=record.agent,
            session_id=session_id,
        )
        for entry in items:
            if not str((entry or {}).get("title", "")).strip() or not str(
                (entry or {}).get("criteria", "")
            ).strip():
                return {
                    "approved": False,
                    "error": "every item needs a title and acceptance criteria",
                }
        created = []
        for entry in items:
            item = self.team_store.create_item(
                space,
                actor,
                title=str(entry["title"]),
                criteria=str(entry["criteria"]),
                description=str(entry.get("description", "")),
                case=str(entry.get("case", "")) or None,
            )
            created.append({"id": item["id"], "title": item["title"]})
        return {
            "approved": True,
            "items": created,
            "note": "items created on the board — staff and assign to start work",
        }

    def team_chat(self, team_id: str, *, mark_read: bool = True) -> dict[str, Any]:
        """The chat view's payload. Viewing IS reading for the user: the badge
        cursor advances on fetch."""
        team = self.teams.get(team_id)
        if team is None or not team.chat_enabled or not team.chat_group:
            return {"enabled": False, "messages": [], "members": []}
        group = self.chat_store.get_group(team.chat_group) or {"members": []}
        messages = self.chat_store.messages(team.chat_group)
        if mark_read and messages:
            self.chat_store.consume(team.chat_group, "user", messages[-1]["seq"])
        return {
            "enabled": True,
            "team_id": team_id,
            "members": group["members"],
            "messages": messages,
        }

    def post_team_chat(self, team_id: str, text: str) -> dict[str, Any]:
        team = self.teams.get(team_id)
        if team is None or not team.chat_enabled or not team.chat_group:
            return {"error": "chat is not enabled for this team"}
        try:
            message = self.chat_store.post(
                team.chat_group, "user", text, author_role="user"
            )
        except (TeamsBoardError, ValueError) as error:
            return {"error": str(error)}
        # A user post wakes every member — kick the drain rather than waiting a tick.
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.team_tick(), self._loop)
        return message

    def journal_overview(self) -> list[dict[str, Any]]:
        return self.journal_store.overview(self._user_actor())

    TEAM_WAKE_CAP_PER_HOUR = 60  # budget gate at the wake gate: silent server cap

    def _team_tools_for(
        self, session_id: str, agent: Any, record: Any, ws: Optional[str]
    ) -> list[Any]:
        """Board/journal verbs, gated by the persona `team:` trait. Leads get the
        coordination set (+ steer); workers get the worker set bound to their roster
        actor id. OPENWORKER_TEAM_BOARD=1 keeps the phase-1 any-session-as-lead dev
        mode."""
        role = getattr(agent, "team", None)
        if role is None and ws and os.environ.get("OPENWORKER_TEAM_BOARD") == "1":
            role = "lead"
        if role is None or not ws:
            return []
        space = self._space_for(record, ws)
        if role == "worker":
            info = (record.team if record is not None else {}) or {}
            actor = TeamActor(
                id=str(info.get("actor") or f"{agent.name}:{session_id[:8]}"),
                role=TeamRole.WORKER,
                persona=agent.name,
                session_id=session_id,
            )
            space = str(info.get("space") or space)
        else:
            actor = TeamActor(
                id=f"{agent.name}:{session_id[:8]}",
                role=TeamRole.LEAD,
                persona=agent.name,
                session_id=session_id,
            )
        tools = board_tools(
            self.team_store,
            space=space,
            actor=actor,
            attachments=self.attachment_store,
            # Resolve live grants at call time, including scratch and directories
            # added or revoked since this engine was built. No engine => no access.
            roots=lambda: getattr(self._engines.get(session_id), "roots", []),
            on_change=self.kick_team_tick,
        ) + journal_tools(
            self.journal_store, actor=actor, space=space
        )
        from ..teams.artifacts import artifact_tools

        def team_identity():
            # Resolve live membership, never a model-supplied team/board id.
            team = self.teams.for_lead_session(session_id)
            if team is None:
                member = self.teams.for_worker_session(session_id)
                if member is not None and member[1].actor == actor.id:
                    team = member[0]
            return team.team_id if team is not None and team.space == space else ""

        tools += artifact_tools(
            self.team_store,
            self.attachment_store,
            space=space,
            actor=actor,
            roots=lambda: getattr(self._engines.get(session_id), "roots", []),
            team_identity=team_identity,
            taint=lambda: bool(getattr(self._engines.get(session_id), "tainted", False)),
        )
        if role == "lead":
            tools.append(self._steer_tool(session_id))
            tools.append(self._team_options_tool())
            tools.append(self._add_worker_tool(session_id))
            # §11.6: a Manual lead answers a worker's parked tool call via decide_worker_call.
            # The decider resolves the worker's own parked prompt; ownership is checked against
            # this lead's team (worker_review_context / _owned_worker_prompt), so a lead cannot
            # proxy-approve a worker that isn't theirs.
            tools.append(
                decide_worker_call_tool(
                    lambda worker, call_id, decision, note: self._lead_decide_worker_call(
                        session_id, worker, call_id, decision, note
                    )
                )
            )
        # post_chat registers for every team persona; it resolves the group at call
        # time (the team may not exist yet at engine build) and fails gracefully
        # when chat is off.
        tools.append(self._post_chat_tool(session_id, role))
        return tools

    def _lead_decide_worker_call(
        self, lead_session: str, worker: str, call_id: str, decision: str, note: str
    ) -> dict:
        """The `decider` wired into this lead's `decide_worker_call_tool`. Verifies the named
        worker is on THIS lead's team (ownership, not the lead's claim), then resolves the
        worker's parked approval prompt with the lead's decision. The lead's own
        approve/deny happens through the tool's permission flow; this only applies the decision
        to the worker's original request. The inbox cascade retires the lead's proxy card if the
        worker resolved first, and supersedes the worker's now-redundant prompt when the lead's
        proxy resolves first (§11.6)."""
        item = self._owned_worker_prompt(lead_session, {"worker": worker, "call_id": call_id})
        if item is None:
            return {"error": "No such pending worker request on this team."}
        resolution = "allow" if str(decision or "").strip().lower() == "allow" else "deny"
        by = f"lead:{worker}"
        # resolve_inbox, not inbox.resolve: a lead decision must also trigger durable
        # resume, or the worker stays parked until the next unrelated board event.
        if self._loop is not None:
            fut = asyncio.run_coroutine_threadsafe(
                self.resolve_inbox(item.id, resolution, by=by), self._loop
            )
            try:
                ok = fut.result(timeout=30)
            except Exception:  # noqa: BLE001 — the resolution itself is durable either way
                refreshed = self.inbox.get(item.id)
                ok = refreshed is not None and refreshed.state == "resolved"
        else:
            ok = self.inbox.resolve(item.id, resolution, by=by)
        if not ok:
            return {"error": "The worker request could not be resolved (already handled?)."}
        return {
            "ok": True,
            "resolution": resolution,
            "call_id": str(call_id or ""),
            "note": str(note or ""),
        }

    def _post_chat_tool(self, session_id: str, role: str) -> Any:
        import aisuite as ai

        manager = self

        def post_chat(text: str, record_on_item: Optional[int] = None) -> dict:
            """Post to # team chat. Mention teammates with @name to reach them —
            only mentioned members are woken (the user always sees it). Chat is for
            questions and consensus; status lives on the board. If your message
            answers something that matters, pass record_on_item to also record it
            as a comment on that work item."""
            team, handle, actor = manager._chat_identity(session_id, role)
            if team is None:
                return {"error": "this session is not part of a team"}
            if not team.chat_enabled or not team.chat_group:
                return {"error": "team chat is not enabled for this team"}
            try:
                message = manager.chat_store.post(
                    team.chat_group, handle, text, author_role=role
                )
            except (TeamsBoardError, ValueError) as error:
                return {"error": str(error)}
            result: dict[str, Any] = {
                "ok": True,
                "mentioned": message["mentions"],
            }
            if record_on_item is not None and actor is not None:
                try:
                    manager.team_store.comment(
                        team.space, actor, int(record_on_item), text
                    )
                    result["recorded_on"] = int(record_on_item)
                except (TeamsBoardError, ValueError) as error:
                    result["record_error"] = str(error)
            return result

        return ai.tool(
            post_chat,
            metadata=ai.ToolMetadata(
                category="team", risk_level="low", capabilities=["team"]
            ),
        )

    def _chat_identity(self, session_id: str, role: str):
        """(team, chat handle, board actor) for a team session — the lead's chat
        handle is "lead"; a worker's handle IS its board actor (the callname)."""
        if role == "lead":
            team = self.teams.for_lead_session(session_id)
            if team is None:
                return None, "", None
            record = self.session_store.load(session_id)
            actor = TeamActor(
                id=team.lead_actor,
                role=TeamRole.LEAD,
                persona=record.agent if record else "",
                session_id=session_id,
            )
            return team, "lead", actor
        found = self.teams.for_worker_session(session_id)
        if found is None:
            return None, "", None
        team, worker = found
        actor = TeamActor(
            id=worker.actor,
            role=TeamRole.WORKER,
            persona=worker.persona,
            session_id=session_id,
        )
        return team, worker.actor, actor

    def _team_options_tool(self) -> Any:
        """Registry-injected staffing knowledge: the lead's options come from the
        persona registry at call time — installing a worker coworker automatically
        widens what a lead can propose; nothing is hardcoded. Solo personas never
        appear (fail closed at the source AND at create_team)."""
        import aisuite as ai

        manager = self

        def team_options() -> dict:
            """List the worker coworkers available for staffing (call before
            propose_team). Only team-capable workers are listed — solo coworkers
            cannot join a team."""
            out = []
            # Registry entries directly — NOT list_all(), which applies the
            # ships:false visibility filter: a lead that is running (internal
            # build or user-enabled) must be able to staff its workers even
            # when those workers are hidden from the settings page.
            for pid in manager.personas.ids():
                entry = manager.personas.get(pid)
                m = getattr(entry, "manifest", None)
                if m is None or m.team != "worker":
                    continue
                if not manager.personas.is_enabled(pid):
                    continue
                out.append(
                    {
                        "persona": pid,
                        "name": m.name,
                        "tagline": m.tagline,
                        "recommended_models": list(m.recommended_models),
                    }
                )
            return {"workers": out}

        return ai.tool(
            team_options,
            metadata=ai.ToolMetadata(
                category="team", risk_level="low", capabilities=["team"]
            ),
        )

    def _add_worker_tool(self, lead_session_id: str) -> Any:
        """The lead's upward staffing verb: pull a specialist in mid-run without
        rebuilding the team. Fails closed on anything `team_options` wouldn't list."""
        import aisuite as ai

        manager = self

        def add_worker(persona: str, name: str = "", reason: str = "") -> dict:
            """Add one MORE worker coworker to your existing team — use when you
            need a specialist that wasn't staffed at the start (e.g. a plot
            architect). Existing teammates, board items and history are untouched.
            `name` is the handle you'll assign/@mention by; omitted = persona id."""
            return manager.add_to_team(
                lead_session_id,
                {"persona": persona, "name": name, "reason": reason},
            )

        return ai.tool(
            add_worker,
            metadata=ai.ToolMetadata(
                category="team", risk_level="medium", capabilities=["team"]
            ),
        )

    def _steer_tool(self, lead_session_id: str) -> Any:
        """The lead's downward steering verb. Text lands in the worker's session
        attributed [Lead] — queued into a live turn, or a fresh background turn when
        idle. Strictly downward: no worker ever gets this tool."""
        import aisuite as ai

        manager = self

        def steer_worker(worker: str, message: str) -> dict:
            """Send steering text to one of your workers (by actor id). Use for
            exceptions — changed requirements, stop/redirect, unblock guidance;
            routine status flows through the board, not steering."""
            team = manager.teams.for_lead_session(lead_session_id)
            if team is None:
                return {"error": "no team yet — propose one with propose_team first"}
            match = next((w for w in team.workers if w.actor == worker), None)
            if match is None:
                return {
                    "error": f"no worker '{worker}' on this team",
                    "workers": [w.actor for w in team.workers],
                }
            if manager._loop is None:
                return {"error": "steering is unavailable in this surface"}
            asyncio.run_coroutine_threadsafe(
                manager.deliver_to_session(
                    match.session_id, f"[Lead] {message}".strip()
                ),
                manager._loop,
            )
            return {"ok": True, "delivered_to": worker}

        return ai.tool(
            steer_worker,
            metadata=ai.ToolMetadata(
                category="team", risk_level="medium", capabilities=["team"]
            ),
        )

    def _prepare_team_worker(
        self,
        member: Any,
        *,
        used: set[str],
        workspace: str,
        model: str,
        mode: Any,
        lead_session: str,
        space: str,
        team_id: str = "",
    ) -> tuple[Optional[TeamWorker], str]:
        """Validate one staffing request and PRE-SPAWN its worker session.

        Spawn ≠ first turn: an unassigned worker costs zero tokens; its first model
        turn fires when the first assignment (or dispatch) lands. Shared by the
        initial staffing gate (`create_team`) and the later dynamic-add path
        (`add_to_team`) so the two can never drift in validation or handle rules.
        Fails closed on personas without `team: worker`. Returns (worker, "") or
        (None, error).
        """
        pid = str((member or {}).get("persona", "")).strip()
        try:
            ag = get_agent(pid)
        except Exception:
            return None, f"unknown coworker '{pid}' — it must be installed and enabled"
        if getattr(ag, "team", None) != "worker":
            # Fail closed: solo personas are not team-eligible — their prompts
            # are written at a human, not a lead.
            return None, f"'{pid}' is not team-capable (needs `team: worker`)"
        # The lead-given callname is the HANDLE: board assignee, @mention target,
        # sidebar label. It must be mention-safe and unique on the team.
        name = str((member or {}).get("name", "")).strip().lower()
        if name and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,23}", name):
            return None, (
                f"'{name}' isn't a usable callname — letters/digits/._- only, max 24"
            )
        actor, n = name or pid, 2
        while actor in used:
            actor, n = f"{name or pid}-{n}", n + 1
        used.add(actor)
        worker_sid = uuid.uuid4().hex[:12]
        self.session_store.save(
            SessionRecord(
                session_id=worker_sid,
                workspace=workspace,
                model=model,
                mode=mode,
                messages=[],
                agent=pid,
            )
        )
        # Written via the dedicated setter: the turn-save upsert never touches
        # `team`, so a worker's first turn can't detach it from its lead.
        self.session_store.set_team(
            worker_sid,
            {
                "team_id": team_id,
                "role": "worker",
                "actor": actor,
                "lead_session": lead_session,
                "space": space,
            },
        )
        return (
            TeamWorker(
                actor=actor,
                persona=pid,
                session_id=worker_sid,
                model=model,
                reason=str((member or {}).get("reason", "")).strip(),
            ),
            "",
        )

    def add_to_team(
        self,
        lead_session_id: str,
        member: dict[str, Any],
        *,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """The dynamic-add gate: staff ONE more worker onto a team that already
        exists — "I need a plot-architecture expert, can I add one mid-run?".

        Deliberately NOT a second `create_team`: the roster, board, journal and
        every in-flight item stay untouched; the new member is simply appended and
        becomes assignable/wakeable immediately. `workspace`/`model` default to the
        lead's own record, but can be passed explicitly for teams whose lead has no
        session record (pipeline teams live on synthetic `creation:<pid>` keys).
        """
        team = self.teams.for_lead_session(lead_session_id)
        if team is None:
            return {
                "approved": False,
                "error": "this session leads no team — create one first",
            }
        record = self.session_store.load(lead_session_id)
        workspace = (
            workspace
            or (record.workspace if record is not None else "")
            or self.default_workspace
            or ""
        )
        if not workspace:
            return {
                "approved": False,
                "error": "no workspace to bind the new worker session to",
            }
        spawned = self.spawn_worker(
            team, member, workspace=workspace, model=model, lead_session=lead_session_id
        )
        if not spawned.get("approved"):
            return spawned
        worker = spawned.pop("worker_obj")
        attached = self.teams.add_worker(team.team_id, worker)
        if attached is None:
            return {"approved": False, "error": "the team no longer exists"}
        return {
            **spawned,
            "note": (
                "worker added — it is idle until assigned or dispatched. The team's"
                " existing items and history are unchanged."
            ),
        }

    def spawn_worker(
        self,
        team,
        member: dict[str, Any],
        *,
        workspace: Optional[str] = None,
        model: Optional[str] = None,
        lead_session: Optional[str] = None,
    ) -> dict[str, Any]:
        """Validate + PRE-SPAWN one worker for an EXISTING team object, WITHOUT
        mutating any registry.

        Split out of `add_to_team` because the roster may live in more than one
        `TeamRegistry` instance over the same teams.json (the deskmate sidecar keeps
        its own): the instance that WRITES the roster must be the caller's, while the
        heavy lifting (session record, team metadata, worker scope) belongs to the
        manager. Returns {"approved": True, "worker": {...}, "worker_obj": TeamWorker}.
        """
        record = self.session_store.load(lead_session or team.lead_session)
        workspace = (
            workspace
            or (record.workspace if record is not None else "")
            or self.default_workspace
            or ""
        )
        if not workspace:
            return {
                "approved": False,
                "error": "no workspace to bind the new worker session to",
            }
        used = {"lead", "user", "board"} | {w.actor for w in team.workers}
        worker, error = self._prepare_team_worker(
            member,
            used=used,
            workspace=workspace,
            model=str(model or (record.model if record is not None else "") or self.model),
            mode=record.mode if record is not None else self.mode,
            lead_session=lead_session or team.lead_session,
            space=team.space,
            team_id=team.team_id,
        )
        if worker is None:
            return {"approved": False, "error": error}
        self._emit_session_created(worker.session_id, worker.persona)
        return {
            "approved": True,
            "team_id": team.team_id,
            "worker": {
                "actor": worker.actor,
                "persona": worker.persona,
                "session_id": worker.session_id,
            },
            "worker_obj": worker,
        }

    def create_team(
        self, session_id: str, members: list[dict[str, Any]], *, enable_chat: bool = False
    ) -> dict[str, Any]:
        """The staffing gate's approved action: PRE-SPAWN worker sessions (state on
        disk, zero tokens — the first model turn fires when the first assignment
        lands) and register the team. Fails closed on personas without `team: worker`."""
        record = self.session_store.load(session_id)
        if record is None or not record.workspace:
            return {"approved": False, "error": "the lead session has no workspace"}
        if self.teams.for_lead_session(session_id) is not None:
            return {"approved": False, "error": "this session already leads a team"}
        space = self._space_for(record, record.workspace)
        workers: list[TeamWorker] = []
        used: set[str] = {"lead", "user", "board"}  # reserved handles
        for member in members:
            worker, error = self._prepare_team_worker(
                member,
                used=used,
                workspace=record.workspace,
                model=str((member or {}).get("model") or record.model),
                mode=record.mode,
                lead_session=session_id,
                space=space,
                team_id="",  # patched below once the team exists
            )
            if worker is None:
                return {"approved": False, "error": error}
            workers.append(worker)
        chat_group = ""
        if enable_chat:
            group = self.chat_store.create_group(
                "team chat",
                [
                    *(
                        {"name": w.actor, "persona": w.persona, "role": "worker"}
                        for w in workers
                    ),
                    {"name": "lead", "persona": record.agent, "role": "lead"},
                ],
            )
            chat_group = group["group_id"]
        team = self.teams.create(
            space=space,
            lead_session=session_id,
            lead_actor=f"{record.agent}:{session_id[:8]}",
            workers=workers,
            chat_enabled=enable_chat,
            chat_group=chat_group,
        )
        for worker in workers:
            self.session_store.set_team(
                worker.session_id,
                {
                    "team_id": team.team_id,
                    "role": "worker",
                    "actor": worker.actor,
                    "lead_session": session_id,
                    "space": space,
                },
            )
            self._emit_session_created(worker.session_id, worker.persona)
        self.session_store.set_team(
            session_id,
            {
                "team_id": team.team_id,
                "role": "lead",
                "actor": team.lead_actor,
                "space": space,
            },
        )
        return {
            "approved": True,
            "team_id": team.team_id,
            "workers": [
                {"actor": w.actor, "persona": w.persona, "session_id": w.session_id}
                for w in workers
            ],
            "note": (
                "team created — workers are idle until you assign. Create work items"
                " and assign them to the actor ids above; review-state items are"
                " yours to verify."
            ),
        }

    def team_summary(self, team_id: str) -> dict[str, Any]:
        """The Team View's one read: tasks grouped by what they need from the human,
        per-worker usage and the pending waits.

        Read-only and side-effect free — the view polls it while open, so it must
        never wake anything (no `kick_team_tick`, no inbox or cursor advance).
        """
        from ..teams.summary import make_summary

        team = self.teams.get(team_id)
        if team is None:
            return {"error": "team not found"}
        if self._board_space(team.lead_session) != team.space:
            return {"error": "the lead's board binding has changed"}
        return make_summary(self, team, time.time())

    def kick_team_tick(self) -> None:
        """Nudge the wake plumbing from outside the turn loop — e.g. after an
        external board client writes through the `/v1/board` API, so a review or a
        new filing reaches the lead now, not at the next 30s scheduler tick."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.team_tick(), self._loop)

    async def team_tick(self) -> int:
        """Drain team queues (called each scheduler tick + kicked after team turns).
        One wake consumes a burst as one digest; durable-until-consumed — the cursor
        advances only after the delivery turn is dispatched."""
        delivered = 0
        for team in self.teams.all():
            if team.paused:
                continue
            for worker in team.workers:
                delivered += await self._drain_team_member(
                    team,
                    session_id=worker.session_id,
                    actor=worker.actor,
                    is_lead=False,
                )
            delivered += await self._drain_team_member(
                team,
                session_id=team.lead_session,
                actor=team.lead_actor,
                is_lead=True,
            )
            delivered += await self._maybe_backstop_lead(team)
        return delivered

    # The lead owns its cadence (sleep_until, stretch-when-quiet); this backstop only
    # exists because prompts aren't guarantees. A forgotten timer must never orphan
    # a running team — and it de-facto covers a worker dying without a transition
    # (its item goes stale; the backstop wake surfaces it in the digest).
    TEAM_LEAD_BACKSTOP_SECS = 600
    # Stalled-worker auto-revive: nudge an idle in_progress worker back to work this
    # many times; after that, auto-block the item and hand it to the lead. Beyond
    # upstream — upstream's watchdog only *tells* the lead, it never re-pings the
    # worker, so a one-turn-stalled worker silently idles for hours.
    TEAM_WORKER_STALL_REPING_CAP = 3

    def _lead_backstop_due(self, team, stalled=None) -> bool:
        sid = team.lead_session
        if self.TEAM_LEAD_BACKSTOP_SECS <= 0 or team.paused:
            return False
        # Local port has no "stopped session" concept (upstream tracked it on the
        # WakeStore); a session is either running or not, and the checks below
        # already cover "don't re-wake an active/queued session".
        if self.is_running(sid) or sid in self._team_inflight:
            return False
        if self.wakes.pending(sid):
            return False  # a timer is set — the lead is on cadence, not forgotten
        # Restart-safe: the first observation starts the clock instead of waking.
        last = self._team_last_alive.setdefault(sid, time.time())
        if time.time() - last < self.TEAM_LEAD_BACKSTOP_SECS:
            return False
        if stalled is None:
            stalled = self._stalled_team_state(team)
        # Alert once per *distinct* stalled state, not every tick.
        return bool(stalled) and self._team_watchdog_alerted.get(sid) != stalled

    async def _maybe_backstop_lead(self, team) -> int:
        stalled = self._stalled_team_state(team)
        if not stalled:
            return 0
        # Beyond upstream: actually nudge a stalled *worker's* in_progress item back
        # to life, instead of only telling the lead. Returns how many were re-pinged.
        revived = await self._revive_stalled_workers(team, stalled)
        if not self._lead_backstop_due(team, stalled):
            return revived
        if not self.teams.count_wake(team.team_id, cap=self.TEAM_WAKE_CAP_PER_HOUR):
            logger.warning("team %s paused for budget this hour", team.team_id)
            return revived
        sid = team.lead_session
        self._team_last_alive[sid] = time.time()
        message = self._team_watchdog_message(team, stalled)
        self._team_inflight.add(sid)

        async def _deliver() -> None:
            try:
                accepted = await self.deliver_to_session(
                    sid, message, source=self._board_source(team, message)
                )
                if accepted:
                    self._team_watchdog_alerted[sid] = stalled
            finally:
                self._team_inflight.discard(sid)

        asyncio.create_task(_deliver())
        return revived + 1

    def _stalled_team_state(self, team) -> tuple:
        """Upstream OpenWorker watchdog (PR #792): which in-flight items have an
        assignee that is genuinely idle — not running, no pending wake, no pending
        inbox. Returns a tuple of
        (item_id, state, updated_seq, assignee, last_alive) so the alert is keyed by
        *distinct* stalled state and not re-fired every tick."""
        workers = {w.actor: w.session_id for w in team.workers}
        workers[team.lead_actor] = team.lead_session
        if self.inbox.list(session_id=team.lead_session, state="pending"):
            return ()
        stalled = []
        for item in self.team_store.list_items(team.space, self._user_actor()):
            if item["state"] not in ("in_progress", "blocked", "review"):
                continue
            sid = workers.get(item["assignee"])
            if sid and (
                self.is_running(sid)
                or sid in self._team_inflight
                or self.wakes.pending(sid)
                or self.inbox.list(session_id=sid, state="pending")
            ):
                continue
            last = self._team_last_alive.get(
                sid, self._team_last_alive.get(team.lead_session, time.time())
            )
            if time.time() - last >= self.TEAM_LEAD_BACKSTOP_SECS:
                stalled.append(
                    (
                        item["id"],
                        item["state"],
                        item["updated_seq"],
                        item["assignee"],
                        self._team_last_alive.get(sid, 0)
                        if sid != team.lead_session
                        else 0,
                    )
                )
        return tuple(stalled)

    async def _revive_stalled_workers(self, team, stalled) -> int:
        """Beyond upstream: when a *worker's* in_progress item has stalled, actually
        re-ping the idle worker to continue — the missing piece that lets a team keep
        working instead of silently idling for hours. Capped; once exhausted the item
        is auto-blocked and the lead is alerted to take over (what a human would do)."""
        workers = {w.actor: w.session_id for w in team.workers}
        revived = 0
        for item_id, state, _seq, assignee, last_alive in stalled:
            if state != "in_progress":
                continue  # review/blocked are the lead's call, not a worker to nudge
            wsid = workers.get(assignee)
            if not wsid or wsid == team.lead_session:
                continue  # not a worker (or it's the lead's own item)
            if (
                self.is_running(wsid)
                or wsid in self._team_inflight
                or self.wakes.pending(wsid)
            ):
                continue  # already alive — don't double-ping
            key = wsid
            count = self._team_reping.get(key, 0)
            try:
                item = self.team_store.get_item(
                    team.space, item_id, actor=self._user_actor(), include_comments=False
                )
                title = item.get("title", f"#{item_id}")
            except Exception:
                title = f"#{item_id}"
            if count >= self.TEAM_WORKER_STALL_REPING_CAP:
                # Exhausted re-pings: stop nudging, auto-block + force a lead alert.
                try:
                    self.team_store.transition(
                        team.space,
                        self._user_actor(),
                        item_id,
                        "blocked",
                        comment=(
                            f"停滞：连续 {count} 次自动唤醒仍无产出，已自动转 blocked，"
                            "待主编接管。"
                        ),
                    )
                except Exception:
                    pass
                self._team_watchdog_alerted[team.lead_session] = stalled
                continue
            self._team_reping[key] = count + 1
            # Mark alive now so the next stall check waits a full backstop window
            # before re-pinging — prevents tight re-ping loops.
            self._team_last_alive[wsid] = time.time()
            minutes = max(1, int((time.time() - last_alive) / 60)) if last_alive else 10
            msg = (
                f"你的工作项 #{item_id}「{title}」仍标记为 in_progress，但系统检测到你已停滞约 {minutes} 分钟、"
                "没有任何产出（无模型活动、无进展）。请立即继续推进：\n"
                "1) 未完成则马上产出实质成果，完成后将此项转为 review（附「完成，请审」）；\n"
                "2) 若确实卡住（缺资料 / 需主编决策 / 工具受限），不要空挂 in_progress，立即转 blocked 并写明卡点；\n"
                "3) 若需多轮才能完成，每轮结束前用 sleep_until 排定下一轮，并告诉主编还差什么。\n"
                "严禁只回「好的 / 我继续」或重复设 in_progress 而不产出——必须给出可见成果，或坦诚说明卡点。"
            )
            self._team_inflight.add(wsid)

            async def _deliver(wsid=wsid, msg=msg) -> None:
                try:
                    await self.deliver_to_session(
                        wsid, msg, source=self._board_source(team, msg)
                    )
                finally:
                    self._team_inflight.discard(wsid)

            asyncio.create_task(_deliver())
            revived += 1
        return revived

    def _team_watchdog_message(self, team, stalled) -> str:
        workers = {w.actor: w.session_id for w in team.workers}
        workers[team.lead_actor] = team.lead_session
        lines = ["Team watchdog — 以下工作项已停滞（idle）至少十分钟、无产出："]
        for item_id, state, _seq, assignee, last_alive in stalled:
            wsid = workers.get(assignee)
            minutes = max(1, int((time.time() - last_alive) / 60)) if last_alive else 10
            try:
                item = self.team_store.get_item(
                    team.space, item_id, actor=self._user_actor(), include_comments=False
                )
                title = item.get("title", f"#{item_id}")
            except Exception:
                title = f"#{item_id}"
            who = assignee if assignee != team.lead_actor else "你（主编）"
            lines.append(
                f"- #{item_id}「{title}」负责人 {who}，状态 {state}，已停滞约 {minutes} 分钟"
            )
        lines.append("")
        lines.append(
            "请核查：是被派活后空转、卡在工具 / 审批、还是忘了续命。需要则 re-ping 该 worker、改派或自己接手；"
            "处理完结束本轮。看板变更会自动唤醒你，不要自己起轮询。"
        )
        return "\n".join(lines)

    TEAM_BATCH_SECONDS = 2.0

    async def _drain_team_member(
        self, team, *, session_id: str, actor: str, is_lead: bool
    ) -> int:
        if session_id in self.wakes.stopped_sessions:
            return 0
        # Interest follows the assignment relation: everyone's feed is the events
        # on their slice (assigned ∪ filed) — comments, moves, reassignments. The
        # lead additionally subscribes to the board-wide decision classes. ONE
        # scanned log range feeds both projections, so a subscription cursor
        # never leaps over an unscanned direct user instruction.
        page = self.team_store.delivery_page(team.space, actor, is_lead=is_lead)
        directs, subs = page["directs"], page["subs"]
        if subs:
            seen = {e["seq"] for e in subs}
            directs = [e for e in directs if e["seq"] not in seen]
        chat_handle = "lead" if is_lead else actor
        chats = (
            self.chat_store.unread_for(team.chat_group, chat_handle)
            if team.chat_enabled and team.chat_group
            else []
        )
        receipt = self._board_receipt(
            team,
            actor,
            directs,
            subs,
            chats,
            chat_handle,
            through_seq=page["through_seq"],
            is_lead=is_lead,
        )
        directs = self._actionable_team_events(team, directs, actor=actor, is_lead=is_lead)
        subs = self._actionable_team_events(team, subs, actor=actor, is_lead=is_lead)
        # Cancel and lost assignments are top-priority: an in-flight worker gets
        # interrupted NOW, so it stops running toward a direction that no longer
        # exists; the queued notice (delivered when the turn dies) tells it why.
        def _holds(event) -> bool:
            try:
                item = self.team_store.get_item(
                    team.space,
                    int(event["item_id"]),
                    actor=self._user_actor(),
                    include_comments=False,
                )
            except Exception:
                return False
            return item["assignee"] == actor

        cancels = [
            e
            for e in directs
            if e["kind"] == "item_transitioned"
            and (e.get("payload") or {}).get("to") == "canceled"
            and _holds(e)
        ]
        lost_assignments = [
            e
            for e in directs
            if e["kind"] == "item_assigned"
            and (e.get("payload") or {}).get("previous") == actor
            and not _holds(e)
        ]
        if (cancels or lost_assignments) and self.is_running(session_id):
            engine = self._engines.get(session_id)
            if engine is not None:
                engine.request_interrupt()
        message, rows = self._team_digest(
            team, directs, subs, chats, is_lead=is_lead, reader=actor
        )
        if not rows:
            # Quiet events remain on the board/UI, but do not create model turns
            # or cancel a user-requested timer. Do not leap over queued input.
            engine = self._engines.get(session_id)
            if any(
                (src or {}).get("connector") == "board"
                for _, src in (
                    [(t, s) for t, s in (engine._steering if engine else [])]
                )
            ):
                return 0
            self._consume_board_receipt(receipt)
            self._clear_team_batch(session_id)
            if page["has_more"]:
                self.kick_team_tick()
            return 0
        if is_lead:
            deadline = self._team_batch_deadlines.setdefault(
                session_id, time.monotonic() + self.TEAM_BATCH_SECONDS
            )
            urgent = any(
                r.get("needs_attention")
                or r.get("kind") in ("waiting", "assigned")
                or (r.get("to") == "blocked" and not r.get("historical"))
                for r in rows
            )
            timer_due = any(
                w.session_id == session_id and w.kind == "timer"
                for w in self.wakes.due()
            )
            if not urgent and not timer_due and time.monotonic() < deadline:
                if session_id not in self._team_batch_handles:
                    loop = asyncio.get_running_loop()

                    def flush():
                        self._team_batch_handles.pop(session_id, None)
                        asyncio.create_task(self.team_tick())

                    self._team_batch_handles[session_id] = loop.call_later(
                        max(0, deadline - time.monotonic()), flush
                    )
                return 0
        if self.is_running(session_id) or session_id in self._team_inflight:
            return 0  # it will drain on its next turn end / next tick
        if not self.teams.count_wake(team.team_id, cap=self.TEAM_WAKE_CAP_PER_HOUR):
            logger.warning("team %s paused for budget this hour", team.team_id)
            return 0
        self._clear_team_batch(session_id)
        self._team_inflight.add(session_id)
        source = self._board_source(team, message, rows=rows)

        async def _deliver() -> None:
            try:
                await self.deliver_to_session(session_id, message, source=source)
                # Consume only after the turn dispatched: a crash before this replays
                # the batch next tick (at-least-once, never silently lost).
                self._consume_board_receipt(receipt)
            finally:
                self._team_inflight.discard(session_id)
                self.kick_team_tick()

        asyncio.create_task(_deliver())
        return 1

    def _clear_team_batch(self, session_id: str) -> None:
        self._team_batch_deadlines.pop(session_id, None)
        handle = self._team_batch_handles.pop(session_id, None)
        if handle is not None:
            handle.cancel()

    def stop_session(self, session_id: str) -> None:
        """User Stop: kill the live turn AND every automatic re-entry into it, so a
        stopped session does not wake itself back up from a timer or a queued board
        batch."""
        self.wakes.set_stopped(session_id, True)
        self.wakes.cancel_sleep(session_id, "the user stopped the session")
        self._clear_team_batch(session_id)
        engine = self._engines.get(session_id)
        if engine is not None:
            engine.request_interrupt()

    @staticmethod
    def _board_receipt(
        team, actor, directs, subs, chats, chat_handle, *, through_seq=None, is_lead=False
    ) -> dict:
        return {
            "space": team.space,
            "actor": actor,
            "feed": through_seq
            if through_seq is not None
            else max([e["seq"] for e in directs + subs], default=0),
            "subscription": through_seq
            if through_seq is not None and is_lead
            else max([e["seq"] for e in subs], default=0),
            "chat_group": team.chat_group,
            "chat_handle": chat_handle,
            "chat": max([e["seq"] for e in chats], default=0),
        }

    def _consume_board_receipt(self, receipt: dict) -> None:
        if receipt.get("feed"):
            self.team_store.consume_feed(
                receipt["space"], receipt["actor"], receipt["feed"]
            )
        if receipt.get("subscription"):
            self.team_store.consume_subscription(
                receipt["space"], receipt["actor"], receipt["subscription"]
            )
        if receipt.get("chat"):
            self.chat_store.consume(
                receipt["chat_group"], receipt["chat_handle"], receipt["chat"]
            )

    def _actionable_team_events(self, team, events, *, actor: str, is_lead: bool) -> list:
        """Wake policy, not storage/visibility policy. Publications/notes are
        evidence, review is the handoff. Explicit questions use needs_attention;
        human notes and lead instructions are never muted. A dependency completion
        wakes only workers with live dependent work."""
        items = {
            i["id"]: i
            for i in self.team_store.list_items(team.space, self._user_actor())
        }
        active = [
            i
            for i in items.values()
            if i["assignee"] == actor and i["state"] not in ("done", "canceled")
        ]
        prerequisites = {
            link["item"]
            for i in active
            for link in i.get("links", [])
            if link["kind"] == "blocked_by"
        }

        def actionable(e):
            kind, p = e["kind"], e.get("payload") or {}
            item = items.get(e.get("item_id"))
            if kind == WORKER_WAITING:
                return is_lead  # digest also checks the exact prompt is still pending
            if kind == "item_commented":
                if p.get("artifact"):
                    # A published deliverable is the worker's hand-off: the lead MUST
                    # see it. Gating this on needs_attention meant a worker could
                    # attach_file and never wake the lead (2026-10-31) — the lead then
                    # judged "zero output" over work that actually existed.
                    return is_lead
                if p.get("attachments"):
                    return bool(p.get("needs_attention")) and is_lead
                if e.get("actor_role") == "user":
                    return True
                if is_lead:
                    return bool(p.get("needs_attention"))
                return e.get("actor_role") == "lead" and bool(
                    active or (item and item["assignee"] == actor)
                )
            if kind == "item_assigned":
                if is_lead:
                    return (
                        bool(p.get("claimed"))
                        or e.get("actor_role") == "user"
                        or actor in (p.get("assignee"), p.get("previous"))
                    )
                return bool(item) and (
                    (p.get("assignee") == actor and item["assignee"] == actor)
                    or (p.get("previous") == actor and item["assignee"] != actor)
                )
            if kind == "item_created":
                return is_lead
            if kind != "item_transitioned" or item is None or item["state"] != p.get("to"):
                return False
            if is_lead:
                return p.get("to") in ("review", "blocked") or e.get("actor_role") == "user"
            if item["id"] in prerequisites and p.get("to") in ("done", "canceled"):
                return True  # includes a worker's own accepted prerequisite
            if item["assignee"] == actor:
                return p.get("to") == "canceled" or (
                    p.get("to") in ("open", "in_progress")
                    and p.get("from") in ("review", "blocked", "canceled")
                )
            return False

        return [e for e in events if actionable(e)]

    # Long comment/hand-off bodies are already durable on the board — the wake
    # message's job is to say what needs DECISIONS, not to re-carry the evidence
    # into the recipient's context on every wake (owner ruling 2026-08-16). The
    # model text clamps hard; the UI sidecar rows clamp softer (the human gets a
    # bigger excerpt on click without re-inflating the lead's prompt).
    DIGEST_CLAMP_MODEL = 300
    DIGEST_CLAMP_UI = 600

    @staticmethod
    def _clamp(text: str, limit: int, *, suffix: str = "…") -> str:
        text = (text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + suffix

    def _team_digest(
        self,
        team,
        directs: list[dict],
        subs: list[dict],
        chats: Optional[list[dict]] = None,
        *,
        is_lead: bool,
        reader: str = "",
    ) -> tuple[str, list[dict]]:
        """Coalesce one queue batch into one wake message. Deterministic, computed
        by code — the model does judgment, not arithmetic. Returns (model text,
        structured rows) — the rows ride the display sidecar so the GUI renders a
        collapsed BoardWakeCard instead of re-parsing prose."""
        clamp = lambda text: self._clamp(  # noqa: E731 — two-site local shorthand
            text, self.DIGEST_CLAMP_MODEL, suffix=" … (full text on the board)"
        )
        lines: list[str] = []
        rows: list[dict] = []
        for event in directs + subs:
            item_id = event.get("item_id")
            payload = event.get("payload") or {}
            item = None
            if item_id is not None:
                try:
                    item = self.team_store.get_item(
                        team.space, int(item_id), actor=self._user_actor()
                    )
                except Exception:
                    item = None
            title = f"#{item_id} {item['title']}" if item else f"#{item_id}"
            row = {
                "item": item_id,
                "title": item["title"] if item else "",
                "actor": event.get("actor", ""),
                "seq": event["seq"],
            }
            if event["kind"] == "item_assigned":
                if item is None:
                    continue
                assignee = payload.get("assignee") or ""
                if payload.get("claimed"):
                    # A self-claim surfacing in the lead's subscription feed —
                    # supervision by exception, not an assignment to the reader.
                    lines.append(
                        f"{event['actor']} claimed {title} — it's theirs now;"
                        " reassign or cancel if that's wrong."
                    )
                    rows.append({**row, "kind": "claimed"})
                    continue
                if reader and payload.get("previous") == reader and assignee != reader:
                    # The reader just LOST this item — its interest ends here.
                    lines.append(
                        f"{title} was reassigned to {assignee} by {event['actor']}"
                        " — stop any work on it; hand off context via a comment"
                        " if useful."
                    )
                    rows.append({**row, "kind": "assigned", "assignee": assignee})
                    continue
                if reader and assignee != reader:
                    # Someone else's assignment surfacing in a broader feed.
                    lines.append(f"{title} assigned to {assignee} by {event['actor']}")
                    rows.append({**row, "kind": "assigned", "assignee": assignee})
                    continue
                lines.append(
                    f"You've been assigned work item {title}.\n"
                    f"  Done when: {item['criteria']}"
                    + (f"\n  Details: {item['description']}" if item["description"] else "")
                )
                rows.append({**row, "kind": "assigned", "assignee": assignee})
            elif event["kind"] == "item_transitioned":
                to = payload.get("to", "?")
                comment = clamp(payload.get("comment") or "")
                note = (
                    f" — “{comment}” (comment seq {event['seq']}; "
                    f"get_item_comment(item={item_id}, seq={event['seq']}))"
                ) if comment else ""
                historical = bool(item and item.get("state") != to)
                if historical:
                    note += f" (historical transition; current state: {item['state']})"
                if to == "canceled" and not is_lead:
                    lines.append(
                        f"{title} was CANCELED by {event['actor']}{note} — stop any"
                        " work on it and pick up your other assignments."
                    )
                else:
                    lines.append(f"{title} moved to {to} by {event['actor']}{note}")
                rows.append(
                    {
                        **row,
                        "kind": "moved",
                        "from": payload.get("from"),
                        "refs": payload.get("refs") or [],
                        "to": to,
                        "historical": historical,
                        "current_state": item.get("state") if item else None,
                        "note": self._clamp(
                            payload.get("comment") or "", self.DIGEST_CLAMP_UI
                        ),
                    }
                )
            elif event["kind"] == "item_created":
                lines.append(f"New item filed by {event['actor']}: {title}")
                rows.append({**row, "kind": "filed"})
            elif event["kind"] == "item_commented":
                artifact = payload.get("artifact") or {}
                # A published deliverable is the hand-off itself: name it in the wake
                # line so a lead scanning the digest sees "there is output" without
                # having to guess from a one-line caption.
                published = ""
                if artifact:
                    published = (
                        f" 〔工件 {artifact.get('name', '?')}"
                        f" v{artifact.get('version', '?')}"
                        f" · {artifact.get('bytes', 0)}B · artifact_id="
                        f"{artifact.get('artifact_id', '')}"
                        f" — 全文用 read_team_artifact 读〕"
                    )
                lines.append(
                    f"Comment on {title} by {event['actor']}:"
                    f" {clamp(payload.get('body', ''))}{published}"
                )
                comment_row = {
                    **row,
                    "kind": "comment",
                    "needs_attention": bool(payload.get("needs_attention")),
                    "note": self._clamp(payload.get("body") or "", self.DIGEST_CLAMP_UI),
                }
                if artifact:
                    comment_row["artifact"] = {
                        "artifact_id": artifact.get("artifact_id", ""),
                        "version": artifact.get("version"),
                        "name": artifact.get("name", ""),
                        "bytes": artifact.get("bytes", 0),
                        "ref": artifact.get("ref", ""),
                    }
                rows.append(comment_row)
            elif event["kind"] == WORKER_WAITING:
                # §11.6: a worker parked on a tool call under a Manual lead. The lead
                # decides (its decision asks the human), or the human answers directly.
                # For a non-lead reader, the wait is informational only — a collaborator
                # is not an approval authority, so keep it out of their action list.
                tool = payload.get("tool") or "a tool"
                prompt_id = payload.get("prompt_id", "")
                waiter = event.get("actor") or ""
                if is_lead:
                    lines.append(
                        f"{waiter} is waiting for your decision on a tool call"
                        + (f" ({tool})" if tool != "a tool" else "")
                        + (
                            f" — answer via decide_worker_call (prompt {prompt_id})."
                            if prompt_id
                            else "."
                        )
                    )
                    rows.append(
                        {
                            **row,
                            "kind": "waiting",
                            "tool": tool,
                            "prompt_id": prompt_id,
                            # True = this reader is the authority who decides, so the UI
                            # renders it as an action. False = informational only.
                            "decision": True,
                            "note": self._clamp(
                                payload.get("preview") or "", self.DIGEST_CLAMP_UI
                            ),
                        }
                    )
                else:
                    lines.append(
                        f"{waiter} is waiting for the lead or user"
                        + (f" on {tool}" if tool != "a tool" else "")
                        + "."
                    )
                    rows.append(
                        {
                            **row,
                            "kind": "waiting",
                            "tool": tool,
                            "prompt_id": prompt_id,
                            "decision": False,
                            "note": self._clamp(
                                payload.get("preview") or "", self.DIGEST_CLAMP_UI
                            ),
                        }
                    )
                continue
        for chat in chats or []:
            who = chat["author"] if chat["author_role"] != "user" else "[User]"
            lines.append(f"# team chat — {who}: {clamp(chat['text'])}")
            rows.append(
                {
                    "kind": "chat",
                    "actor": who,
                    "note": self._clamp(chat["text"], self.DIGEST_CLAMP_UI),
                }
            )
        body = "\n".join(f"- {line}" for line in lines) or "- (no detail)"
        status = (
            self._team_status_lines(team)
            if is_lead and (directs or subs or chats)
            else []
        )
        if is_lead:
            message = (
                "⏰ Board wake — your team needs decisions:\n"
                + body
                + "\n\nFull hand-off comments live on the board (get_item)."
                " Verify review items against their acceptance criteria (then"
                " done, or send back with a comment), unblock or reassign blocked"
                " items, and triage new filings. Steer only where needed."
            )
            if status:
                message += "\n\n" + "\n".join(status)
            # Time anchor: every timing judgement the lead makes is relative to
            # this local clock line, never a bare UTC value from the board.
            message += f"\n\nnow: {local_clock()}"
        else:
            message = (
                "[Lead] Board update:\n"
                + body
                + self._roster_note(team)
                + "\n\nMove your item to in_progress when you start; blocked (with a"
                " comment) if stuck; review with a hand-off comment when finished."
                " Journal evidence as you go."
            )
        return message, rows

    @staticmethod
    def _ago(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        return f"{minutes // 60}h {minutes % 60}m"

    def _team_status_lines(self, team, *, now: Optional[float] = None) -> list[str]:
        """One machine-signal line per worker on every lead wake.

        "在跑" (running) must be a fact the system reports, never an inference
        from "no message arrived": a slow model sits inside a single turn for
        minutes, and a lead reading only the event log concluded "nobody moved"
        (2026-10-31 incident). Fields come straight from the Team View projection
        (summary.make_summary) — running / step / tokens / waiting / blocked —
        so this adds no state, no event type, and no second source of truth.
        """
        from ..teams.summary import make_summary

        if not team.workers:
            return []
        now = time.time() if now is None else now
        try:
            view = make_summary(self, team, now)
        except Exception:
            logger.debug("team status projection unavailable", exc_info=True)
            return []
        active: dict[str, list[dict]] = {}
        for item in view["items"]:
            if item["group"] in ("done", "canceled"):
                continue
            active.setdefault(item["assignee"], []).append(item)
        lines = [
            "Team status (machine signal — a running worker is NOT idle):"
        ]
        for member in view["workers"]:
            parts = ["running" if member["running"] else "idle"]
            tokens = sum((member.get("tokens") or {}).values())
            if tokens >= 1000:
                parts.append(f"{tokens / 1000:.1f}k tokens")
            elif tokens:
                parts.append(f"{tokens} tokens")
            step = member.get("step")
            if step and step.get("total"):
                parts.append(f"step {step['done']}/{step['total']}")
            if member.get("last_activity"):
                label = "last model step" if member["running"] else "last activity"
                parts.append(f"{label} {self._ago(now - member['last_activity'])} ago")
            if member["running"] and member.get("last_activity") and (
                now - member["last_activity"] > 600
            ):
                # Long-turn yardstick, not a stall verdict: flash turns run 2–7
                # minutes, big deliverables longer, so >10m is "still normal".
                parts.append("long turn (10m+ — normal for a big deliverable)")
            mine = active.get(member["actor"], [])
            waiting = next((i for i in mine if i.get("waiting")), None)
            if waiting is not None:
                tool = (waiting.get("waiting") or {}).get("tool") or "a tool"
                parts.append(f"waiting on {tool} (#{waiting['id']}) for your decision")
            else:
                blocked = [i for i in mine if i["state"] == "blocked"]
                working = [i for i in mine if i["state"] == "in_progress"]
                for item in blocked[:2]:
                    parts.append(
                        f"blocked on #{item['id']}「{self._clamp(item['title'], 40)}」"
                    )
                if not blocked:
                    for item in working[:2]:
                        parts.append(
                            f"on #{item['id']}「{self._clamp(item['title'], 40)}」"
                        )
            lines.append(f"- @{member['actor']}: " + ", ".join(parts))
        return lines

    @staticmethod
    def _roster_note(team) -> str:
        """Teammate awareness as a mechanism: every worker digest carries the
        roster, so tagging teammates never depends on the lead remembering to
        introduce them."""
        if not team.workers:
            return ""
        mates = "; ".join(
            f"{w.actor} ({w.persona}" + (f" — {w.reason})" if w.reason else ")")
            for w in team.workers
        )
        reach = (
            " Reach them or the lead with @name in # team chat (post_chat)."
            if team.chat_enabled
            else " Coordinate through item comments; the lead reads the board."
        )
        return f"\n\nYour team: {mates}; lead (coordinator).{reach}"

    @staticmethod
    def _board_source(
        team, message: str, *, rows: Optional[list[dict]] = None
    ) -> dict[str, Any]:
        """Display-only MessageSource sidecar for board deliveries — the same
        mechanism connector messages use, so the GUI renders a structured card
        instead of a fake user bubble (owner ask 2026-08-16). The framed message
        stays the model-facing text; this only shapes presentation. `rows` are
        the digest's structured events — the BoardWakeCard renders those
        (collapsed to one line by default) instead of re-parsing the prose."""
        return {
            "connector": "board",
            "kind": "channel",
            "channel_id": team.space,
            "channel_name": "Team board",
            "sender_id": "board",
            "sender_name": "Board",
            "ts": time.time(),
            "text": message,
            "board": {"rows": rows or []},
        }

    def team_staleness_digest(self, session_id: str) -> str:
        """Attached to a lead's TIMER wakes: pure code over the board — a
        nothing's-wrong wake is one cheap glance, never a re-survey. Scoped by
        role membership: sessions with no team role get nothing."""
        team = self.teams.for_lead_session(session_id)
        if team is None:
            return ""
        try:
            items = self.team_store.list_items(team.space, self._user_actor())
        except Exception:
            return ""
        by_state: dict[str, int] = {}
        for item in items:
            by_state[item["state"]] = by_state.get(item["state"], 0) + 1
        unassigned = sum(
            1 for i in items if i["state"] == "open" and not i["assignee"]
        )
        parts = [f"{n} {state}" for state, n in sorted(by_state.items())]
        lines = [f"Board: {', '.join(parts) or 'empty'}."]
        if unassigned:
            lines.append(f"{unassigned} open item(s) have no assignee.")
        reviews = [i for i in items if i["state"] == "review"]
        if reviews:
            lines.append(
                "Awaiting your review: "
                + ", ".join(f"#{i['id']} {i['title']}" for i in reviews[:5])
            )
        blocked = [i for i in items if i["state"] == "blocked"]
        if blocked:
            lines.append(
                "Blocked: " + ", ".join(f"#{i['id']} {i['title']}" for i in blocked[:5])
            )
        # Same local-clock anchor as the board digest — a timer wake is exactly
        # where "how long has this been going" gets guessed.
        lines.append(f"now: {local_clock()}")
        return "\n".join(lines)

    def _artifact_scan_root(self, session_id: str) -> Optional[Path]:
        """The dir the Artifacts panel lists: the session's SCRATCH surface only
        (workspace-scratch-design.md §2.5). For orphan sessions that's the workspace
        itself; for folder-gated sessions it's the side scratch root — never the user's
        repo, which would list the whole codebase as 'artifacts'."""
        record = self.session_store.load(session_id)
        workspace = record.workspace if record else self.default_workspace
        if workspace and self.is_temp_workspace(workspace):
            return Path(workspace).expanduser().resolve()
        if self._SESSION_ID_RE.match(session_id or "") and session_id not in {".", ".."}:
            d = (self.scratch_base() / session_id).resolve()
            if d.is_dir():
                return d
        # Legacy fallback (pre-universal-scratch sessions on a custom scratch base):
        # a workspace that is itself disposable still scans.
        if workspace and not record:
            return Path(workspace).expanduser().resolve()
        return None

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        root = self._artifact_scan_root(session_id)
        if root is None or not root.is_dir():
            return []
        out: list[dict[str, Any]] = []
        suffixes = {
            ".md",
            ".markdown",
            ".html",
            ".htm",
            ".txt",
            ".json",
            ".csv",
            ".tsv",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".css",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
            ".pdf",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".pptm",
            ".docx",
            ".doc",
            ".docm",
        }
        # os.walk with in-place pruning, NOT rglob: rglob descends first and filters after,
        # so a home-directory workspace walked into ~/Library and tripped the macOS App Data
        # TCC prompt ("OpenWorker would like to access data from other apps") on every turn.
        # Pruning here means those directories are never entered at all.
        from ..tools.search import OS_DATA_DIRS

        skip = {"node_modules", "target", "dist", "__pycache__"} | OS_DATA_DIRS
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
            for name in files:
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                if path.suffix.lower() not in suffixes:
                    continue
                try:
                    st = path.stat()
                    if not path.is_file():
                        continue
                    out.append(
                        {
                            "path": str(path.relative_to(root)),
                            # Absolute path for "Copy path" — the relative one is useless
                            # outside the app (tester catch 2026-07-12: it copied just the
                            # filename).
                            "abs_path": str(path),
                            "name": path.name,
                            "kind": _artifact_kind(path),
                            "size": st.st_size,
                            "modified_at": st.st_mtime,
                        }
                    )
                except OSError:
                    continue
        out.sort(key=lambda a: a["modified_at"], reverse=True)
        return out[:80]

    MAX_BINARY_PREVIEW = 25 * 1024 * 1024  # base64-over-JSON gets heavy past this

    def _artifact_target(
        self, session_id: str, path: str, *, allow_dir: bool = False
    ) -> tuple[Optional[Path], Optional[str]]:
        """Resolve an artifact path under one of the session's roots — workspace first,
        then the scratch dir, then user-granted extra roots. Universal scratch means a
        gated session's artifacts live BESIDE its workspace, so single-root resolution
        would orphan every transcript chip pointing at scratch."""
        record = self.session_store.load(session_id)
        workspace = record.workspace if record else self.default_workspace
        candidates: list[Path] = []
        if workspace:
            candidates.append(Path(workspace).expanduser().resolve())
        if self._SESSION_ID_RE.match(session_id or "") and session_id not in {".", ".."}:
            scratch = (self.scratch_base() / session_id).resolve()
            if scratch.is_dir() and scratch not in candidates:
                candidates.append(scratch)
        for r in (record.extra_roots if record else []) or []:
            p = Path(str(r.get("path", ""))).expanduser()
            if p.is_dir():
                rp = p.resolve()
                if rp not in candidates:
                    candidates.append(rp)
        if not candidates:
            return None, "no workspace"
        found_missing = False
        for root in candidates:
            target = (root / path).expanduser().resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if allow_dir and target.is_dir():
                return target, None
            if target.is_file():
                return target, None
            found_missing = True
        if found_missing:
            return None, (
                "This isn't in the conversation's folder anymore — it may have been "
                "moved or deleted."
            )
        return None, "path escapes workspace"

    def read_artifact(self, session_id: str, path: str) -> dict[str, Any]:
        # Folders are readable too (a model sometimes links a whole package, e.g. a skill
        # build dir): return a listing the viewer can render instead of a dead end.
        target, err = self._artifact_target(session_id, path, allow_dir=True)
        if target is None:
            return {"ok": False, "error": err}
        if target.is_dir():
            entries: list[dict[str, Any]] = []
            try:
                children = sorted(
                    target.iterdir(), key=lambda c: (c.is_file(), c.name.lower())
                )
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            for child in children[:500]:
                try:
                    size = 0 if child.is_dir() else child.stat().st_size
                except OSError:
                    continue
                entries.append({"name": child.name, "dir": child.is_dir(), "size": size})
            return {"ok": True, "path": path, "kind": "folder", "entries": entries}
        kind = _artifact_kind(target)
        if kind == "office":
            # PowerPoint/Word binaries can't be previewed inline; the UI offers
            # "Open in default app" instead of trying to render them.
            return {"ok": True, "path": path, "kind": "office"}
        if kind in ("image", "pdf", "sheet"):
            import base64

            if target.stat().st_size > self.MAX_BINARY_PREVIEW:
                return {
                    "ok": False,
                    "error": "file too large to preview — use Reveal to open it",
                }
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".pdf": "application/pdf",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".xls": "application/vnd.ms-excel",
            }.get(target.suffix.lower(), "application/octet-stream")
            data = base64.b64encode(target.read_bytes()).decode("ascii")
            return {
                "ok": True,
                "path": path,
                "kind": kind,
                "data_url": f"data:{mime};base64,{data}",
            }
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file cannot be previewed"}
        return {
            "ok": True,
            "path": path,
            "kind": kind,
            "content": text[:500000],
            "truncated": len(text) > 500000,
        }

    def reveal_artifact(
        self, session_id: str, path: str, mode: str = "reveal"
    ) -> dict[str, Any]:
        """Show the file in the OS file manager (`reveal`) or open it with its default app
        (`open`). The server runs on the user's machine in both desktop and browser builds, so
        this is local. Cross-platform: macOS `open`, Windows Explorer/ShellExecute, Linux
        `xdg-open`."""
        import os
        import subprocess
        import sys

        target, err = self._artifact_target(session_id, path, allow_dir=True)
        if target is None:
            return {"ok": False, "error": err}
        # A folder "opens" as itself in the file manager, whatever the mode.
        is_dir = target.is_dir()
        try:
            if sys.platform == "darwin":
                args = (
                    ["open", "-R", str(target)]
                    if mode == "reveal" and not is_dir
                    else ["open", str(target)]
                )
                subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            elif sys.platform == "win32":
                if mode == "reveal" and not is_dir:
                    # Explorer wants the path glued to the switch: /select,<path>
                    subprocess.Popen(["explorer", f"/select,{target}"])
                else:
                    os.startfile(str(target))  # type: ignore[attr-defined]  # open in default app
            else:  # Linux/BSD
                tgt = str(target.parent) if mode == "reveal" and not is_dir else str(target)
                subprocess.Popen(
                    ["xdg-open", tgt],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    # -- web search -------------------------------------------------------------
    def get_web_search(self) -> dict[str, Any]:
        from ..config import load_config
        from ..web import provider_names

        profile = self.secrets.get("web_search:default") or {}
        provider = (
            profile.get("provider") or load_config().web_search_provider or "duckduckgo"
        )
        return {
            "provider": provider,
            "has_key": bool(profile.get("api_key")),
            "providers": provider_names(),
        }

    def set_web_search(
        self, provider: str, api_key: Optional[str] = None
    ) -> dict[str, Any]:
        from ..web import provider_names

        if provider not in provider_names():
            return {"ok": False, "error": f"unknown provider: {provider}"}
        before = self.get_web_search()["provider"]
        profile: dict[str, Any] = {"provider": provider}
        if api_key:
            profile["api_key"] = api_key
        self.secrets.put("web_search:default", profile)
        # §1.9: "Always allow searches this session" is consent to a NAMED destination —
        # the card says which provider the queries go to. A new provider is a new
        # destination, so every live session's grant dies with the old one. (Scheduled
        # tasks that name-allow web_search are unaffected: their approver re-allows.)
        if provider != before:
            for engine in self._engines.values():
                engine.permissions.session_allow_tools.discard("web_search")
        return {"ok": True, "provider": provider}

    # -- model providers (OpenAI, Ollama, …) ------------------------------------
    def get_providers(self) -> list[dict[str, Any]]:
        """Descriptor + per-provider status for the Settings UI. Never returns secret values;
        non-secret field values (e.g. the Ollama base URL) ARE returned so the form can prefill.
        """
        out: list[dict[str, Any]] = []
        for d in provider_descriptors():
            profile = self.secrets.get(f"provider:{d.name}") or {}
            configured = descriptor_configured(d, profile)
            values = {
                f.key: profile.get(f.key)
                for f in d.fields
                if not f.secret and profile.get(f.key)
            }
            row = {
                **d.to_dict(),
                "configured": configured,
                "values": values,
                "suggested_models": self._suggested_models(d.name),
                # Key hygiene for the Settings pane: when the key was saved (date, stamped
                # by set_provider) and when the provider last served a completion (epoch,
                # stamped by the router's on_use hook). Absent for env-only config.
                "key_set_at": profile.get("key_set_at"),
                "last_used_at": (self._prefs.get("provider_last_used") or {}).get(
                    d.name
                ),
            }
            if d.auth == "oauth":
                # Sign-in state instead of key state; the token values themselves
                # never leave the SecretStore.
                row["signed_in"] = configured
                row["account"] = profile.get("account_email") or profile.get(
                    "account_id"
                )
                if d.name == "openai-codex":
                    row["authorizing"] = self._codex_authorizing
                    row["last_error"] = self._codex_error
            out.append(row)
        return out

    def pick_native_folder(self) -> dict[str, Any]:
        """Open the OS folder picker FROM THE SIDECAR — the browser GUI can't obtain absolute
        paths from web file dialogs, but the sidecar is local and can (the desktop shell uses
        Tauri's own picker instead). Blocking until pick/cancel; callers run it off-thread.
        """
        import subprocess
        import sys

        if sys.platform == "darwin":
            cmd = [
                "osascript",
                "-e",
                'tell application "System Events" to activate',
                "-e",
                'POSIX path of (choose folder with prompt "Give the coworker access to a folder")',
            ]
        elif sys.platform == "win32":
            # WinForms folder dialog via PowerShell — no extra deps. -STA is required
            # (the dialog silently fails in the default MTA apartment).
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
                "$f.Description = 'Give the coworker access to a folder'; "
                "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Out.Write($f.SelectedPath) }"
            )
            cmd = ["powershell.exe", "-NoProfile", "-STA", "-Command", ps]
        else:
            # Linux: zenity when present; otherwise the GUI's paste-a-path input remains.
            cmd = ["zenity", "--file-selection", "--directory"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "error": "no native folder picker available"}
        path = (out.stdout or "").strip()
        if out.returncode != 0 or not path:
            return {"ok": False, "canceled": True}
        return {"ok": True, "path": path}

    def pick_native_file(self, prompt: str = "选择原著文件") -> dict[str, Any]:
        """Open the OS file picker FROM THE SIDECAR (single-file variant of
        ``pick_native_folder``) — the browser GUI can't obtain absolute paths from web
        file dialogs. Blocks until pick/cancel; callers run it off-thread.
        """
        import subprocess
        import sys

        if sys.platform == "darwin":
            cmd = [
                "osascript",
                "-e",
                'tell application "System Events" to activate',
                "-e",
                f'POSIX path of (choose file with prompt "{prompt}")',
            ]
        elif sys.platform == "win32":
            # WinForms open-file dialog via PowerShell — no extra deps, -STA required.
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$f = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$f.Title = '{prompt}'; "
                "$f.Filter = 'Novel files (*.txt;*.docx;*.md)|*.txt;*.docx;*.md|All files (*.*)|*.*'; "
                "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Out.Write($f.FileName) }"
            )
            cmd = ["powershell.exe", "-NoProfile", "-STA", "-Command", ps]
        else:
            # Linux: zenity when present; otherwise the GUI's paste-a-path input remains.
            cmd = ["zenity", "--file-selection"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "error": "no native file picker available"}
        path = (out.stdout or "").strip()
        if out.returncode != 0 or not path:
            return {"ok": False, "canceled": True}
        return {"ok": True, "path": path}

    def _note_provider_use(self, name: str) -> None:
        """Router on_use hook: remember when a provider last served a completion. Persisted
        THROTTLED (once per provider per minute) — this fires on every model call, from engine
        threads, and prefs.json isn't a place for a write-per-token-of-work."""
        import time

        now = time.time()
        used = self._prefs.setdefault("provider_last_used", {})
        if now - float(used.get(name) or 0) < 60:
            return
        used[name] = now
        try:
            self._save_prefs()
        except OSError:
            pass

    # Suggestions for the OpenAI-compatible vendor providers (checked against vendor docs
    # 2026-07-04; refresh alongside `recommended_model` in providers/registry.py).
    COMPAT_MODELS = {
        "zai": ["glm-5.2", "glm-4.6"],
        "deepseek": ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"],
        "kimi": ["kimi-k2.6", "kimi-k2.5"],
        "minimax": ["MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M3"],
        "qwen": ["qwen3-max", "qwen3-coder-plus", "qwen-plus"],
        "xai": ["grok-4.3", "grok-4"],
        "mistral": ["mistral-large-latest", "mistral-small-latest"],
    }

    def _suggested_models(self, name: str) -> list[str]:
        """Bare model-name suggestions for the 'add model' form (datalist), per provider.
        Ollama → live `/api/tags` (best-effort); everyone else → the curated matrix,
        topped up with the compat-vendor extras the matrix doesn't vouch for."""
        if name == "ollama":
            return [m.split(":", 1)[-1] for m in self._ollama_models()]
        from ..providers.matrix import models_for_provider

        return list(
            dict.fromkeys(
                [*models_for_provider(name), *self.COMPAT_MODELS.get(name, [])]
            )
        )

    def set_provider(
        self, name: str, fields: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Store a provider's config in its `provider:<name>` SecretStore profile and rebuild
        its cached client. Merges provided fields into any existing profile."""
        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        fields = fields or {}
        profile = dict(self.secrets.get(f"provider:{name}") or {})
        for f in d.fields:
            if f.key not in fields:
                continue
            val = fields.get(f.key)
            if isinstance(val, str):
                val = val.strip()
            if val:
                profile[f.key] = val
            elif not f.required:
                profile.pop(f.key, None)
        missing = [f.label for f in d.fields if f.required and not profile.get(f.key)]
        if missing:
            return {"ok": False, "error": "missing: " + ", ".join(missing)}
        # A (re)pasted key stamps its save date — Settings shows "key added <date>" so stale
        # keys are visible. Endpoint-only saves keep the original stamp.
        if isinstance(fields.get("api_key"), str) and fields["api_key"].strip():
            from datetime import date

            profile["key_set_at"] = date.today().isoformat()
        self.secrets.put(f"provider:{name}", profile)
        self._refresh_provider(name)
        # Convenience: if the provider recommends a model and it's actually available, add it to
        # the curated list so it shows up in the composer right after configuring the provider.
        rec = d.recommended_model
        added: Optional[str] = None
        if rec and rec in self._suggested_models(name):
            # OpenAI models stay bare (the router's default); others carry their prefix.
            added = rec if name == "openai" else f"{name}:{rec}"
            self.add_model(added)
        # First working provider wins the default: if the current default model belongs to a
        # provider with no usable config (the fresh-install gpt-5.6-sol case), switch the default to
        # this provider's model. A default that already works is never stolen.
        if added and not self._provider_configured(self._model_provider(self.model)):
            self.set_default_model(added)
        return {"ok": True, "provider": name, "recommended_model": rec}

    def remove_provider(self, name: str) -> dict[str, Any]:
        """Forget a provider's stored config (Settings ▸ Models "Remove key"). The whole
        `provider:<name>` profile goes — key, endpoint, key_set_at — so the provider reads
        as never configured. Curated models stay; they just gray out until a new key."""
        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        self.secrets.delete(f"provider:{name}")
        self._refresh_provider(name)
        return {"ok": True, "provider": name}

    # -- ChatGPT-subscription provider (OAuth, no key) ---------------------------
    def begin_codex_signin(self) -> None:
        """Flag `authorizing` BEFORE the background sign-in task starts, so the GUI's
        first poll after the button press already shows it (same reasoning as
        begin_mcp_connect)."""
        self._codex_authorizing = True
        self._codex_error = None

    async def codex_signin(self) -> dict[str, Any]:
        """Run the interactive browser sign-in and store the tokens. Long-running
        (the user completes it in the browser) — routes run it as a background task
        and the GUI polls codex_status for the flip."""
        from ..providers import codex_auth

        self._codex_authorizing = True
        self._codex_error = None
        try:
            result = await codex_auth.sign_in(self.secrets)
        except Exception as exc:
            self._codex_error = str(exc)
            return {"ok": False, "error": str(exc)}
        finally:
            self._codex_authorizing = False
        self._refresh_provider("openai-codex")
        # Same convenience as set_provider: surface the recommended model right away,
        # and win the default when the current default's provider isn't usable.
        added = "openai-codex:gpt-5.6-sol"
        self.add_model(added)
        if not self._provider_configured(self._model_provider(self.model)):
            self.set_default_model(added)
        return result

    def codex_status(self) -> dict[str, Any]:
        from ..providers import codex_auth

        store = codex_auth.CodexTokenStore(self.secrets)
        return {
            "signed_in": store.signed_in(),
            "account": store.account_label(),
            "authorizing": self._codex_authorizing,
            "last_error": self._codex_error,
            "authorize_url": codex_auth.last_authorize_url,
        }

    def codex_signout(self) -> dict[str, Any]:
        from ..providers import codex_auth

        had_tokens = codex_auth.CodexTokenStore(self.secrets).clear()
        self._codex_error = None
        self._refresh_provider("openai-codex")
        return {"ok": True, "had_tokens": had_tokens}

    def verify_provider(
        self, name: str, fields: Optional[dict[str, Any]]
    ) -> dict[str, Any]:
        """Test a provider's credentials with a live read-only call, WITHOUT persisting them, so
        onboarding can offer a "Test" button. Falls back to stored/env values when the form left
        a field blank (e.g. testing an already-configured provider)."""
        import os

        d = get_descriptor(name)
        if d is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        if d.auth == "oauth":
            # No key form — verify from the stored token set (signed-out / expired / OK).
            from ..providers import codex_auth

            return codex_auth.verify(self.secrets)
        fields = fields or {}
        profile = self.secrets.get(f"provider:{name}") or {}
        merged = {}
        for f in d.fields:
            val = fields.get(f.key) or profile.get(f.key) or ""
            if isinstance(val, str):
                val = val.strip()
            if val:
                merged[f.key] = val
        api_key = merged.get("api_key", "")
        if not api_key and d.env_key:
            api_key = os.environ.get(d.env_key, "").strip()
        has_key_field = any(f.key == "api_key" for f in d.fields)
        if d.needs_key and has_key_field and not api_key:
            return {"ok": False, "error": "Enter an API key to test."}
        if d.needs_key and not has_key_field:
            # Multi-field cloud providers (Bedrock): required fields must be present;
            # actual credentials may be ambient (~/.aws, env) and are checked by the call.
            missing = [f.label for f in d.fields if f.required and not merged.get(f.key)]
            if missing:
                return {"ok": False, "error": "missing: " + ", ".join(missing)}
        return verify_provider_key(
            name, api_key=api_key, base_url=merged.get("base_url", ""), fields=merged
        )

    def _model_provider(self, model: str) -> str:
        """The provider a model string routes to (known `prefix:` or the OpenAI default)."""
        if ":" in (model or ""):
            prefix = model.split(":", 1)[0]
            if get_descriptor(prefix) is not None:
                return prefix
        return "openai"

    def _provider_configured(self, name: str) -> bool:
        d = get_descriptor(name)
        if d is None:
            return False
        return descriptor_configured(d, self.secrets.get(f"provider:{name}") or {})

    # -- settings / prefs (model API key, default model, onboarding) -------------
    def _prefs_path(self) -> Path:
        return self._data_base / "prefs.json"

    def _load_prefs(self) -> dict[str, Any]:
        try:
            return json.loads(self._prefs_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_prefs(self) -> None:
        self._prefs_path().write_text(
            json.dumps(self._prefs, indent=2), encoding="utf-8"
        )

    # -- direct-message routing -------------------------------------------------
    def dm_session(self) -> Optional[str]:
        """The session a DM to the bot is routed to (user-designated). None → DMs are parked."""
        sid = self._prefs.get("dm_session")
        return sid or None

    def set_dm_session(self, session_id: Optional[str]) -> dict[str, Any]:
        """Designate (or clear, with a falsy id) the session that handles incoming DMs."""
        sid = (session_id or "").strip()
        if sid:
            self._prefs["dm_session"] = sid
        else:
            self._prefs.pop("dm_session", None)
        self._save_prefs()
        return {"ok": True, "dm_session": self.dm_session()}

    def _ollama_alive(self) -> bool:
        """Best-effort local-Ollama liveness, cached 30s (get_settings runs on every GUI
        fetch — no 2s probe inline). Keyless is not the same as PRESENT: `ollama:*` picker
        entries render only when an Ollama actually answers, so a machine with no Ollama
        never shows phantom local models (e.g. a stray pasted string saved as a model id,
        caught 2026-07-21)."""
        import time

        now = time.monotonic()
        cached = getattr(self, "_ollama_alive_cache", None)
        if cached and now - cached[0] < 30:
            return cached[1]
        profile = self.secrets.get("provider:ollama") or {}
        base = (profile.get("base_url") or "http://localhost:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        try:
            import httpx

            alive = httpx.get(base + "/api/tags", timeout=0.8).status_code == 200
        except Exception as e:
            logger.debug("Ollama liveness check failed: %s", e)
            alive = False
        self._ollama_alive_cache = (now, alive)
        return alive

    def _ollama_models(self) -> list[str]:
        """Live list of models pulled into the configured Ollama server (via its native
        `/api/tags`), as `ollama:<name>` so they're directly selectable. Empty if Ollama isn't
        configured or unreachable — best-effort, never raises."""
        profile = self.secrets.get("provider:ollama")
        if not profile:
            return []
        base = (profile.get("base_url") or "http://localhost:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        try:
            import httpx

            data = httpx.get(base + "/api/tags", timeout=2.0).json()
            return [
                f"ollama:{m['name']}" for m in data.get("models", []) if m.get("name")
            ]
        except Exception as e:
            logger.debug("Ollama model list fetch failed: %s", e)
            return []

    def _curated_models(self) -> list[str]:
        """The models offered in the composer's selector: every curated-matrix model
        (`get_settings` culls the ones whose provider has no key) plus custom ids the user
        added, minus matrix models they removed. Deliberately NO built-in seed list — a
        fresh install offers nothing until a provider key exists, and then exactly that
        provider's matrix models appear. The active default is always kept selectable.
        """
        from ..providers.matrix import all_model_ids

        user = self._prefs.get("models")
        user = user if isinstance(user, list) else []
        hidden = set(self._prefs.get("hidden_models") or [])
        models = [m for m in [*all_model_ids(), *user] if m not in hidden]
        return list(dict.fromkeys([self.model, *models]))

    def add_model(self, model: str) -> dict[str, Any]:
        """Add a model id (e.g. `gpt-4o`, `ollama:qwen2.5-coder:32b`) to the picker.
        Custom ids persist in prefs; a previously removed matrix model is just unhidden
        (storing it too would shadow future matrix updates)."""
        from ..providers.matrix import is_derived_model

        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "empty model"}
        hidden = [m for m in self._prefs.get("hidden_models") or [] if m != model]
        if hidden:
            self._prefs["hidden_models"] = hidden
        else:
            self._prefs.pop("hidden_models", None)
        models = self._prefs.get("models")
        models = models if isinstance(models, list) else []
        if model not in models and not is_derived_model(model):
            models.append(model)
        self._prefs["models"] = models
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def remove_model(self, model: str) -> dict[str, Any]:
        """Remove a model id from the picker. Custom ids are dropped; matrix models are
        hidden by id (the matrix is derived, not stored, so a bare drop would resurrect
        them on the next read)."""
        from ..providers.matrix import is_derived_model

        models = self._prefs.get("models")
        models = models if isinstance(models, list) else []
        self._prefs["models"] = [m for m in models if m != model]
        if is_derived_model(model):
            hidden = self._prefs.get("hidden_models") or []
            if model not in hidden:
                self._prefs["hidden_models"] = [*hidden, model]
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def get_settings(self) -> dict[str, Any]:
        """Model-access + UI status. Never returns the key; `source` says where it comes from."""
        import os

        env_key = bool(os.environ.get("OPENAI_API_KEY"))
        stored = bool((self.secrets.get("provider:openai") or {}).get("api_key"))
        # Only surface models whose provider is actually configured — the composer picker
        # reflects exactly what's connected. The active default is always kept selectable
        # (it's hidden behind the "No model" state until a provider is connected anyway).
        # Ollama is keyless, so "configured" is meaningless there — its models show only
        # while a local Ollama answers (cached liveness probe).
        def _selectable(m: str) -> bool:
            provider = self._model_provider(m)
            if provider == "ollama":
                return self._ollama_alive()
            return self._provider_configured(provider)

        selectable = [m for m in self._curated_models() if _selectable(m)]
        if self.model not in selectable:
            selectable.insert(0, self.model)
        from ..providers.matrix import model_context_windows, model_labels
        from ..providers.registry import get_descriptor

        # Free/paid tier per model id: keyless local/free gateways (Ollama, OmniRoute)
        # are "free"; everything that needs a billable API key is "paid". Drives
        # the composer picker's grouping. (opencode / Kilo 外网免费车道 2026-10-18
        # 已删除，见 tests/test_free_lane_removed.py。)
        tiers: dict[str, str] = {}
        for _m in selectable:
            _prov = _m.split(":", 1)[0] if ":" in _m else "openai"
            _d = get_descriptor(_prov)
            # `:free` 后缀的模型（如 openrouter 免费档）无论所属 provider 是否需 key，
            # 实际 $0 余额可用，故直接判为免费档，避免被归到「付费」区误导用户。
            tiers[_m] = (
                "free"
                if (_m.endswith(":free") or (_d is not None and not _d.needs_key))
                else "paid"
            )

        # 上游厂商分组（2026-10-13 萧哥要求）：{full id → 厂商显示名}。驱动 composer
        # picker 按「厂商 × 免费/付费」分组折叠——免费/付费两档平铺几十项没法看。
        # 厂商名取 descriptor 的 title（设置页同款名字，如 "Volcengine Agent Plan" /
        # "Xiaomi token-plan"）；裸 id（OpenAI 默认）归 OpenAI，未知前缀归「自定义模型」。
        vendors: dict[str, str] = {}
        for _m in selectable:
            if ":" in _m:
                _vp = _m.split(":", 1)[0]
                _vd = get_descriptor(_vp)
                vendors[_m] = _vd.title if _vd is not None else "自定义模型"
            else:
                vendors[_m] = "OpenAI"

        return {
            "provider": "openai",
            "model": self.model,
            "models": selectable,
            # Curated-matrix display names ({full id → "GLM-5.2 · via Together"}) so every
            # picker shows human labels; custom models absent here render their raw id.
            "model_labels": model_labels(),
            # {full id → "free" | "paid"}, so the GUI can split the picker into two tiers.
            "model_tiers": tiers,
            # {full id → 厂商显示名}（2026-10-13）：picker 按厂商 × 档位分组折叠。
            "model_vendors": vendors,
            # {full id → context window in tokens}, verified matrix entries only —
            # drives the composer's context-fill meter (absent id → meter hides).
            "model_context_windows": model_context_windows(),
            "has_key": env_key or stored,
            # Provider-agnostic "can this default model actually run?" — true when the default
            # model's provider is configured (any provider, not just OpenAI). Drives the GUI's
            # "No model connected" composer chip and the onboarding Skip warning.
            "model_ready": self._provider_configured(self._model_provider(self.model)),
            "source": "env" if env_key else ("store" if stored else None),
            "onboarded": bool(self._prefs.get("onboarded")),
            "experimental_connectors": experimental_enabled(self.secrets),
            "surfaces": self._surfaces(),
            "nav_layout": self._nav_layout(),
            "sessions_peek": self.sessions_peek(),
            "context_bar": self.context_bar(),
            # #611: collapse the live reasoning stream to a quiet indicator while a
            # turn runs (default FALSE — the trace shows as before). The finalized
            # reasoning still appears after the turn.
            "hide_reasoning_running": self.hide_reasoning_running(),
            # Reasoning effort for new sessions: "default" (provider's own), or an
            # explicit low/medium/high override understood by the OpenAI/Codex paths.
            "reasoning_effort": self.reasoning_effort(),
            # Auto-Approve feature flag + its shadow-eval sibling (spec §1.5). Drive the
            # Settings toggles and gate the composer's Auto-Approve mode entry.
            "auto_approve": self.auto_approve(),
            "auto_approve_shadow": self.auto_approve_shadow(),
            "scratch_base": self._prefs.get("scratch_base")
            or self.DEFAULT_SCRATCH_BASE,
            # Real on-disk secrets location, so the UI shows the OS-native path instead of a
            # hardcoded POSIX one (Windows -> %APPDATA%\coworker, macOS/Linux -> ~/.config).
            "secrets_path": str(self.secrets.path),
            **self.pdf_settings(),
            **self.compaction_settings_payload(),
            **self.governance_settings(),
        }

    def _surfaces(self) -> dict[str, bool]:
        """Which session surfaces are shown in the sidebar. Cowork is always on; Chat and Code
        are opt-in (default off) so a new user sees Cowork only."""
        return {
            "cowork": True,
            "chat": bool(self._prefs.get("show_chat", False)),
            "code": bool(self._prefs.get("show_code", False)),
        }

    def set_surfaces(
        self, chat: Optional[bool] = None, code: Optional[bool] = None
    ) -> dict[str, Any]:
        """Toggle Chat/Code visibility (Cowork is always shown). Persisted in prefs."""
        if chat is not None:
            self._prefs["show_chat"] = bool(chat)
        if code is not None:
            self._prefs["show_code"] = bool(code)
        self._save_prefs()
        return {"ok": True, "surfaces": self._surfaces()}

    def _nav_layout(self) -> str:
        """Sidebar layout: ``"flat"`` (default) or ``"grouped"`` (by persona). Persisted in
        prefs (UI-REFRESH §7)."""
        return "grouped" if self._prefs.get("nav_layout") == "grouped" else "flat"

    def set_nav_layout(self, nav_layout: str) -> dict[str, Any]:
        """Set + persist the sidebar layout. Unknown values fall back to ``"flat"``."""
        value = "grouped" if (nav_layout or "").strip() == "grouped" else "flat"
        self._prefs["nav_layout"] = value
        self._save_prefs()
        return {"ok": True, "nav_layout": value}

    DEFAULT_SESSIONS_PEEK = 5

    def sessions_peek(self) -> int:
        """How many sessions a sidebar group shows before "Show more" (owner ask, 2026-07-03)."""
        try:
            n = int(self._prefs.get("sessions_peek", self.DEFAULT_SESSIONS_PEEK))
        except (TypeError, ValueError):
            n = self.DEFAULT_SESSIONS_PEEK
        return max(1, min(n, 50))

    def set_sessions_peek(self, n: int) -> dict[str, Any]:
        try:
            self._prefs["sessions_peek"] = max(1, min(int(n), 50))
        except (TypeError, ValueError):
            return {"ok": False, "error": "sessions_peek must be a number"}
        self._save_prefs()
        return {"ok": True, "sessions_peek": self.sessions_peek()}

    def context_bar(self) -> bool:
        """Whether the composer shows the context-window fill bar. OFF by default (owner
        ask): the chip then states the session total, and the popover keeps both numbers."""
        return bool(self._prefs.get("context_bar", False))

    def set_context_bar(self, shown: Any) -> dict[str, Any]:
        self._prefs["context_bar"] = bool(shown)
        self._save_prefs()
        return {"ok": True, "context_bar": self.context_bar()}

    def hide_reasoning_running(self) -> bool:
        """#611: while a turn runs, collapse the live (fast-scrolling) reasoning
        stream to a quiet one-line indicator instead of showing the trace grow.
        The finalized reasoning still appears after the turn, so nothing is lost.
        OFF by default — people who want to watch the trace get it as before."""
        return bool(self._prefs.get("hide_reasoning_running", False))

    def set_hide_reasoning_running(self, hidden: Any) -> dict[str, Any]:
        self._prefs["hide_reasoning_running"] = bool(hidden)
        self._save_prefs()
        return {"ok": True, "hide_reasoning_running": self.hide_reasoning_running()}

    # Valid reasoning_effort values understood by the OpenAI/Codex paths; providers that
    # don't model effort (Anthropic thinking_budget, Gemini thinking_config) ignore this
    # key safely (their _request_kwargs whitelist-filters unknown settings). "default" = the
    # provider's own server-side default (no override sent).
    VALID_REASONING_EFFORT = ("default", "low", "medium", "high")

    def reasoning_effort(self) -> str:
        """Per-session-model reasoning effort: "default" (provider's own), "low", "medium",
        or "high". Applied to new sessions via the engine's model_settings."""
        v = str(self._prefs.get("reasoning_effort", "default")).strip().lower()
        return v if v in self.VALID_REASONING_EFFORT else "default"

    def set_reasoning_effort(self, effort: str) -> dict[str, Any]:
        v = str(effort or "default").strip().lower()
        if v not in self.VALID_REASONING_EFFORT:
            return {"ok": False, "error": f"invalid reasoning_effort: {effort}"}
        self._prefs["reasoning_effort"] = v
        self._save_prefs()
        return {"ok": True, "reasoning_effort": self.reasoning_effort()}

    def model_settings_for_session(self) -> dict[str, Any]:
        """Reasoning knobs forwarded to the engine's provider.stream call (engine.py passes
        **settings through). Empty when effort is "default" so providers use their own."""
        effort = self.reasoning_effort()
        if effort == "default":
            return {}
        return {"reasoning_effort": effort}

    # -- Auto-Approve (spec §1.5, Part 6 step 3) --------------------------------
    # The feature flag and its shadow-eval sibling live in prefs (GUI-writable), falling
    # back to the config.toml value a power user may have hand-set. Prefs is user-global,
    # so a cloned repo still can't enable either — same guarantee as the config path.
    def auto_approve(self) -> bool:
        from ..config import load_config

        if "auto_approve" in self._prefs:
            return bool(self._prefs["auto_approve"])
        return bool(load_config().auto_approve)

    def auto_approve_shadow(self) -> bool:
        from ..config import load_config

        if "auto_approve_shadow" in self._prefs:
            return bool(self._prefs["auto_approve_shadow"])
        return bool(load_config().auto_approve_shadow)

    def set_auto_approve(self, on: Any) -> dict[str, Any]:
        self._prefs["auto_approve"] = bool(on)
        self._save_prefs()
        return {
            "ok": True,
            "auto_approve": self.auto_approve(),
            "auto_approve_shadow": self.auto_approve_shadow(),
        }

    def set_auto_approve_shadow(self, on: Any) -> dict[str, Any]:
        self._prefs["auto_approve_shadow"] = bool(on)
        self._save_prefs()
        return {
            "ok": True,
            "auto_approve": self.auto_approve(),
            "auto_approve_shadow": self.auto_approve_shadow(),
        }

    # -- PDF attachments / token savings (owner ask, 2026-07-17) ----------------
    DEFAULT_PDF_MAX_PAGES = 20
    DEFAULT_PDF_MAX_MB = 10

    def pdf_settings(self) -> dict[str, Any]:
        """Fallback mode for models without native PDF support + the attach-time
        thresholds (Settings → Token savings: big PDFs quietly eat tokens)."""
        from ..pdf_support import FALLBACK_MODES

        mode = self._prefs.get("pdf_fallback")
        try:
            pages = int(self._prefs.get("pdf_max_pages", self.DEFAULT_PDF_MAX_PAGES))
        except (TypeError, ValueError):
            pages = self.DEFAULT_PDF_MAX_PAGES
        try:
            mb = int(self._prefs.get("pdf_max_mb", self.DEFAULT_PDF_MAX_MB))
        except (TypeError, ValueError):
            mb = self.DEFAULT_PDF_MAX_MB
        return {
            "pdf_fallback": mode if mode in FALLBACK_MODES else "text",
            "pdf_max_pages": max(1, min(pages, 100)),
            "pdf_max_mb": max(1, min(mb, 10)),
        }

    def compaction_settings(self) -> dict[str, Any]:
        """The live auto-compaction knobs (OPE-27) — read by every engine per check, so a
        Settings change applies without a rebuild. Only the two spec'd overrides plus the
        summarizer-model pin; absent keys fall back to compaction.py defaults."""
        from ..compaction import DEFAULT_CAP_TOKENS, DEFAULT_THRESHOLD_PCT

        return {
            "threshold_pct": float(
                self._prefs.get("compaction_threshold_pct") or DEFAULT_THRESHOLD_PCT
            ),
            "cap_tokens": int(
                self._prefs.get("compaction_cap_tokens") or DEFAULT_CAP_TOKENS
            ),
            # "" → the session's own model (engine falls back to self.model).
            "model": str(self._prefs.get("compaction_model") or ""),
        }

    def compaction_settings_payload(self) -> dict[str, Any]:
        """The same knobs under REST-facing names (prefixed to keep /v1/settings flat)."""
        settings = self.compaction_settings()
        return {
            "compaction_threshold_pct": settings["threshold_pct"],
            "compaction_cap_tokens": settings["cap_tokens"],
            "compaction_model": settings["model"],
        }

    def set_compaction_settings(
        self,
        threshold_pct: Any = None,
        cap_tokens: Any = None,
        model: Any = None,
    ) -> dict[str, Any]:
        """Persist the auto-compaction overrides (OPE-27). Threshold is a percentage of
        the model's context window (10–95); the cap is an absolute token ceiling; model
        pins the summarizer ('' → the session's own model). Engines read these live via
        `compaction_settings()`, so changes apply to running sessions immediately."""
        if threshold_pct is not None:
            try:
                pct = float(threshold_pct)
            except (TypeError, ValueError):
                return {"ok": False, "error": "compaction_threshold_pct must be a number"}
            if not 0.10 <= pct <= 0.95:
                return {
                    "ok": False,
                    "error": "compaction_threshold_pct must be between 0.10 and 0.95",
                }
            self._prefs["compaction_threshold_pct"] = pct
        if cap_tokens is not None:
            try:
                self._prefs["compaction_cap_tokens"] = max(
                    10_000, min(int(cap_tokens), 2_000_000)
                )
            except (TypeError, ValueError):
                return {"ok": False, "error": "compaction_cap_tokens must be a number"}
        if model is not None:
            self._prefs["compaction_model"] = str(model)
        self._save_prefs()
        return {"ok": True, **self.compaction_settings()}

    def set_pdf_settings(
        self,
        fallback: Any = None,
        max_pages: Any = None,
        max_mb: Any = None,
    ) -> dict[str, Any]:
        from ..pdf_support import FALLBACK_MODES, set_fallback_mode

        if fallback is not None:
            if fallback not in FALLBACK_MODES:
                return {"ok": False, "error": "pdf_fallback must be 'text' or 'images'"}
            self._prefs["pdf_fallback"] = fallback
        for key, value, ceiling in (
            ("pdf_max_pages", max_pages, 100),
            ("pdf_max_mb", max_mb, 10),
        ):
            if value is None:
                continue
            try:
                self._prefs[key] = max(1, min(int(value), ceiling))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{key} must be a number"}
        self._save_prefs()
        settings = self.pdf_settings()
        set_fallback_mode(settings["pdf_fallback"])  # engines read the module global
        return {"ok": True, **settings}

    # -- Governance: execution level + sandbox (Settings → Safety & sandbox) ------
    # The execution level is hot-applied to running engines; the sandbox flags take
    # effect for new sessions (they wrap the executor process at build time). Both are
    # persisted in prefs so the choice survives restarts.
    DEFAULT_EXECUTION_LEVEL = "auto"
    DEFAULT_SANDBOX_ENABLED = True
    DEFAULT_SANDBOX_NETWORK = True

    def governance_settings(self) -> dict[str, Any]:
        el = self._prefs.get("execution_level")
        if el not in EXECUTION_LEVELS:
            el = self.DEFAULT_EXECUTION_LEVEL
        se = self._prefs.get("sandbox_enabled")
        sn = self._prefs.get("sandbox_network")
        return {
            "execution_level": el,
            "sandbox_enabled": bool(se) if isinstance(se, bool) else self.DEFAULT_SANDBOX_ENABLED,
            "sandbox_network": bool(sn) if isinstance(sn, bool) else self.DEFAULT_SANDBOX_NETWORK,
            "sandbox_deny_paths": list(self._prefs.get("sandbox_deny_paths") or []),
        }

    def set_governance_settings(
        self,
        execution_level: Any = None,
        sandbox_enabled: Any = None,
        sandbox_network: Any = None,
        sandbox_deny_paths: Any = None,
    ) -> dict[str, Any]:
        """Persist governance/sandbox choices and hot-apply the execution level to every
        running engine (mirrors the WS `set_mode` handler). Invalid execution levels are
        rejected; other values are coerced best-effort."""
        if execution_level is not None:
            if execution_level not in EXECUTION_LEVELS:
                return {"ok": False, "error": f"execution_level must be one of {EXECUTION_LEVELS}"}
            self._prefs["execution_level"] = execution_level
        if sandbox_enabled is not None:
            self._prefs["sandbox_enabled"] = bool(sandbox_enabled)
        if sandbox_network is not None:
            self._prefs["sandbox_network"] = bool(sandbox_network)
        if sandbox_deny_paths is not None:
            if not isinstance(sandbox_deny_paths, list):
                return {"ok": False, "error": "sandbox_deny_paths must be a list"}
            self._prefs["sandbox_deny_paths"] = [str(p) for p in sandbox_deny_paths]
        self._save_prefs()

        # Hot-apply the execution level to every live engine (runtime guard on/off +
        # severity behavior). Sandbox flags apply to new sessions at build_engine time.
        el = self._prefs.get("execution_level")
        if el in EXECUTION_LEVELS:
            for engine in self._engines.values():
                try:
                    engine.permissions.execution_level = el
                    if el == "off":
                        engine.permissions.tool_guard = None
                    elif engine.permissions.tool_guard is None:
                        from ..governance import RuleGuardian

                        engine.permissions.tool_guard = RuleGuardian()
                except Exception:  # noqa: BLE001 - never let a settings save wedge sessions
                    continue
        return {"ok": True, **self.governance_settings()}

    def set_model_key(self, api_key: str) -> dict[str, Any]:
        """Persist the model API key to the SecretStore (0600). The new provider client is
        built lazily on the next turn, so it picks the key up without a restart."""
        api_key = (api_key or "").strip()
        if not api_key:
            return {"ok": False, "error": "empty api key"}
        # Merge, don't replace: the profile may also hold a custom endpoint (base_url).
        profile = dict(self.secrets.get("provider:openai") or {})
        profile.update({"type": "api_key", "api_key": api_key})
        self.secrets.put("provider:openai", profile)
        self._refresh_provider("openai")  # rebuild the OpenAI client with the new key
        return {"ok": True, **self.get_settings()}

    def set_default_model(self, model: str) -> dict[str, Any]:
        """Set + persist the default model for new sessions (the UI pre-selects it)."""
        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "empty model"}
        self.model = model
        self._prefs["default_model"] = model
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    def set_onboarded(self, value: bool = True) -> dict[str, Any]:
        """Record that first-run setup is complete (so it isn't shown again)."""
        self._prefs["onboarded"] = bool(value)
        self._save_prefs()
        return {"ok": True, "onboarded": bool(value)}

    def set_scratch_base(self, path: str) -> dict[str, Any]:
        """Set + persist the common area where each Cowork conversation's scratch directory is
        created (default ~/OpenWorker). The raw value is stored so the UI shows it as entered;
        new conversations use it immediately (existing ones keep their provisioned dir).
        """
        path = (path or "").strip()
        if not path:
            return {"ok": False, "error": "empty path"}
        try:
            Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        self._prefs["scratch_base"] = path
        self._save_prefs()
        return {"ok": True, **self.get_settings()}

    # -- gateway + connector allow-list (inbound messaging) ---------------------
    def allow_user(
        self,
        name: str,
        user_id: str,
        team_id: Optional[str] = None,
        *,
        display_name: str = "",
    ) -> dict[str, Any]:
        out = self._set_allowed(name, user_id, team_id=team_id, add=True)
        # Directory picks arrive with the name in hand — record it so the chip
        # is readable immediately (message-driven allows learn it on arrival).
        if out.get("ok") and display_name:
            self._note_person(name, user_id, display_name)
        return out

    def disallow_user(
        self, name: str, user_id: str, team_id: Optional[str] = None
    ) -> dict[str, Any]:
        if name == "slack" and user_id in self.slack_approval_owner_ids(team_id):
            return {
                "ok": False,
                "error": "Remove this person as an approval owner first.",
            }
        return self._set_allowed(name, user_id, team_id=team_id, add=False)

    def slack_approval_owner_ids(self, team_id: Optional[str] = None) -> set[str]:
        """Stable Slack user ids allowed to resolve consequential Inbox prompts.

        Managed relay installs are installer-owned. Manual Socket Mode has no
        human OAuth identity, so its owners are selected explicitly.
        """
        key = f"slack:team:{team_id}" if team_id else "slack:default"
        profile = self.secrets.get(key) or {}
        if team_id:
            installer = str(profile.get("slack_user_id") or "").strip()
            return {installer} if installer else set()
        if profile.get("mode") == "relay":
            return set()
        return {
            str(user_id).strip()
            for user_id in (profile.get("approval_owner_ids") or [])
            if str(user_id).strip()
        }

    def set_slack_approval_owner(
        self, user_id: str, *, add: bool, display_name: str = ""
    ) -> dict[str, Any]:
        """Edit Manual Socket Mode approval owners.

        Owner status implies inbound permission. Relay ownership is derived from
        the OAuth installer and is intentionally not editable here.
        """
        user_id = str(user_id).strip()
        if not user_id:
            return {"ok": False, "error": "user_id required"}
        profile = self.secrets.get("slack:default")
        if not profile:
            return {"ok": False, "error": "Slack is not connected in Manual mode."}
        if profile.get("mode") == "relay" or profile.get("managed"):
            return {
                "ok": False,
                "error": "Relay approval ownership is set by the Slack installer.",
            }

        owners = self.slack_approval_owner_ids()
        if add:
            owners.add(user_id)
        else:
            owners.discard(user_id)
            if not owners and self._has_manual_slack_inbox_binding():
                return {
                    "ok": False,
                    "error": (
                        "Choose another approval owner before removing the last one "
                        "while Slack Inbox routing is active."
                    ),
                }
        profile["approval_owner_ids"] = sorted(owners)
        if add:
            allowed = set(profile.get("allowed_users") or [])
            allowed.add(user_id)
            profile["allowed_users"] = sorted(allowed)
        self.secrets.put("slack:default", profile)
        if display_name:
            self._note_person("slack", user_id, display_name)
        if self.gateway is not None and "slack" in self.gateway.settings:
            self.gateway.settings["slack"].allowed_users = set(
                profile.get("allowed_users") or []
            )
        return {
            "ok": True,
            "approval_owner_ids": sorted(owners),
            "allowed_users": list(profile.get("allowed_users") or []),
        }

    def _has_manual_slack_inbox_binding(self) -> bool:
        for raw in self.inbox_routing.bindings():
            if raw.get("channel") != "slack":
                continue
            team_id, _ = slack_split(str(raw.get("target") or ""))
            if team_id is None:
                return True
        return False

    def _slack_actor_owns_item(
        self,
        item,
        *,
        actor_id: str,
        chat_id: str,
        team_id: Optional[str],
    ) -> bool:
        """Authorize a Slack resolution against both its owner and delivery binding."""
        event_team, event_channel = slack_split(chat_id)
        event_team = team_id or event_team
        binding = self.inbox_routing.binding_for(item.inbox)
        owner_team = event_team
        if binding.channel == "slack":
            owner_team, bound_channel = slack_split(binding.target)
            if owner_team != event_team or bound_channel != event_channel:
                return False
        return bool(actor_id) and actor_id in self.slack_approval_owner_ids(owner_team)

    def set_inbox_binding(
        self, name: str, *, channel: Optional[str], target: str
    ) -> dict[str, Any]:
        """Persist an Inbox transport after validating its approval identity."""
        channel = str(channel or "").strip() or None
        target = str(target or "").strip()
        if channel and not target:
            return {"ok": False, "error": "Choose a destination channel."}
        if channel == "slack":
            settings = load_settings(self.secrets).get("slack")
            if settings is None or not settings.enabled:
                return {"ok": False, "error": "Slack is not connected."}
            team_id, destination = slack_split(target)
            if not destination:
                return {"ok": False, "error": "Choose a destination channel."}
            key = f"slack:team:{team_id}" if team_id else "slack:default"
            if not self.secrets.get(key):
                return {
                    "ok": False,
                    "error": "That Slack workspace is not connected.",
                }
            if not self.slack_approval_owner_ids(team_id):
                return {
                    "ok": False,
                    "error": (
                        "Choose at least one approval owner in Slack settings before "
                        "routing Inbox requests there."
                    ),
                }
        self.inbox_routing.set_binding(name, channel=channel, target=target)
        return {"ok": True, "bindings": self.inbox_routing.bindings()}

    def _set_allowed(
        self, name: str, user_id: str, *, team_id: Optional[str] = None, add: bool
    ) -> dict[str, Any]:
        """Add/remove a sender on the allow-list. With `team_id` the edit targets that
        scope's profile — a workspace's `slack:team:<id>`, or a GitHub App
        installation's `github:install:<id>` (the same per-tenant pattern);
        without, the flat `<name>:default` list (manual single-workspace mode)."""
        user_id = str(user_id).strip()
        if not user_id:
            return {"ok": False, "error": "user_id required"}
        scope = "install" if name == "github" else "team"
        profile_key = f"{name}:{scope}:{team_id}" if team_id else f"{name}:default"
        profile = self.secrets.get(profile_key)
        if not profile:
            return {
                "ok": False,
                "error": (
                    "workspace not connected" if team_id else "connector not connected"
                ),
            }
        allowed = set(profile.get("allowed_users") or [])
        allowed.add(user_id) if add else allowed.discard(user_id)
        profile["allowed_users"] = sorted(allowed)
        self.secrets.put(profile_key, profile)
        # reflect into the live gateway so it takes effect without a restart
        if self.gateway is not None and name in self.gateway.settings:
            if team_id:
                from ..connectors import TeamAuth

                teams = self.gateway.settings[name].teams
                team = teams.setdefault(team_id, TeamAuth())
                team.allowed_users = set(allowed)
            else:
                self.gateway.settings[name].allowed_users = set(allowed)
        return {"ok": True, "allowed_users": sorted(allowed), "team_id": team_id}

    async def disconnect_slack_workspace(self, team_id: str) -> dict[str, Any]:
        """Stop relaying ONE workspace: delete the cloud routing row (best-effort),
        drop the local per-team token, and hot-reload the gateway. Removing the last
        workspace also clears relay mode on slack:default so the connector reads
        disconnected (the manual Socket Mode fields, if any, are left untouched)."""
        team_id = str(team_id).strip()
        profile_key = f"slack:team:{team_id}"
        if not team_id or not self.secrets.get(profile_key):
            return {"ok": False, "error": "workspace not connected"}
        from .. import cloud
        from ..config import load_config

        await asyncio.to_thread(
            lambda: cloud.slack_disconnect_workspace(
                self.secrets, load_config(), team_id
            )
        )
        self.secrets.delete(profile_key)
        remaining = [
            m["profile"]
            for m in self.secrets.status()
            if m.get("profile", "").startswith("slack:team:")
        ]
        if not remaining:
            default = self.secrets.get("slack:default") or {}
            if default.get("mode") == "relay":
                default.pop("mode", None)
                default.pop("managed", None)
                if default.get("bot_token"):
                    # Manual Socket Mode creds predating the relay switch: keep them
                    # stored but DISABLED — removing the last workspace must never
                    # silently start listening with old tokens.
                    default["type"] = "token"
                    default["enabled"] = False
                    self.secrets.put("slack:default", default)
                else:
                    default.pop("type", None)
                    default.pop("enabled", None)
                    if default:  # e.g. a flat allow-list worth keeping
                        self.secrets.put("slack:default", default)
                    else:
                        self.secrets.delete("slack:default")
        await self.refresh_gateway()
        return {"ok": True, "remaining_workspaces": len(remaining)}

    def slack_status(self) -> dict[str, Any]:
        """Slack connection health in three honest layers (UX-DECISIONS §21):
        the desktop↔relay socket, the cloud sign-in that authorizes it, and each
        workspace's bot token. The desktop can't see the Slack↔cloud leg, so no
        layer here ever claims it — event silence ≠ outage."""
        from .. import cloud

        default = self.secrets.get("slack:default") or {}
        mode = default.get("mode") or ""
        signin = cloud.status(self.secrets)

        relay: dict[str, Any] = {
            "state": "offline",
            "reconnects": 0,
            "last_event_at": None,
            "last_error": "",
        }
        teams: dict[str, Any] = {}
        adapter = (
            self.gateway._adapters.get("slack") if self.gateway is not None else None
        )
        snapshot = getattr(
            adapter, "status", None
        )  # relay adapter only; Socket Mode has none
        if callable(snapshot):
            relay = snapshot()
            teams = relay.pop("teams", {})
        return {
            "ok": True,
            "mode": mode,
            "relay": relay,
            "signed_in": bool(signin.get("signed_in")),
            "teams": teams,
        }

    async def disconnect_github_installation(
        self, installation_id: str
    ) -> dict[str, Any]:
        """Stop relaying ONE GitHub installation: delete the cloud routing rows
        (best-effort), drop the local profile, hot-reload the gateway. The Slack
        per-workspace disconnect, GitHub flavour — a manual PAT stays untouched."""
        installation_id = str(installation_id).strip()
        from .. import cloud
        from ..config import load_config
        from ..connectors import github_installs

        if not installation_id or not self.secrets.get(
            github_installs.PREFIX + installation_id
        ):
            return {"ok": False, "error": "installation not connected"}
        await asyncio.to_thread(
            lambda: cloud.github_disconnect_installation(
                self.secrets, load_config(), installation_id
            )
        )
        result = github_installs.disconnect_install(self.secrets, installation_id)
        await self.refresh_gateway()
        return result

    def github_status(self) -> dict[str, Any]:
        """GitHub relay health, same three honest layers as Slack: the shared
        relay socket, the cloud sign-in, and per-installation token health."""
        from .. import cloud

        default = self.secrets.get("github:default") or {}
        signin = cloud.status(self.secrets)
        relay: dict[str, Any] = {
            "state": "offline",
            "reconnects": 0,
            "last_event_at": None,
            "last_error": "",
        }
        installs: dict[str, Any] = {}
        missed: dict[str, Any] = {}
        adapter = (
            self.gateway._adapters.get("github") if self.gateway is not None else None
        )
        snapshot = getattr(adapter, "status", None)
        if callable(snapshot):
            relay = snapshot()
            installs = relay.pop("installs", {})
            missed = relay.pop("missed", {})
        return {
            "ok": True,
            "mode": default.get("mode") or "",
            "relay": relay,
            "signed_in": bool(signin.get("signed_in")),
            "installs": installs,
            "missed": missed,
        }

    async def start_gateway(self) -> list[str]:
        """Build the messaging gateway and start enabled listeners. Inbound messages route to
        durable sessions: a channel message to its subscribers, a DM to the designated DM session
        (else parked). Returns the platforms whose listeners came up."""
        # Team steering/kicks are dispatched from tool threads; they need the app loop.
        self._loop = asyncio.get_running_loop()
        # 上一次进程死得难看留下的幽灵 run 先落终态，否则 UI 永远「在跑」、停不掉。
        try:
            reconciled = self.reconcile_task_runs()
            if reconciled:
                logger.info("启动对账：%s 个幽灵 run 标记为 interrupted", reconciled)
        except Exception:
            logger.warning("启动对账失败（fail-open）", exc_info=True)
        self.scheduler.start()  # tick scheduler for automations (independent of connectors)
        return await self._build_and_start_gateway()

    async def refresh_gateway(self) -> list[str]:
        """Hot-reload the messaging listeners with fresh secrets — called after a connector
        connect/disconnect so pasting new tokens takes effect immediately. A platform socket
        (Slack Socket Mode) authenticates at connect time, so new creds mean reopening that
        socket; this replaces the adapters in-process — the sidecar never restarts."""
        await self.stop_gateway()
        started = await self._build_and_start_gateway()
        print(f"[coworker] messaging gateway reloaded: {', '.join(started) or 'none'}")
        return started

    async def _build_and_start_gateway(self) -> list[str]:
        settings = load_settings(self.secrets)
        self.gateway = Gateway(
            secrets=self.secrets,
            settings=settings,
            handler=self._dispatch_inbound,
            reply_resolver=self._resolve_inbox_reply,
            interaction_handler=self._on_interaction,
            on_unauthorized=self._park_unauthorized,
        )
        # Managed Slack relay wiring (only used when a connector picks relay mode):
        # the cloud sign-in JWT authorizes the relay WebSocket, and the relay
        # endpoint comes from config. Both are lazy — Socket Mode needs neither.
        from ..cloud import fresh_access_token
        from ..config import load_config

        cloud_config = load_config()

        def _relay_token() -> str:
            return fresh_access_token(self.secrets, cloud_config) or ""

        # Every relay-mode platform shares ONE cloud socket; the hub fans frames
        # out by provider tag. Built lazily on the first relay adapter.
        relay_ws_url = getattr(cloud_config, "cloud_relay_ws_url", "") or None
        relay_hub = None
        if relay_ws_url:
            from ..connectors.relay_client import RelayHub

            relay_hub = RelayHub(relay_ws_url, _relay_token)

        async def _github_token(installation_id: str) -> str:
            from ..cloud import github_installation_token

            return await asyncio.to_thread(
                github_installation_token, self.secrets, cloud_config, installation_id
            )

        for platform, st in settings.items():
            if not st.enabled:
                continue
            profile = self.secrets.get(f"{platform}:default") or {}
            adapter = make_adapter(
                platform,
                profile,
                secrets=self.secrets,
                token_provider=_relay_token,
                relay_url=relay_ws_url,
                relay_hub=relay_hub,
                github_token_client=_github_token,
            )
            if adapter is not None:
                self.gateway.register(adapter)
        return await self.gateway.start()

    async def stop_gateway(self) -> None:
        if self.gateway is not None:
            await self.gateway.stop()
            self.gateway = None

    # -- unauthorized inbound (parked, §19) --------------------------------------
    def _note_person(
        self, platform: str, user_id: Optional[str], name: Optional[str]
    ) -> None:
        """Remember a sender's display name (persisted) so ID-keyed surfaces — the allow-list
        chips above all — can show who a U07JK… actually is. Best-effort, newest name wins.
        """
        if not user_id or not name:
            return
        key = f"{platform}:{user_id}"
        if self._people.get(key) != name:
            self._people[key] = name
            try:
                self._people_path.write_text(json.dumps(self._people))
            except OSError:
                pass

    async def _park_unauthorized(self, event) -> None:
        """Gateway callback: keep what an unallowed sender said (names already resolved by the
        adapter, best-effort) so the owner can allow-and-deliver without a re-send."""
        s = event.source
        self._note_person(s.platform, s.user_id, s.user_name)
        self.parked.park(
            platform=s.platform,
            chat_id=s.chat_id,
            chat_name=s.chat_name,
            user_id=s.user_id or "?",
            user_name=s.user_name,
            chat_type=s.chat_type,
            thread_id=s.thread_id,
            team_id=s.team_id,
            text=event.text or "",
        )

    async def resolve_unauthorized(
        self, name: str, item_id: str, action: str
    ) -> dict[str, Any]:
        """Resolve one parked message: "dismiss" throws it away; "allow" adds the sender to the
        allow-list (future messages flow); "allow_deliver" also re-injects the parked message
        through the NORMAL inbound path — buffer + subscriptions — as if it just arrived.
        """
        item = self.parked.pop(item_id)
        if item is None or item.platform != name:
            return {"ok": False, "error": "unknown item"}
        if action == "dismiss":
            return {"ok": True}
        if action not in ("allow", "allow_deliver"):
            return {"ok": False, "error": f"unknown action: {action}"}
        allowed = self._set_allowed(name, item.user_id, team_id=item.team_id, add=True)
        if not allowed.get("ok"):
            return allowed
        if action == "allow_deliver":
            from ..connectors import MessageEvent, SessionSource

            event = MessageEvent(
                text=item.text,
                source=SessionSource(
                    platform=item.platform,
                    chat_id=item.chat_id,
                    user_id=item.user_id,
                    user_name=item.user_name,
                    chat_name=item.chat_name,
                    chat_type=item.chat_type,
                    thread_id=item.thread_id,
                    team_id=item.team_id,
                ),
            )
            await self._dispatch_inbound(event)
        return {"ok": True}

    # -- per-session live view --------------------------------------------------
    def register_event_client(self, send_cb: Any) -> None:
        self._event_clients.add(send_cb)

    def unregister_event_client(self, send_cb: Any) -> None:
        self._event_clients.discard(send_cb)

    async def broadcast_event(self, message: dict) -> None:
        """Fan an app-wide event out to every /ws/events socket. Best-effort: a dead
        socket is dropped, never fatal to the caller."""
        for cb in list(self._event_clients):
            try:
                await cb(message)
            except Exception:
                self.unregister_event_client(cb)

    def register_session_client(self, session_id: str, send_cb: Any) -> None:
        self._session_clients.setdefault(session_id, set()).add(send_cb)

    def unregister_session_client(self, session_id: str, send_cb: Any) -> None:
        clients = self._session_clients.get(session_id)
        if clients is not None:
            clients.discard(send_cb)
            if not clients:
                self._session_clients.pop(session_id, None)

    def has_session_clients(self, session_id: str) -> bool:
        return bool(self._session_clients.get(session_id))

    async def promote_pending_prompts(self, session_id: str) -> int:
        """The last viewer left while the agent waits on an inline prompt: move it to the
        Inbox and mirror it, so the question is visible somewhere (see
        InboxStore.promote_to_inbox). Returns how many prompts moved."""
        if self.has_session_clients(session_id):
            return 0
        changed = self.inbox.promote_to_inbox(session_id)
        for item in changed:
            await self.mirror_inbox_item(item)
        return len(changed)

    async def broadcast_session(self, session_id: str, message: dict) -> None:
        """Fan a turn event out to every socket viewing this session. Best-effort: a dead socket
        is dropped, never fatal to the turn (delivery is socket-independent)."""
        data = message.get("data") or {}
        if message.get("type") == "permission_required" and data.get("name") == "decide_worker_call":
            # Attach the worker's actual waiting call so the lead's approval card shows WHAT
            # is being allowed/denied, not an opaque call id (§11.6).
            worker_call = self.worker_call_for(data.get("arguments") or {}, lead_session=session_id)
            if worker_call:
                message = {**message, "data": {**data, "worker_call": worker_call}}
        for cb in list(self._session_clients.get(session_id, ())):
            try:
                await cb(message)
            except Exception:
                self.unregister_session_client(session_id, cb)

    async def aclose(self) -> None:
        await self.scheduler.stop()
        await self.stop_gateway()
        await self.mcp.aclose()
        self.audit_store.close()

    # -- automation (scheduled tasks) -------------------------------------------
    def worker_call_for(
        self, arguments: dict[str, Any], *, lead_session: str | None = None
    ) -> dict[str, Any] | None:
        """The worker's waiting tool call that a lead's `decide_worker_call` answers
        (`call_id` is the worker's parked Inbox item). Attached to the lead's approval card
        so the human sees WHAT is being allowed or denied, not an id. None when the item is
        unknown or is not an approval."""
        item = self.inbox.get(str((arguments or {}).get("call_id") or ""))
        if item is None or item.kind != "approval":
            return None
        if lead_session is not None and not self._owned_worker_prompt(lead_session, arguments):
            return None
        data = item.data or {}
        task_fields = {}
        found = self.teams.for_worker_session(item.session_id)
        if found:
            from ..teams.summary import all_events

            team, _worker = found
            for event in all_events(self.team_store, team.space):
                if (
                    event["kind"] == WORKER_WAITING
                    and event["payload"].get("prompt_id") == item.id
                    and event.get("item_id") is not None
                ):
                    try:
                        task = self.team_store.get_item(
                            team.space, event["item_id"], actor=self._user_actor()
                        )
                        task_fields = {"item_id": task["id"], "item_title": task["title"]}
                    except TeamsBoardError:
                        pass
        return {
            **task_fields,
            "worker": str((arguments or {}).get("worker") or ""),
            "tool": str(data.get("tool") or ""),
            "arguments": data.get("arguments") or {},
            "reason": (item.body or "").split("\n", 1)[0],
            "state": item.state,
            "resolution": item.resolution,
        }

    def _owned_worker_prompt(self, lead_session: str, arguments: dict):
        """The worker's parked approval item this lead is allowed to answer, or None.

        Ownership = the named worker is on THIS lead's team and the item belongs to that
        worker's session. Never trusts the lead's own claim of ownership — checked against
        the team registry."""
        team = self.teams.for_lead_session(lead_session)
        member = (
            next(
                (
                    w
                    for w in team.workers
                    if w.actor == str(arguments.get("worker") or "").strip().lower()
                ),
                None,
            )
            if team
            else None
        )
        item = self.inbox.get(str(arguments.get("call_id") or ""))
        return (
            item
            if member and item and item.kind == "approval" and item.session_id == member.session_id
            else None
        )

    def worker_review_context(self, lead_session: str, arguments: dict) -> dict:
        """Resolve exactly one owned, pending call and carry its permission floors.

        Read only harness state. No worker transcript, lead rationale, file contents,
        or environment values are added to the reviewer's context."""
        from ..providers import ToolCall

        item = self._owned_worker_prompt(lead_session, arguments)
        if item is None or item.state != "pending":
            return {
                "hard_deny": True,
                "reason": "The worker request is missing, belongs to another team, or was already answered.",
            }
        name = item.data.get("tool")
        args = item.data.get("arguments")
        if not isinstance(name, str) or not isinstance(args, dict) or name == "decide_worker_call":
            return {"hard_deny": True, "reason": "The original worker action is invalid."}
        worker = self.get_engine(item.session_id)
        spec = worker.registry.get(name)
        if spec is None:
            return {"hard_deny": True, "reason": "The worker tool is no longer available."}
        decision = worker.permissions.evaluate(name, args, spec.metadata)
        call = ToolCall(id=item.id, name=name, arguments=args)
        downloaded = worker._downloaded_target(call) is not None
        # Preserve the floor recorded when the worker parked the call, even after
        # an engine rebuild loses runtime-only provenance.
        saved = item.data.get("escalation") or {}
        human_only = decision.human_only or downloaded or saved.get("kind") == "human_required"
        reason = (
            (saved.get("reason") if saved.get("kind") == "human_required" else None)
            or (
                "The worker would execute a downloaded file; a human must decide."
                if downloaded
                else decision.reason
            )
        )
        return {
            "tool": name,
            "arguments": args,
            "reason": reason,
            "hard_deny": not decision.allowed and not decision.needs_user,
            "human_only": human_only,
            "provenance": item.data.get("provenance") or worker._provenance(call),
            "context": {
                "worker": arguments.get("worker"),
                "request_id": item.id,
                "workspace": str(worker.permissions.workspace_root or ""),
                "working_folders": [
                    {"path": str(path), "writable": writable}
                    for path, writable in worker.permissions._resolved_roots()
                ],
                "permission_reason": decision.reason,
                "mode": worker.permissions.mode.value,
                "settings_epoch": worker.reviewer_settings_epoch,
                "category": getattr(spec.metadata, "category", ""),
                "destination": item.data.get("mcp_destination"),
                "runtime_facts": getattr(worker, "runtime_facts", {}),
            },
        }

    def approval_prompt_data(self, session_id: str, request) -> dict[str, Any]:
        """Extra Inbox-item payload for a parked approval. Always carries the tool name +
        arguments so the GUI can render the same humanized card (§35) it shows live —
        without them a reopened session fell back to the raw 'Run `tool`?' treatment.
        Automation runs additionally carry the owning task + (when the call is eligible)
        the exact target a standing rule would pin: the GUI offers "Allow every time" only
        when both are present — in-app only, never on Slack-mirrored buttons (§25)."""
        from ..permissions import standing_rule_candidate

        data: dict[str, Any] = {
            "tool": request.tool_name,
            "arguments": getattr(request, "arguments", None) or {},
            # §35 parity (OPE-136): the parked card shows the same scope chip + reason the
            # live card would — carry the escalation kind, provenance, and MCP destination
            # stamped on the request, plus any non-boilerplate reason.
            "escalation": getattr(request, "escalation", None),
            "provenance": getattr(request, "provenance", ""),
        }
        if request.tool_name == "decide_worker_call":
            worker_call = self.worker_call_for(data["arguments"], lead_session=session_id)
            if worker_call:
                data["worker_call"] = worker_call
                # Store-owned dependency: atomically retires this redundant gate if the
                # original worker prompt resolves before or after creation.
                data["worker_prompt_id"] = str(data["arguments"]["call_id"])
        category = getattr(getattr(request, "metadata", None), "category", "")
        if category:
            data["category"] = category
        dest = getattr(request, "mcp_destination", None)
        if dest:
            data["mcp_destination"] = dest
        reason = (getattr(request, "reason", "") or "").strip()
        if reason and reason != "requires approval":
            data["reason"] = reason
        task = self.task_store.task_for_run_session(session_id)
        if task is None:
            return data
        data.update({"task_id": task.id, "task_title": task.title})
        target = standing_rule_candidate(
            request.tool_name,
            getattr(request, "arguments", None) or {},
            getattr(request, "metadata", None),
        )
        if target:
            data["standing_target"] = target
        return data

    def mint_task_rule(
        self, session_id: str, tool_name: str, arguments: Any, metadata: Any = None
    ) -> bool:
        """Persist a standing rule a human minted via "Allow every time" on a run's
        approval card (§25's retrofit path). Server-side validation, not trust in the
        card: the session must be an automation run and the call must be rule-eligible
        (external risk, declared target argument, non-empty target). Also applies the
        rule to the live engine so the run's next call auto-allows."""
        from ..permissions import standing_rule_candidate

        task = self.task_store.task_for_run_session(session_id)
        if task is None:
            return False
        target = standing_rule_candidate(tool_name, arguments or {}, metadata)
        if not target or not task.add_rule(tool_name, target):
            return False
        self.task_store.save(task)
        engine = self._engines.get(session_id)
        if engine is not None:
            engine.permissions.task_rules.setdefault(tool_name, set()).add(target)
        try:
            self.audit_store.append(
                {
                    "session_id": session_id,
                    "tool": tool_name,
                    "arguments": arguments or {},
                    "stage": "standing_rule_minted",
                    "status": "granted",
                    "reason": f"allow every time: {tool_name} → {target} (task {task.id})",
                }
            )
        except Exception:
            pass
        return True

    def approval_outcome(self, resolution: str, request, session_id: str):
        """Map an approval resolution (from any surface) to an ApprovalOutcome, handling
        the task-persistent "always_task" vocabulary alongside the session-scoped ones.

        Server-side validated, not trusted from the caller: a grant that no UI offers for
        this tool is downgraded to a one-time approval rather than honoured. The GUI already
        hides the broad "always allow" for run_shell / connectors / save_skill, and Slack
        mirrors only ever render approve/deny — but `POST /v1/inbox/{id}/resolve` takes a raw
        string, so without this check any local API caller could mint a session-wide
        any-argument shell grant. Same philosophy as mint_task_rule: validate here, don't
        trust the card.
        """
        from ..engine import ApprovalOutcome

        if resolution == "superseded":
            # A lead's proxy approval whose original worker request was already resolved
            # (e.g. the human answered the worker's own card directly). Nothing to do but
            # record it — the engine retires the proxy without re-executing.
            original = (
                self._owned_worker_prompt(
                    session_id, getattr(request, "arguments", None) or {}
                )
                if request.tool_name == "decide_worker_call"
                else None
            )
            return (
                ApprovalOutcome.SUPERSEDED
                if original is not None and original.state == "resolved"
                else ApprovalOutcome.DENY
            )
        if resolution == "always_task":
            minted = self.mint_task_rule(
                session_id,
                request.tool_name,
                getattr(request, "arguments", None),
                getattr(request, "metadata", None),
            )
            if not minted:
                self._audit_grant_refused(session_id, request, resolution)
            return ApprovalOutcome.ONCE
        try:
            outcome = ApprovalOutcome(resolution)
        except ValueError:
            if resolution == "allow":
                return ApprovalOutcome.ONCE
            if resolution == "always":
                outcome = ApprovalOutcome.ALWAYS_TOOL
            else:
                return ApprovalOutcome.DENY
        if outcome in (
            ApprovalOutcome.ALWAYS_TOOL,
            ApprovalOutcome.ALWAYS_COMMAND,
            ApprovalOutcome.ALWAYS_DOMAIN,
        ) and not _grant_offered(outcome, request):
            self._audit_grant_refused(session_id, request, resolution)
            return ApprovalOutcome.ONCE
        return outcome

    def audit_autonomy_change(
        self, session_id: str, kind: str, before: Any, after: Any
    ) -> None:
        """Record a change to how much the agent may do unsupervised — the permission mode,
        or the attended/unattended toggle. Without this, "who turned on auto mode, and when"
        is unanswerable from the audit store, which is at odds with the per-call trail the
        rest of the engine keeps. Raising autonomy is flagged so it can be filtered."""
        # AUTO_APPROVE sits above interactive (turning the reviewer on means fewer human
        # checks — that IS raising autonomy) and below bypass, which removes checks
        # entirely. "auto" is the legacy spelling of "bypass-approvals".
        order = {
            "discuss": 0,
            "plan": 1,
            "interactive": 2,
            "custom": 2,
            "auto-approve": 3,
            "auto": 4,
            "bypass-approvals": 4,
        }
        raised = (
            order.get(str(after), 0) > order.get(str(before), 0)
            if kind == "mode"
            else bool(after) and not bool(before)
        )
        try:
            self.audit_store.append(
                {
                    "session_id": session_id,
                    "tool": "",
                    "arguments": {},
                    "stage": f"{kind}_changed",
                    "status": "raised" if raised else "lowered",
                    "reason": f"{kind}: {before} → {after}",
                }
            )
        except Exception:
            pass

    def set_unattended(self, session_id: str, on: bool) -> dict[str, Any]:
        """Flip the attended/unattended toggle, with an audit row. Note this changes only
        WHERE the human is reached, never the autonomy ceiling (that's the mode) — but it is
        still worth recording, since an unattended session routes prompts away from the
        screen the user is looking at."""
        before = self.unattended.is_unattended(session_id)
        self.unattended.set(session_id, on)
        if before != on:
            self.audit_autonomy_change(session_id, "unattended", before, on)
        return {"ok": True, "session_id": session_id, "unattended": on}

    def _audit_grant_refused(self, session_id: str, request, resolution: str) -> None:
        try:
            self.audit_store.append(
                {
                    "session_id": session_id,
                    "tool": getattr(request, "tool_name", ""),
                    "arguments": getattr(request, "arguments", None) or {},
                    "stage": "grant_refused",
                    "status": "downgraded",
                    "reason": (
                        f"resolution {resolution!r} is not offered for this tool — "
                        "applied as a one-time approval"
                    ),
                }
            )
        except Exception:
            pass

    def _scheduled_approver(self, task, session_id: str):
        """Auto-approve every permission request for a scheduled (unattended) run.

        Consent is captured at creation time (the `requires_approval` consent card on
        create_scheduled_task, or the GUI automation form), so a running automation is
        trusted to act without a human at the keyboard. Falling back to the Inbox would
        just park the run forever — nobody is watching an unattended task — so we approve
        inline instead.

        Note: the governance Tool Guard's CRITICAL findings are denied outright by the
        permission engine *before* this callback is ever reached. That floor is narrow,
        though: only findings graded exactly CRITICAL are denied, and the classic
        data-loss commands (`rm -rf`, `find -delete`, `mv`, `chmod 777`) grade HIGH, so
        they do NOT stop a scheduled run. HIGH/MEDIUM shell findings never become a
        prompt for an unattended run either — the user opted into autonomous execution
        when the task was created."""
        from ..engine import ApprovalOutcome

        async def approver(request):
            return ApprovalOutcome.ONCE

        return approver

    def _scheduled_write_roots(self, task) -> list:
        """Writable roots for a scheduled task's engine.

        History: this exists because write-path scoping once blocked scheduled
        deliverables — `_build_task_engine` never passed `roots`, so the engine fell back
        to a single writable root = the workspace, and anything the user asked an
        automation to drop on the Desktop (a common "so I can find it" target) came back
        "path is not in a writable directory".

        What these roots still do (re-measured 2026-10-02): scheduled runs execute in
        BYPASS_APPROVALS, whose early return skips the permission engine's write-path
        gate — so `roots` no longer gates the FILE TOOLS for a scheduled run. They are
        still load-bearing, so do not delete this as dead code:
          * they are the write scope the macOS sandbox hands a run's SHELL
            (agent.py `mounts_from_write_roots`) — that shell-side check does still fire;
          * they are the `save_skill` file-gathering allowlist;
          * they resume governing the file tools the moment a run is not in bypass
            (interactive / delegated runs, or any future restore of the floors).

        The set is (a) the user's standard drop folders (Desktop / Documents /
        Downloads), (b) any explicit write target the task's consent card already
        surfaced + the human approved, and (c) ~/.deskmate/auto_tasks for the local
        assistant's own ledger. All are the user's own local folders.
        """
        from ..automation.models import rule_parts
        from ..risk import WRITE_TOOLS

        ws = Path(task.workspace).expanduser().resolve()
        roots: list = [RootDir(path=ws, writable=True, label="workspace")]
        # User-owned output folders a scheduled task may legitimately deliver into.
        for d in (Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"):
            try:
                rd = d.expanduser().resolve()
            except Exception:
                continue
            if rd.is_dir():
                roots.append(RootDir(path=rd, writable=True, label=d.name))
        # Honor explicit write grants declared on the task (e.g. write_file → ~/Desktop/x.md):
        # the consent card already surfaced these exact targets and the human approved them.
        for entry in task.always_allowed_tools or []:
            tool, target = rule_parts(entry)
            if tool in WRITE_TOOLS and target:
                p = Path(target).expanduser()
                if not p.is_absolute():
                    p = ws / p
                try:
                    p = p.resolve()
                except Exception:
                    continue
                parent = p if p.is_dir() else p.parent
                if parent.is_dir() and parent != ws:
                    roots.append(RootDir(path=parent, writable=True, label=parent.name))
        # (c) The local assistant's own ledger lives outside any workspace. Its scheduled
        # tasks legitimately append to it (daily reading → ~/.deskmate/auto_tasks/
        # timeline.jsonl) and used to fail with "Operation not permitted", handing the
        # user a .sh to run by hand instead. Scope: only that subdirectory — ~/.deskmate
        # itself holds settings files, and no other agent needs the ledger.
        if getattr(task, "agent", None) == "deskmate":
            try:
                ledger = (Path.home() / ".deskmate" / "auto_tasks").expanduser().resolve()
            except Exception:
                ledger = None
            if ledger is not None and ledger.is_dir():
                roots.append(RootDir(path=ledger, writable=True, label="auto_tasks"))
        # De-duplicate by resolved path (keep first), preserving the workspace as index 0.
        seen: set[str] = set()
        deduped: list = []
        for r in roots:
            key = str(r.path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        return deduped

    def _seed_task_permissions(self, engine: TurnEngine, task) -> None:
        """Apply a task's standing allowances to an engine: target-bound rules feed the
        permission engine's matcher (connector tools included — the target binding is the
        safety); name-only legacy entries keep their session-allowlist behavior.

        Write-file tools are also granted for the run session. Background: a task run
        (whether AUTO-scheduled or a manual "Run now" over the WS) was already authorized
        at creation, and its deliverable folders (Desktop / Documents / Downloads + any
        declared write target) live in the writable roots. But a *manual* run rides an
        INTERACTIVE engine, and `write_file` is WRITE_LOCAL risk — the standing-rule
        matcher only covers EXTERNAL risk (standing_rule_candidate returns None for
        write_local), so the call would otherwise park on a "requires approval" card that
        the unattended/same-task flow shouldn't need. Granting the write tools at the
        session level (paths still bounded by the writable-root gate) makes manual runs
        behave like the AUTO path: authorized to write where the task is allowed to."""
        engine.permissions.task_rules = task.standing_rules()
        for tool in task.name_allowed_tools():
            engine.permissions.allow_tool_for_session(tool)
        from ..risk import WRITE_TOOLS

        for tool in WRITE_TOOLS:
            engine.permissions.allow_tool_for_session(tool)

    def _build_task_engine(
        self, task, *, session_id: str, messages: Optional[list[dict[str, Any]]] = None
    ) -> TurnEngine:
        ag = get_agent(task.agent)
        Path(task.workspace).mkdir(parents=True, exist_ok=True)
        # Scheduled runs own the workspace as the primary writable root, plus the user's
        # common drop folders (Desktop / Documents / Downloads) and any write target the task
        # declared on its consent card — so an automation told to deliver an article to the
        # Desktop can actually write there. NOTE: this comment used to claim the permission
        # engine's write-path gate "runs in ALL modes, including AUTO" — it does NOT.
        # Under BYPASS_APPROVALS the gate is skipped (evaluate() short-circuits before it),
        # so the roots no longer gate the file tools for a scheduled run; they are still
        # passed because they scope the run's SHELL through the macOS sandbox. Details and
        # the full list of what survives: _scheduled_write_roots.
        roots = self._scheduled_write_roots(task)
        # 自我驱动（autonomy loop）：目标制任务才注册 sleep_for / sleep_until /
        # wake_on / wake_on_event，让她自己安排下一次复查，而不是被 cron 硬推。
        # 普通一次性任务保持原样（回归）。
        goal_wake = None
        goal_asker = None
        try:
            from deskmate.autonomy.goal import goal_enabled

            if goal_enabled(task):
                goal_wake = getattr(self, "wakes", None)
                # needs_you：目标制任务注册 ask_user，缺信息时落 Inbox 找人问，
                # 不是闷头猜或者直接报错；超时会自动放行并让她说明卡点。
                try:
                    goal_asker = self._task_question_asker(
                        session_id, getattr(task, "agent", "cowork"), task=task
                    )
                except Exception:
                    goal_asker = None
        except Exception:
            goal_wake, goal_asker = None, None
        engine = build_engine(
            agent=ag,
            workspace=task.workspace,
            model=task.model or self.model,
            messages=messages,
            wake_store=goal_wake,
            question_asker=goal_asker,
            # Scheduled runs are autonomous by design: the human already signed off when
            # the task was created (the `requires_approval` consent card on
            # create_scheduled_task, or the GUI automation form). Running unattended in
            # INTERACTIVE mode would park a per-run approval card in the Inbox that nobody
            # can click, hanging the run forever (see coworker/automation/tools.py:22).
            # BYPASS_APPROVALS grants full access for the run. The "hard floors" this
            # comment used to promise are NOT all there: writable-root scoping sits BELOW
            # the mode short-circuit in evaluate(), so it is skipped. What actually
            # survives is self-protection and — while the Tool Guard is loaded — shell
            # commands graded exactly CRITICAL (data-loss commands like `rm -rf` grade
            # HIGH and pass). See the block at the top of permissions.evaluate().
            mode=Mode.BYPASS_APPROVALS,
            approver=self._scheduled_approver(task, session_id),
            provider=self.provider,
            memory_store=self.memory_store,
            memory_workspace=self._memory_key_for(None, task.workspace),
            memory_off=not self.memory_settings.enabled,
            memory_saving_enabled=lambda: self.memory_settings.enabled,
            # Callable, not a snapshot: editing your instructions in Settings applies
            # to conversations already open (same reason as the saving switch).
            user_rules=lambda: self.memory_settings.user_rules,
            on_memory_saved=self._memory_saved_notifier(session_id),
            secrets=self.secrets,
            # No scheduling tools inside a scheduled run: the executing agent's job is to DO the
            # task, and instructions that mention timing ("every day at 5:32pm…") otherwise tempt
            # it to create another automation instead of running this one.
            task_store=None,
            roots=roots,
            session_id=session_id,
            audit_sink=self.audit_store.append,
            # Scheduled runs respect the same per-session connection hierarchy as live sessions:
            # expose only the persona's effective-enabled connectors' tools (§4.3).
            connector_filter=self.effective_connectors(session_id, task.agent),
            # User governance/sandbox settings also govern scheduled runs.
            **self.governance_settings(),
            skill_filter=lambda sid=session_id, w=task.workspace, a=task.agent: (
                self.effective_skill_names(sid, w, agent=a)
            ),
            extra_skill_dirs=(
                [d] if (d := self.persona_skill_scope(task.agent)[0]) is not None else None
            ),        )
        self._seed_task_permissions(engine, task)
        # 定时/手动运行的任务引擎：跑出的 web_search 等工具结果不是萧盛影响，
        # 必须打上自动化标记，否则检索转储会冒充满「内部·萧盛」标。
        engine.is_automation = True
        return engine

    # -- mirroring inbox items to a bound channel -------------------------------
    async def mirror_inbox_item(self, item) -> None:
        """Mirror an Inbox item to its bound channel. Discrete choices (approve/deny, ask_user
        options) render as BUTTONS — the item id rides in each, so a click resolves it
        unambiguously. Free-text answers aren't offered over messaging (open the app).
        """
        from ..interactions import buttons_for

        binding = self.inbox_routing.binding_for(item.inbox)
        if not (binding.channel and self.gateway is not None):
            return
        if binding.channel == "slack":
            team_id, _ = slack_split(binding.target)
            # Legacy bindings may predate approval ownership. Keep the item
            # available in-app, but never mirror it to an ownerless channel.
            if not self.slack_approval_owner_ids(team_id):
                return
        target = f"{binding.channel}:{binding.target}"
        body = "\n".join(p for p in (item.title, item.body) if p).strip()
        buttons = buttons_for(item)
        try:
            if buttons:
                await self.gateway.deliver_interactive(target, body, buttons)
            else:
                await self.gateway.deliver(
                    target,
                    f"{body}\n(Open the app to respond.)\n[ow:{item.id}]".strip(),
                )
        except Exception:
            pass

    # -- interactive prompt buttons (Slack/Telegram) ----------------------------
    async def _on_interaction(self, event) -> None:
        """A button click on a mirrored Inbox prompt. The button value carries the item id + the
        resolution, so this is unambiguous — resolve the item, then swap the buttons for the
        outcome. Resolving releases any agent suspended on it (first-responder-wins)."""
        from ..interactions import decode

        decoded = decode(getattr(event, "value", "") or "")
        if decoded is None:
            return
        item_id, resolution = decoded
        item = self.inbox.get(item_id)
        if item is None:
            return
        protected_kinds = {"approval", "directory", "plan"}
        if (
            getattr(event, "platform", "") == "slack"
            and item.kind in protected_kinds
        ):
            actor_id = str(getattr(event, "user_id", "") or "")
            if not self._slack_actor_owns_item(
                item,
                actor_id=actor_id,
                chat_id=getattr(event, "chat_id", "") or "",
                team_id=getattr(event, "team_id", None),
            ):
                if self.gateway is not None:
                    await self.gateway.reject_interaction(event)
                return
        already = item is not None and item.state != "pending"
        resolved = await self.resolve_inbox(item_id, resolution)
        if not resolved and not already:
            return
        who = getattr(event, "user_name", None) or "someone"
        title = item.title
        outcome = "already resolved" if already else f"“{resolution}” — by {who}"
        if self.gateway is not None and getattr(event, "message_id", None):
            try:
                await self.gateway.update_message(
                    getattr(event, "platform", "slack"),
                    getattr(event, "chat_id", ""),
                    event.message_id,
                    f"{title}\n✅ {outcome}",
                )
            except Exception:
                pass

    # -- inbox replies over messaging connectors --------------------------------
    def _resolve_inbox_reply(self, event) -> bool:
        """Try to handle an inbound Slack/Telegram message as an Inbox reply. Returns True if the
        message carried an `[ow:<id>]` token (so it's consumed here, not routed as a new turn) —
        resolving the item also releases any agent suspended on it."""
        from ..inbox_routing import resolve_from_reply

        text = getattr(event, "text", "") or ""

        def _resolve(item_id: str, resolution: str) -> bool:
            item = self.inbox.get(item_id)
            if item is None:
                return False
            if (
                getattr(event.source, "platform", "") == "slack"
                and item.kind in {"approval", "directory", "plan"}
            ):
                actor_id = str(getattr(event.source, "user_id", "") or "")
                if not self._slack_actor_owns_item(
                    item,
                    actor_id=actor_id,
                    chat_id=getattr(event.source, "chat_id", "") or "",
                    team_id=getattr(event.source, "team_id", None),
                ):
                    return False
            return self.inbox.resolve(item_id, resolution)

        return resolve_from_reply(text, _resolve) is not None

    # -- self-wake resumption ---------------------------------------------------
    async def _scheduler_tick(self) -> None:
        """Drain team queues before due sleeps so simultaneous activity combines.
        Team deliveries dispatch as tasks (a long worker turn must not stall the
        scheduler)."""
        try:
            await self.team_tick()
        except Exception:
            logger.exception("team tick failed")
        await self.resume_due_wakes()

    async def resume_due_wakes(self) -> int:
        """Resume sessions whose self-wakes are due (called each scheduler tick). A suspended
        agent (it called sleep_for / sleep_until / wake_on / wake_on_event and ended its turn) is
        re-invoked on its own session with a wake message so it continues where it left off.
        Dispatch is a task, not a sequential await: one slow worker turn must not stall every
        other wake behind it. Returns the count dispatched."""
        resumed = 0
        for wake in self.wakes.due():
            if wake.session_id in self.wakes.stopped_sessions:
                if wake.kind == "timer":
                    self.wakes.cancel_sleep(
                        wake.session_id, "the user stopped the session"
                    )
                continue
            if self.is_running(wake.session_id) or wake.session_id in self._team_inflight:
                continue
            self._team_inflight.add(wake.session_id)

            async def deliver(wake=wake):
                try:
                    await self._resume_wake(wake)
                except Exception:
                    logger.exception(
                        "self-wake delivery failed for %s", wake.session_id
                    )
                finally:
                    self.wakes.mark_fired(wake.id)
                    self._team_inflight.discard(wake.session_id)
                    self.kick_team_tick()

            asyncio.create_task(deliver())
            resumed += 1
        return resumed

    async def _combined_extra_tick(self) -> None:
        """每次 scheduler tick 的扩展钩子：先恢复 self-wake，再触发 DeskMate 后台主动推理。

        默认只做 OpenWorker 原生 resume_due_wakes。DeskMate 后台主动推理**默认开启**（P0-2：
        开箱即用、防睡死——scheduler 每 30s 常驻 tick，不依赖前端轮询）；仅当环境变量
        DESKMATE_PROACTIVE_BACKGROUND=0 时显式关闭。所有外部调用都包在 try/except 中：
        即使 deskmate 未安装或出错，也不影响 OpenWorker 原生调度行为。
        """
        # 官方 Teams 框架 tick（resume self-wake 会话 + 团队队列排空）作为统一入口，
        # 使合并进来的 Teams/Triage Lead 框架随调度器运行；底层仍走原生 resume 逻辑。
        try:
            await self._scheduler_tick()
        except Exception:
            logging.exception("scheduler/team tick failed in combined extra_tick")

        # P0-2：后台主动性默认开启。未显式设置或 =1 → 开；仅 DESKMATE_PROACTIVE_BACKGROUND=0 → 关。
        # （run.py 启动期 _load_env_file 已把 ~/.config/coworker/.env 灌入 os.environ，
        #   因此 .env 里的设置能真正生效——env 死结已解。）
        if os.environ.get("DESKMATE_PROACTIVE_BACKGROUND", "1") != "0":
            try:
                from pathlib import Path as _Path

                from deskmate.proactivity.background import make_extra_tick
                from deskmate.proactivity.tick import ProactiveEngine

                desk_dir = Path(
                    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
                )
                # Reuse cached engine to preserve DisturbManager state across ticks
                if self._deskmate_proactive_engine is None:
                    self._deskmate_proactive_engine = ProactiveEngine(base_dir=desk_dir)
                tick = make_extra_tick(self._deskmate_proactive_engine)
                result = await tick()
                if result is not None:
                    await self._broadcast_deskmate_suggestion(result)
            except Exception:
                logging.exception("deskmate proactive background tick failed")

    async def _broadcast_deskmate_suggestion(self, result) -> None:
        """把一次 proactive tick 产生的「建议」广播给所有已连接会话。

        DeskMate 浮层只在 agent=="deskmate" 的会话里渲染，因此广播给全部会话是安全的
        （其它 agent 的窗口没有监听 deskmate:suggestion 的组件）。
        """
        try:
            if not getattr(result, "has_suggestion", False):
                return
            j = result.judgment
            if j is None:
                return
            # P1（2026-11-05）：后台广播原本不落台账、不记 delivered_at，
            # 「接受/忽略」无账可查（17:50 后零新增、零送达变化）。
            # 补一条送达凭证，id 同时用作 WS payload 的 id，供反馈回查。
            rid = ""
            target = ""
            try:
                from pathlib import Path as _Path

                from deskmate.proactivity.ledger import record_suggestion_delivery
                from deskmate.proactivity.writing_context import extract_book_titles

                titles = extract_book_titles(getattr(j, "suggestion_content", ""))
                target = sorted(titles)[0] if titles else ""
                desk_dir = _Path(
                    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
                )
                rid = record_suggestion_delivery(
                    base_dir=desk_dir,
                    content=getattr(j, "suggestion_content", ""),
                    suggestion_type=getattr(j, "suggestion_type", ""),
                    reasoning=getattr(j, "reasoning", ""),
                    confidence=getattr(result.decision, "confidence", 0.0)
                    if result.decision
                    else 0.0,
                    target=target,
                    delivery_channel="background_ws",
                )
            except Exception:
                logging.exception("deskmate suggestion delivery ledger failed")
            payload = {
                "id": rid or f"sug-{int(time.time() * 1000)}",
                "ledger_id": rid,
                "target": target,
                "suggestion_type": getattr(j, "suggestion_type", ""),
                "suggestion_content": getattr(j, "suggestion_content", ""),
                "confidence": getattr(result.decision, "confidence", 0.0)
                if result.decision
                else 0.0,
                "reasoning": getattr(j, "reasoning", ""),
            }
            msg = {"type": "deskmate_suggestion", "data": payload}
            for sid in list(self._session_clients.keys()):
                try:
                    await self.broadcast_session(sid, msg)
                except Exception:
                    logging.exception("broadcast deskmate suggestion failed for %s", sid)
        except Exception:
            logging.exception("broadcast deskmate suggestion failed")

    def mark_running(self, session_id: str) -> None:
        self._running_sessions.add(session_id)

    def try_mark_running(self, session_id: str) -> bool:
        """Atomically claim an idle session for one turn on the server event loop."""
        if session_id in self._running_sessions:
            return False
        self._running_sessions.add(session_id)
        return True

    def mark_idle(self, session_id: str) -> None:
        self._running_sessions.discard(session_id)
        # Every turn path (WS, background delivery, durable resume) marks idle when it
        # finishes — the one shared post-turn moment, so auto-titling hooks in here and
        # can never add latency to the response itself.
        self._maybe_autotitle(session_id)
        # Team sessions: a finished turn is the moment new board events exist (an
        # assign, a review transition) — kick the queue drain now instead of waiting
        # for the next scheduler tick. Cheap no-op for teamless sessions.
        if self.teams.for_lead_session(session_id):
            self._team_last_alive[session_id] = time.time()
        if self._loop is not None and (
            self.teams.for_lead_session(session_id)
            or self.teams.for_worker_session(session_id)
        ):
            asyncio.run_coroutine_threadsafe(self.team_tick(), self._loop)
        if session_id in self._promotion_rebuild:
            # Promotion happened this turn: drop the cached engine so the next turn
            # rebuilds with the new primary (relative anchoring, env snapshot, git).
            self._promotion_rebuild.discard(session_id)
            self._engines.pop(session_id, None)

    def is_running(self, session_id: str) -> bool:
        return session_id in self._running_sessions

    async def _resume_wake(self, wake) -> None:
        message = self._wake_message(wake)
        # A lead's timer wake carries the staleness digest — pure code over the
        # board, scoped by role membership (teamless sessions get a bare wake).
        try:
            digest = self.team_staleness_digest(wake.session_id)
        except Exception:
            digest = ""
        if digest:
            message = f"{message}\n\n{digest}"
        # 自我驱动：任务 run 会话醒来的那一轮也必须是 TaskRun（checkpoint/投递/结案/
        # 叫停都还在账本里），否则她醒来干完活，结果只会留在会话文件里没人知道。
        task = None
        try:
            task = self.task_store.task_for_run_session(wake.session_id)
        except Exception:
            task = None
        if task is not None:
            if not getattr(task, "enabled", True):
                self._cancel_task_wakes(wake.session_id, "task is paused")
                return
            try:
                await self._run_scheduled_task(
                    task, "wake", session_id=wake.session_id, wake=wake
                )
                return
            except Exception:
                logger.exception(
                    "self-wake task resume failed for %s — falling back to a plain turn",
                    wake.session_id,
                )
        await self.deliver_to_session(wake.session_id, message)

    async def deliver_to_session(
        self, session_id: str, message: str, *, source: Optional[dict[str, Any]] = None
    ) -> None:
        """Deliver an out-of-band message to a (durable) session — the agent stays resumable
        forever, so this works with no live socket. Busy (mid tool-loop): steer it into the live
        turn at its next step (don't start a colliding run). Idle: run a fresh background turn
        (results persist; if the session is Unattended, any approvals route to the Inbox). Shared
        by self-wake and channel-subscription delivery. `source` is the display-only MessageSource
        sidecar for connector messages (framed `message` stays the model-facing text).
        """
        engine = self.get_engine(session_id)
        if engine is None:
            return
        if not self.try_mark_running(session_id):
            engine.queue_steering(message, source)
            return
        try:
            async for event in engine.run(message, source=source):
                # Stream every event to any socket viewing this session, so a background turn
                # (channel delivery, self-wake, durable resume) is seen live — not just on reselect.
                await self.broadcast_session(
                    session_id, {"type": event.type.value, "data": event.data}
                )
                # A background turn has no user watching to read an inline error: a dead model or
                # tool failure would otherwise vanish. Log it and park it in the dead-letter store.
                if event.type.value == "error":
                    reason = (event.data or {}).get("error", "unknown error")
                    logger.warning(
                        "background turn failed for %s: %s", session_id, reason
                    )
                    self.unrouted.record(session_id, "-", message, reason=reason)
            self.save(session_id, engine)
        except (
            Exception
        ) as exc:  # an unexpected raise out of the turn must not be swallowed
            logger.warning("background turn crashed for %s: %s", session_id, exc)
            self.unrouted.record(session_id, "-", message, reason=str(exc))
            await self.broadcast_session(
                session_id, {"type": "error", "data": {"error": str(exc)}}
            )
        finally:
            self.mark_idle(session_id)
            await self.broadcast_session(session_id, {"type": "turn_done", "data": {}})

    # -- channel subscriptions (inbound messaging) ------------------------------
    async def _dispatch_inbound(self, event) -> None:
        """Route a non-token inbound message. Channel messages are buffered (for catch-up) and
        fanned out to every subscribed session; a DM (or any non-channel) goes to the user-designated
        DM session (delivered like any background turn) or, if none is set, is parked as unrouted.
        """
        src = event.source
        text = getattr(event, "text", "") or ""
        who = src.user_name or src.user_id or "?"
        channel = f"{src.platform}:{src.chat_id}"  # thread-agnostic channel address
        self._note_person(src.platform, src.user_id, src.user_name)
        # Structured sidecar (display-only) built from the resolved identities on the event — the
        # framed text below stays the model-facing `content`; `ms.text` carries the RAW message.
        ms = MessageSource(
            connector=src.platform,
            kind="channel" if src.chat_type in ("channel", "group") else "dm",
            channel_id=src.chat_id,
            channel_name=src.chat_name or src.chat_id,
            sender_id=src.user_id or "",
            sender_name=src.user_name or src.user_id or "?",
            ts=_inbound_epoch(getattr(event, "message_id", None)),
            text=text,
        )
        if src.chat_type in ("channel", "group"):
            self.channel_buffer.record(
                channel, who, text, name=src.chat_name
            )  # buffer all, even unsubscribed
            subs = self.subscriptions.for_channel(channel)
            # §31 mention router: a direct @-mention of the bot outranks the passive fan-out —
            # subscribed sessions must answer it; an unsubscribed channel spawns (or steers)
            # the per-thread coworker session.
            if getattr(event, "mentions_me", False):
                await self._route_mention(event, ms, subs)
                return
            if subs:
                # Chattiness tiers (§31): untagged channel traffic is judgement-only —
                # silence is the default; the must-respond framing is the mention path's.
                msg = (
                    f"💬 New message on {src.chat_name or channel} from {who}: {text}\n"
                    f"(You're subscribed to this channel but were NOT mentioned. Use your "
                    f"judgement: stay silent unless the message clearly concerns your job and "
                    f"a reply adds real value — most channel chatter needs no response from "
                    f'you. If you do reply, use the send_message tool with target "{channel}".)'
                )
                for sub in subs:
                    # Per-session connection hierarchy (§4.3): a session that has muted this
                    # connector skips delivery — the message is still buffered (above) for catch-up.
                    if not self._inbound_connector_allowed(
                        sub.session_id, src.platform
                    ):
                        continue
                    try:
                        await self.deliver_to_session(
                            sub.session_id, msg, source=ms.to_dict()
                        )
                    except Exception:
                        pass
                return
            return  # channel with no subscribers — nobody is listening
        # DM (or any non-channel): route to the designated session, else park it for visibility.
        dm = self.dm_session()
        if dm and self._inbound_connector_allowed(dm, src.platform):
            await self.deliver_to_session(dm, event.tagged_text(), source=ms.to_dict())
        elif dm:
            # Designated, but this session has muted the connector → park rather than deliver.
            self.unrouted.record(
                src.target, who, text, reason="connector muted for DM session"
            )
        else:
            self.unrouted.record(
                src.target, who, text, reason="no DM session designated"
            )

    # -- mention router (§31) ----------------------------------------------------
    async def _route_mention(self, event, ms: MessageSource, subs) -> None:
        """@OpenWorker tagged in a channel. A subscribed (user-connected) coworker owns the channel
        and must answer; otherwise the per-thread coworker session handles it — spawned on the
        first tag, steered by follow-ups (deduped on the thread target)."""
        from ..connectors.base import format_target

        src = event.source
        # Slack semantics: replying to a top-level message threads on THAT message's ts, so a
        # top-level tag (no thread_ts) keys — and is answered — on its own ts.
        thread_key = src.thread_id or getattr(event, "message_id", None)
        thread_target = format_target(src.platform, src.chat_id, thread_key)
        who = src.user_name or src.user_id or "?"
        chan = f"#{src.chat_name}" if src.chat_name else src.chat_id
        if subs:
            # The user connected a coworker to this channel — it answers tags; no spawn.
            msg = (
                f"🔔 You were tagged by {who} in {chan}: {event.text}\n"
                f"(You are subscribed to this channel and were mentioned directly — you must "
                f"respond. Reply in the thread with the send_message tool, target "
                f'"{thread_target}".)'
            )
            for sub in subs:
                if not self._inbound_connector_allowed(sub.session_id, src.platform):
                    continue
                try:
                    await self.deliver_to_session(
                        sub.session_id, msg, source=ms.to_dict()
                    )
                except Exception:
                    pass
            return
        sid = self.mention_sessions.get(thread_target)
        if sid and self.session_store.load(sid) is not None:
            # Follow-up tag in a thread we already own → steer the same session.
            msg = (
                f"💬 Follow-up in your Slack thread ({chan}) from {who}: {event.text}\n"
                f'(Reply in the thread with the send_message tool, target "{thread_target}" '
                f"— replies there are pre-approved.)"
            )
            await self.deliver_to_session(sid, msg, source=ms.to_dict())
            return
        await self._spawn_mention_session(event, ms, thread_target)

    async def _spawn_mention_session(
        self, event, ms: MessageSource, thread_target: str
    ) -> None:
        """First tag in a thread: a NEW visible coworker session that owns the thread. Its
        in-thread replies carry a standing grant (§25 shape, exact-target match) so the
        conversation never stalls on an approval nobody in Slack can see; everything else
        asks as usual (approvals park to the Inbox)."""
        import uuid

        src = event.source
        who = src.user_name or src.user_id or "?"
        chan = f"#{src.chat_name}" if src.chat_name else src.chat_id
        sid = uuid.uuid4().hex
        engine = self.get_engine(sid, agent=self.personas.default_id())
        if engine is None:
            self.unrouted.record(
                src.target, who, event.text, reason="could not spawn mention session"
            )
            return
        # Durable mapping FIRST (a fast follow-up tag mid-turn dedupes into steering),
        # then the live grant; get_engine re-derives it from the store on any rebuild.
        self.mention_sessions.set(
            thread_target, sid, channel=f"{src.platform}:{src.chat_id}"
        )
        engine.permissions.task_rules.setdefault("send_message", set()).add(
            thread_target
        )
        self.save(sid, engine)  # the sessions row must exist before rename/set_origin
        # Title = the ASK first, channel last (owner call 2026-07-14): the text is what
        # varies between sessions, so it gets the truncation budget; the mention token is
        # noise (origin is already told by the From Slack group + icon + origin_label).
        ask = re.sub(r"<@[^>]+>", "", event.text or "")
        ask = " ".join(ask.split())[:48]
        self.session_store.rename(sid, f"{ask} — {chan}" if ask else chan)
        label = chan + (f" · {src.team_id}" if src.team_id else "")
        self.session_store.set_origin(sid, src.platform, label)
        # Up to 6 lines of channel context, minus the tag itself (it's the opening line).
        recent = self.channel_buffer.recent(f"{src.platform}:{src.chat_id}", 7)[:-1]
        context = "\n".join(f"- {m['from']}: {m['text']}" for m in recent)
        opening = (
            f"🔔 You were mentioned on Slack in {chan} by {who}: {event.text}\n\n"
            f"You own this Slack thread. Reply in the thread using the send_message tool "
            f'with target "{thread_target}" — replies to this thread are pre-approved and '
            f"never prompt the user. Anything else (other channels, files, external "
            f"actions) asks for approval as usual. Keep replies concise and "
            f"Slack-appropriate."
            + (f"\n\nRecent channel context:\n{context}" if context else "")
        )
        try:
            await self.deliver_to_session(sid, opening, source=ms.to_dict())
        except Exception:
            logger.exception("mention session %s opening turn failed", sid)

    @staticmethod
    def _wake_message(wake) -> str:
        note = f" (note: {wake.note})" if getattr(wake, "note", "") else ""
        if wake.kind == "completion":
            return (
                f"⏰ Wake — the job `{wake.job_id}` you were waiting on has completed{note}. "
                "Continue where you left off."
            )
        if wake.kind == "event":
            return (
                f"⏰ Wake — the event `{wake.event_key}` you were waiting on has fired{note}. "
                "Continue where you left off."
            )
        return (
            f"⏰ Wake — the timer you set has fired{note}. Continue where you left off."
        )

    def _submit_close_loop_task(self, instruction: str, workspace: str) -> None:
        """主动闭环执行：把 deskmate 识别到的关联未竟以一次性任务提交执行。

        由 ``deskmate.proactivity.executor.register_close_loop_executor`` 注册为全局
        执行器，在 after_action 收尾的 daemon 线程里被调用。本方法把协程提交到
        scheduler 的事件循环异步执行，不阻塞调用线程。复用 ``_build_task_engine``
        （即 ``Mode.BYPASS_APPROVALS``，下文旧称 mode=AUTO）。

        ⚠ 2026-10-02 实测更正：本 docstring 原写「写入路径作用域 + CRITICAL 治理拒杀
        两道硬防线仍然生效（High/Critical 命令仍被拦，文件写入仍限 workspace 内）」，
        其中前一道**不成立**。bypass 的短路块跑在全部权限地板**之前**，写入路径作用域
        被整体跳过：文件可写到 workspace 之外，``.git/hooks``、``save_skill``、
        定时任务增删改同样放行。真正还在的只有自保护，以及严重度恰为 CRITICAL 的 shell
        命令（``rm -rf`` 属 HIGH 级，不在其列）。详见 permissions.evaluate() 顶部注释。
        """
        import logging as _lg

        try:
            import asyncio

            from coworker.automation.models import Schedule, ScheduledTask

            task = ScheduledTask(
                title="主动闭环（agent 自发）",
                instructions=instruction,
                schedule=Schedule(kind="once"),
                workspace=workspace or self.default_workspace or "",
                agent="cowork",
                notify_on_completion=False,
            )
            loop = None
            sched_task = getattr(self.scheduler, "_task", None)
            if sched_task is not None:
                loop = sched_task.get_loop()
            if loop is None:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
            # 异步提交到 scheduler loop，后台执行，不阻塞 after_action 线程
            asyncio.run_coroutine_threadsafe(
                self._run_scheduled_task(task, "close_loop"), loop
            )
            _lg.getLogger(__name__).info(
                "主动闭环任务已提交：%s", instruction[:80]
            )
        except Exception as e:
            _lg.getLogger(__name__).warning("主动闭环任务提交失败：%s", e)

    async def _run_scheduled_task(
        self,
        task,
        trigger: str,
        *,
        session_id: Optional[str] = None,
        wake=None,
        resume: bool = False,
    ) -> TaskRun:
        run = TaskRun(
            task_id=task.id, trigger=trigger
        )  # __post_init__ sets run.session_id
        if session_id:
            # 自我驱动续跑：wake 醒来的一轮复用原会话，跨轮消息不断、账本不丢。
            run.session_id = session_id
        # §C1: stamp the run with the goal generation it belongs to. Old tasks
        # (and SimpleNamespace-shaped test doubles) read as generation 0, so this
        # is a no-op for every pre-C1 task.
        run.goal_generation = int(getattr(task, "goal_generation", 0) or 0)
        self.task_store.add_run(run)  # mark "running"

        # Autonomy §B2: opt-in goal-based runs. The previous run's checkpoint is
        # the ONLY cross-run state (no wake_store wiring in this path — see the
        # construction plan §B4). Everything here is fail-open: if the goal module
        # is missing or a stored blob is odd, this stays an ordinary scheduled run.
        goal_on = False
        goal_prev: dict = {}
        _goal_budget_reason = ""
        _goal_backoff = 0.0
        try:
            from deskmate.autonomy.goal import (
                backoff_remaining,
                budget_exceeded,
                goal_enabled,
                is_concluded,
                previous_goal_state,
            )

            goal_on = goal_enabled(task)
            if goal_on:
                prior = [
                    r
                    for r in (self.task_store.runs(task.id, limit=20) or [])
                    if r.run_id != run.run_id
                ]
                if is_concluded(prior, generation=run.goal_generation):
                    # Already reported and closed in THIS generation; cron keeps
                    # firing, but a finished goal must never produce a second
                    # delivery. §C1: the generation filter is what lets a
                    # restarted goal run again without rewriting old done runs.
                    run.status, run.finished_at = "skipped", _epoch()
                    run.checkpoint = {
                        "goal": {
                            "done": True,
                            "skipped": True,
                            "generation": run.goal_generation,
                        }
                    }
                    self.task_store.add_run(run)
                    return run
                goal_prev = previous_goal_state(
                    prior, generation=run.goal_generation
                )
                if goal_prev:
                    inherited = dict(goal_prev)
                    inherited["generation"] = run.goal_generation
                    run.checkpoint = {"goal": inherited}
                # 硬预算：自续期不是永动机；退避：连续失败别按原节奏硬撞。
                try:
                    _goal_budget_reason = budget_exceeded(goal_prev, time.time())
                    _goal_backoff = backoff_remaining(goal_prev, time.time())
                except Exception:
                    _goal_budget_reason, _goal_backoff = "", 0.0
        except Exception:
            goal_on, goal_prev = False, {}

        # 不可逆问询等到上限：停下任务并报告卡点（绝不往前猜）。
        if goal_on:
            _pq0 = goal_prev.get("pending_question")
            if isinstance(_pq0, dict) and _pq0.get("item_id"):
                _qitem = None
                try:
                    _qitem = self.inbox.get(str(_pq0["item_id"]))
                except Exception:
                    _qitem = None
                _unanswered = _qitem is not None and getattr(
                    _qitem, "state", ""
                ) != "resolved"
                try:
                    from deskmate.autonomy.goal import question_overdue

                    _overdue = _unanswered and question_overdue(
                        _pq0, time.time()
                    )
                except Exception:
                    _overdue = False
                if _overdue:
                    run.status, run.finished_at = "blocked", _epoch()
                    try:
                        _asked = float(_pq0.get("asked_at") or 0)
                        _waited_h = max(0.0, (time.time() - _asked) / 3600.0)
                    except (TypeError, ValueError):
                        _waited_h = 0.0
                    run.result_text = (
                        f"任务停在卡点上：{_pq0.get('title', '')}（已等你 "
                        f"{_waited_h:.1f} 小时，问了 "
                        f"{int(_pq0.get('attempts') or 1)} 次）。"
                        "恢复方式：回答对话里那张卡，或直接告诉我该怎么做。"
                    )
                    _blocked_goal = dict(goal_prev)
                    _blocked_goal.update({"done": True, "blocked": True})
                    run.checkpoint = {"goal": _blocked_goal}
                    self.task_store.add_run(run)
                    try:
                        from deskmate.autonomy.delivery import (
                            build_completion_event,
                            deliver_run_result,
                        )

                        _lid = deliver_run_result(task, run)
                        if _lid:
                            try:
                                from deskmate.autonomy.goal import (
                                    record_concluded,
                                )

                                record_concluded(run)
                            except Exception:
                                pass
                            try:
                                await self._report_to_origin(
                                    task, "【任务停下】" + (run.result_text or "")[:400]
                                )
                            except Exception:
                                logger.warning(
                                    "任务卡点回写原对话失败（fail-open）",
                                    exc_info=True,
                                )
                            self._cancel_task_wakes(
                                run.session_id, "blocked on unanswered question"
                            )
                            self.task_store.add_run(run)
                            await self.broadcast_event(
                                build_completion_event(
                                    task, run, status=run.status, ledger_id=_lid
                                )
                            )
                    except Exception:
                        logger.warning(
                            "任务卡点投递失败（fail-open）", exc_info=True
                        )
                    return run

        # 到预算上限：不再复查，把「最后状态 + 为什么停」交付出去并结案。
        if goal_on and _goal_budget_reason:
            run.status, run.finished_at = "expired", _epoch()
            last_state = str(goal_prev.get("state") or "").strip()
            run.result_text = (
                f"自主任务到预算上限，已停止复查：{_goal_budget_reason}。"
                + (f"\n最后一轮状态：{last_state}" if last_state else "")
            )
            expired_goal = dict(goal_prev)
            expired_goal.update(
                {"done": True, "expired": _goal_budget_reason, "rounds": goal_prev.get("rounds")}
            )
            run.checkpoint = {"goal": expired_goal}
            self.task_store.add_run(run)
            try:
                from deskmate.autonomy.delivery import (
                    build_completion_event,
                    deliver_run_result,
                )

                ledger_id = deliver_run_result(task, run)
                if ledger_id:
                    try:
                        from deskmate.autonomy.goal import record_concluded

                        record_concluded(run)
                    except Exception:
                        pass
                    try:
                        await self._report_to_origin(
                            task, "【任务结束】" + (run.result_text or "")[:400]
                        )
                    except Exception:
                        logger.warning(
                            "任务预算结案回写原对话失败（fail-open）", exc_info=True
                        )
                    self._cancel_task_wakes(run.session_id, "budget exhausted")
                    self.task_store.add_run(run)
                    await self.broadcast_event(
                        build_completion_event(
                            task, run, status=run.status, ledger_id=ledger_id
                        )
                    )
            except Exception:
                logger.warning("自主任务预算结案投递失败（fail-open）", exc_info=True)
            return run

        # 连续失败退避期内：cron 到点也先不动，等退避结束（wake 不受影响）。
        if goal_on and wake is None and not resume and _goal_backoff > 0:
            run.status, run.finished_at = "skipped", _epoch()
            skip_goal = dict(goal_prev)
            skip_goal["skipped"] = True
            skip_goal["backoff"] = True
            run.checkpoint = {"goal": skip_goal}
            self.task_store.add_run(run)
            return run

        # 自我驱动（autonomy loop）：本轮自己挂了 wake → 她就是「安排了下次复查」，
        # 不是做完了。cron 到点也不能再开一条腿，否则同一个目标会有两条循环。
        if goal_on and wake is None:
            sleeping = bool(goal_prev.get("sleeping"))
            wake_id = str(goal_prev.get("wake_id") or "")
            if sleeping:
                alive = False
                try:
                    alive = bool(wake_id) and any(
                        w.id == wake_id for w in self.wakes.pending()
                    )
                except Exception:
                    alive = False
                if alive:
                    run.status, run.finished_at = "skipped", _epoch()
                    skip_goal = dict(goal_prev)
                    skip_goal["skipped"] = True
                    run.checkpoint = {"goal": skip_goal}
                    self.task_store.add_run(run)
                    return run
                # wake 丢了（进程重启/被清）→ cron 兜底续跑，绝不永远 skipped。
                if isinstance(run.checkpoint.get("goal"), dict):
                    run.checkpoint["goal"]["sleeping"] = False
                    run.checkpoint["goal"]["wake_id"] = ""
        if (wake is not None or resume) and isinstance(
            run.checkpoint.get("goal"), dict
        ):
            # 醒来/续跑这轮本身就是一次正常复查，先清掉睡眠标记。
            run.checkpoint["goal"]["sleeping"] = False
            run.checkpoint["goal"]["wake_id"] = ""
        # 进这一轮之前已经挂着的 wake（含即将被 mark_fired 的这一条）不算「本轮新睡」。
        _pre_wake_ids: set[str] = set()
        if goal_on:
            try:
                _pre_wake_ids = {w.id for w in self.wakes.pending(run.session_id)}
            except Exception:
                _pre_wake_ids = set()
        # UX-026: tell every open app window a SCHEDULED run just started (the 5s
        # top-right toast). A quiet goal check must not toast on every cron tick —
        # "no change" means no interruption at all.
        if not goal_on:
            await self.broadcast_event(
                {
                    "type": "automation_run_started",
                    "data": {
                        "task_id": task.id,
                        "task_title": task.title,
                        "session_id": run.session_id,
                        "workspace": task.workspace,
                        "agent": task.agent,
                        "trigger": trigger,
                    },
                }
            )
        # Each run is a real, persisted conversation thread: it runs the instructions under its
        # own session id, then saves the transcript. The user can reopen that session and ask a
        # follow-up — the scheduled agent is no longer fire-and-forget.
        engine = None
        if wake is not None or resume:
            # 醒来/续跑：优先复用内存里的引擎（消息连续）；进程重启后从会话记录重建。
            engine = self._engines.get(run.session_id)
            if engine is None:
                messages = None
                try:
                    record = self.session_store.load(run.session_id)
                    messages = record.messages if record else None
                except Exception:
                    messages = None
                engine = self._build_task_engine(
                    task, session_id=run.session_id, messages=messages
                )
        if engine is None:
            engine = self._build_task_engine(task, session_id=run.session_id)
        # Register the live engine up-front: a parked approval persists the session
        # mid-run (durable suspend), and resolving from the Inbox must find this engine.
        self._engines[run.session_id] = engine
        if resume:
            opening = ""  # 续跑不注入新开场，engine.resume() 从挂起点接着走
        elif wake is not None:
            opening = self._wake_message(wake)
            try:
                digest = self.team_staleness_digest(run.session_id)
            except Exception:
                digest = ""
            if digest:
                opening = f"{opening}\n\n{digest}"
        else:
            # The first turn is the task itself. The framing matters: instructions often restate the
            # schedule ("every day at 5:32pm…"), so make explicit that the schedule already fired and
            # the job now is to execute, not to (re)schedule.
            opening = (
                f"⏰ Scheduled run — {task.title}\n\n"
                "This automation is due now: carry out the task below immediately and produce the "
                "result. The schedule already exists — do not create or modify any scheduled tasks.\n\n"
                f"{task.instructions}"
            )
        if goal_on:
            try:
                from deskmate.autonomy.goal import build_goal_opening

                opening += build_goal_opening(goal_prev)
            except Exception:
                pass
            # 上轮在原对话里问过的问题：答了就把答案带回来；没答就提醒别重复问。
            _pending_q = goal_prev.get("pending_question")
            if isinstance(_pending_q, dict) and _pending_q.get("item_id"):
                try:
                    _item = self.inbox.get(str(_pending_q["item_id"]))
                except Exception:
                    _item = None
                _pq_kind = str(_pending_q.get("kind") or "irreversible")
                if _item is not None and getattr(_item, "state", "") == "resolved":
                    opening += (
                        "\n\n--- 之前的提问已有回复 ---\n"
                        f"问题：{_pending_q.get('title', '')}\n"
                        f"萧盛的回答：{getattr(_item, 'resolution', '') or ''}\n"
                        "按这个回答继续；不要重复问同一个问题。"
                    )
                    if _pq_kind == "reversible":
                        opening += (
                            "这可逆——如果与你上轮的默认假设不同，改回来即可并说明改了哪。"
                        )
                    else:
                        opening += "这是不可逆的，先确认你此前没按假设越权做过事。"
                    if isinstance(run.checkpoint.get("goal"), dict):
                        run.checkpoint["goal"].pop("pending_question", None)
                elif _pq_kind == "reversible":
                    opening += (
                        "\n\n--- 你上轮的可逆提问还没答 ---\n"
                        f"问题：{_pending_q.get('title', '')}\n"
                        "按你在 [[STATE]] 里写明的默认假设继续，别停下等人、也别重复问；"
                        "你的假设要标明「这是我的假设，可一键改」。"
                    )
                else:
                    opening += (
                        "\n\n--- 你上轮的不可逆提问还没答 ---\n"
                        f"问题：{_pending_q.get('title', '')}\n"
                        "绝不猜：只做不依赖它就能做的事，把等待写进 [[STATE]]；"
                        "不要重复问（系统会退避提醒，到时限会停下任务）。"
                    )
        # 会话互斥：wake 与 cron 两条腿都可能来，running 标记让后来者让路。
        # manager 未经 __init__（测试/回放）时没有这张表，就不做互斥、照常跑。
        _claimed_session = False
        if goal_on and getattr(self, "_running_sessions", None) is not None:
            try:
                _claimed_session = self.try_mark_running(run.session_id)
            except Exception:
                _claimed_session = False
            if not _claimed_session:
                run.status, run.finished_at = "skipped", _epoch()
                self.task_store.add_run(run)
                return run
        # 让 asker 知道「上一轮问过什么」，同一个问题不重复发卡（见 _task_question_asker）。
        if goal_on:
            try:
                self.__dict__.setdefault("_task_origin_prev_q", {})[
                    run.session_id
                ] = goal_prev.get("pending_question") or {}
            except Exception:
                pass
        # Must exist before the try/finally: an exception path still reads them.
        _quiet_this_run, _goal_state = False, ""
        _goal_stuck, _blocker = False, ""
        _engine_error = ""
        _goal_sleeping, _sleep_wake_id = False, ""
        _asked_this_run = False
        try:
            _stream = engine.resume() if resume else engine.run(opening)
            async for _event in _stream:
                # Autonomy §A1: persist progress incrementally so a crash mid-run
                # doesn't throw away everything already done. Throttled to every
                # 5th event — one SQLite write per tool call is write amplification
                # (see the 24h/2178-row empty-oplog lesson).
                try:
                    run.event_count += 1
                    ev_data = getattr(_event, "data", None) or {}
                    ev_name = getattr(getattr(_event, "type", None), "name", "")
                    if ev_name == "ERROR":
                        # 模型/工具层的错误被引擎吞成事件，不会让 run 抛错。目标制跑
                        # 必须把它记下来，否则下一轮 opening 里看不到「上次为什么没成」。
                        detail = ""
                        if isinstance(ev_data, dict):
                            detail = str(ev_data.get("error") or ev_data.get("message") or "")
                        if detail:
                            _engine_error = detail
                    if ev_name == "ITERATION_END" and isinstance(ev_data, dict):
                        step = int(ev_data.get("iteration", 0) or 0)
                        run.current_step = f"第 {step} 步执行中"
                        # Merge, never replace: a goal run's inherited state lives
                        # in this dict and must survive the whole tool loop.
                        if not isinstance(run.checkpoint, dict):
                            run.checkpoint = {}
                        run.checkpoint.update(
                            {"last_iteration": step, "event_count": run.event_count}
                        )
                    if run.event_count % 5 == 0:
                        self.task_store.add_run(run)
                except Exception:
                    pass
            run.result_text = _last_assistant_text(engine.messages)
            run.artifacts = _recent_files(task.workspace, since=run.started_at)
            run.status = "ok"
            # A goal run may declare either "no change" (a quiet check) or a blocker
            # ([[STUCK]]). Both are parsed before any notification / memory write:
            # a stuck turn is NOT a result, and a quiet turn must not disturb.
            if goal_on:
                try:
                    from deskmate.autonomy.goal import parse_quiet, parse_stuck

                    _goal_stuck, _blocker = parse_stuck(run.result_text or "")
                    if _goal_stuck:
                        run.status = "stuck"
                    else:
                        _quiet_this_run, _goal_state = parse_quiet(
                            run.result_text or ""
                        )
                except Exception:
                    pass
            # 自己挂了唤醒 → 这轮是「等待」，不是结论：不投递、不结案、不发事件，
            # 但把引擎留在内存里，醒来那轮才有连续上下文。
            if goal_on and not _goal_stuck:
                try:
                    _new_wakes = [
                        w
                        for w in self.wakes.pending(run.session_id)
                        if w.id not in _pre_wake_ids
                    ]
                except Exception:
                    _new_wakes = []
                if _new_wakes:
                    _goal_sleeping = True
                    _sleep_wake_id = _new_wakes[-1].id
                    run.status = "sleeping"
            # 这一轮把问题投递到了原对话 → 它不是「结果」，是 needs_you。
            if goal_on:
                try:
                    _q_holder = (
                        self.__dict__.get("_task_questions") or {}
                    ).get(run.session_id) or {}
                except Exception:
                    _q_holder = {}
                if _q_holder.get("question") and not _goal_stuck:
                    _asked_this_run = True
                    run.status = "needs_you"
            # 全局 after-action：自动/定时任务完成也记一笔 + 刷新小说圣经。
            # 静默检查不写记忆：否则每轮「无变化」都会污染记忆库。
            try:
                import logging as _log
                import threading as _th

                from deskmate.memory.after_action import reflect_and_persist

                if (
                    not _quiet_this_run
                    and not _goal_stuck
                    and not _goal_sleeping
                    and not _asked_this_run
                ):
                    _th.Thread(
                        target=reflect_and_persist,
                        args=(task.workspace, task.instructions, run.result_text or ""),
                        kwargs={"artifacts": run.artifacts},
                        daemon=True,
                    ).start()
            except Exception:
                _log.warning("after_action 任务收尾触发失败", exc_info=True)
            # §C1: a run that was just cancelled/paused must not fire the
            # "task done" notification on its way out.
            if (
                task.notify_on_completion
                and not _quiet_this_run
                and not _goal_stuck
                and not _goal_sleeping
                and not _asked_this_run
                and not self._control_marks().get(run.run_id)
            ):
                await self._notify_task_done(task, run)
        except Exception as exc:
            run.status, run.error = "error", str(exc)
            # 错误也要留 traceback：只有字符串时，排查「为什么没成」只能靠猜。
            logger.exception("自主任务执行异常: %s", getattr(task, "id", ""))
        finally:
            run.finished_at = _epoch()
            # §C1 "stoppable": a control endpoint (cancel) or the enabled switch
            # (pause) writes an in-process mark that the run loop can actually
            # see. The store alone is NOT enough — the loop's own add_run(run)
            # would overwrite the endpoint's write with its in-memory object.
            halted = self._control_marks().get(run.run_id)
            if halted == "cancel":
                run.status, run.cancel_requested = "canceled", True
            elif halted == "pause":
                run.status, run.paused_at = "paused", run.finished_at
            quiet = bool(goal_on and _quiet_this_run and run.status == "ok")
            _goal_outcome, _give_up = "", False
            if goal_on and not halted:
                # A halted turn is NOT a completed check: leave the previous goal
                # state untouched so the next run / resumed goal continues.
                # 自主循环：每轮都记下「实际做了什么、结果如何、错在哪」，下一轮
                # opening 带着它，模型才会换路子而不是原样重试同一动作。
                try:
                    from deskmate.autonomy.goal import (
                        STUCK_GIVE_UP,
                        attempt_from_messages,
                        note_attempt,
                    )

                    if _engine_error and not run.error:
                        run.error = _engine_error
                    if run.status == "error" or (_engine_error and not quiet and not _goal_stuck):
                        _goal_outcome = "error"
                    elif _goal_stuck:
                        _goal_outcome = "stuck"
                    elif _goal_sleeping:
                        _goal_outcome = "sleeping"
                    elif quiet:
                        _goal_outcome = "quiet"
                    else:
                        _goal_outcome = "changed"
                    if _asked_this_run and _goal_outcome in ("quiet", "changed"):
                        _goal_outcome = "needs_you"
                    # 这一轮投递过的提问：记进检查点，下一轮检查回复。
                    _topic = None
                    try:
                        _q_holder = (
                            self.__dict__.get("_task_questions") or {}
                        ).pop(run.session_id, None)
                    except Exception:
                        _q_holder = None
                    if _q_holder and _q_holder.get("question"):
                        _topic = dict(_q_holder["question"])
                        _topic["asked_at"] = run.finished_at
                    try:
                        (self.__dict__.get("_task_origin_prev_q") or {}).pop(
                            run.session_id, None
                        )
                    except Exception:
                        pass
                    # 静默轮别把 [[NO_CHANGE]] 原文再喂回去，否则模型容易照抄成
                    # 「本轮也没变化」；只保留动作与结论摘要。
                    _attempt_result = (
                        run.result_text or ""
                        if _goal_outcome in ("changed", "stuck", "needs_you")
                        else ""
                    )
                    note_attempt(
                        run,
                        outcome=_goal_outcome,
                        attempt=attempt_from_messages(
                            engine.messages,
                            result=_attempt_result,
                            error=run.error or "",
                            at=run.finished_at or 0.0,
                        ),
                        state=_goal_state if _goal_outcome == "quiet" else "",
                        blocker=_blocker if _goal_outcome == "stuck" else "",
                        now=run.finished_at,
                    )
                    if _topic is not None and isinstance(
                        run.checkpoint.get("goal"), dict
                    ):
                        run.checkpoint["goal"]["pending_question"] = _topic
                    if _goal_outcome == "sleeping":
                        goal_now = run.checkpoint.get("goal")
                        if isinstance(goal_now, dict):
                            goal_now["sleeping"] = True
                            goal_now["wake_id"] = _sleep_wake_id or goal_now.get(
                                "wake_id", ""
                            )
                            goal_now["done"] = False
                    if _goal_outcome == "stuck":
                        goal_now = run.checkpoint.get("goal")
                        stuck_rounds = int((goal_now or {}).get("stuck_rounds") or 0)
                        if stuck_rounds >= STUCK_GIVE_UP:
                            # 连续卡住到上限：投一次卡点并结案，不再空转。
                            _give_up = True
                            if not (run.result_text or "").strip():
                                run.result_text = (
                                    f"我卡住了：{_blocker or '反复尝试仍无法推进'}"
                                )
                    goal_state = run.checkpoint.get("goal")
                    if isinstance(goal_state, dict):
                        goal_state["generation"] = run.goal_generation
                except Exception:
                    logger.warning("自主任务尝试记录失败（fail-open）", exc_info=True)
            # Persist the run as a continuable session + keep the live engine for an immediate
            # follow-up; record the run (now carrying its session_id).
            try:
                self.save(run.session_id, engine)
                self._engines[run.session_id] = engine
            except Exception:
                pass
            self.task_store.add_run(run)
            # 进度回原对话：静默轮按节奏报，卡住/失败立即报（结果另有【任务结果】）。
            if goal_on and getattr(task, "origin_session_id", ""):
                try:
                    _progress = self._origin_progress_text(
                        task, run, _goal_outcome, goal_prev
                    )
                    if _progress:
                        await self._report_to_origin(task, _progress)
                except Exception:
                    logger.warning("任务进度回写原对话失败（fail-open）", exc_info=True)
            # Autonomy §A2: close the delivery loop for BOTH success and failure.
            # The old path only broadcast a task_done to a session socket that has
            # zero frontend consumers; the result vanished when no window was open.
            # Now: durable ledger row (surfaced by the opening line / mailbox) plus
            # a global "automation_run_finished" event on /ws/events for an
            # immediate hint. Everything here is fail-open — delivery must never
            # turn a completed task into an error.
            # §C1: a cancelled/paused run is NOT a result — no ledger row, no
            # completion toast, no goal conclusion. It gets its own stop event.
            # 自主循环：非终局轮（静默检查 / 尚未到上限的卡住轮）不投递、不结案、
            # 不发完成 toast —— 只有真的「有结果」或「卡到上限」才打扰用户。
            suppress = bool(
                goal_on
                and (
                    (_goal_outcome == "stuck" and not _give_up)
                    or _goal_outcome in ("sleeping", "needs_you")
                )
            )
            if not quiet and not halted and not suppress:
                try:
                    from deskmate.autonomy.delivery import (
                        build_completion_event,
                        deliver_run_result,
                    )

                    ledger_id = deliver_run_result(task, run)
                    # 修「先结案后投递」：只有投递真的拿到台账 id 才算结案。
                    # 投递失败/空产出时保持 done=False，下一轮还能补投，绝不静默丢。
                    if goal_on and run.status in ("ok", "stuck") and ledger_id:
                        try:
                            from deskmate.autonomy.goal import record_concluded

                            record_concluded(run)
                            self._cancel_task_wakes(
                                run.session_id, "goal concluded"
                            )
                        except Exception:
                            logger.warning(
                                "自主任务结案标记失败（fail-open）", exc_info=True
                            )
                    self.task_store.add_run(run)  # persist the delivered_at receipt
                    if ledger_id:
                        if goal_on:
                            try:
                                await self._report_to_origin(
                                    task,
                                    "【任务结果】" + (run.result_text or "")[:400],
                                )
                            except Exception:
                                logger.warning(
                                    "任务结果回写原对话失败（fail-open）", exc_info=True
                                )
                        await self.broadcast_event(
                            build_completion_event(
                                task, run, status=run.status, ledger_id=ledger_id
                            )
                        )
                except Exception:
                    logger.warning("自主任务完成投递失败（fail-open）", exc_info=True)
            elif halted:
                self._cancel_task_wakes(run.session_id, f"run {halted}")
                try:
                    await self.broadcast_event(
                        {
                            "type": "automation_run_stopped",
                            "data": {
                                "task_id": task.id,
                                "task_title": task.title,
                                "run_id": run.run_id,
                                "session_id": run.session_id,
                                "workspace": task.workspace,
                                "agent": task.agent,
                                "status": run.status,
                            },
                        }
                    )
                except Exception:
                    logger.warning("自主任务停止事件发送失败", exc_info=True)
            self._control_marks().pop(run.run_id, None)
            if _claimed_session:
                try:
                    self.mark_idle(run.session_id)
                except Exception:
                    logger.debug("mark_idle 失败（fail-open）", exc_info=True)
        return run

    async def _notify_task_done(self, task, run: TaskRun) -> None:
        summary = (run.result_text or "").strip()[:280]
        # Notify any socket viewing this scheduled run's session (it's a durable session of its own).
        await self.broadcast_session(
            run.session_id,
            {
                "type": "task_done",
                "data": {
                    "task": task.title,
                    "id": task.id,
                    "text": summary,
                    "run_id": run.run_id,
                },
            },
        )
        if task.notify_target:
            from ..connectors.base import parse_target
            from ..connectors.senders import DEFAULT_SENDERS

            try:
                platform, chat_id, thread = parse_target(task.notify_target)
                sender = DEFAULT_SENDERS.get(platform)
                creds = self.secrets.get(f"{platform}:default") or {}
                if sender and creds.get("bot_token"):
                    await asyncio.to_thread(
                        sender,
                        creds["bot_token"],
                        chat_id,
                        f"✓ {task.title}\n\n{summary}",
                        thread,
                    )
            except Exception:
                pass

    # -- automation REST --------------------------------------------------------
    # -- run control (autonomy §C1) ---------------------------------------------
    def _control_marks(self) -> dict[str, str]:
        """In-process run control marks, shared across coroutines.

        Lazily created so a manager built without ``__init__`` (tests, replay)
        still works. Never persisted: a process restart already killed the run.
        The store alone is not a usable channel here — the run loop writes its
        own in-memory ``run`` back with ``add_run()`` on every checkpoint.
        """
        return self.__dict__.setdefault("_run_controls", {})

    def _try_interrupt(self, run) -> None:
        """Actually stop the turn: ``engine.request_interrupt()`` makes a running
        loop yield INTERRUPTED instead of continuing to call tools."""
        engine = self._engines.get(run.session_id)
        if engine is None:
            return
        try:
            engine.request_interrupt()
        except Exception:
            logger.warning("中断自主任务引擎失败", exc_info=True)

    def _cancel_task_wakes(self, session_id: str, reason: str) -> None:
        """Drop a run session's pending self-wake (pause / cancel / conclude).

        Without this a task that is paused or concluded would still be re-invoked
        by its own stale timer — the wake must not outlive the reason it existed.
        """
        try:
            self.wakes.cancel_sleep(session_id, reason)
        except Exception:
            logger.debug("取消自主任务睡眠失败（fail-open）", exc_info=True)

    def reconcile_task_runs(self) -> int:
        """启动对账：把「没人跑、也没结束」的 run 标成 interrupted。

        kill -9 / 优雅退出时 run 的 finally 可能来不及落终态，留下 status=running
        的幽灵行：UI 永远显示在跑，cancel_run 又回「已结束」，停也停不掉。
        有活引擎的 run 跳过；sleeping 是合法中间态（有 pending wake），不动。
        返回修正条数，fail-open。
        """
        fixed = 0
        try:
            for task in self.task_store.list():
                for run in self.task_store.runs(task.id, limit=50):
                    if run.status != "running":
                        continue
                    if self._engines.get(run.session_id) is not None:
                        continue
                    run.status = "interrupted"
                    run.finished_at = run.finished_at or _epoch()
                    cp = run.checkpoint if isinstance(run.checkpoint, dict) else {}
                    cp["interrupted"] = True
                    run.checkpoint = cp
                    self.task_store.add_run(run)
                    fixed += 1
        except Exception:
            logger.warning("任务 run 启动对账失败（fail-open）", exc_info=True)
        return fixed

    def _task_question_asker(
        self, session_id: str, agent: str, *, task=None, timeout_s: float = 1800.0
    ):
        """目标制任务的 ask_user。

        有 origin 会话（用户在对话里下的任务）：问询落**原对话**——inline 卡 + 一条
        对话消息，并且**不阻塞**：她先去做不用等这个回答的部分，下一轮自动检查回复。
        不是让用户到处找入口，是她回到对话里来找人。

        没有 origin（GUI 表单建的任务）：保持原 Inbox 阻塞（超时自动放行）行为。
        """
        origin_sid = ""
        if task is not None:
            origin_sid = str(getattr(task, "origin_session_id", "") or "").strip()
        if origin_sid:

            async def ask_origin(
                args: dict[str, Any], tool_call_id: Optional[str] = None
            ) -> dict:
                from ..tools.ask import question_item_fields

                fields = question_item_fields(args)
                if fields is None:
                    return {"answer": "", "error": "no question"}
                from deskmate.autonomy.goal import (
                    next_retry_delay,
                    question_kind,
                    question_overdue,
                    same_question,
                )

                def _num(value, default=0.0):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        return default

                kind = question_kind(args.get("kind"))
                now = time.time()
                # 语义去重：同一个问题换个说法也算同一个（避免每轮骚扰）。
                prev_q = (self.__dict__.get("_task_origin_prev_q") or {}).get(
                    session_id
                ) or {}
                if prev_q.get("title") and same_question(
                    prev_q.get("title"), fields["title"]
                ):
                    try:
                        prev_item = self.inbox.get(str(prev_q.get("item_id") or ""))
                    except Exception:
                        prev_item = None
                    if prev_item is not None and prev_item.state == "resolved":
                        # 已答：直接把答案当工具结果返回，不再发卡打扰用户。
                        return {"answer": str(prev_item.resolution or "")}
                    if prev_item is not None and prev_item.state == "pending":
                        holder = self.__dict__.setdefault(
                            "_task_questions", {}
                        ).setdefault(session_id, {})
                        pending = dict(prev_q)
                        if question_overdue(pending, now):
                            return {
                                "answer": "",
                                "error": "this question has been waiting too "
                                "long; STOP this task and report the blocker — "
                                "do not ask again",
                            }
                        if now >= _num(pending.get("next_retry_at")):
                            attempts = int(pending.get("attempts") or 1) + 1
                            delay = next_retry_delay(attempts)
                            pending.update(
                                {"attempts": attempts, "next_retry_at": now + delay}
                            )
                            waited = int(
                                (now - _num(pending.get("asked_at"))) // 60
                            )
                            await self._report_to_origin(
                                task,
                                f"（还在等你回答：{pending.get('title', '')}；"
                                f"已等 {waited} 分钟，下次提醒约 "
                                f"{delay // 60} 分钟后）",
                            )
                        holder["question"] = pending
                        return {
                            "answer": "",
                            "error": "you already asked this; it is still "
                            "pending in the conversation — do other work, do "
                            "not ask again (the system handles reminders)",
                        }
                inbox_name = self.inbox_routing.route_for(origin_sid, agent)
                item = self.inbox.add_question(
                    origin_sid,
                    inbox=inbox_name,
                    # inline：只出现在这个对话里，不塞进全局 Inbox 让用户到处找。
                    visibility="inline",
                    # 不给 tool_call_id：原会话没有挂起的 turn，避免被 durable-resume
                    # 当成待续跑的工具调用再掀一轮。
                    tool_call_id=None,
                    **fields,
                )
                holder = self.__dict__.setdefault("_task_questions", {}).setdefault(
                    session_id, {}
                )
                holder["question"] = {
                    "item_id": item.id,
                    "title": fields["title"],
                    "options": list(fields.get("options") or []),
                    "kind": kind,
                    "asked_at": now,
                    "attempts": 1,
                    "next_retry_at": now + next_retry_delay(1),
                }
                try:
                    self.persist_session(origin_sid)
                except Exception:
                    pass
                await self._report_to_origin(
                    task, self._question_text(fields, kind=kind)
                )
                # 关键：不 await 人。返回「已投递」让模型继续干不依赖它的活。
                if kind == "reversible":
                    return {
                        "answer": "",
                        "error": "question posted to the conversation (reversible). "
                        "Do NOT block: pick a clearly-labeled default assumption, "
                        "continue the work, and say it can be changed when the "
                        "answer arrives.",
                    }
                return {
                    "answer": "",
                    "error": "question posted to the conversation (irreversible). "
                    "Do NOT block: continue only with work that does not depend "
                    "on this decision, and never guess it; the system will "
                    "remind and eventually stop the task.",
                }

            return ask_origin

        async def ask(args: dict[str, Any], tool_call_id: Optional[str] = None) -> dict:
            from ..tools.ask import answer_result, question_item_fields

            fields = question_item_fields(args)
            if fields is None:
                return {"answer": "", "error": "no question"}
            inbox_name = self.inbox_routing.route_for(session_id, agent)
            item = self.inbox.add_question(
                session_id, inbox=inbox_name, tool_call_id=tool_call_id, **fields
            )
            if item.state != "pending":
                return answer_result(item.questions, item.resolution)
            try:
                self.persist_session(session_id)
            except Exception:
                pass
            try:
                await self.mirror_inbox_item(item)
            except Exception:
                pass
            try:
                answer = await asyncio.wait_for(
                    self.inbox.wait(item.id), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                try:
                    self.inbox.resolve(
                        item.id, "（超时未答：自动放行，按未答继续）", by="system"
                    )
                except Exception:
                    logger.debug("自主任务问人超时结卡失败（fail-open）", exc_info=True)
                return {
                    "answer": "",
                    "error": "nobody answered in time; proceed with the least "
                    "destructive option, or report [[STUCK]] with what you need",
                }
            return answer_result(item.questions, answer)

        return ask

    def _question_text(self, fields: dict, *, kind: str = "irreversible") -> str:
        title = str(fields.get("title") or "（问题）")
        options = [str(o) for o in (fields.get("options") or [])]
        line = f"需要你定一下：{title}"
        if options:
            line += "\n选项：" + " / ".join(options)
        if kind == "reversible":
            return line + "\n（可逆：我先按明标的默认假设推进，你回来可以一键改）"
        return line + "\n（不可逆：我不会先做，等你回复；系统会定时提醒，不会反复打扰）"

    def _append_origin_message(self, session_id: str, text: str, *, task_id: str = "") -> bool:
        """只往原对话追加一条消息（不触发模型 turn），并留痕 task_id。

        ConversationStore 是 append-only：load 全量、append 一条、save 只追加尾部。
        与用户正在聊天存在极小写竞态，失败只记日志（fail-open），不拖垮任务。
        """
        if not session_id or not text:
            return False
        try:
            record = self.session_store.load(session_id)
            if record is None:
                return False
            record.messages.append(
                {
                    "role": "assistant",
                    "content": text,
                    "ts": time.time(),
                    "_task_id": task_id,
                }
            )
            self.session_store.save(record)
            return True
        except Exception:
            logger.warning("任务消息追加到原对话失败（fail-open）", exc_info=True)
            return False

    async def _report_to_origin(self, task, text: str) -> bool:
        """把一条消息送回任务发起的那个对话，并推给打开的窗口。"""
        sid = str(getattr(task, "origin_session_id", "") or "").strip()
        if not sid or not text:
            return False
        ok = self._append_origin_message(
            sid, text, task_id=str(getattr(task, "id", ""))
        )
        if ok:
            try:
                await self.broadcast_session(
                    sid, {"type": "assistant_message", "data": {"text": text}}
                )
            except Exception:
                logger.debug("任务消息推送原会话失败（fail-open）", exc_info=True)
        return ok

    def _origin_progress_text(self, task, run, outcome: str, goal_prev: dict) -> str:
        """静默轮按节奏、异常轮立即往原对话报一条进度；changed/needs_you 另有汇报。"""
        if outcome in ("changed", "needs_you", "sleeping", ""):
            return ""
        goal = (run.checkpoint or {}).get("goal") or {}
        rounds = int((goal_prev or {}).get("rounds") or 0) + 1
        state = str(goal.get("state") or "")[:140]
        if outcome == "quiet":
            try:
                every = max(1, int(os.environ.get("COWORKER_TASK_PROGRESS_EVERY", "3")))
            except Exception:
                every = 3
            if rounds % every:
                return ""
            head = f"【盯梢进度】第 {rounds} 轮：没有变化"
        elif outcome == "stuck":
            stuck = int(goal.get("stuck_rounds") or 0)
            head = f"【盯梢进度】第 {rounds} 轮：卡住了（连续 {stuck} 轮）"
        elif outcome == "error":
            head = f"【盯梢进度】第 {rounds} 轮：执行失败"
        else:
            return ""
        return f"{head}{('；' + state) if state else ''}"

    def cancel_run(self, task_id: str, run_id: str) -> dict[str, Any]:
        """Stop the CURRENT run without disabling the task itself (§C1).

        Writes an in-process mark (visible to the run loop), persists
        ``cancel_requested`` for the UI, then interrupts the live engine. The
        run's own ``finally`` marks it ``canceled`` and suppresses both the
        ledger delivery and the goal conclusion — a stopped turn is not a result.
        """
        run = self.task_store.find_run(run_id)
        if run is None or run.task_id != task_id:
            return {"ok": False, "error": "not found"}
        if run.status == "sleeping":
            # 睡眠中的 run 没有活引擎可中断，但「叫停」必须能停掉它的下一次唤醒。
            self._cancel_task_wakes(run.session_id, "run canceled by user")
            run.status, run.cancel_requested = "canceled", True
            run.finished_at = run.finished_at or _epoch()
            self.task_store.add_run(run)
            return {"ok": True, "run": run.to_dict()}
        if run.status == "running" and run.finished_at is not None:
            # 僵尸行（进程退出时 finally 没来得及改状态）：没有活引擎可中断，
            # 直接落终态，别再把「叫停」回成「已结束」。
            run.status, run.cancel_requested = "canceled", True
            self.task_store.add_run(run)
            return {"ok": True, "run": run.to_dict()}
        if run.finished_at is not None or run.status != "running":
            return {"ok": False, "error": "run already finished"}
        self._control_marks()[run_id] = "cancel"
        run.cancel_requested = True
        self.task_store.add_run(run)
        self._try_interrupt(run)
        return {"ok": True, "run": run.to_dict()}

    def list_automations(self) -> dict[str, Any]:
        # Unseen = runs started after the task's seen mark (UX-023 sidebar badges).
        # `unseen_failed` tints the badge when the NEWEST unseen run errored.
        tasks = []
        for t in self.task_store.list():
            unseen = [
                r for r in self.task_store.runs(t.id) if r.started_at > t.seen_runs_at
            ]
            tasks.append(
                {
                    **t.public(),
                    "unseen_runs": len(unseen),
                    "unseen_failed": bool(unseen) and unseen[0].status == "error",
                }
            )
        return {"tasks": tasks}

    def mark_automation_seen(self, task_id: str) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        task.seen_runs_at = time.time()
        self.task_store.save(task)
        return {"ok": True}

    def get_automation(self, task_id: str) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"error": "not found"}
        return {
            "task": task.public(),
            "runs": [r.to_dict() for r in self.task_store.runs(task_id)],
        }

    def create_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create an automation directly from the GUI (the "New automation" / template flow).
        Mirrors the agent-facing `create_scheduled_task` validation, but binds the task to a
        fresh per-task scratch workspace instead of an origin conversation's folder."""
        from croniter import croniter

        title = (payload.get("title") or "").strip()
        instructions = (payload.get("instructions") or "").strip()
        cron = (payload.get("cron") or "").strip() or None
        fire_at = (payload.get("fire_at") or "").strip() or None
        timezone = (payload.get("timezone") or "").strip() or "local"

        if not title:
            return {"ok": False, "error": "title is required"}
        if not instructions:
            return {"ok": False, "error": "instructions are required"}
        if not cron and not fire_at:
            return {
                "ok": False,
                "error": "provide a cron (recurring) or a fire_at ISO datetime (one-time)",
            }
        if cron and not croniter.is_valid(cron):
            return {"ok": False, "error": f"invalid cron expression: {cron}"}

        schedule = Schedule(
            kind="once" if (fire_at and not cron) else "cron",
            cron=cron,
            fire_at=fire_at,
            timezone=timezone,
        )
        from ..automation.models import grant_entries

        task = ScheduledTask(
            title=title,
            instructions=instructions,
            schedule=schedule,
            workspace="",
            origin_surface="cowork",
            agent="cowork",
            # Human-driven path (GUI form / onboarding recipes): the creating surface
            # rendered the grants, the submit IS the consent. Same validation as the
            # agent tool — only target-bound write grants survive.
            always_allowed_tools=grant_entries(payload.get("permissions")),
            goal_mode=str(payload.get("goal_mode") or ""),
        )
        task.workspace = self._provision_scratch(task.task_session_id)
        self.task_store.save(task)
        return {"ok": True, "task": task.public()}

    def update_automation(
        self, task_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        if "enabled" in changes:
            raw = changes["enabled"]
            # bool("false") is True — parse strictly so an API caller can't
            # accidentally re-enable a task with a string.
            new_enabled = (
                raw
                if isinstance(raw, bool)
                else str(raw).strip().lower() in ("1", "true", "yes", "on")
            )
            task.enabled = new_enabled
            if not new_enabled:
                # §C1: pausing must also stop the turn that is ALREADY running,
                # not merely prevent future ticks. Its goal checkpoint survives,
                # so resuming continues the goal instead of starting over.
                for r in self.task_store.runs(task.id, limit=5):
                    # 暂停也要掐掉自己安排的睡眠，否则暂停后仍会被定时唤醒跑一轮。
                    self._cancel_task_wakes(r.session_id, "task paused")
                    if r.status == "running" and r.finished_at is None:
                        self._control_marks()[r.run_id] = "pause"
                        self._try_interrupt(r)
                        break
            else:
                for r in self.task_store.runs(task.id, limit=5):
                    if r.status == "paused":
                        r.resumed_at = time.time()
                        self.task_store.add_run(r)
                        break
        if changes.get("instructions") is not None:
            task.instructions = changes["instructions"]
        if changes.get("title") is not None:
            task.title = changes["title"]
        if changes.get("goal_mode") is not None:
            # Declared at launch/update time, never inferred from a task category.
            task.goal_mode = str(changes["goal_mode"] or "")
        if changes.get("restart_goal"):
            # §C1: re-open a concluded goal WITHOUT rewriting history. Every run
            # is stamped with the generation it belongs to (see _run_scheduled_task),
            # so bumping the counter makes prior done/skipped runs invisible to
            # the new generation. Never touch an old run's `done`.
            task.goal_generation = int(getattr(task, "goal_generation", 0) or 0) + 1
            if changes.get("goal_mode") is None and not str(
                getattr(task, "goal_mode", "") or ""
            ).strip():
                task.goal_mode = "watch"
        if changes.get("cron") is not None:
            from croniter import croniter

            if not croniter.is_valid(changes["cron"]):
                return {"ok": False, "error": "invalid cron"}
            task.schedule.cron, task.schedule.kind = changes["cron"], "cron"
        if changes.get("revoke"):
            # Revocation from the task detail page ("Allowed without asking … · Revoke").
            # Human-only, like minting; the agent-facing update tool has no such field.
            task.revoke_rule(str(changes["revoke"]))
        self.task_store.save(task)
        if changes.get("revoke"):
            # A live run engine may still hold the revoked rule — reseed from the record.
            for sid, engine in self._engines.items():
                owner = self.task_store.task_for_run_session(sid)
                if owner is not None and owner.id == task.id:
                    engine.permissions.task_rules = task.standing_rules()
        return {"ok": True, "task": task.public()}

    def delete_automation(self, task_id: str) -> dict[str, Any]:
        try:
            for r in self.task_store.runs(task_id):
                self._cancel_task_wakes(r.session_id, "task deleted")
        except Exception:
            logger.debug("删除任务前清理睡眠失败（fail-open）", exc_info=True)
        return {"ok": self.task_store.delete(task_id), "id": task_id}

    def prepare_manual_run(self, task_id: str) -> dict[str, Any]:
        """Create a 'running' manual run and return its session, so the GUI can open it and
        drive the task LIVE over the normal session WS (you watch the agent + follow up). The
        automatic scheduler path stays headless (`_run_scheduled_task`)."""
        task = self.task_store.get(task_id)
        if task is None:
            return {"ok": False, "error": "not found"}
        Path(task.workspace).mkdir(parents=True, exist_ok=True)
        run = TaskRun(
            task_id=task.id, trigger="manual"
        )  # status "running", session_id auto
        self.task_store.add_run(run)
        return {
            "ok": True,
            "run_id": run.run_id,
            "session_id": run.session_id,
            "workspace": task.workspace,
            "agent": task.agent,
            # Same execute-now framing as the headless path — manual runs ride a normal live
            # session whose engine DOES have scheduling tools, so be explicit.
            "prompt": (
                f"⏰ Running automation '{task.title}' now. Carry out these instructions "
                "immediately and produce the result. The schedule already exists — do not create "
                f"or modify any scheduled tasks.\n\n{task.instructions}"
            ),
        }

    def finalize_manual_run(self, task_id: str, run_id: str) -> dict[str, Any]:
        """Mark a manual run complete once its first turn finished (the WS already saved the
        session). Pulls result text + artifacts from the persisted transcript/workspace.
        """
        run = next(
            (r for r in self.task_store.runs(task_id) if r.run_id == run_id), None
        )
        task = self.task_store.get(task_id)
        if run is None or task is None:
            return {"ok": False, "error": "not found"}
        if run.status == "running":
            record = self.session_store.load(run.session_id)
            run.result_text = _last_assistant_text(record.messages) if record else None
            run.artifacts = _recent_files(task.workspace, since=run.started_at)
            run.status = "ok"
            # 全局 after-action：手动运行完成也记一笔 + 刷新小说圣经
            try:
                import logging as _log
                import threading as _th

                from deskmate.memory.after_action import reflect_and_persist

                _th.Thread(
                    target=reflect_and_persist,
                    args=(task.workspace, task.instructions, run.result_text or ""),
                    kwargs={"artifacts": run.artifacts},
                    daemon=True,
                ).start()
            except Exception:
                _log.warning("after_action 手动收尾触发失败", exc_info=True)
            run.finished_at = _epoch()
            self.task_store.add_run(run)
            task.last_run, task.last_status = run.finished_at, "ok"
            task.run_count += 1
            self.task_store.save(task)
        return {"ok": True, "run": run.to_dict()}

    def save(self, session_id: str, engine: TurnEngine, touch: bool = True) -> None:
        executor = getattr(engine, "executor", None)
        workspace = os.path.realpath(str(executor.cwd)) if executor else ""
        self.session_store.save(
            SessionRecord(
                session_id=session_id,
                workspace=workspace,
                model=engine.model,
                mode=engine.permissions.mode.value,
                messages=engine.messages,
                title=title_from(engine.messages),
                agent=getattr(engine, "agent_name", "code"),
                extra_roots=self._extra_roots_of(engine, session_id),
                grants=_grants_of(engine),
                compaction=(
                    engine.compaction_state.as_dict()
                    if getattr(engine, "compaction_state", None)
                    else {}
                ),
            ),
            touch=touch,
        )

    @staticmethod
    def _apply_grants(engine: TurnEngine, grants: dict[str, Any]) -> None:
        """Re-apply a reloaded session's persisted "Always allow" approvals — they're
        session-scoped, and the session outlives the process (owner-hit 2026-07-22)."""
        for tool in grants.get("tools") or []:
            engine.permissions.allow_tool_for_session(str(tool))
        for command in grants.get("commands") or []:
            engine.permissions.allow_command_for_session(str(command))
        if grants.get("readonly"):
            engine.permissions.allow_readonly_for_session()

    def _extra_roots_of(
        self, engine: TurnEngine, session_id: str
    ) -> list[dict[str, Any]]:
        """User/agent-added folders = the engine's roots minus the primary (index 0) AND
        the session's provisioned scratch root. Persisting the scratch as an "extra"
        would re-add it as a plain folder on every rebuild (universal scratch made
        index-0-only slicing wrong for dual-root sessions)."""
        roots = getattr(engine, "roots", None) or []
        scratch = (self.scratch_base() / session_id).expanduser()
        try:
            scratch = scratch.resolve()
        except OSError:
            pass
        return [
            {"path": str(r.path), "writable": bool(r.writable), "label": r.label}
            for r in roots[1:]
            if r.path != scratch
        ]

    # -- LLM auto-titles (FB-010) -------------------------------------------------
    _AUTOTITLE_PROMPT = (
        "You title chat sessions. Given the user's opening message(s) — and, when "
        "present, the assistant's first reply for context — reply with ONLY a 4-5 word "
        "title for the session, named after what the session is actually about — no "
        "quotes or punctuation wrapping it. If there is no topic at all ("
        '"hey", "how are you", "hi there" and a generic reply), reply with exactly: '
        "small-talk"
    )

    def _maybe_autotitle(self, session_id: str) -> None:
        """Kick off title generation after a turn completes, fire-and-forget. Only while
        the session has neither a manual rename nor a generated title, at most twice:
        attempt 1 rides turn 1, and the second window exists solely for the small-talk
        retry (with both openers). Attempts are counted in memory rather than derived
        from the user-message count — steering injections also land as role "user", and
        counting them would silently suppress titling on a steered first turn. A restart
        forgetting the counter is harmless: renamed/auto_title still gate re-titling."""
        if session_id.startswith("__"):
            return
        engine = self._engines.get(session_id)
        if engine is None or session_id in self._autotitle_inflight:
            return
        if self.task_store.task_for_run_session(session_id) is not None:
            return  # automation runs are titled by their task
        # Three windows, not two (owner ruling 2026-08-24): opener-only at turn 1 start,
        # opener+assistant-reply at turn 1 end (titles a "hey"-then-real-work session),
        # and both-openers at turn 2 start. The signature guard makes each fire at most
        # once; sessions with a meaty first message still title on attempt 1.
        if self._autotitle_attempts.get(session_id, 0) >= 3:
            return
        users = [m for m in engine.messages if m.get("role") == "user"]
        if not users:
            return
        state = self.session_store.title_state(session_id)
        if state is None or state["renamed"] or state["auto_title"]:
            return
        from ..attachments import content_to_text

        openers = [
            text
            for m in users
            if (text := content_to_text(m.get("content"), image_placeholder="").strip())
        ][:2]
        if not openers:
            return
        # The agent's first reply is fair evidence for a TITLE (unlike the reviewer,
        # naming a session is not a security boundary — owner ruling 2026-08-24): it is
        # what turns "hey" + a generic ask into "Semgrep security review".
        assistant = next(
            (
                text
                for m in engine.messages
                if m.get("role") == "assistant"
                and (
                    text := content_to_text(
                        m.get("content"), image_placeholder=""
                    ).strip()
                )
            ),
            "",
        )[:400]
        # Same evidence as the last attempt → nothing new to say; skip WITHOUT burning an
        # attempt (this is how the turn-start and turn-end triggers coexist).
        sig = (len(openers), bool(assistant))
        if self._autotitle_sig.get(session_id) == sig:
            return
        self._autotitle_sig[session_id] = sig
        self._autotitle_attempts[session_id] = (
            self._autotitle_attempts.get(session_id, 0) + 1
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop to ride (sync caller) — skip, never block
        self._autotitle_inflight.add(session_id)
        # Retain the task: the loop holds only a weak ref, and a GC'd task would both
        # kill the title mid-flight and strand the inflight guard.
        task = loop.create_task(
            self._generate_autotitle(session_id, engine, openers, assistant)
        )
        self._autotitle_tasks.add(task)
        task.add_done_callback(self._autotitle_tasks.discard)

    async def _generate_autotitle(
        self,
        session_id: str,
        engine: TurnEngine,
        openers: list[str],
        assistant: str = "",
    ) -> None:
        """One cheap non-streaming completion on the session's own provider/model. Every
        failure (provider error, empty, absurdly long) is swallowed — the title_from
        fallback stays; the small-talk sentinel leaves auto_title unset so the turn-2
        retry can run."""
        try:
            turn = await asyncio.to_thread(
                engine.provider.complete,
                model=engine.model,
                messages=[
                    {"role": "system", "content": self._AUTOTITLE_PROMPT},
                    {
                        "role": "user",
                        "content": "\n\n".join(openers)
                        + (
                            f"\n\n[the assistant's first reply]\n{assistant}"
                            if assistant
                            else ""
                        ),
                    },
                ],
                temperature=0.2,
                # Reasoning-routed models spend hidden tokens BEFORE emitting text; a
                # tight cap plus default effort yields an empty completion and a silent
                # no-op. Effort "none" reaches only the OpenAI-compat path (the native
                # providers whitelist their settings), and 64 leaves headroom either way.
                max_tokens=64,
                reasoning_effort="none",
            )
            raw = (getattr(turn, "text", None) or "").strip()
            # Sanitize: surrounding quotes off, whitespace collapsed, capped at 60.
            title = " ".join(raw.strip("\"'“”‘’`").split())
            # Sentinel tolerance: models riff on the exact token ("Small talk.", quoted,
            # trailing period) — normalize before comparing, else the riff becomes the title.
            if title.lower().strip(".!,;:'\"").replace(" ", "-").replace("_", "-") in (
                "small-talk",
                "smalltalk",
            ):
                return
            if not title or len(title) > 80:
                return
            if self.session_store.set_auto_title(session_id, title[:60]):
                # Best-effort nudge for any live viewer; the sidebar's poll and
                # post-turn refresh pick the new title up regardless.
                await self.broadcast_session(
                    session_id,
                    {
                        "type": "session_title",
                        "data": {"session_id": session_id, "title": title[:60]},
                    },
                )
        except Exception:
            # A failed title must never surface as a session error — but it must
            # not be invisible either (a silent provider 400 hid the max_tokens
            # rejection for a whole owner test pass, 2026-07-20).
            # warning, not debug: debug was invisible in packaged builds, which re-hid
            # exactly the class of failure this comment warns about (2026-08-24: the plan
            # backend 400-ing on max_output_tokens went unseen for a whole test pass).
            logger.warning("autotitle failed for %s", session_id, exc_info=True)
        finally:
            self._autotitle_inflight.discard(session_id)

    # -- session roots (orphan Cowork: scratch + added folders) ------------------
    def get_roots(self, session_id: str) -> list[dict[str, Any]]:
        """The directories this session can touch: primary scratch first, then added folders.
        Reads the live engine when one is running; otherwise reconstructs from persisted state.
        """
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None):
            return [
                {
                    "path": str(r.path),
                    "writable": bool(r.writable),
                    "label": r.label,
                    "primary": i == 0,
                    "exists": r.path.is_dir(),
                }
                for i, r in enumerate(engine.roots)
            ]
        record = self.session_store.load(session_id)
        primary = (
            record.workspace
            if record and record.workspace
            else self._provision_scratch(session_id)
        )
        extra = (record.extra_roots if record else []) or []
        primary_is_scratch = self.is_temp_workspace(primary)
        out = [
            {
                "path": primary,
                "writable": True,
                "label": "scratch" if primary_is_scratch else "workspace",
                "primary": True,
                "exists": Path(primary).is_dir(),
            }
        ]
        # Universal scratch: a real-folder session also carries its provisioned scratch
        # root (mirrors the engine-side shape so a cold read matches a live one).
        if not primary_is_scratch and self._SESSION_ID_RE.match(session_id or ""):
            scratch = self.scratch_base() / session_id
            out.append(
                {
                    "path": str(scratch.expanduser().resolve()),
                    "writable": True,
                    "label": "scratch",
                    "primary": False,
                    "exists": scratch.is_dir(),
                }
            )
        for r in extra:
            p = str(r.get("path", ""))
            out.append(
                {
                    "path": p,
                    "writable": bool(r.get("writable", False)),
                    "label": r.get("label") or Path(p).name,
                    "primary": False,
                    "exists": Path(p).is_dir(),
                }
            )
        return out

    def promote_workspace(self, session_id: str, path: str) -> dict[str, Any]:
        """Root promotion (workspace-scratch-design.md §5): adopt `path` as the session's
        primary workspace. One-way and once — only while the primary is still the
        provisioned scratch; a session that already has a real workspace is never
        re-pointed. Mutates the live session (roots + shell cwd), persists, and marks
        the engine for a post-turn rebuild."""
        p = Path(path).expanduser()
        if not p.is_dir():
            return {"ok": False, "error": f"not a directory: {path}"}
        resolved = p.resolve()
        engine = self._engines.get(session_id)
        if engine is None:
            return {"ok": False, "error": "no live session to promote"}
        executor = getattr(engine, "executor", None)
        current = str(executor.cwd) if executor is not None else None
        if not current or not self.is_temp_workspace(current):
            return {"ok": False, "error": "this session already has a workspace"}
        roots = getattr(engine, "roots", None)
        if roots is None:
            return {"ok": False, "error": "this session has no directory list"}
        # Shared list: permissions, file tools, and the context injector see the new
        # primary immediately. The old scratch primary stays as the scratch root.
        roots[:] = [
            RootDir(path=resolved, writable=True, label="workspace"),
            *[r for r in roots if r.path != resolved],
        ]
        try:
            # Move the live shell too — save() derives the persisted workspace from the
            # executor's cwd, so this is also what makes the promotion durable.
            res = executor.run(f"cd {shlex.quote(str(resolved))}", timeout=15)
            if res.get("exit_code") != 0:
                executor.cwd = str(resolved)
        except Exception:
            executor.cwd = str(resolved)  # a respawned shell starts there
        self.save(session_id, engine)
        self.session_store.touch_workspace(str(resolved))
        self._promotion_rebuild.add(session_id)
        return {"ok": True, "path": str(resolved), "roots": self.get_roots(session_id)}

    def add_root(
        self, session_id: str, path: str, writable: bool = False
    ) -> dict[str, Any]:
        """Grant the session access to another folder (read-only or read-write). Mutates the live
        engine in place when running (file tools + permissions + context see it immediately) and
        persists it so a later resume still has it."""
        p = Path(path).expanduser()
        if not p.is_dir():
            return {"ok": False, "error": f"not a directory: {path}"}
        resolved = p.resolve()
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None) is not None:
            if any(r.path == resolved for r in engine.roots):
                # already present: just update its access level
                for r in engine.roots:
                    if r.path == resolved:
                        r.writable = bool(writable)
            else:
                engine.roots.append(RootDir(path=resolved, writable=bool(writable)))
            self.session_store.set_extra_roots(
                session_id, self._extra_roots_of(engine, session_id)
            )
        else:
            # A brand-new conversation has no record yet (it's only saved after the first turn) —
            # create one now so set_extra_roots has a row to update and the folder survives.
            if self.session_store.load(session_id) is None:
                self.session_store.save(
                    SessionRecord(
                        session_id=session_id,
                        workspace=self._provision_scratch(session_id),
                        model=self.model,
                        mode=self.mode.value,
                        messages=[],
                        agent="cowork",  # folder access is a Cowork affordance
                    )
                )
            session_scratch = str((self.scratch_base() / session_id).expanduser().resolve())
            extra = [
                r
                for r in self.get_roots(session_id)
                if not r["primary"] and r["path"] != session_scratch
            ]
            extra = [r for r in extra if Path(r["path"]).resolve() != resolved]
            extra.append(
                {
                    "path": str(resolved),
                    "writable": bool(writable),
                    "label": resolved.name,
                }
            )
            self.session_store.set_extra_roots(
                session_id,
                [
                    {
                        "path": r["path"],
                        "writable": r["writable"],
                        "label": r.get("label", ""),
                    }
                    for r in extra
                ],
            )
        self.session_store.touch_workspace(str(resolved))
        # Grant-time notice (pass 20): if this directory's project already has
        # memory or a board, say so — one line, pointer only, to agent + user.
        notice = self._project_notice(str(resolved))
        engine = self._engines.get(session_id)
        if notice and engine is not None:
            engine._append_notice("project_presence", notice)
        return {"ok": True, "roots": self.get_roots(session_id), "notice": notice}

    def _project_notice(self, path: str) -> Optional[str]:
        """One-line presence pointer for a newly granted directory, or None."""
        try:
            key = project_key(path)
            pres = project_presence(
                key, memory_store=self.memory_store, team_store=self.team_store
            )
        except Exception:
            return None
        parts = []
        if pres["memories"]:
            parts.append(f"project memory ({pres['memories']} entries)")
        if pres["board_items"]:
            parts.append("a board")
        if not parts:
            return None
        label = project_label(key)["label"]
        return f"“{label}” already has {' and '.join(parts)} — bind it by name or start a session there to use it."

    def remove_root(self, session_id: str, path: str) -> dict[str, Any]:
        """Revoke a previously-added folder. The primary scratch cannot be removed."""
        resolved = Path(path).expanduser().resolve()
        engine = self._engines.get(session_id)
        if engine is not None and getattr(engine, "roots", None):
            if engine.roots and engine.roots[0].path == resolved:
                return {
                    "ok": False,
                    "error": "cannot remove the primary scratch directory",
                }
            engine.roots[:] = [r for r in engine.roots if r.path != resolved]
            self.session_store.set_extra_roots(
                session_id, self._extra_roots_of(engine, session_id)
            )
        else:
            current = self.get_roots(session_id)
            if (
                current
                and current[0]["primary"]
                and Path(current[0]["path"]).resolve() == resolved
            ):
                return {
                    "ok": False,
                    "error": "cannot remove the primary scratch directory",
                }
            session_scratch = (self.scratch_base() / session_id).expanduser().resolve()
            extra = [
                r
                for r in current
                if not r["primary"]
                and Path(r["path"]).resolve() not in (resolved, session_scratch)
            ]
            self.session_store.set_extra_roots(
                session_id,
                [
                    {
                        "path": r["path"],
                        "writable": r["writable"],
                        "label": r.get("label", ""),
                    }
                    for r in extra
                ],
            )
        return {"ok": True, "roots": self.get_roots(session_id)}

    def session_messages(self, session_id: str) -> list[dict[str, Any]]:
        # A live engine's in-memory thread is authoritative: mid-turn it's ahead of the
        # persisted record — which may not even exist yet for a scheduled run's first turn
        # (opening a "running" automation showed a blank session; owner report 2026-07-04).
        engine = self._engines.get(session_id)
        if engine is not None:
            return list(engine.messages)
        record = self.session_store.load(session_id)
        return record.messages if record else []

    def append_session_messages(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> bool:
        """Append already-built transcript messages (e.g. a generated review-report
        card) to a session so they persist and reappear on reload.

        Keeps the live engine (if one exists for this session) in sync — otherwise a
        later `save(engine)` would rewrite the file from the engine's shorter list and
        drop what we just appended. Returns False if the session doesn't exist.
        """
        if not messages:
            return True
        engine = self._engines.get(session_id)
        if engine is not None:
            engine.messages.extend(messages)
        record = self.session_store.load(session_id)
        if record is None:
            return False
        record.messages = (record.messages or []) + messages
        self.session_store.save(record)
        return True

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be renamed"}
        ok = self.session_store.rename(session_id, title)
        return {
            "ok": ok,
            "session_id": session_id,
            "title": " ".join((title or "").split())[:120],
        }

    def set_session_flags(
        self,
        session_id: str,
        *,
        pinned: Optional[bool] = None,
        archived: Optional[bool] = None,
    ) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be modified here"}
        ok = self.session_store.set_flags(session_id, pinned=pinned, archived=archived)
        return {"ok": ok, "session_id": session_id}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        if session_id.startswith("__"):
            return {"ok": False, "error": "internal sessions cannot be deleted here"}
        # A team member is not an ordinary chat. Deleting one used to leave the team
        # registry pointing at a session that no longer existed: `assign` still validated
        # (the ghost worker was listed), the wake went to a session with no record and no
        # engine, nothing ran, and nothing told the lead — dispatched work just vanished
        # and the lead ended up doing it itself. Detach FIRST, so the team stops
        # advertising the member before its record goes away.
        detached = self.teams.detach_session(session_id)
        if detached["role"] is not None:
            logger.info(
                "session %s deleted: detached %s '%s' from team %s (%d workers left%s)",
                session_id,
                detached["role"],
                detached["actor"],
                detached["team_id"],
                detached["workers_left"],
                ", team dropped" if detached["team_removed"] else "",
            )
        engine = self._engines.pop(session_id, None)
        if engine is not None:
            try:
                # (was engine.interrupt() — a method that never existed; the AttributeError
                # was silently swallowed, so deleting a running session never stopped it.)
                engine.request_interrupt()
            except Exception:
                pass
        record = self.session_store.load(session_id)
        ok = self.session_store.delete(session_id)
        # Deleting a session is the one implicit unsubscribe (otherwise subscriptions are permanent).
        self.subscriptions.remove_session(session_id)
        # ...and releases any Slack threads it owned (§31): the next tag there spawns fresh.
        self.mention_sessions.remove_session(session_id)
        # ...and drops its per-session connector overrides (§4.2, like subscriptions).
        self.session_connections.remove_session(session_id)
        # ...and its per-session skill mutes (SKILLS-SPEC §3 — mutes die with the session).
        self.session_skills.remove_session(session_id)
        # ...and closes its pending Inbox items — an orphaned approval/question can never be
        # meaningfully answered (owner call, 2026-07-03).
        self.inbox.resolve_session(session_id)
        # ...and its scratch dir. STRICTLY scoped: only a directory inside scratch_base is
        # removed — a real project folder the user picked is never touched.
        if ok and record and record.workspace:
            scratch = self.scratch_base().resolve()
            ws = Path(record.workspace)
            try:
                resolved = ws.resolve()
                if (
                    resolved.is_relative_to(scratch)
                    and resolved != scratch
                    and resolved.is_dir()
                ):
                    shutil.rmtree(resolved)
            except OSError:
                pass  # a stale/foreign path must not fail the delete
        out = {"ok": ok, "session_id": session_id}
        # Surfaced so the caller can say "removed from team X" instead of silently
        # leaving a board full of work assigned to a session that is gone.
        if detached["role"] is not None:
            out["team"] = detached
        return out

    # -- provider proxy ---------------------------------------------------------
    def provider_complete(self, model, messages, tools=None):
        return self.provider.complete(model=model, messages=messages, tools=tools)

    def _refresh_provider(self, name: Optional[str] = None) -> None:
        """Drop the router's cached client(s) so the next turn rebuilds with fresh config.
        No-op for an injected non-router provider (tests)."""
        invalidate = getattr(self.provider, "invalidate", None)
        if callable(invalidate):
            invalidate(name)

    # -- read models ------------------------------------------------------------
    def list_sessions(self, workspace: Optional[str] = None) -> list[dict[str, Any]]:
        ws = self.resolve_workspace(workspace) if workspace else None
        return [
            {
                "session_id": r.session_id,
                "title": r.title or "New session",
                "workspace": r.workspace,
                "agent": r.agent,
                "model": r.model,
                "mode": r.mode,
                "updated_at": r.updated_at,
                "messages": r.message_count,
                "pinned": r.pinned,
                "archived": r.archived,
                # §31: non-user origin ("slack") + display label — drives the sidebar's
                # "From Slack" group and the row's platform icon.
                "origin": r.origin,
                "origin_label": r.origin_label,
                # Attention = Inbox items awaiting this session (the amber count that bubbles
                # session → persona → footer Inbox). Liveness = working (in-flight turn) /
                # sleeping (a self-wake is pending) / idle — a count-less dot that never bubbles.
                "attention": len(self.inbox.pending(session_id=r.session_id)),
                "liveness": self._session_liveness(r.session_id),
                # When sleeping: the next timer fire (ISO) — drives the "sleeping
                # until…" strip so a scheduled agent never reads as a dead one.
                "sleeping_until": self._sleeping_until(r.session_id),
                # Channels this session listens to (inbound subscriptions) — drives the per-session
                # "connections" indicator.
                "subscriptions": [
                    s.channel for s in self.subscriptions.for_session(r.session_id)
                ],
                # Agent teams: {} for plain sessions. Workers carry role/lead_session
                # (+ a computed current-item line); leads carry role/team_id — drives
                # the sidebar's ONE expandable team entry.
                "team": self._session_team_row(r),
            }
            for r in self.session_store.list(workspace=ws)
            if not r.session_id.startswith("__")  # hide internal threads
        ]

    def _session_team_row(self, record: SessionRecord) -> dict[str, Any]:
        info = record.team or {}
        if not info:
            return {}
        row = {
            "role": info.get("role", ""),
            "team_id": info.get("team_id", ""),
            "lead_session": info.get("lead_session", ""),
        }
        if info.get("role") == "lead":
            team = self.teams.get(str(info.get("team_id", "")))
            if team is not None and team.chat_enabled and team.chat_group:
                row["chat_enabled"] = True
                row["chat_unread"] = self.chat_store.unread_count(
                    team.chat_group, "user"
                )
        if info.get("role") == "worker" and info.get("space") and info.get("actor"):
            try:
                items = self.team_store.list_items(
                    str(info["space"]), self._user_actor(), assignee=str(info["actor"])
                )
            except Exception:
                items = []
            active = next(
                (
                    i
                    for state in ("blocked", "review", "in_progress", "open")
                    for i in items
                    if i["state"] == state
                ),
                None,
            )
            row["actor"] = info["actor"]
            row["current_item"] = (
                f"#{active['id']} {active['state'].replace('_', ' ')}" if active else "idle"
            )
            row["status"] = active["state"] if active else "idle"
        return row

    def _sleeping_until(self, session_id: str) -> Optional[str]:
        fires = [
            w.fire_at
            for w in self.wakes.pending(session_id)
            if w.kind == "timer" and w.fire_at
        ]
        return min(fires) if fires else None

    def _session_liveness(self, session_id: str) -> str:
        if self.is_running(session_id):
            return "working"
        if self.wakes.pending(session_id):
            return "sleeping"
        return "idle"

    def list_agents(self) -> list[dict[str, Any]]:
        return _list_agents()

    # -- skills (SKILLS-SPEC §4.4) ------------------------------------------------
    def list_skills(self, workspace: Optional[str] = None) -> list[dict[str, Any]]:
        """Enriched rows for the Settings screen (scope/source/enabled). Optional workspace
        adds that project's skills, with project copies shadowing same-named global ones."""
        return self.skill_store.rows(workspace or None)

    def reveal_skill(
        self, name: str, workspace: Optional[str] = None
    ) -> dict[str, Any]:
        """Open the skill's folder in the OS file manager (§6 "Show folder" — the power-user
        window into folder-is-truth). Same local-machine rationale as reveal_artifact."""
        import subprocess
        import sys

        try:
            folder, _scope = self.skill_store.find(name, workspace or None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif sys.platform == "win32":
                import os

                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(
                    ["xdg-open", str(folder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def persona_mcp_scope(self, persona_id: str) -> Optional[set[str]]:
        """The persona's declared MCP-server scope (OPE-58 sibling stub): the manifest's
        `mcp:` names, or None when it declares none (= no scoping). Only ever narrows —
        the user's enabled/configured/authed gates apply regardless."""
        entry = self.personas.get(persona_id)
        names = list((entry.manifest.mcp if entry and entry.manifest else []) or [])
        return {n for n in names if n} or None

    def persona_skill_scope(
        self, persona_id: str
    ) -> tuple[Optional[Path], Optional[set[str]]]:
        """The persona's own skill folder + optional allowlist (OPE-58).

        A manifest-backed persona carries skills as a `skills/` dir next to its manifest —
        the sharing bundle shape (manifest + skill folders). The manifest's `skills:` list,
        when non-empty, narrows which of those activate. Additive on top of global/project
        scopes: the persona SHIPS skills; it never hides the user's own."""
        entry = self.personas.get(persona_id)
        manifest = entry.manifest if entry else None
        if manifest is None or not manifest.source:
            return None, None
        d = Path(manifest.source).parent / "skills"
        if not d.is_dir():
            return None, None
        allow = {s for s in manifest.skills if s} or None
        return d, allow

    def effective_skill_names(
        self,
        session_id: str,
        workspace: Optional[str | Path] = None,
        agent: Optional[str] = None,
    ) -> set[str]:
        """The session's skill menu (§3): merged scopes − Settings disables − session mutes.
        The single resolver behind the engine catalog, the rail list, and the composer popup.
        Persona-carried skills (OPE-58) join the merge for the session's persona — user
        disables and mutes still win over them."""
        dirs = [self.skill_store.global_dir]
        if workspace:
            dirs.append(self.skill_store.project_dir(workspace))
        loader = SkillLoader(dirs)
        names = set(loader.names())
        persona_dir, allow = self.persona_skill_scope(self._persona_of(session_id, agent))
        if persona_dir is not None:
            persona_names = set(SkillLoader([persona_dir]).names())
            if allow is not None:
                persona_names &= allow
            names |= persona_names
        return effective_skills(
            names=names,
            disabled=self.skill_store.disabled_names(),
            session_overrides=self.session_skills.get(session_id),
        )

    def session_skills_view(
        self, session_id: str, workspace: Optional[str] = None
    ) -> dict[str, Any]:
        """The rail payload: every in-scope, Settings-enabled skill with its mute state.
        Persona-carried skills (OPE-58) appear with scope "coworker" — mutable per session
        like any other, but owned by the persona bundle, not the Settings store."""
        disabled = self.skill_store.disabled_names()
        overrides = self.session_skills.get(session_id)
        rows = [
            {
                "name": r["name"],
                "description": r["description"],
                "scope": r["scope"],
                "enabled": overrides.get(r["name"], True),
            }
            for r in self.skill_store.rows(workspace or None)
            if r["name"] not in disabled
        ]
        seen = {r["name"] for r in rows}
        persona_dir, allow = self.persona_skill_scope(self._persona_of(session_id))
        if persona_dir is not None:
            for entry in SkillLoader([persona_dir]).catalog():
                name = entry["name"]
                if name in seen or name in disabled:
                    continue  # a global/project copy shadows the bundle's
                if allow is not None and name not in allow:
                    continue
                rows.append(
                    {
                        "name": name,
                        "description": entry["description"],
                        "scope": "coworker",
                        "enabled": overrides.get(name, True),
                    }
                )
        return {"skills": rows}

    def _scratch_workspace_error(self, workspace: Any) -> Optional[dict[str, Any]]:
        """Refuse skill WRITES into a per-conversation scratch dir — a skill saved there is
        stranded in a throwaway folder. Backend chokepoint: guards every entry path (UI,
        REST, future import), not just the flows the GUI happens to gate."""
        if not workspace:
            return None
        try:
            ws = Path(str(workspace)).expanduser().resolve()
            if ws.is_relative_to(self.scratch_base().resolve()):
                return {
                    "ok": False,
                    "error": (
                        "That folder is a temporary session space — skills saved there "
                        "would be lost. Save it globally or pick a real project."
                    ),
                }
        except OSError:
            pass
        return None

    def create_skill(self, body: dict[str, Any]) -> dict[str, Any]:
        blocked = self._scratch_workspace_error(body.get("workspace"))
        if blocked:
            return blocked
        try:
            created = self.skill_store.create(
                name=str(body.get("name", "")),
                description=str(body.get("description", "")),
                instructions=str(body.get("instructions", "")),
                scope=str(body.get("scope", "global") or "global"),
                workspace=body.get("workspace") or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": created}

    def update_skill(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if "enabled" in body:
                self.skill_store.set_enabled(name, bool(body["enabled"]))
            if body.get("description") is not None or body.get("instructions") is not None:
                self.skill_store.update(
                    name,
                    description=body.get("description"),
                    instructions=body.get("instructions"),
                    workspace=body.get("workspace") or None,
                )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def delete_skill(self, name: str, workspace: Optional[str] = None) -> dict[str, Any]:
        try:
            self.skill_store.delete(name, workspace or None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def move_skill(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        # Moving INTO project scope must not target a scratch dir (moving OUT is fine —
        # that's the rescue path for already-stranded skills).
        if str(body.get("scope", "")) == "project":
            blocked = self._scratch_workspace_error(body.get("workspace"))
            if blocked:
                return blocked
        try:
            moved = self.skill_store.move(
                name,
                to_scope=str(body.get("scope", "")),
                workspace=body.get("workspace") or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": moved}

    def stage_skill_upload(self, data: bytes, filename: str = "") -> dict[str, Any]:
        try:
            preview = self.skill_store.stage_upload(data, filename)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **preview}

    def confirm_skill_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        blocked = self._scratch_workspace_error(body.get("workspace"))
        if blocked:
            return blocked
        try:
            saved = self.skill_store.confirm_upload(
                str(body.get("token", "")),
                scope=str(body.get("scope", "global") or "global"),
                workspace=body.get("workspace") or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "skill": saved}

    def _memory_saved_notifier(self, session_id: str):
        """MEMORY-SPEC §5.1: push the memory_saved event that powers the GUI's save
        toast ("I'll remember that — … [Undo]"). Best-effort by design: `remember` may
        run with no socket attached (background runs) or off the loop thread — a lost
        toast never fails the save."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def notify(item, previous=None) -> None:
            if loop is None or not loop.is_running():
                return
            payload = {
                "type": "memory_saved",
                "data": {
                    "id": item.id,
                    "scope": item.scope.value,
                    "summary": item.summary or "",
                    "content": item.content,
                    # Set when this was an EDIT of an existing memory: the surface says
                    # "I've updated what I remember" and Undo restores this text.
                    "previous": previous or "",
                },
            }
            try:
                asyncio.run_coroutine_threadsafe(
                    self.broadcast_session(session_id, payload), loop
                )
            except RuntimeError:
                pass

        return notify

    # -- project bindings (pass 20 / UX-044) -------------------------------------

    def project_menu(self, session_id: str, kind: str) -> dict[str, Any]:
        """The submenu payload: the session's derived project (pinned, labeled per
        the UX-044 rules) + named entries MRU-ordered. The GUI shows 5 and grows a
        filter at 6+; the full named list ships so the filter reaches everything."""
        record = self.session_store.load(session_id)
        ws = (record.workspace if record else None) or self.default_workspace
        derived_key = project_key(ws) if ws else None
        names = self.session_store.names()
        bound = ((record.bindings if record else {}) or {}).get(kind)
        named = names.list(kind)
        return {
            "kind": kind,
            "bound": bound,
            "derived": (
                {**project_label(derived_key), "key": derived_key}
                if derived_key
                else None
            ),
            "named": [{"name": n["name"], "key": n["key"]} for n in named],
        }

    def set_binding(
        self, session_id: str, kind: str, name: Optional[str]
    ) -> dict[str, Any]:
        """Bind (or unbind, name=None) a named project for this session. Takes
        effect at the next engine build — the running engine keeps the knowledge
        it started with (same doctrine as memory deletions)."""
        if kind not in ("memory", "board"):
            return {"ok": False, "error": f"unknown kind {kind!r}"}
        if self.is_running(session_id):
            return {"ok": False, "error": "wait for the current task to finish first"}
        if name and self.session_store.names().resolve(kind, name) is None:
            return {"ok": False, "error": f"no {kind} named {name!r}"}
        record = self.session_store.load(session_id)
        bindings = dict((record.bindings if record else {}) or {})
        if name:
            bindings[kind] = name
        else:
            bindings.pop(kind, None)
        if record is None:
            return {"ok": False, "error": "unknown session"}
        self.session_store.set_bindings(session_id, bindings)
        # Rebind applies from the next engine build; drop the cached engine so the
        # next turn rebuilds with the new key (messages persist via the record).
        self._engines.pop(session_id, None)
        return {"ok": True, "bindings": bindings}

    def name_current_project(
        self, session_id: str, kind: str, name: str
    ) -> dict[str, Any]:
        """Give the session's derived project a user name (UX-044 'Name current…')."""
        record = self.session_store.load(session_id)
        ws = (record.workspace if record else None) or self.default_workspace
        if not ws:
            return {"ok": False, "error": "session has no workspace"}
        try:
            entry = self.session_store.names().name_current(
                kind, name, project_key(ws)
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **entry}

    def list_memory(self) -> list[dict[str, Any]]:
        return [
            {
                "id": m.id,
                "scope": m.scope.value,
                "content": m.content,
                "summary": m.summary or "",
                "created_at": m.created_at or "",
            }
            for m in self.memory_store.list()
        ]

    def add_memory(
        self, content: str, scope: str = "workspace", workspace: Optional[str] = None
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"ok": False, "error": "content required"}
        chosen = Scope(scope) if scope in _SCOPES else Scope.WORKSPACE
        ws = self.resolve_workspace(workspace) if chosen is Scope.WORKSPACE else None
        item = self.memory_store.add(content, scope=chosen, workspace=ws)
        return {"id": item.id, "scope": item.scope.value, "content": item.content}

    def update_memory(self, item_id: int, content: str) -> dict[str, Any]:
        """Edit-in-place from the memory screen (§5.3). The user rewrote the fact, so
        the stale one-line summary is cleared rather than left contradicting it."""
        content = (content or "").strip()
        if not content:
            return {"ok": False, "error": "content required"}
        item = self.memory_store.update(item_id, content, summary="")
        if item is None:
            return {"ok": False, "error": f"no memory with id {item_id}"}
        return {"ok": True, "id": item.id, "content": item.content}

    def delete_memory(self, item_id: int) -> dict[str, Any]:
        """Row delete on the memory screen — and the toast's Undo (§5.1)."""
        if self.memory_store.delete(item_id):
            return {"ok": True, "id": item_id}
        return {"ok": False, "error": f"no memory with id {item_id}"}

    def delete_all_memory(self) -> dict[str, Any]:
        return {"ok": True, "deleted": self.memory_store.delete_all()}

    def get_memory_settings(self) -> dict[str, Any]:
        return self.memory_settings.snapshot()

    def set_memory_settings(
        self,
        enabled: Optional[bool] = None,
        user_rules: Optional[str] = None,
        auto_inject: Optional[bool] = None,
    ) -> dict[str, Any]:
        return self.memory_settings.set(
            enabled=enabled, user_rules=user_rules, auto_inject=auto_inject
        )


def _parse_inbox_json(s: str) -> dict[str, Any]:
    """Parse a structured Inbox resolution (directory/plan carry their reply as a JSON string)."""
    import json as _json

    try:
        v = _json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _epoch() -> float:
    import time

    return time.time()


# A Slack message ts looks like "1700000001.000001" (epoch seconds + microseconds). Other
# platforms use opaque/incrementing ids (e.g. a Telegram integer), so only parse the Slack shape.
_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")


def _inbound_epoch(message_id: Optional[str]) -> float:
    """Best-effort epoch-seconds for a MessageSource: a Slack-style ts, else wall-clock now."""
    if message_id and _SLACK_TS_RE.match(str(message_id)):
        try:
            return float(message_id)
        except ValueError:
            pass
    return time.time()


def _last_assistant_text(messages: list[dict[str, Any]]) -> Optional[str]:
    for msg in reversed(messages or []):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return None


def _recent_files(workspace: str, *, since: float, limit: int = 20) -> list[str]:
    """Files in the task workspace modified during the run — the run's artifacts."""
    out: list[str] = []
    root = Path(workspace)
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            if path.is_file() and path.stat().st_mtime >= since - 1:
                out.append(str(path.relative_to(root)))
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".xlsx", ".xls"}:
        return "sheet"
    if suffix in {".pptx", ".ppt", ".pptm", ".docx", ".doc", ".docm"}:
        return "office"
    if suffix in {".csv", ".tsv"}:
        return "csv"
    if suffix in {".py", ".js", ".ts", ".tsx", ".css", ".json"}:
        return "code"
    return "text"


def _redact(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy of a server config safe to return over REST — env/header values masked."""
    out = dict(raw)
    for key in ("env", "headers"):
        if isinstance(out.get(key), dict):
            out[key] = {k: ("***" if v else v) for k, v in out[key].items()}
    return out


def _git_branch(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=3,
        )
        branch = result.stdout.strip()
        return branch or None
    except (OSError, subprocess.SubprocessError):
        return None
