"""
情感画像读取辅助层——供花园情绪定色、CareToast 关怀、情感访谈共用。

统一从 ``~/.deskmate/deskmate.db`` 的 ``layer='emotional'`` 画像读取结构化情感信号，
把「画像 → 展示 / 关怀 / 提问」的解耦掉：上游只消费本模块，不直接碰 store。

为什么需要它：
- 情感画像（emotional_trigger / recovery_pattern / care_preference）是情感层的
  真相源，但花园、judge、interview 各自读库会重复且口径不一；
- 本模块提供三个粒度的读取：
    * ``triggers()``          —— 情感触发点（正/负）
    * ``recovery_patterns()`` —— 回血方式
    * ``care_preferences()``  —— 关怀偏好
    * ``emotional_context()`` —— 合成一段「情感记忆」文本，供 judge/访谈注入上下文
fail-open：画像缺失 / 空 → 返回空列表 / 空串，绝不阻断上游。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 维度 tag（与 emotion_extract.py 写入一致）
_TAG_TRIGGER = "emotional_trigger"
_TAG_RECOVERY = "recovery_pattern"
_TAG_CARE = "care_preference"


def _load_emotional_profile():
    """加载情感画像；失败返回 None（fail-open）。

    测试注入：设环境变量 DESKMATE_EMOTION_PROFILE_PATH 可指向临时库；
    默认读真实库（~/.deskmate/deskmate.db）的 emotional 层。
    """
    try:
        import os
        from deskmate.cognitive.profile import CognitiveProfileManager

        override = os.environ.get("DESKMATE_EMOTION_PROFILE_PATH")
        if override:
            pm = CognitiveProfileManager(override, layer="emotional")
        else:
            from deskmate.cognitive.profile import get_profile_manager
            pm = get_profile_manager(layer="emotional")
        return pm.get()
    except Exception:
        logger.warning("情感画像读取失败（fail-open）", exc_info=True)
        return None


def _by_tag(profile, tag: str) -> list:
    if profile is None:
        return []
    return [
        s for s in profile.stances
        if getattr(s, "status", "active") == "active" and tag in (list(getattr(s, "tags", []) or []))
    ]


def _by_confidence(items: list[dict]) -> list[dict]:
    """按置信度降序（同分比来源数）。

    底层 store 取条目是 ``ORDER BY id``（uuid，等价插入随机序），调用方直接
    ``[:3]`` 等于随机抽三条 —— 注入 prompt 的触发点未必是画像里最确定的那些。
    """
    return sorted(
        items,
        key=lambda x: (float(x.get("confidence", 0.5)), int(x.get("source_count", 0))),
        reverse=True,
    )


def triggers(positive_only: Optional[bool] = None) -> list[dict]:
    """情感触发点。positive_only=True 只返回正向，False 只返回负向，None 全量。"""
    items = _by_tag(_load_emotional_profile(), _TAG_TRIGGER)
    out = []
    for s in items:
        tags = list(getattr(s, "tags", []) or [])
        polarity = "positive" if "positive" in tags else ("negative" if "negative" in tags else "")
        if positive_only is True and polarity != "positive":
            continue
        if positive_only is False and polarity != "negative":
            continue
        out.append({
            "topic": getattr(s, "topic", ""),
            "content": getattr(s, "content", ""),
            "polarity": polarity,
            "confidence": getattr(s, "confidence", 0.5),
            "source_count": len(getattr(s, "sources", []) or []),
        })
    return _by_confidence(out)


def recovery_patterns() -> list[dict]:
    """回血方式。"""
    return _by_confidence([
        {
            "topic": getattr(s, "topic", ""),
            "content": getattr(s, "content", ""),
            "confidence": getattr(s, "confidence", 0.5),
            "source_count": len(getattr(s, "sources", []) or []),
        }
        for s in _by_tag(_load_emotional_profile(), _TAG_RECOVERY)
    ])


def care_preferences() -> list[dict]:
    """关怀偏好。"""
    return _by_confidence([
        {
            "topic": getattr(s, "topic", ""),
            "content": getattr(s, "content", ""),
            "confidence": getattr(s, "confidence", 0.5),
            "source_count": len(getattr(s, "sources", []) or []),
        }
        for s in _by_tag(_load_emotional_profile(), _TAG_CARE)
    ])


def has_emotional_data() -> bool:
    """是否已有情感画像数据（供上游判断是否启用画像驱动）。"""
    return bool(triggers() or recovery_patterns() or care_preferences())


def mood_tint_for_event(kind: str) -> str:
    """根据事件类型 + 情感画像，返回花园花开该用的情绪定色。

    优先级（画像优先，事件类型兜底）：
    1. 情感画像里有没有该事件对应的情绪（如 user_corrected → 创作受阻/现实不平）
    2. 没有则回退到事件类型默认映射（保留旧行为，fail-open）
    """
    # 事件类型 → 情感画像 tag 的桥（只桥有明确情绪含义的事件）
    _EVENT_TOPIC_HINTS = {
        "user_corrected": "受阻",
        "creation_rework": "受阻",
        "creation_approved": "顺畅",
        "skill_evolved": "顺畅",
        "writing": "顺畅",
        "roundtable": "顺畅",
        "suggestion_accepted": "顺畅",
    }
    hint = _EVENT_TOPIC_HINTS.get(kind)
    if hint:
        t = triggers()
        # 优先匹配正/负情绪与 hint 方向一致的画像条目
        if hint == "受阻":
            neg = [x for x in t if x["polarity"] == "negative"]
            if neg:
                return "低落"
        else:
            pos = [x for x in t if x["polarity"] == "positive"]
            if pos:
                return "顺利"
    # 兜底：事件类型默认映射（与 FLOWER_KINDS 一致）
    _DEFAULT = {
        "skill_created": "顺利", "creation_approved": "顺利", "badge_unlocked": "顺利",
        "user_corrected": "成长", "creation_rework": "成长",
        "roundtable": "顺利", "suggestion_accepted": "顺利",
        "skill_evolved": "顺利", "writing": "平静", "report": "顺利",
    }
    return _DEFAULT.get(kind, "平静")


def emotional_context(limit_trigger: int = 3, limit_recovery: int = 2) -> str:
    """合成「情感记忆」注入文本——供 judge 关怀、访谈提问用。

    包含：情感触发点（正/负）、回血方式、关怀偏好。无数据返回空串（fail-open）。
    """
    t = triggers()[:limit_trigger]
    r = recovery_patterns()[:limit_recovery]
    c = care_preferences()[:2]
    if not (t or r or c):
        return ""

    lines: list[str] = ["## 关于他的情感（情感画像）"]
    if t:
        lines.append("### 什么让他情绪变化")
        for x in t:
            polarity = "正向" if x["polarity"] == "positive" else "负向"
            lines.append(f"- [{polarity}] {x['content'].strip()}")
    if r:
        lines.append("### 他怎么回血")
        for x in r:
            lines.append(f"- {x['content'].strip()}")
    if c:
        lines.append("### 他偏好的关怀方式")
        for x in c:
            lines.append(f"- {x['content'].strip()}")
    lines.append("（以上来自他的真实日记表达，是他亲口说过的；关怀与提问应贴合这些，而非空泛问候。）")
    return "\n".join(lines)


# 情感层访谈缺口：关怀偏好维度数据仍空时，spawn 一个 explore 型 gap（供情感访谈提问）
# ⚠ reason 会被 `interview._build` 当作 {content} **说给萧哥听**（走 _speakable 洗一遍），
# 所以写的是**第二人称**。旧版「他偏好的被关怀方式还未知…他喜欢被问进度」是对着
# 他说话却用「他」——他读到的是系统自检记录（2026-09-22 统一改）。
_EMOTIONAL_GAP_META_KEY = "emotional_gaps_initialized"
_EMOTIONAL_GAP_TOPICS = [
    ("情感·关怀偏好", "你喜欢被关心的方式我还不清楚——是希望我多问几句进度，还是更想一个人待着？"),
    ("情感·回血方式", "你说低落时会靠看片、读书、出去走走回血，除此之外还有别的吗？"),
]
# topic → 它对应的情感维度 tag。用于判断「这一维度是否已经填实」——
# 已填实就不要再问（已知的事不必重问）。tag 与 emotion_extract 写入一致。
_GAP_TOPIC_TO_TAG = {
    "情感·关怀偏好": _TAG_CARE,
    "情感·回血方式": _TAG_RECOVERY,
}


def ensure_emotional_gaps(pm=None, base_dir=None) -> list[dict]:
    """确保情感层「仍空白」的维度都有可访谈的 open gap（按维度幂等，可再补种）。

    为什么不能只播一次（2026-09-22 修）：旧实现靠 meta 标记 ``emotional_gaps_initialized``
    一次性短路——只要播过一次，**此后任何新增的 topic 都再也播不进来**，
    连「某维度后来变空了」也补不回。那是把「初始化」误当成了「永久完成」。
    现在改为按**两条独立判据**决定是否播：
      · 该 topic 已存在于 cognitive_gaps（不论 open/resolved）→ 不重播同一句话；
      · 该维度已有 active 条目（tag 命中）→ 不问已知的事。
    于是本函数幂等且**可补种**：清空的维度、新增的 topic 都会在下一 tick 自动补齐；
    已答完的维度则静默不动（当前情感层三个维度均已有实证数据 → 行为与修复前一致，
    这是**语义修正**而非新增题源）。

    ⚠ 不新增/不臆造题面：``_EMOTIONAL_GAP_TOPICS`` 里每一条都必须有真实依据，
    否则就变成「无据的 gap」，违反 ``interview._gap_is_askable`` 的前提。

    fail-open：任何异常都不影响 tick 主流程。
    pm：测试可注入 CognitiveProfileManager(layer='emotional')；默认全局。
    base_dir：多 workspace 隔离入口——此前 tick 无参调用，gap 永远播进真实库。
    """
    try:
        if pm is None:
            from deskmate.cognitive.profile import resolve_profile_manager
            pm = resolve_profile_manager(layer="emotional", base_dir=base_dir)
        store = pm.store
        profile = pm.profile
        existing_topics = {g.topic for g in profile.cognitive_gaps}
        filled_tags: set[str] = set()
        for s in getattr(profile, "stances", []) or []:
            if getattr(s, "status", "active") != "active":
                continue
            filled_tags |= set(getattr(s, "tags", []) or [])
        added = []
        for topic, reason in _EMOTIONAL_GAP_TOPICS:
            if topic in existing_topics:
                continue
            tag = _GAP_TOPIC_TO_TAG.get(topic)
            if tag and tag in filled_tags:
                continue  # 维度已有实证数据，不必再问
            g = pm.add_gap(topic, reason, priority=0.7)
            added.append({"gap_id": g.id, "topic": topic})
        # meta 仅作「初始化跑过」的留痕，不再作为短路条件
        store.set_meta(_EMOTIONAL_GAP_META_KEY, True)
        return added
    except Exception:
        logger.warning("情感访谈缺口初始化失败（fail-open）", exc_info=True)
        return []
