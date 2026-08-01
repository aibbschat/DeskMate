"""Event model — the contract between the turn engine and any surface (TUI/GUI/IDE).

No token streaming in v1, so granularity is per-message/per-tool. Streaming later adds
`assistant_delta` / `tool_output_delta` without changing the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    TURN_START = "turn_start"
    ASSISTANT_DELTA = "assistant_delta"
    REASONING_DELTA = "reasoning_delta"  # model thinking text (display-only, never replayed)
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_PROPOSED = "tool_proposed"
    PERMISSION_REQUIRED = "permission_required"
    DIRECTORY_REQUESTED = "directory_requested"  # agent asks the user to grant a folder
    QUESTION_REQUESTED = (
        "question_requested"  # agent asks the user a free-text/multiple-choice question
    )
    PLAN_PROPOSED = (
        "plan_proposed"  # agent presents a plan for approval (plan mode exit)
    )
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    ITERATION_END = "iteration_end"
    TURN_END = "turn_end"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    # DeskMate overlay signals (carried over the same /ws/session channel, translated
    # by the GUI into window CustomEvents the DeskMateOverlay listens for).
    DESKMATE_SUGGESTION = "deskmate_suggestion"
    DESKMATE_MEMORY_UPDATE = "deskmate_memory_update"
    DESKMATE_MEMORY_LEARNED = "deskmate_memory_learned"
    COMPACTING = "compacting"  # compaction started — surfaces show a transient signal
    COMPACTED = "compacted"  # outbound history was compacted (summary or trim)


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
