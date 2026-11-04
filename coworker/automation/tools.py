"""Agent-facing scheduling tools (Cowork + MyHelper).

`create_scheduled_task` is gated (`requires_approval`) so it surfaces a confirm card before a
standing automation is created (approve-at-creation). The agent converts natural language
("7:10pm everyday") into a cron string itself. Tools are origin-bound: a created task records
the launching session and runs in its workspace, so the origin conversation can read the
results (the artifacts are real files in that folder).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import aisuite as ai

from .models import Schedule, ScheduledTask, grant_entries
from .store import TaskStore

# Baseline standing grants a scheduled (autonomous) task receives when the creating
# agent proposes no `permissions` of its own. Without these, an auto task that reads
# files or shells out (the deskmate's normal behavior) would block on a per-run approval
# card that nobody can click in `Mode.INTERACTIVE`, hanging forever. These are low-risk
# file/workspace-scoped tools on the user's own machine, surfaced for approval at creation
# time via the consent card — so the human still signs off before the task goes live.
DEFAULT_SCHEDULED_TOOLS = [
    "run_shell",
    "read_file",
    "list_directory",
    "write_file",
]

_CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_scheduled_task",
        "description": (
            "Create a scheduled automation that re-runs `instructions` on a schedule. Convert "
            "the user's natural-language timing into a cron expression yourself (e.g. "
            "'every day at 7:10pm' → '10 19 * * *'), or pass a one-time `fire_at` ISO datetime. "
            "The user confirms before it is created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short label, e.g. 'Daily news briefing'.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "What to do on each run, written as a direct command to execute "
                        "immediately (e.g. 'Prepare a market analysis report covering …'). Do "
                        "NOT restate the schedule or timing here — timing belongs in cron/"
                        "fire_at; this text is handed verbatim to the agent every run."
                    ),
                },
                "cron": {
                    "type": "string",
                    "description": "5-field cron, e.g. '10 19 * * *'. Omit for one-time.",
                },
                "fire_at": {
                    "type": "string",
                    "description": "ISO datetime for a one-time run. Omit for recurring.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA tz, e.g. 'America/New_York'. Defaults to the machine's local time — pass it only to override.",
                },
                "goal_mode": {
                    "type": "string",
                    "enum": ["", "watch"],
                    "description": (
                        "Omit for an ordinary scheduled run. Pass 'watch' when the user "
                        "wants a standing goal instead of a per-run result (e.g. 'keep an "
                        "eye on this page and tell me only when it changes'). Each quiet "
                        "check stays silent; the first real change is delivered once and "
                        "closes the goal."
                    ),
                },
                "permissions": {
                    "type": "array",
                    "description": (
                        "What this automation will touch, surfaced on the creation consent "
                        "card. List every external read and write the instructions imply. "
                        "Reads (access:'read') are disclosure only. Writes (access:'write') "
                        "become standing grants IF the user approves: the automation may then "
                        "call that exact tool against that exact target without asking each "
                        "run. Targets must be exact (a channel address like 'slack:T…/C…', a "
                        "recipient) — no wildcards. Omit writes whose target you don't know "
                        "yet; the run will ask instead."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "description": "Exact tool name, e.g. 'send_message'.",
                            },
                            "target": {
                                "type": "string",
                                "description": "The exact target argument value the rule binds to.",
                            },
                            "access": {
                                "type": "string",
                                "enum": ["read", "write"],
                                "description": "'write' proposes a standing grant; 'read' is disclosure.",
                            },
                        },
                        "required": ["tool", "target", "access"],
                    },
                },
            },
            "required": ["title", "instructions"],
        },
    },
}

_UPDATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_scheduled_task",
        "description": "Enable/disable or edit a scheduled task (its instructions, cron, title, goal mode, or restart a concluded goal).",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "instructions": {"type": "string"},
                "cron": {"type": "string"},
                "title": {"type": "string"},
                "goal_mode": {
                    "type": "string",
                    "description": "Opt into a goal-based watch: keep checking on each scheduled run and report only when the target changes. Empty string = ordinary one-shot run.",
                },
                "restart_goal": {
                    "type": "boolean",
                    "description": "Re-open a concluded goal as a fresh generation, without rewriting any past run. Use this when the user asks to watch the same target again after it has already reported.",
                },
            },
            "required": ["id"],
        },
    },
}

_ID_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_scheduled_task",
        "description": "Delete a scheduled task and its run history.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
}

_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_scheduled_tasks",
        "description": "List the user's scheduled tasks (title, schedule, next run, status).",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _gated(func: Callable, schema: dict, *, approval: bool) -> Callable:
    func.__name__ = schema["function"]["name"]
    func.__doc__ = schema["function"]["description"]
    func.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name=schema["function"]["name"],
        category="automation",
        risk_level="medium" if approval else "low",
        capabilities=["scheduling"],
        requires_approval=approval,
    )
    func.__coworker_schema__ = schema
    return func


def scheduling_tools(
    store: TaskStore,
    *,
    origin: dict[str, Any],
    default_workspace: str,
) -> list[Callable[..., Any]]:
    def create_scheduled_task(
        title, instructions, cron=None, fire_at=None, timezone="local",
        permissions=None, goal_mode="",
    ):
        from croniter import croniter

        if not cron and not fire_at:
            return {
                "error": "provide a cron (recurring) or a fire_at ISO datetime (one-time)"
            }
        if cron and not croniter.is_valid(cron):
            return {"error": f"invalid cron expression: {cron}"}
        schedule = Schedule(
            kind="once" if (fire_at and not cron) else "cron",
            cron=cron,
            fire_at=fire_at,
            timezone=timezone or "local",
        )
        workspace = origin.get("workspace") or default_workspace
        # The agent PROPOSES permissions; the human granted them by approving this gated
        # call (the consent card rendered the proposal). Only validated write grants stick:
        # tool must declare a target argument (never exec/destructive), target non-empty.
        grants = grant_entries(permissions)
        # Systemic fix: an autonomous task with zero standing grants stalls on the first
        # run_shell/read it attempts (approval card nobody can click). Fall back to a safe
        # baseline so scheduled tasks stay runnable unattended.
        if not grants:
            grants = list(DEFAULT_SCHEDULED_TOOLS)
        task = ScheduledTask(
            title=title,
            instructions=instructions,
            schedule=schedule,
            workspace=workspace,
            origin_surface=origin.get("surface", "cowork"),
            origin_session_id=origin.get("session_id", ""),
            agent=origin.get("agent", "cowork"),
            always_allowed_tools=grants,
            goal_mode=str(goal_mode or ""),
        )
        store.save(task)
        return {
            "ok": True,
            "id": task.id,
            "title": title,
            "schedule": schedule.human(),
            "next_run": task.next_run,
            "workspace": workspace,
            "always_allowed": grants,
        }

    def list_scheduled_tasks():
        return {"tasks": [t.public() for t in store.list()]}

    def update_scheduled_task(
        id, enabled=None, instructions=None, cron=None, title=None,
        goal_mode=None, restart_goal=False,
    ):
        from croniter import croniter

        task = store.get(id)
        if task is None:
            return {"error": f"no such task: {id}"}
        if cron is not None:
            if not croniter.is_valid(cron):
                return {"error": f"invalid cron expression: {cron}"}
            task.schedule.cron = cron
            task.schedule.kind = "cron"
        if enabled is not None:
            task.enabled = bool(enabled)
        if instructions is not None:
            task.instructions = instructions
        if title is not None:
            task.title = title
        if goal_mode is not None:
            # Declared by the user's instruction, never inferred from a category.
            task.goal_mode = str(goal_mode or "")
        if restart_goal:
            # §C1: bump the generation instead of clearing old runs' `done`.
            # Kept in lockstep with manager.update_automation, but self-contained
            # here so the scheduling tool never imports the deskmate package
            # (that package is optional on the cloud replica).
            task.goal_generation = int(getattr(task, "goal_generation", 0) or 0) + 1
            if goal_mode is None and not str(
                getattr(task, "goal_mode", "") or ""
            ).strip():
                task.goal_mode = "watch"
        store.save(task)
        return {"ok": True, "task": task.public()}

    def delete_scheduled_task(id):
        return {"ok": store.delete(id), "id": id}

    return [
        _gated(create_scheduled_task, _CREATE_SCHEMA, approval=True),
        _gated(list_scheduled_tasks, _LIST_SCHEMA, approval=False),
        _gated(update_scheduled_task, _UPDATE_SCHEMA, approval=True),
        _gated(delete_scheduled_task, _ID_SCHEMA, approval=True),
    ]
