"""建议类型收敛 —— 把 judge 自由发挥的 snake_case 归一成固定枚举（2026-10-24 主动性改造阶段三）。

为什么需要这一层：2026-10-21 至 10-24 的 disturb_state 里，写作类建议散在
16 种不同的 suggestion_type 里（writing_review、review_writing_break、
writing_block_help……），而 PreferenceStore 要求“同类型被接受 2 次才沉淀偏好”，
类型一散，偏好永远学不到；台账 kind 一散，同一问题也难合并、难统计。

本模块是纯函数、无 IO、无 LLM，可单测；未知类型一律落 generic，绝不丢建议。
"""

from __future__ import annotations

CANONICAL_TYPES = frozenset({
    "plot_contradiction",
    "character_inconsistency",
    "timeline_error",
    "missing_foreshadow",
    "writing_stall",
    "chapter_updated",
    "writing_review",
    "research_gap",
    "emotional_care",
    "self_expression",
    "task_reminder",
    "generic",
})

_WRITING_TYPES = frozenset({
    "plot_contradiction",
    "character_inconsistency",
    "timeline_error",
    "missing_foreshadow",
    "writing_stall",
    "chapter_updated",
    "writing_review",
})

# 历史散落类型的显式别名（来自 2026-10-21 至 10-24 disturb_state 实测）。
_ALIASES = {
    "writing_block_assistance": "writing_review",
    "writing_block_help": "writing_review",
    "writing_block_support": "writing_review",
    "writing_assistance_followup": "writing_review",
    "writing_support_request": "writing_review",
    "writing_breakpoint_check": "writing_review",
    "writing_breakpoint_review": "writing_review",
    "review_writing_break": "writing_review",
    "review_writing_breakpoint": "writing_review",
    "writing_review_request": "writing_review",
    "user_requested_writing_review": "writing_review",
    "user_request_writing_check": "writing_review",
    "user_request_writing_assist": "writing_review",
    "character_consistency_check": "character_inconsistency",
    "character_inconsistency_check": "character_inconsistency",
    "plot_repetition": "plot_contradiction",
    "stall": "writing_stall",
    "stall_flag": "writing_stall",
    "chapter_update": "chapter_updated",
    "new_chapter": "chapter_updated",
}

# 关键词兜底（别名未命中时）：按“最具体先行”排序，避免 writing 通吃。
_KEYWORD_RULES = (
    ("foreshadow", "missing_foreshadow"),
    ("plot", "plot_contradiction"),
    ("character", "character_inconsistency"),
    ("timeline", "timeline_error"),
    ("stall", "writing_stall"),
    ("chapter", "chapter_updated"),
    ("writing", "writing_review"),
    ("review", "writing_review"),
)


def normalize_suggestion_type(raw) -> str:
    """把任意 judge 输出归一成规范类型；空值 / 未知值 → generic。"""
    try:
        t = str(raw or "").strip().lower()
        if not t:
            return "generic"
        if t in CANONICAL_TYPES:
            return t
        if t in _ALIASES:
            return _ALIASES[t]
        for keyword, canonical in _KEYWORD_RULES:
            if keyword in t:
                return canonical
        return "generic"
    except Exception:
        return "generic"


def thresholds_for(suggestion_type: str) -> tuple[float, float]:
    """返回 (suggest_threshold, prepare_threshold)。

    写作类放宽到 0.65 / 0.45：写作发现本来就难到 0.8，且中置信已由阶段二暂存
    待递兜底，不会直接弹。emotional_care / self_expression 不走这条路，
    它们在 confidence.evaluate_action 里有专属分支（0.6 / 0.55），调用方传
    入的阈值对它们无效——这里仍返回通用值，仅作文档与单测口径。
    """
    t = normalize_suggestion_type(suggestion_type)
    if t in _WRITING_TYPES:
        return (0.65, 0.45)
    return (0.8, 0.5)
