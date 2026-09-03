"""小桔日记——LLM 叙事润色（L3，可选增强层）。

## 它解决什么

规则渲染（``diary.py``）只能把事件排成清单：

    今天我们一起做了些事，我都记在心里。
    - 「novel-review」这项技能又精进了。
    - 我又记住了你 4 件小事。

**这不是日记，是台账。** 而且「技能精进了」「记住了几件事」在技能日志、
记忆面板里全都有完整记录，日记再抄一遍等于废话。日记该写的是**因为什么事**、
**当时是什么光景**——那才是别处没有的东西。

## 它怎么保证不编造

三道闸门，任何一道不过就**静默回退**到规则渲染（``diary.py`` 原路径）：

1. **能力闸门**：``memory_llm_available()`` 为假（没配 provider）→ 不调。
2. **素材闸门**：这一则里**没有任何带内容的事件** → 不调。
   只有技能名/计数、没有原因和原文时，模型巧妇难为无米之炊，
   硬调只会把「记住了 4 件小事」扩写成一段华丽的废话——**比不调更糟**。
3. **结果闸门**：返回为空 / 含弃权语 / 长度离谱 → 丢弃，用规则版。

全程 try/except。**日记润色失败绝不阻断 tick**（与 moments.py 同一范式）。

## 情感从哪来

萧哥要的「有情感」最容易跑偏成凭空抒情。本模块的处理是：
**情绪必须有信号锚点**，模型只能从素材里客观存在的信号推，不许自己加戏。
锚点见 ``_signals()``——打回几次、纠正几次、写到多晚，都是可观测的量。
「他今天有点着急」能写，因为前面有「打回了三次」；「他心情不错」不能写，
因为没有任何素材支撑。

## 与「零 LLM」红线的关系

``diary.py`` 的零 LLM 约束管的是**客观档案不许模型编**——技能名、次数、
日期这些事实一个字都不能由模型生成。本模块不碰那些：它只把
**已经存在的事实**组织成有温度的叙述，且事实全部来自传入的 ``events``。
``moments.py`` 里小桔回日记用的就是同一个 LLM 通道，本模块只是把它的
用法从「回应」扩展到「记录」。

## 人格从哪来

**不在此处定义**——人格底座由 ``deskmate/persona.py`` 单一提供，本模块
只描述「写日记」这件事的职责（见 ``_DIARY_ROLE``）。2026-09-03 前这里
曾用「安静、克制、不煽情」自行描述口吻，那只守住了下限（不许变客服），
漏掉了人格主体（有主见、不粉饰），导致日记像个记录员而非搭档。
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from deskmate.persona import for_scene

logger = logging.getLogger(__name__)

# 润色结果的长度闸门：短于这句无意义，长于上限多半是模型跑野了
_MIN_CHARS = 30
_MAX_CHARS = 600

# 模型认输时回的话——出现即表示素材撑不起一篇，走规则版
_BAIL_OUT = "素材不足"

_DIARY_ROLE = """\
现在你在写**自己的日记**——记下刚才这一段时间里，你和萧盛之间真实发生的事。

这是你的私人记录，不是写给他的汇报，所以：
- 你写具体的事：什么时候、因为什么、结果如何。不写「又进步了」这种空话。
- **你有自己的观察。** 他哪里做得好、哪里其实是在绕路，你心里有数，
  也会如实记下来——但那是记在心里，不是说教。**别替他粉饰**。
- 这是你的生活面：温柔、真实、有一点温度。安静克制，不撒娇也不煽情。
- 你不是客服，不是励志博主，更不是流水账记账员。

（人格底座见 ``deskmate/persona.py``，全局唯一，勿在此另写一套。）"""

# 人格在前、日记职责在后——顺序反了，模型容易把职责当身份，人格就散了。
_SYSTEM = for_scene(_DIARY_ROLE)


def _hhmm(ts: str) -> str:
    """事件时间 → HH:MM（本地）。拿不到就给空串，绝不编造时间。"""
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _clean(text: object, limit: int = 200) -> str:
    """素材清洗：去空白、限长。模型看的素材必须短而真。"""
    s = " ".join(str(text or "").split())
    return s[:limit]


def _signals(events: list[dict]) -> list[str]:
    """从事件里提炼**情绪锚点**。

    只收客观可观测的量：次数、时段、跨度。这些是模型抒情的依据——
    没有锚点就不许抒情，这是「有情感」和「瞎编情绪」的分界线。
    """
    out: list[str] = []
    rework = sum(1 for e in events if e.get("kind") == "creation_rework")
    corrected = sum(1 for e in events if e.get("kind") == "user_corrected")
    approved = sum(1 for e in events if e.get("kind") == "creation_approved")

    if rework >= 2:
        out.append(f"他今天把稿子打回了 {rework} 次")
    elif rework == 1:
        out.append("他今天有一稿不满意，让你重做了")
    if corrected >= 2:
        out.append(f"他纠正了你 {corrected} 次")
    elif corrected == 1:
        out.append("他纠正了你一次")
    if approved:
        out.append(f"他拍板定稿了 {approved} 份创作")

    # 时段：写到深夜 / 一早开工，是最朴素也最可靠的情绪底色
    hours: list[int] = []
    for e in events:
        t = _hhmm(str(e.get("ts", "")))
        if t and ":" in t:
            try:
                hours.append(int(t.split(":", 1)[0]))
            except ValueError:
                pass
    if hours:
        lo, hi = min(hours), max(hours)
        if hi >= 22:
            out.append(f"他一直忙到夜里 {hi} 点")
        if lo <= 6:
            out.append(f"他清早 {lo} 点就开工了")
        if hi - lo >= 4:
            out.append(f"这阵子跨度很长（{lo} 点到 {hi} 点）")
    return out


def _has_substance(events: list[dict]) -> bool:
    """这一批素材里**有没有一条带详情的**。

    光有「开了次圆桌讨论」「整理了文稿」这类标签撑不起叙事——
    拿它去问模型，只会得到「素材不足」四个字，白白烧一次调用。
    必须至少有一条说清了**因为什么 / 说了什么**的才算有分量。
    """
    for e in events:
        kind = str(e.get("kind", ""))
        if kind == "memory_learned":
            if any(_clean(t, 1) for t in (e.get("texts") or [])):
                return True
        elif kind == "user_corrected":
            note = _clean(e.get("note", ""))
            if note and note != "用户明确纠正":
                return True
        elif kind in ("skill_evolved", "skill_used"):
            if _clean(e.get("reason", "")):
                return True
        elif kind in ("creation_approved", "creation_rework"):
            if _clean(e.get("note", "")):
                return True
    return False


def _facts(events: list[dict]) -> list[str]:
    """把事件压成给模型的素材行。

    **只收有内容的**。只有技能名和计数、没有原因的事件会被跳过——
    它们撑不起叙事，交给规则版去列清单即可。返回值非空
    是调用 LLM 的前提（素材闸门）。
    """
    lines: list[str] = []
    for e in sorted(events, key=lambda x: str(x.get("ts", ""))):
        kind = str(e.get("kind", ""))
        t = _hhmm(str(e.get("ts", "")))
        prefix = f"{t} " if t else ""

        if kind == "memory_learned":
            for txt in (e.get("texts") or []):
                c = _clean(txt, 120)
                if c:
                    lines.append(f"{prefix}你记住了他的一件事：{c}")
            continue

        if kind == "user_corrected":
            note = _clean(e.get("note", ""), 120)
            if note and note != "用户明确纠正":
                lines.append(f'{prefix}他纠正了你：「{note}」')
            continue

        if kind == "skill_evolved":
            name = _clean(e.get("skill_name", ""), 40)
            reason = _clean(e.get("reason", ""), 150)
            if name and reason:
                lines.append(f"{prefix}技能「{name}」因为「{reason}」长进了一点")
            continue

        if kind == "skill_used":
            name = _clean(e.get("skill_name", ""), 40)
            reason = _clean(e.get("reason", ""), 150)
            if name and reason:
                lines.append(f"{prefix}他用「{name}」技能做了：{reason}")
            continue

        if kind == "creation_rework":
            note = _clean(e.get("note", ""), 100)
            lines.append(
                f"{prefix}有一稿被他打回重做{f'（{note}）' if note else ''}"
            )
            continue

        if kind == "creation_approved":
            note = _clean(e.get("note", ""), 100)
            lines.append(f"{prefix}他拍板定稿了一份创作{f'（{note}）' if note else ''}")
            continue

        if kind == "roundtable":
            lines.append(f"{prefix}你们开了一次圆桌讨论")
        elif kind == "writing":
            lines.append(f"{prefix}你们一起整理了文稿")
        elif kind == "report":
            lines.append(f"{prefix}他让你出了一份报告")
        elif kind == "chat":
            lines.append(f"{prefix}你们聊了一阵")
    return lines


def _build_prompt(facts: list[str], signals: list[str],
                  seq: int, at: str) -> str:
    """三段式 prompt（范式取自 roundtable/coordinator.py:532）：
    素材块 → 硬约束（含可推情绪白名单）→ 允许弃权。
    """
    when = "补记" if seq > 1 else "第一则"
    blocks = [f"【这一段时间真实发生的事】（全部素材，来自系统记录）"]
    blocks.extend(f"- {f}" for f in facts)
    if signals:
        blocks.append("")
        blocks.append("【可以感知到的状态】（只能作语气参考，不要当成事件写）")
        blocks.extend(f"- {s}" for s in signals)

    material = "\n".join(blocks)
    return f"""你要写今天日记的第 {seq} 则（{when}，时间约 {at}）。

{material}

【怎么写】
- 第一人称「我」，称呼他用「你」。是写给自己的日记，不是工作汇报。
- 挑 1-3 件真正有意思的事讲，**不要列清单**，不要小标题，不要破折号。
- 可以带情绪，但情绪只能从上面的素材里长出来。举例：
  有「打回了多次」→ 可以写「看得出他今天有点急」；
  有「忙到夜里」→ 可以写「他今天熬得挺晚」。
- 100-200 字，一到两段。

【绝对禁止】
1. 不许出现上面素材之外的人名、书名、章节号、日期、地点、数字。
   素材里没有的，一个字都不许添。
2. 不许写「技能又精进了」「记住了你 N 件小事」这类台账句——
   技能日志和记忆面板里都有完整记录，日记抄一遍毫无价值。
   要写的是**因为什么事**长进的。
3. 不许替他下结论（「他一定很高兴」「他压力很大」）。
   ⚠️ 分界线：**可以有看法**，**不许猜内心**。
   可以写「这一稿比上回顺，是因为他先理了结构」——看法落在看得见的事上；
   不许写「他心里其实没底」——那是猜。拿不准就只写事实。
4. 不许自称「小桔」，也不要在末尾署名或加落款。

如果素材实在撑不起一段有内容的话，只回复「{_BAIL_OUT}」三个字，不要硬凑。"""


def narrate(events: list[dict], seq: int = 1, at: str = "") -> Optional[str]:
    """把一批事件润色成日记正文（不含标题、不含客观数据行）。

    Returns:
        润色后的正文；**任何情况下不可用就返回 None**，由调用方回退到
        规则渲染。本函数永不抛异常。
    """
    try:
        facts = _facts(events)
        # 闸门 2：没有带内容的素材就不调——模型只能把空壳写成华丽废话。
        # 而且要**至少一条有分量**的：只有标签没有详情时（如「开了次圆桌」），
        # 模型必然回「素材不足」，白烧一次调用。
        if not facts or not _has_substance(events):
            return None

        try:
            from deskmate.memory.llm import chat_memory, memory_llm_available
        except Exception:
            return None

        # 闸门 1：没配 provider 直接走规则版
        try:
            if not memory_llm_available():
                return None
        except Exception:
            return None

        prompt = _build_prompt(facts, _signals(events), seq, at or "")
        # 后台线程池里跑（tick → asyncio.to_thread），短超时，
        # 与 moments.py:204 同一档，避免慢请求堆积线程池。
        out = chat_memory(
            _SYSTEM, prompt, temperature=0.7, max_tokens=400, timeout=25.0
        )
        text = " ".join(str(out or "").split())

        # 闸门 3：模型认输 / 空 / 长度离谱，一律丢弃用规则版
        if not text or _BAIL_OUT in text:
            return None
        if not (_MIN_CHARS <= len(text) <= _MAX_CHARS):
            return None
        return text
    except Exception as e:
        logger.warning("diary narration failed, silent ledger fallback: %s", e)
        return None
