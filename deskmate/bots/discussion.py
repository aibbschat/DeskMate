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
from typing import Mapping, Sequence

MIN_MEMBERS = 1
MAX_MEMBERS = 6
MAX_ROUNDS = 3
MAX_MESSAGES = 10
# 单成员下限说明（2026-11-08 调整）：原值 2 抄自 Hermes 同款硬边界；萧哥
# 实证「独立专家一对一 > 群聊」后，单人群 = 一对一作业房间是正当形态
# （「单独找脑洞 bot 深聊」）。上限 6、3 轮、10 条不动。

_RESERVED_HANDLES = frozenset({"all", "everyone"})
# \w 在 Python 默认含 Unicode 字母，中文 handle 也能命中；与 ChatStore.post 的
# @ 解析口径一致（coworker/teams/chat.py:124）。
_MENTION_RE = re.compile(r"@([\w.-]+)", re.UNICODE)

# 显式弃权：短句 + 弃权标记才判 pass，避免把「我不接受这个观点」这类
# 实质发言误杀。群聊里 pass 不落库，也不参与后续轮。
_PASS_MARKERS = (
    "pass", "不发言", "不接话", "不补充", "没有补充", "无需补充",
    "我没补充", "跳过", "弃权", "略过", "不说了",
)
_PASS_STRIP = re.compile(r"[\s，。！？、；：,.!?;:…·\-—()（）\[\]【】]")

# 深挖触发（2026-11-08 萧哥拍板「给群聊加深挖层」）：用户显式要系统级方案时，
# 被唤醒的 bot 单独放开预算（读稿 8 万字/次 + 迭代上限提高 + 材料给全书），
# 其余护栏照旧。词表必须**长标记**，防误触发：
# - 「盘」单字不行（地盘／盘问／棋盘）；「盘一下」「盘个」「盘全」可以
# - 「方案」单用不行；「出个完整方案」「完整方案」可以
# - 11-08 晚修正：萧哥说「先读原稿」「我需要脑洞大开的安排和设计」，那是
#   **开脑洞**，不是要系统级方案。把这两句留在深挖表里，会把开脑洞直接推成
#   「盘局势 → 支点 → 分层方案」的收敛档（16:57 专家那轮就是这么从脑洞变
#   成方案的）。开脑洞现在由人设默认档 + guidance 接住；深挖表只留**明确
#   要方案**的词。
# 触发只对**用户发言**判定；续轮（continue_stream 传空 text）天然不触发。
_DEEP_DIG_MARKERS = (
    "深挖", "盘一下", "盘个", "盘全", "盘清楚", "盘明白", "盘一遍",
    "完整方案", "出个方案", "系统方案", "系统性", "一套完整", "作战方案",
    "通读", "读全书", "先读全书",
)


def is_deep_dig(text: str) -> bool:
    """这句用户发言是在要深挖（系统级方案 / 往下盘）吗？

    只有显式标记才触发。默认态是**开脑洞/普通群聊**：材料按「开头 + 停笔点」
    常备、12 迭代、3 轮 10 条护栏照旧；深挖命中只给第 1 轮加强作业指令
    （盘局势 → 支点 → 方案），不是开权限。开脑洞要「给几条真不同的」，不能
    被这套收敛作业法带偏，所以「脑洞大开」这类话不在此表。
    """
    s = str(text or "")
    return any(m in s for m in _DEEP_DIG_MARKERS)


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
    text: str,
    handles: Sequence[str],
    *,
    default_all: bool = True,
    aliases: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """这条自然语言该唤醒哪些 bot。

    - 一个 @ 都没有（且是用户发言）→ 全员；
    - ``@all`` / ``@everyone`` → 全员；
    - ``@historian`` → 只唤醒 historian；
    - ``@史料考据`` → 经 ``aliases`` 映射到 historian 后只唤醒它（UI 显示的是中文名，
      必须支持中文 @，否则用户点了名却静默无人应答）；
    - @ 了不存在的 handle → 空（不误唤醒，也不报错）。
    """
    ordered = tuple(dict.fromkeys(handles or ()))
    found = {m.group(1).casefold() for m in _MENTION_RE.finditer(text or "")}
    if not found and default_all:
        return ordered
    if found & _RESERVED_HANDLES:
        return ordered
    alias_lookup = {str(k).casefold(): str(v) for k, v in (aliases or {}).items()}
    wanted = set(found)
    for token in found:
        mapped = alias_lookup.get(token)
        if mapped:
            wanted.add(mapped.casefold())
    return tuple(h for h in ordered if h.casefold() in wanted)


def is_pass(text: str) -> bool:
    """bot 显式弃权吗？pass 不落库、不生成占位消息、不参与后续轮。

    只认短句里的弃权标记，长回复里出现这些词不算（那是在讨论，不是弃权）。
    """
    s = _PASS_STRIP.sub("", str(text or "")).casefold()
    if not s or len(s) > 24:
        return False
    return any(marker in s for marker in _PASS_MARKERS)


def bound_reason(
    round_index: int, message_count: int, limits: DiscussionLimits | None = None
) -> str | None:
    """达到上界时返回原因（``max_messages`` / ``max_rounds``），否则 None。

    ``round_index`` 传**已完成的轮数**（第 1 轮开始传 0，第 3 轮开始传 2），
    这样 ``max_rounds=3`` 正好允许 3 轮发言，第 4 轮才收口。

    消息数优先判定：先看总量爆炸，再看轮次，避免最后一轮里继续灌消息。
    """
    lim = limits or DiscussionLimits()
    if message_count >= lim.max_messages:
        return "max_messages"
    if round_index >= lim.max_rounds:
        return "max_rounds"
    return None
