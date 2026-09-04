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
    return out


def recovery_patterns() -> list[dict]:
    """回血方式。"""
    return [
        {
            "topic": getattr(s, "topic", ""),
            "content": getattr(s, "content", ""),
            "confidence": getattr(s, "confidence", 0.5),
            "source_count": len(getattr(s, "sources", []) or []),
        }
        for s in _by_tag(_load_emotional_profile(), _TAG_RECOVERY)
    ]


def care_preferences() -> list[dict]:
    """关怀偏好。"""
    return [
        {
            "topic": getattr(s, "topic", ""),
            "content": getattr(s, "content", ""),
            "confidence": getattr(s, "confidence", 0.5),
            "source_count": len(getattr(s, "sources", []) or []),
        }
        for s in _by_tag(_load_emotional_profile(), _TAG_CARE)
    ]


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
_EMOTIONAL_GAP_META_KEY = "emotional_gaps_initialized"
_EMOTIONAL_GAP_TOPICS = [
    ("情感·关怀偏好", "他偏好的被关怀方式还未知（数据为空）——他喜欢被问进度，还是倾向独处？"),
    ("情感·回血方式", "他情绪低落时的回血方式还不够清楚——除了写史料，还有别的吗？"),
]


def ensure_emotional_gaps(pm=None) -> list[dict]:
    """确保情感层存在可访谈的 open gap（幂等，只补一次）。

    情感画像刚起步，触发点/回血方式已有真实数据，但「关怀偏好」等仍空；
    没有 open gap，InterviewEngine 就不会主动提问。本函数在首次启用时
    补上这些 explore 型 gap，让情感访谈有可问的东西。
    幂等：meta 标记后不再重复补。fail-open。
    pm：测试可注入 CognitiveProfileManager(layer='emotional')；默认全局。
    """
    try:
        from deskmate.cognitive.profile import get_profile_manager
        if pm is None:
            pm = get_profile_manager(layer="emotional")
        store = pm.store
        if store.get_meta(_EMOTIONAL_GAP_META_KEY, default=False):
            return []
        added = []
        existing_topics = {g.topic for g in pm.profile.cognitive_gaps}
        for topic, reason in _EMOTIONAL_GAP_TOPICS:
            if topic in existing_topics:
                continue
            g = pm.add_gap(topic, reason, priority=0.7)
            added.append({"gap_id": g.id, "topic": topic})
        store.set_meta(_EMOTIONAL_GAP_META_KEY, True)
        return added
    except Exception:
        logger.warning("情感访谈缺口初始化失败（fail-open）", exc_info=True)
        return []
