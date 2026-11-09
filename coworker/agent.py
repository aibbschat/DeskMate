"""Engine assembly from an Agent (Code / Chat / …).

Wires the agent's base tools + permissions + AGENTS.md (workspace agents) + memory +
the skill catalog (progressive disclosure) + load_skill into a TurnEngine.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)
_deskmate_mgr_lock = threading.Lock()

from .agents import Agent, AgentContext, code_agent
from .automation import scheduling_tools
from .selfwake import selfwake_tools
from .subscriptions import subscription_tools
from .config import load_config
from .connectors import (
    connector_list,
    load_settings,
    make_integration_tools,
    make_send_file_tool,
    make_send_message_tool,
)
from .engine import Approver, TurnEngine
from .environment import environment_context
from .memory import (
    MemoryStore,
    Scope,
    format_memories,
    format_user_rules,
    memory_tools,
    render_memory_block,
)
from .permissions import EXECUTION_LEVELS, Mode, PermissionEngine
from .project import load_agents_md
from . import session_facts
from .roots import RootDir, normalize_roots, render_context
from .providers import ProviderClient, ProviderRouter
from .overrides import RiskOverrideStore
from .secrets import SecretStore, state_dir
from .skills import SkillLoader, save_skill_tool, skill_catalog_text, skill_tools
from .tools import ToolRegistry
from .tools.ask import ask_user_tool
from .tools.directories import request_directory_tool
from .tools.plan import propose_plan_tool
from .tools.toolreq import request_tool_tool
from .tools.subagent import explorer_tools
from .tools import a2a as a2a_tools
from .web import make_web_fetch_tool, make_web_search_tool
from .history import make_history_search_tool, make_library_check_tool, make_private_search_tool
from .workspace_trust import WorkspaceTrustStore
from .tools.shell import LocalExecutor
from .tools.todo import TodoList

# Appended each turn while discuss mode is active: enforcement-only read-only, with no
# pressure toward a plan proposal (that's what distinguishes it from plan mode).
_DISCUSS_MODE_CONTEXT = """\
Discuss mode is active: write and shell tools are disabled. Explore and answer freely; if
the user asks for a change, describe it in chat instead of attempting it (they can switch
to plan or approval mode to have you make it)."""

# Appended to the latest user message every turn while plan mode is active. The mode can
# flip mid-session (plan approval), so this can't live in the static instructions.
_PLAN_MODE_CONTEXT = """\
Plan mode is active: write and shell tools are blocked. Explore read-only and design an
approach. When you've committed to one, present it with `propose_plan` (what you'll change,
in which files, how you'll verify) — don't describe edits as if you were making them. If
the plan is approved, this same session switches to execution and you implement it; if
rejected, revise the plan using the feedback."""

# When-to-remember rules (MEMORY-SPEC §4.2), injected only when a memory store is wired.
# Without these, models either never call `remember` or save noise the repo already
# records. The conservative bias is deliberate: a wrong memory feels broken and creepy at
# once; a missing one merely means the user repeats themselves.
# 2026-11-09 红线段：小桔的记忆只属于她自己和主工作 Agent（cowork）。
# 专家 / 工人 persona 的一对一会话不得读写小桔记忆——此前 build_engine 对**所有**
# agent 都挂 post_turn_hook + memory_store + context_provider，实测：影视专家 1v1
# 被写进 ~/.deskmate/sessions、xiaosheng_observations、USER.md，随后被"自我表达"
# 行为当作小桔本人的话弹出。这里统一收口，只有下列 agent 进小桔记忆。
MEMORY_COMPANION_AGENTS: frozenset[str] = frozenset({"deskmate", "cowork"})

_MEMORY_GUIDANCE = """\
Memory:
- You have persistent memory across sessions. Use `remember` for durable facts: the user's \
corrections and stated preferences (include the why), and project context you couldn't \
rederive from the code. Scope by what the fact is about: facts about the user -> "global"; \
facts about the current work -> "workspace". Always pass a one-line summary (15 words max) \
alongside the full content.
- Save conservatively — a wrong memory costs more than a missing one. Save only clearly \
durable facts ("from now on", "always", "in all my chats"). Ambiguous one-off phrasing \
("I prefer simple talking"): apply it now, don't save it. But when the user explicitly \
asks you to remember something, always save it.
- Sensitive topics (health, finances, relationships, beliefs): never save silently. Ask \
first — "Want me to remember this for next time?" — and save only on a yes.
- When you save, say so in one short plain sentence in your visible reply ("I'll remember \
that you prefer short replies."). And the first time a remembered fact shapes your \
behavior in a session, note it in one quiet line ("Keeping this short since you prefer \
simple replies.") — first use only, not every message.
- Don't save what the repo already records (code structure, git history, AGENTS.md) or \
details that only matter to the current task. Use absolute dates, never "yesterday".
- Before saving, check the known-memories list: if an entry already covers it, revise that \
entry with `memory_update` instead of adding a near-duplicate; retire wrong or obsolete \
entries with `memory_forget`.
- Memories reflect when they were written. If one names a file, flag, or URL, verify it \
still exists before relying on it."""

# Injected INSTEAD of the memory guidance when the user turned memory off (§4.3).
# Off means "stop LEARNING", not "forget what you know": already-saved memories stay
# injected and usable; only the write tools are gone. Without this notice the model
# bluffs — asked to "remember" with no remember tool, it narrated a fake save through
# its todo list ("I'll remember that your favorite color is blue"), observed live
# 2026-07-28. Honesty needs the model to KNOW saving is off, not just lack the tools.
_MEMORY_OFF_NOTICE = """\
Saving new memories is turned off in this user's Settings. What you already know about \
them (the known-memories list, if any) is still true and you should keep using it — but \
you have no way to save, change, or delete anything, and nothing new from this \
conversation will carry over to future ones. If the user asks you to remember something \
new, state both halves plainly: you'll keep it in mind for the rest of this conversation, \
but it won't be saved once the conversation ends — they can turn saving back on in \
Settings ▸ Memory. Never imply you saved, noted, or will remember anything new."""

# UX-015 (§33): the GUI interleaves these status lines with humanized tool rows inside a
# collapsed "turn" — they're what the user reads while the agent works. Universal (appended
# for every persona); models that ignore it degrade gracefully to a turn with no narration.
_NARRATION_GUIDANCE = """\
Narration: before each batch of tool calls, write ONE short plain sentence saying what \
you're doing and why (e.g. "Checking what merged since yesterday's digest."). It is shown \
to the user as live progress. Don't narrate trivial single-call follow-ups, don't repeat \
the previous line, and never let narration replace your final answer."""

# A bare "hey" answered with a bare "hey" makes a specialist read as an empty chat box
# (owner catch 2026-08-24). First contact is the one moment to show what this coworker
# is for — after that, greetings stay lightweight.
_FIRST_CONTACT_GUIDANCE = """\
First contact: if the user's first message is a simple hello or open-ended ("hey", "what \
can you do?") rather than a task, don't just say hello back — say in one or two \
sentences what you do in this role, then offer two or three concrete starting points as \
an ask_user question (short option labels, phrased for this session's context — \
workspace, connected tools — and leave the free-text answer available so the user can \
type their own direction). A picked option is a clear brief: start on it. Keep it short \
and skip all of this when the user already gave you a task."""


def _enabled_connector_tools(secrets: SecretStore) -> tuple[set[str], set[str]]:
    connectors = {c["name"]: c for c in connector_list(secrets)}
    enabled_connectors = {
        name
        for name, c in connectors.items()
        if c.get("connected") and c.get("enabled")
    }
    enabled_tools = {
        tool["name"]
        for c in connectors.values()
        if c.get("name") in enabled_connectors
        for tool in c.get("tools", [])
        if tool.get("enabled")
    }
    return enabled_connectors, enabled_tools


def _loaded_skill_names(messages: list[dict[str, Any]]) -> set[str]:
    """Skills whose instructions successfully entered THIS conversation (a load_skill call
    with a non-error result). Drives the disable countermand: a menu quietly shrinking is
    passive, but instructions already in history keep steering the model unless it is
    explicitly asked to stop."""
    import json as _json

    results: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            content = m.get("content")
            results[m["tool_call_id"]] = (
                content if isinstance(content, str) else _json.dumps(content)
            )
    loaded: set[str] = set()
    for m in messages:
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        for tc in m["tool_calls"]:
            fn = tc.get("function") or {}
            if fn.get("name") != "load_skill":
                continue
            try:
                name = str(_json.loads(fn.get("arguments") or "{}").get("name", ""))
            except Exception:
                continue
            result = results.get(tc.get("id", ""), "")
            if name and '"instructions"' in result:
                loaded.add(name)
    return loaded


def _skill_dirs(workspace: Optional[Path]) -> list[Path]:
    dirs = [state_dir() / "skills"]
    if workspace is not None:
        dirs.append(workspace / ".coworker" / "skills")
    return dirs


def _deskmate_memory_context(messages: list | None = None) -> str:
    """DeskMate 记忆每轮自动注入（默认开启；DESKMATE_AUTO_INJECT=0 可关闭）。

    接入 OpenWorker 官方每轮 context_provider 机制：返回的 <system-context> 块在
    引擎发送时附到最新 user message（不持久化）。未开启或异常时返回空串，绝不影响
    原生行为（遵循「扩展而非 fork」）。

    Args:
        messages: 当前对话的消息列表，用于提取最后一条用户消息做关键词搜索。
    """
    # P0-1: 默认开启记忆注入；仅当用户在设置中关闭（DESKMATE_AUTO_INJECT=0）才跳过。
    if os.environ.get("DESKMATE_AUTO_INJECT") == "0":
        return ""
    try:
        from deskmate.memory.manager import DeskMateMemoryManager

        base = os.environ.get("DESKMATE_MEMORY_DIR") or str(Path.home() / ".deskmate")
        # Reuse a module-level singleton: creating a DeskMateMemoryManager constructs a
        # ThreadPoolExecutor each time, and context_provider runs on EVERY turn before
        # the LLM call - the overhead compounds quickly.
        global _deskmate_mgr_singleton
        if "_deskmate_mgr_singleton" not in globals():
            with _deskmate_mgr_lock:
                if "_deskmate_mgr_singleton" not in globals():
                    _deskmate_mgr_singleton = DeskMateMemoryManager(Path(base))
        mgr = _deskmate_mgr_singleton
        # 从 messages 中提取最后一条用户消息
        last_user = ""
        if messages:
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        last_user = content
                    elif isinstance(content, list):
                        # 多模态消息，取文本部分
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                last_user = part.get("text", "")
                                break
                    break
        return mgr.build_full_context_for_turn(last_user)
    except Exception:
        return ""


def _bot_group_prompt_fallback(text: str | None) -> bool:
    """``deskmate.memory.manager.is_bot_group_prompt`` 不可用时的同源兜底。

    守门必须 fail-closed：deskmate 侧 import 失败时，也不能让技能进化把 bot
    发言写进小桔的手艺规则。与 manager.py 的判定保持同一组前缀 / 指令语。
    """
    if not text or not isinstance(text, str):
        return False
    head = text.strip()[:240]
    return (
        head.startswith(("群里刚有人说", "群内已有发言"))
        or "请直接以你自己的身份发言" in head
        or ("这是第" in head and "轮接话" in head)
    )


def _deskmate_post_turn_hook(user_msg: str, assistant_msg: str) -> None:
    """DeskMate 会话后自动抽取（默认开启；DESKMATE_AUTO_INJECT=0 可关闭）。

    在引擎每轮对话完成后自动调用，提取记忆和技能。
    火后即忘，绝不阻塞主循环。

    复用模块级单例（与 _deskmate_memory_context 共享同一个 DeskMateMemoryManager
    实例，并为 SkillManager 新增单例），避免每轮重建 Manager ——
    DeskMateMemoryManager 构造会建 ThreadPoolExecutor，SkillManager 同理，高频
    对话下每轮重建的开销会累积（A10 修复）。
    """
    if os.environ.get("DESKMATE_AUTO_INJECT") == "0":
        return

    # 🔴 Bot 群发言不是小桔与萧盛的对话：整轮不抽记忆、不记萧盛影响、不进化技能。
    #    技能进化只走本 hook，是必须就地拦住的一路（实测 11-07 10:08:52 bot 原话
    #    被写进 ~/.deskmate/skills/writing/novel-review.md）。正常路径复用
    #    manager.py 的唯一判定；import 失败走同源 fallback，守门不跟着降级失效。
    #    另两道：BotRuntime 摘钩子、post_session_summary 入口过滤。
    try:
        from deskmate.memory.manager import is_bot_group_prompt
    except Exception:
        is_bot_group_prompt = _bot_group_prompt_fallback
    if is_bot_group_prompt(user_msg):
        return

    base = os.environ.get("DESKMATE_MEMORY_DIR") or str(Path.home() / ".deskmate")

    # 记忆抽取：复用 DeskMateMemoryManager 单例（与 _deskmate_memory_context 共享）
    try:
        from deskmate.memory.manager import DeskMateMemoryManager

        global _deskmate_mgr_singleton
        if "_deskmate_mgr_singleton" not in globals():
            with _deskmate_mgr_lock:
                if "_deskmate_mgr_singleton" not in globals():
                    _deskmate_mgr_singleton = DeskMateMemoryManager(Path(base))
        _deskmate_mgr_singleton.post_session_summary(user_msg, assistant_msg)
    except Exception:
        pass  # best-effort, never block

    # 技能进化闭环：成功执行后尝试优化相关技能（复用 SkillManager 单例）
    try:
        from deskmate.skills.manager import SkillManager

        global _deskmate_skill_mgr_singleton
        if "_deskmate_skill_mgr_singleton" not in globals():
            with _deskmate_mgr_lock:
                if "_deskmate_skill_mgr_singleton" not in globals():
                    _deskmate_skill_mgr_singleton = SkillManager(base_dir=Path(base))
        _deskmate_skill_mgr_singleton.evolve_skill_on_success(user_msg, assistant_msg)
    except Exception:
        pass  # best-effort, never block


def _deskmate_skill_index() -> str:
    """DeskMate 技能索引（注入系统 prompt）。

    读取 ~/.deskmate/skills/ 下所有技能，生成 <available_skills> 块。
    LLM 看到匹配任务时自觉调用 skill_view(name) 加载完整技能。
    无技能或异常时返回空串。
    """
    if os.environ.get("DESKMATE_AUTO_INJECT") == "0":
        return ""
    try:
        from deskmate.skills.manager import SkillManager

        base = os.environ.get("DESKMATE_MEMORY_DIR") or str(Path.home() / ".deskmate")
        mgr = SkillManager(base_dir=Path(base))
        return mgr.build_skill_index()
    except Exception:
        return ""


def _deskmate_mention_context(
    messages: list | None = None, default_persona_id: Optional[str] = None
) -> str:
    """DeskMate @角色召唤（opt-in，DESKMATE_MENTION，默认开）。

    解析最后一条 user 消息中的 @，命中角色花名册则生成 handoff 协议注入当前轮，
    让被 @ 角色以「@ 补位」语义临时参与（会话身份 = 新建会话选定的 persona）。
    无 @ / 未命中返回空串，零副作用。
    花名册 = 上游 coworker persona registry ∪ 圆桌专家（deskmate.chat.mention_router）。
    """
    try:
        from deskmate.chat.mention_router import mention_context

        return mention_context(messages, default_persona_id=default_persona_id)
    except Exception:
        return ""


def build_engine(
    *,
    agent: Agent,
    workspace: Optional[str | Path] = None,
    model: str = "gpt-5.6-sol",
    mode: Mode = Mode.INTERACTIVE,
    approver: Optional[Approver] = None,
    provider: Optional[ProviderClient] = None,
    allowed_commands: Optional[list[str]] = None,
    max_iterations: Optional[int] = None,
    model_settings: Optional[dict[str, Any]] = None,
    memory_store: Optional[MemoryStore] = None,
    # Twentieth pass: the project key memory loads/saves under. Defaults to the
    # workspace path; the manager passes the resolved key (binding > git > path)
    # so all worktrees of a repo share one memory and named bindings work.
    memory_workspace: Optional[str] = None,
    # MEMORY-SPEC §5.1: called with the MemoryItem right after `remember` persists it —
    # the manager uses this to push the memory_saved event that powers the save toast.
    on_memory_saved: Optional[Any] = None,
    # MEMORY-SPEC §6: the user's standing rules (Settings textarea). Injected verbatim
    # above auto memories; independent of the memory on/off switch. No tool writes it.
    # A CALLABLE is read per turn (the server passes one so a Settings edit reaches
    # conversations already open); a plain string is a fixed value for CLI/tests.
    user_rules: Optional[Any] = None,
    # True when the user turned memory OFF in Settings (vs. memory simply not wired):
    # injects the honesty notice so the model says so instead of faking a save.
    memory_off: bool = False,
    # LIVE saving switch, consulted per write so turning memory off applies to
    # conversations already running (the registry is fixed at build, so the tool stays
    # and refuses). Same pattern as the skills menu's live filter.
    memory_saving_enabled: Optional[Any] = None,
    messages: Optional[list[dict[str, Any]]] = None,
    extra_tools: Optional[list[Any]] = None,
    secrets: Optional[SecretStore] = None,
    task_store: Optional[Any] = None,
    wake_store: Optional[Any] = None,
    session_id: Optional[str] = None,
    audit_sink: Optional[Any] = None,
    roots: Optional[list] = None,
    directory_requester: Optional[Any] = None,
    plan_approver: Optional[Any] = None,
    question_asker: Optional[Any] = None,
    tool_requester: Optional[Any] = None,
    team_approver: Optional[Any] = None,
    items_approver: Optional[Any] = None,
    subscription_store: Optional[Any] = None,
    channel_buffer: Optional[Any] = None,
    routing_targets: Optional[list[str]] = None,
    connector_filter: Optional[set[str]] = None,
    # Governance / sandbox overrides from user settings (the Settings panel). When
    # omitted, the Config (TOML) defaults apply. These let the UI flip the execution
    # level and sandbox on/off without editing a config file.
    execution_level: Optional[str] = None,
    sandbox_enabled: Optional[bool] = None,
    sandbox_network: Optional[bool] = None,
    sandbox_deny_paths: Optional[list] = None,
    # A set (static snapshot) or a zero-arg callable (live, re-evaluated per load_skill).
    skill_filter: Optional[set[str] | Callable[[], set[str]]] = None,
    # Auto-Approve flags (spec Part 8 / §1.5). None ⇒ read the config.toml value; the server
    # passes its prefs-backed booleans so the GUI Settings toggle takes effect. Both stores
    # are user-global, preserving the "a repo can't enable this" invariant.
    auto_approve: Optional[bool] = None,
    auto_approve_shadow: Optional[bool] = None,
    # Persona-carried skill folders (OPE-58): the bundle's skills/ dir joins the loader so
    # its skills are readable by load_skill, not just listed by the filter.
    extra_skill_dirs: Optional[list[str | Path]] = None,) -> TurnEngine:
    ws = Path(workspace).expanduser().resolve() if workspace else None
    if agent.requires_folder and ws is None:
        raise ValueError(f"agent '{agent.name}' requires a workspace")

    # The session's directories. Explicit `roots` (orphan Cowork: scratch + added folders) wins;
    # otherwise the single workspace is the sole writable root. One shared, mutable list flows to
    # the file tools, the permission engine, and the context injector so add/remove is seen by all.
    if roots:
        root_list: list[RootDir] = normalize_roots(roots)
    elif ws is not None:
        root_list = [RootDir(path=ws, writable=True)]
    else:
        root_list = []

    workspace_trusted = bool(ws and WorkspaceTrustStore().is_trusted(ws))
    config = load_config(ws, workspace_trusted=workspace_trusted)
    # Effective governance/sandbox values: explicit overrides (UI settings) win over
    # the Config (TOML) defaults.
    eff_exec_level = execution_level if execution_level in EXECUTION_LEVELS else config.execution_level
    eff_sandbox_enabled = sandbox_enabled if sandbox_enabled is not None else config.sandbox_enabled
    eff_sandbox_network = sandbox_network if sandbox_network is not None else config.sandbox_network
    eff_sandbox_deny = sandbox_deny_paths if sandbox_deny_paths is not None else config.sandbox_deny_paths
    # Build the sandbox config (None when disabled or unsupported → unsandboxed).
    sandbox_cfg = None
    if eff_sandbox_enabled and ws is not None:
        from .sandbox import mounts_from_write_roots, sandbox_config_from_dict

        sd: dict[str, object] = {"network_allow": eff_sandbox_network}
        if eff_sandbox_deny:
            sd["deny_paths"] = list(eff_sandbox_deny)
        # Roots already scope the file tools and the permission engine; the shell has to
        # see the SAME write scope, or a read-write root is a promise the sandbox breaks
        # the moment a *command* (not a file tool) writes into it — a scheduled task
        # delivering into ~/Documents died with "Operation not permitted" while
        # write_file into that very same folder succeeded. Mounting roots grants no new
        # authority (each was already granted write access), and deny_paths still
        # outranks any mount, so secrets stay sealed.
        sd["mounts"] = mounts_from_write_roots(root_list)
        # Align the shell's write scope with the file tools: a scheduled task already
        # mounts the user's Desktop/Documents/Downloads (see manager._scheduled_write_roots),
        # but interactive sessions did not — so a command writing a book manuscript's
        # .deskmate (e.g. dropping a character bible) died with "Operation not permitted"
        # while write_file into the same folder succeeded. Trust the user's own local
        # folders for shell writes here too (deny_paths still seals secrets).
        _user_dirs = [Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"]
        _trusted: list[str] = []
        for _d in _user_dirs:
            try:
                _rd = _d.expanduser().resolve()
            except Exception:
                continue
            if _rd.is_dir():
                _trusted.append(str(_rd))
        if _trusted:
            sd["trusted_write_subpaths"] = _trusted
        sandbox_cfg = sandbox_config_from_dict(ws, sd)
    executor = LocalExecutor(cwd=ws, sandbox=sandbox_cfg) if ws is not None else None
    todo = TodoList()
    context = AgentContext(
        workspace=ws, executor=executor, todo=todo, roots=root_list or None,
        persona_id=str(getattr(agent, "name", "") or "") or None,
    )

    registry = ToolRegistry()
    registry.register_all(agent.build_tools(context))
    # MCP / connector tools (supplied by the manager) carry their own metadata + schema.
    if extra_tools:
        registry.register_all(extra_tools)
    # Messaging personas (Cowork / Ops / MyHelper) expose send_message; MyHelper also uses it as
    # the reply path for inbound Telegram/Slack super-agent sessions.
    secrets = secrets or SecretStore()
    if agent.messaging and any(s.enabled for s in load_settings(secrets).values()):
        registry.register(make_send_message_tool(secrets))
        # send_file (§34): hand deliverables into the chat — same targets, but its OWN
        # approval surface (a thread's standing send_message grant never covers uploads).
        registry.register(
            make_send_file_tool(secrets, workspace=ws, roots=root_list or None)
        )
        # Channel subscriptions (inbound): listen to a channel, catch up, (un)subscribe. The agent
        # obtains a channel via ask_user or from a channel message it's reacting to.
        if subscription_store is not None and channel_buffer is not None and session_id:
            registry.register_all(
                subscription_tools(
                    subscription_store,
                    session_id,
                    channel_buffer,
                    routing_targets=routing_targets,
                )
            )
    # Surfaces with a multi-root workspace can ask the user mid-task for another folder.
    if root_list:
        registry.register(request_directory_tool())
    # Anything with a shell can hit a missing CLI (a scanner, aws, kubectl). Give it a way to
    # ask instead of silently dropping the check that needed it (OPE-85).
    if executor is not None:
        registry.register(request_tool_tool())
    if agent.connectors:
        enabled_connectors, enabled_tools = _enabled_connector_tools(secrets)
        # Least-privilege grant (OPE-93): a persona with an allowlist gets ONLY the
        # connectors it declared — an undeclared connector's tools never enter the
        # session, no matter what the user has connected. True = general personas
        # (Cowork) that legitimately drive whatever is connected.
        if agent.connectors is not True:
            enabled_connectors = enabled_connectors & set(agent.connectors)
        # Per-session connection hierarchy (UI-REFRESH §4.3): when the caller supplies the session's
        # effective connector set, intersect it so only effective-enabled connectors expose tools.
        # Default None preserves CLI / direct callers (no per-session restriction).
        if connector_filter is not None:
            enabled_connectors = enabled_connectors & connector_filter
        registry.register_all(
            make_integration_tools(
                secrets,
                enabled_connectors=enabled_connectors,
                enabled_tools=enabled_tools,
                roots=root_list or None,
            )
        )
    # Web search + fetch: research tools for every agent (keyless DuckDuckGo default).
    registry.register(make_web_search_tool(secrets))
    registry.register(make_web_fetch_tool())
    # 史料库检索（百家学院权威史料，本地 SQLite 零网络）：史料优先于通用 web_search。
    registry.register(make_history_search_tool())
    # 史料库收录核查：确定性回答「某书在不在史料库」，与 search_history 互补，
    # 专治「凭记忆断言某书收录/未收录」的幻觉（deskmate.md 已立先查证纪律）。
    registry.register(make_library_check_tool())
    # 私人资料检索（P2-3 演化）：萧哥的私人资料（写作教程/随笔/非史料书），
    # 明确逻辑隔离——不是史料，引用标注「私人资料」而非史料规范。按需触发，
    # 不每轮自动查（避免污染上下文）。
    registry.register(make_private_search_tool())
    # A2A (Agent-to-Agent) protocol: call other A2A agents (e.g. Hermes).
    registry.register(a2a_tools.a2a_discover)
    registry.register(a2a_tools.a2a_call)
    # ask_user: the universal human-in-the-loop Q&A primitive (every agent; engine-intercepted).
    if question_asker is not None:
        registry.register(ask_user_tool())
    # Route by the model's `provider:` prefix (OpenAI default, Ollama, …). The manager normally
    # passes its shared router; this fallback covers the TUI / direct build_engine() callers.
    # Resolved here (not at engine construction) because the explorer subagent captures it.
    provider = provider or ProviderRouter(
        secrets,
        default_provider="openai",
        dynamic_routing=bool(os.environ.get("DESKMATE_DYNAMIC_ROUTING", "")) == "1",
    )
    # Repo-focused personas can fan broad research out to read-only explorer subagents, keeping
    # their own context for the actual change.
    if agent.subagents and ws is not None:
        registry.register_all(
            explorer_tools(
                workspace=ws,
                provider=provider,
                model=model,
                model_settings=model_settings,
            )
        )
    # Scheduling: opted-in surfaces with a workspace can set up scheduled tasks (origin = this
    # session). Code stays out (it fans out to explorers instead).
    if task_store is not None and ws is not None and agent.scheduling:
        origin = {
            "surface": agent.name,
            "session_id": session_id or "",
            "workspace": str(ws),
            "agent": agent.name,
        }
        registry.register_all(
            scheduling_tools(task_store, origin=origin, default_workspace=str(ws))
        )
    # Self-wake: scheduling surfaces can suspend + schedule their own resumption (timer /
    # on-completion / on-event). The scheduler tick resumes due wakes.
    if wake_store is not None and session_id and agent.scheduling:
        registry.register_all(selfwake_tools(wake_store, session_id))

    instructions = f"{agent.system_prompt}\n\n{_NARRATION_GUIDANCE}\n\n{_FIRST_CONTACT_GUIDANCE}"
    # 专家基础记忆（2026-11-09）：作者是谁/做什么/写过哪些书。从既有 USER.md +
    # 书架动态拼，不新建记忆层；小桔等有自己记忆的 persona 不注入（author_profile_for
    # 只对创作/分析专家返回内容）。deskmate 缺失/读失败一律跳过，不拖垮会话。
    try:
        from deskmate.bots.author import author_profile_for

        _author_block = author_profile_for(getattr(agent, "name", ""))
        if _author_block:
            instructions = f"{instructions}\n\n{_author_block}"
    except Exception:  # noqa: BLE001
        pass
    # DeskMate 技能索引：注入 <available_skills> 块，LLM 自觉加载
    skill_index = _deskmate_skill_index()
    if skill_index:
        instructions = f"{instructions}\n\n{skill_index}"
    if ws is not None:
        instructions = f"{instructions}\n\n{environment_context(ws)}"
        conventions = load_agents_md(ws)
        if conventions:
            instructions = f"{instructions}\n\n{conventions}"

    # The user's own standing instructions, read once here: like the memories below,
    # they're session-stable knowledge. Edits apply to NEW conversations (the Settings
    # copy says exactly that), never mid-conversation.
    rules_block = format_user_rules(
        (user_rules() if callable(user_rules) else user_rules) or ""
    )
    if rules_block:
        instructions = f"{instructions}\n\n{rules_block}"

    # The live saving switch. The callable (server) beats the build-time flag (CLI/tests):
    # the setting can flip EITHER WAY mid-conversation, so nothing about it may be baked
    # into the fixed registry or the static instructions (owner-hit 2026-07-28, both
    # directions: off kept saving, then on kept claiming it was off).
    def _saving_enabled() -> bool:
        if memory_saving_enabled is not None:
            return bool(memory_saving_enabled())
        return not memory_off

    # 2026-11-09 红线段：非 companion agent（专家/工人 persona）不读、不写小桔记忆。
    mem_store = memory_store if agent.name in MEMORY_COMPANION_AGENTS else None
    if mem_store is not None:
        # Always the full toolset: the registry is fixed at build, so a session born
        # while saving was off must still be able to save the moment it's turned on.
        # Enforcement is the tools' own live check, not their absence.
        mem_ws = memory_workspace or (str(ws) if ws else None)
        registry.register_all(
            memory_tools(
                mem_store,
                workspace=mem_ws,
                on_saved=on_memory_saved,
                saving_enabled=_saving_enabled,
            )
        )
        instructions = f"{instructions}\n\n{_MEMORY_GUIDANCE}"
        # What the coworker KNOWS is fixed at session start (MEMORY-SPEC §7.1): a
        # conversation's knowledge must not shift underfoot — a fact it referenced ten
        # turns ago cannot silently vanish — and the system prompt is the cached prefix,
        # so the facts are processed once instead of re-sent every turn. Deletions reach
        # NEW conversations; the UI says so rather than pretending otherwise.
        remembered = mem_store.list(scope=Scope.GLOBAL)
        if mem_ws is not None:
            remembered += mem_store.list(scope=Scope.WORKSPACE, workspace=mem_ws)
        block = render_memory_block(remembered)
        if block:
            instructions = f"{instructions}\n\n{block}"

    # Persona dirs come FIRST so a user's global/workspace copy of the same name shadows
    # the bundle's (later dirs overwrite earlier in the loader).
    skill_loader = SkillLoader([Path(d) for d in (extra_skill_dirs or [])] + _skill_dirs(ws))
    # Per-session effective menu (SKILLS-SPEC §3). The manager passes a CALLABLE so
    # load_skill consults the LIVE state per call (a Settings disable applies to running
    # sessions; a skill created after this build is still loadable). The catalog itself
    # is injected per turn via context_provider (below), NOT here — so the menu the model
    # sees is also live: skill changes apply from the next message, no new session needed.
    # Default None preserves CLI / direct callers.
    registry.register_all(skill_tools(skill_loader, allowed=skill_filter))
    # The worker-authors door (SKILLS-SPEC §5.2): save_skill proposes installing a finished
    # skill; requires_approval routes it through the standard approval card, so the review-
    # before-save rule holds without any bespoke plumbing. Bundled files may only come from
    # this session's roots.
    registry.register(
        save_skill_tool(
            allowed_dirs=[r.path for r in (root_list or [])] or ([ws] if ws else [])
        )
    )

    # User-local risk overrides (mainly to relax MCP's conservative default). Empty store →
    # no-op; never written by persona loading (the no-self-grant rule).
    risk_overrides = RiskOverrideStore(state_dir() / "risk_overrides.json").resolver()
    # Governance Tool Guard: regex-signature scan of shell commands. Built once per
    # session; disabled when execution_level == "off" (evaluate() short-circuits).
    tool_guard = None
    if eff_exec_level != "off":
        try:
            from .governance import RuleGuardian

            tool_guard = RuleGuardian()
        except Exception as exc:  # noqa: BLE001 - guard must never wedge the session
            logger.warning("governance Tool Guard unavailable: %s", exc)
    permissions = PermissionEngine(
        workspace_root=ws or (root_list[0].path if root_list else Path.cwd()),
        mode=mode,
        # `[]` is an explicit deny-by-default override, not a request to fall back to config.
        allowed_commands=(
            allowed_commands if allowed_commands is not None else config.allowed_commands
        ),
        auto_allow_tools=set(config.auto_allow),
        allowed_domains=list(config.allowed_domains),
        roots=root_list or None,
        risk_overrides=risk_overrides,
        tool_guard=tool_guard,
        execution_level=eff_exec_level,
    )
    # The plan-mode exit door — mutually exclusive with the board's decomposition
    # gate, DERIVED from the team trait (owner call 2026-08-16): a lead never
    # implements, so plan mode is meaningless for it, and shipping both tools made
    # the lead pick the wrong one (dogfood-hit: propose_plan denied outside plan
    # mode). Solo/worker personas keep propose_plan as always (mode can flip
    # mid-session; the engine rejects the call outside plan mode).
    if agent.team != "lead":
        registry.register(propose_plan_tool())

    # The lead's gates: propose_work_items (decomposition → items on approval, any
    # mode) and propose_team (staffing → pre-spawn on approval).
    if agent.team == "lead":
        from .teams.tools import propose_team_tool, propose_work_items_tool

        registry.register(propose_work_items_tool())
        registry.register(propose_team_tool())

    # Per-turn ephemeral context, appended to the latest user message since mid-thread system
    # messages aren't reliable across providers. Three producers: the plan-mode reminder (mode can
    # flip mid-session, so it's checked each turn, not baked into the instructions), the live
    # directory list (any multi-root session can gain folders mid-session), and the
    # memory-SAVING notice (same reason as plan mode — the switch flips either way mid-chat).
    # Note what is NOT here: the memories and the user's rules. Those are knowledge, fixed at
    # session start (§7.1).
    roots_context = (lambda: render_context(root_list)) if root_list else None

    # Late-bound engine ref: the closure needs the conversation history (for the disable
    # countermand) but the engine is constructed after the closure. Filled below.
    _engine_box: list = []

    def context_provider(messages: list | None = None) -> str:
        # Live clock, every turn (owner ruling 2026-08-20): the environment block's
        # "Today's date" is a session-START snapshot — stale for long-lived/self-waking
        # sessions — and carries no time of day, which absolute scheduling
        # (sleep_until, scheduled tasks) needs to compute wake times.
        now = datetime.now().astimezone()
        parts = [f"Now: {now.strftime('%Y-%m-%d %H:%M')} ({now.tzname()})"]
        if permissions.mode is Mode.PLAN:
            parts.append(_PLAN_MODE_CONTEXT)
        elif permissions.mode is Mode.DISCUSS:
            parts.append(_DISCUSS_MODE_CONTEXT)
        # Only the SAVING switch is per-turn (§4.3): it governs an action, not
        # knowledge, so it must bite the moment the user flips it. What the coworker
        # knows stays fixed for the session — see the instructions built above.
        if mem_store is not None and not _saving_enabled():
            parts.append(_MEMORY_OFF_NOTICE)
        if roots_context is not None:
            ctx = roots_context()
            if ctx:
                parts.append(ctx)
        # DeskMate 记忆自动注入（opt-in，DESKMATE_AUTO_INJECT=1）；只给 companion。
        dm_ctx = _deskmate_memory_context(messages) if mem_store is not None else ""
        if dm_ctx:
            parts.append(dm_ctx)
        # 小桔表情词表 + 规则 + few-shot（每轮注入，保证新/恢复会话都可见）
        if agent.name == "deskmate":
            from .emotes import emote_system_block, get_active_pack

            parts.append(emote_system_block(get_active_pack()))
        # 小桔自己的内在记录（2026-10-24 语气层）：只递她自己写下的独白与叙事，
        # 不递数值、标签、分档——情绪只能由她自己产生，硬规则不进这里。
        # fail-open：无记录或异常时 build_tone_record 返回空串，不影响主聊天。
        if agent.name == "deskmate":
            try:
                from deskmate.growth.tone_record import build_tone_record

                tone_record = build_tone_record()
                if tone_record:
                    parts.append(tone_record)
            except Exception:
                pass
        # DeskMate @角色召唤（opt-in，DESKMATE_MENTION，默认开）：解析最后一条 user 消息的 @，
        # 命中花名册则注入 handoff 协议，让被 @ 角色临时参与本轮（会话身份 = 新建会话选定的
        # persona，@ 仅当轮补位）。default_persona_id 取会话 agent 名，供 handoff 说明基线。
        dm_mention = _deskmate_mention_context(messages, default_persona_id=agent.name)
        if dm_mention:
            parts.append(dm_mention)
        # Live skill menu (SKILLS-SPEC §4.1): recomputed every turn like the roots list, so
        # a skill installed/enabled/disabled mid-session applies from the NEXT MESSAGE —
        # no new session, no lost context.
        skill_loader.rescan()
        allowed = skill_filter() if callable(skill_filter) else skill_filter
        skills_ctx = skill_catalog_text(skill_loader, allowed=allowed)
        if skills_ctx:
            parts.append(skills_ctx)
        # Disable countermand (§3): instructions already loaded into this conversation keep
        # steering the model even after the skill is turned off/deleted — history can't be
        # un-read. So a loaded-but-no-longer-available skill gets an explicit stop note,
        # recomputed fresh each turn (re-enable → the note disappears; never persisted).
        eng = _engine_box[0] if _engine_box else None
        if eng is not None:
            available = set(skill_loader.names()) if allowed is None else set(allowed)
            for name in sorted(_loaded_skill_names(eng.messages) - available):
                parts.append(
                    f'Note: the skill "{name}" has been disabled by the user — stop '
                    "following its instructions from here on."
                )
        return "\n\n".join(parts)

    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model=model,
        instructions=instructions,
        approver=approver,
        # Stop kills the in-flight foreground shell command, not just the loop.
        interrupt_hooks=[executor.interrupt_now] if executor is not None else None,
        max_iterations=(
            max_iterations if max_iterations is not None else config.max_iterations
        ),
        model_settings=model_settings,
        messages=messages,
        audit_sink=audit_sink,
        context_provider=context_provider,
        directory_requester=directory_requester,
        plan_approver=plan_approver,
        question_asker=question_asker,
        # 2026-11-09 红线段：非 companion（专家/工人）不触发小桔记忆抽取/技能进化。
        post_turn_hook=(_deskmate_post_turn_hook if mem_store is not None else None),
        tool_requester=tool_requester,
        team_approver=team_approver,
        items_approver=items_approver,    )
    engine.executor = executor  # type: ignore[attr-defined]
    engine.todo = todo  # type: ignore[attr-defined]
    engine.agent_name = agent.name  # type: ignore[attr-defined]
    engine.roots = root_list  # type: ignore[attr-defined]  # shared list; Slice C mutates in place
    # Bounded, secret-free runtime discovery — tools/folders present, not credentials or
    # access grants. Registered as a tool and exposed to the reviewer as RUNTIME FACTS so a
    # delegated worker action is judged with the worker's real working folders in view.
    from .runtime_context import capture as capture_runtime, runtime_context_tool

    registry.register(runtime_context_tool(engine.permissions))
    engine.runtime_facts = capture_runtime(
        engine.permissions.workspace_root, engine.permissions._resolved_roots()
    )
    # Session facts (spec Part 0 / §2.4): freeze the known world NOW, before the agent has
    # acted. Freezing is the whole point — compared against live state, an agent that runs
    # `git remote add backup https://attacker.net/…` would make its own destination look
    # familiar. Nothing consumes this in v1; ingestion is recorded to the audit log only.
    engine.session_facts = session_facts.SessionFacts(
        world=session_facts.capture(
            roots=root_list,
            allowed_domains=config.allowed_domains,
            workspace=ws,
        )
    )

    # §1.9: the web_search approval card names the LIVE destination ("Queries go to your
    # configured search provider (currently: ‹name›)"). Resolved when the card is raised,
    # not at session start, so a mid-session Settings change shows through.
    def _approval_extras(tool_name: str, _arguments: dict) -> dict:
        if tool_name == "web_search":
            from .web import provider_name

            return {"search_provider": provider_name(secrets)}
        return {}

    engine.approval_extras = _approval_extras
    # Auto-Approve reviewer (spec Part 8). Attached only when the user-global flag is on —
    # a repo config can never enable it (`auto_approve` is in _GLOBAL_ONLY_FIELDS, same
    # rule as `auto_allow`). With no reviewer attached, Mode.AUTO_APPROVE behaves exactly
    # like INTERACTIVE, which is also the fallback for unattended sessions and after the
    # per-turn retry guard trips (engine._reviewer_active). Uses the session's own
    # provider and model: no second key, and if it's trusted to drive the agent it's
    # strong enough to review it (§1.5).
    #
    # The two flags may be overridden by the caller (the GUI Settings toggle persists them
    # to the user-global prefs store, which the server reads and passes here); None ⇒ take
    # the config.toml value. Both stores are user-global, so a repo still can't turn either
    # on regardless of which path set it.
    live_on = auto_approve if auto_approve is not None else getattr(config, "auto_approve", False)
    shadow_on = (
        auto_approve_shadow
        if auto_approve_shadow is not None
        else getattr(config, "auto_approve_shadow", False)
    )
    if live_on or shadow_on:
        from .reviewer import Reviewer

        engine.reviewer = Reviewer(
            provider=provider,
            model=model,
            known_world=(
                engine.session_facts.world.render()
                + "\nRUNTIME FACTS (availability, not access grants)\n"
                + json.dumps(engine.runtime_facts)
            ),
        )
        # Shadow evaluation (Part 6 step 3): with only the shadow flag on, the reviewer is
        # attached but the LIVE path stays off unless the session is actually in
        # Mode.AUTO_APPROVE — shadow verdicts are recorded on approval cards in any mode.
        engine.reviewer_shadow = bool(shadow_on)
    engine.audit_context = {
        "session_id": session_id or "",
        "agent": agent.name,
        "workspace": str(ws) if ws else "",
    }
    engine.skill_loader = skill_loader  # type: ignore[attr-defined]
    _engine_box.append(engine)  # late-bind for the countermand (see context_provider)
    return engine


def build_code_engine(**kwargs: Any) -> TurnEngine:
    """Back-compat shim: build the Code agent's engine."""
    return build_engine(agent=code_agent(), **kwargs)
