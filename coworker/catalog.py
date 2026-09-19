"""Vetted tool catalog — the stable ``id → capability`` layer a persona references.

A *capability* bundles a group of tools (the existing ``tools/`` factories) behind a stable
id, plus what session context it needs (``requires``) and the risk classes it can produce
(``risk``, used by the Phase 2 install-consent screen). ``expand(ids, context)`` turns a
persona's ``tools:`` list into concrete callables, skipping capabilities whose context
prerequisites aren't met (e.g. no shell without an executor) — matching the per-agent
factories that used to assemble tools by hand.

The catalog is **platform-owned and closed**: third parties get breadth from us adding
vetted capabilities here and from MCP, never by adding entries. MCP tools are *not* in the
catalog (see ``PERMISSIONS-AND-INBOX.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import aisuite as ai

from .agents.base import AgentContext
from .risk import RiskClass
from .tools.files import file_tools
from .tools.git import git_tools
from .tools.search import search_tools
from .tools.shell import shell_tools
from .tools.todo import todo_tools

# Editor expert tools (deskmate package, sibling to coworker).
# fail-open：缺 deskmate 时 review 能力优雅降级为空工具集，不拖垮整个 catalog 模块导入。
try:
    from deskmate.tools.review import (
        review_chapter,
        generate_report,
        load_manuscript_file,
    )
except Exception:  # pragma: no cover - deskmate 缺失属于部署形态问题
    review_chapter = generate_report = load_manuscript_file = None

# Context prerequisites a capability may require, mapped to a predicate over AgentContext.
_REQUIREMENTS: dict[str, Callable[[AgentContext], bool]] = {
    "workspace": lambda c: c.workspace is not None,
    "executor": lambda c: c.executor is not None,
    "todo": lambda c: c.todo is not None,
}


@dataclass(frozen=True)
class Capability:
    id: str
    name: str  # human label (consent screen)
    description: str
    build: Callable[[AgentContext], list]
    requires: tuple[str, ...] = ()
    risk: tuple[RiskClass, ...] = (RiskClass.READ,)

    def available(self, context: AgentContext) -> bool:
        return all(_REQUIREMENTS[r](context) for r in self.requires)


# -- capability builders --------------------------------------------------------
# These reproduce, exactly, what the Code and Cowork agent factories assembled by hand.


def _code_files(context: AgentContext) -> list:
    """Repo-oriented files: line-numbered/windowed `read_file`. Our `grep` and windowed
    `read_file` replace aisuite's slower `search_files` / `read_file`/`read_file_lines`.
    Multi-root aware (universal scratch): with session roots, writes/reads reach the
    scratch and granted dirs too; the workspace stays the relative-path anchor.
    """
    ws = str(context.workspace)
    replaced = {"search_files", "read_file", "read_file_lines"}
    file_kwargs = (
        {"roots": context.roots} if context.roots else {"root": ws, "allow_write": True}
    )
    files = [
        t
        for t in ai.toolkits.files(**file_kwargs)
        if getattr(t, "__name__", "") not in replaced
    ]
    return [*files, *file_tools(ws, roots=context.roots)]


def _files(context: AgentContext) -> list:
    """Knowledge-work files: multi-root aware (reads/writes across the session's roots).
    One reader everywhere (owner ruling 2026-08-20): the windowed, line-numbered
    `read_file` replaces aisuite's `read_file`/`read_file_lines`, and our `grep`
    replaces the slow `search_files` — same set Code uses.
    """
    ws = str(context.workspace)
    file_kwargs = (
        {"roots": context.roots} if context.roots else {"root": ws, "allow_write": True}
    )
    replaced = {"search_files", "read_file", "read_file_lines"}
    files = [
        t
        for t in ai.toolkits.files(**file_kwargs)
        if getattr(t, "__name__", "") not in replaced
    ]
    return [*files, *file_tools(ws, roots=context.roots)]


def _git(context: AgentContext) -> list:
    ws = str(context.workspace)
    return [*ai.toolkits.git(root=ws), *git_tools(ws)]  # git_status, git_diff, git_log


def _search(context: AgentContext) -> list:
    return search_tools(str(context.workspace))  # grep + search_text (ripgrep, .gitignore-aware)


def _shell(context: AgentContext) -> list:
    return shell_tools(context.executor)  # run_shell + background task tools


def _todo(context: AgentContext) -> list:
    return todo_tools(context.todo)  # todo_write (drives the Progress panel)


_CAPS: list[Capability] = [
    Capability(
        id="code_files",
        name="Code files",
        description="Read & edit files in a single repo workspace (line-numbered reads).",
        build=_code_files,
        requires=("workspace",),
        risk=(RiskClass.READ, RiskClass.WRITE_LOCAL),
    ),
    Capability(
        id="files",
        name="Files",
        description="Read & edit files across the session's workspace folders.",
        build=_files,
        requires=("workspace",),
        risk=(RiskClass.READ, RiskClass.WRITE_LOCAL),
    ),
    Capability(
        id="git",
        name="Git",
        description="Inspect git state and history (status, diff, log).",
        build=_git,
        requires=("workspace",),
        risk=(RiskClass.READ,),
    ),
    Capability(
        id="search",
        name="Search",
        description="Fast code/content search (grep).",
        build=_search,
        requires=("workspace",),
        risk=(RiskClass.READ,),
    ),
    Capability(
        id="shell",
        name="Shell",
        description="Run shell commands in a persistent session.",
        build=_shell,
        requires=("executor",),
        risk=(RiskClass.EXEC,),
    ),
    Capability(
        id="todo",
        name="Task list",
        description="Maintain a visible task/progress list.",
        build=_todo,
        requires=("todo",),
        risk=(RiskClass.READ,),
    ),
    Capability(
        id="review",
        name="Manuscript review",
        description="Deep chapter-by-chapter literary review + overall report generation.",
        build=lambda _ctx: [
            t
            for t in (review_chapter, generate_report, load_manuscript_file)
            if t is not None
        ],
        requires=(),
        risk=(RiskClass.READ,),
    ),
]

CATALOG: dict[str, Capability] = {c.id: c for c in _CAPS}


def capability(cap_id: str) -> Capability:
    cap = CATALOG.get(cap_id)
    if cap is None:
        raise KeyError(f"Unknown capability id: {cap_id!r}")
    return cap


# A persona may reference an *external* MCP tool directly via ``mcp:<server>:<tool>``.
# These ids are NOT vetted catalog capabilities — they resolve at engine-build time to a
# live MCP callable (see `SessionManager.prepare_mcp_tools`), so `expand` leaves them
# untouched and `_validate_tools` whitelists them. This is the seam that lets a persona
# "bring its own code" (or rather, borrow a third-party MCP server's tool) without us
# having to hardcode the capability in this closed catalog.
MCP_REF_PREFIX = "mcp:"


def is_mcp_ref(tool_id: str) -> bool:
    """True if ``tool_id`` is an MCP tool reference (``mcp:<server>:<tool>``)."""
    return tool_id.startswith(MCP_REF_PREFIX)


def parse_mcp_ref(tool_id: str) -> tuple[str, str]:
    """Split ``mcp:<server>:<tool>`` → (server, tool). Raises ValueError if malformed."""
    if not is_mcp_ref(tool_id):
        raise ValueError(f"not an mcp ref: {tool_id!r}")
    rest = tool_id[len(MCP_REF_PREFIX):]
    if ":" not in rest:
        raise ValueError(
            f"mcp ref must be `mcp:<server>:<tool>`, got {tool_id!r}"
        )
    server, _, tool = rest.partition(":")
    if not server or not tool:
        raise ValueError(
            f"mcp ref must be `mcp:<server>:<tool>`, got {tool_id!r}"
        )
    return server, tool


def expand(ids: list[str], context: AgentContext) -> list:
    """Expand a persona's ``tools:`` id list into concrete tool callables for this context.
    Capabilities whose context prerequisites aren't met are skipped (no shell without an
    executor, no files without a workspace) — exactly like the old hand-written factories.

    ``mcp:<server>:<tool>`` references are NOT expanded here — they are resolved to live
    MCP callables by the session manager (which owns the MCP connections), not by this
    closed, offline catalog.
    """
    tools: list = []
    for cap_id in ids:
        if is_mcp_ref(cap_id):
            continue  # resolved downstream (prepare_mcp_tools)
        cap = capability(cap_id)
        if cap.available(context):
            tools.extend(cap.build(context))
    return tools


def risk_summary(ids: list[str]) -> set[RiskClass]:
    """The union of risk classes a tool list can produce — for the install-consent screen.

    ``mcp:<server>:<tool>`` references are external tools; their risk is governed by the
    server's ``requires_approval`` gate at connect time, not by this catalog, so they
    contribute no catalog risk class here (and must not raise on the catalog lookup).
    """
    out: set[RiskClass] = set()
    for cap_id in ids:
        if is_mcp_ref(cap_id):
            continue
        out.update(capability(cap_id).risk)
    return out
