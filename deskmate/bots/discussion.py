"""一轮群内讨论的**纯策略**（无 LLM、无 I/O、无表单）。

数值照 Hermes Bot Mode 本地源码 ``gateway/hosted_room_discussion.py:28-33`` 实测：
    MAX_DISCUSSION_MEMBERS = 6 / MIN = 2 / MAX_ROUNDS = 3 / MAX_MESSAGES = 10
以及 ``resolve_mentions`` 的语义：用户消息不 @ 任何人 = 全员，@ 谁谁答，
bot 可以 pass。Hermes 用 ``bounded`` 状态收口，这里同构用 ``bound_reason``。

为什么必须有硬上限：今天的三个主编团队本小时 68 次唤醒、10-31 那次 6262 条
审计事件，全部来自「自治调度层不停 tick」。bot 群只在用户发一句之后跑，且跑完
最多 3 轮 / 10 条就停，成本天然有上界。

**绝不引入** topic / manuscript_path / chapter_range / direction / criteria /
peers / max_rounds 这类表单入参 —— 那是旧圆桌的形态，不是 bot 群。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

MIN_MEMBERS = 2
MAX_MEMBERS = 6
MAX_ROUNDS = 3
MAX_MESSAGES = 10

_RESERVED_HANDLES = frozenset({"all", "everyone"})
# \w 在 Python 默认含 Unicode 字母，中文 handle 也能命中；与 ChatStore.post 的
# @ 解析口径一致（coworker/teams/chat.py:124）。
_MENTION_RE = re.compile(r"@([\w.-]+)", re.UNICODE)


@dataclass(frozen=True)
class DiscussionLimits:
    """一次用户发言所触发的讨论的上界；与回放校验分离。"""

    max_members: int = MAX_MEMBERS
    min_members: int = MIN_MEMBERS
    max_rounds: int = MAX_ROUNDS
    max_messages: int = MAX_MESSAGES

    def __post_init__(self) -> None:
        if self.min_members < 1 or self.max_members < self.min_members:
            raise ValueError("invalid member bounds")
        if self.max_rounds < 1 or self.max_messages < 1:
            raise ValueError("invalid discussion bounds")


def resolve_mentions(
    text: str, handles: Sequence[str], *, default_all: bool = True
) -> tuple[str, ...]:
    """这条自然语言该唤醒哪些 bot。

    - 一个 @ 都没有（且是用户发言）→ 全员；
    - ``@all`` / ``@everyone`` → 全员；
    - ``@historian`` → 只唤醒 historian；
    - @ 了不存在的 handle → 空（不误唤醒，也不报错）。
    """
    ordered = tuple(dict.fromkeys(handles or ()))
    found = {m.group(1).casefold() for m in _MENTION_RE.finditer(text or "")}
    if not found and default_all:
        return ordered
    if found & _RESERVED_HANDLES:
        return ordered
    return tuple(h for h in ordered if h.casefold() in found)


def bound_reason(
    round_index: int, message_count: int, limits: DiscussionLimits | None = None
) -> str | None:
    """达到上界时返回原因（``max_messages`` / ``max_rounds``），否则 None。

    消息数优先判定：先看总量爆炸，再看轮次，避免最后一轮里继续灌消息。
    """
    lim = limits or DiscussionLimits()
    if message_count >= lim.max_messages:
        return "max_messages"
    if round_index >= lim.max_rounds:
        return "max_rounds"
    return None
