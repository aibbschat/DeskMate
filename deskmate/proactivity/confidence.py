"""置信度评估 — 根据置信度分数决定行动类型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActionType(Enum):
    """行动类型。"""
    SUGGEST = "suggest"        # 高置信度 → 直接弹出建议
    PREPARE = "prepare"        # 中置信度 → 静默准备，用户询问时提供
    NO_ACTION = "no_action"    # 低置信度 → 不行动


@dataclass
class ActionDecision:
    """行动决策。"""
    action: ActionType
    confidence: float          # 0.0 - 1.0
    reason: str


# 置信度门槛（参考技术方案 4.3.3）
# 默认值；ProactiveEngine.tick 会用成长系统的信任感调制后的阈值覆盖
THRESHOLD_SUGGEST = 0.8    # > 80% → 直接弹出
THRESHOLD_PREPARE = 0.5    # 50%-80% → 静默准备
# < 50% → 不行动

# P2-5 情感关怀专属门槛：纯粹的关心不需要高把握（judge 引导 0.6-0.8 即可），
# 用更低门槛让其更容易触发；且不走 PREPARE（关怀不需要「静默准备」）。
THRESHOLD_SUGGEST_CARE = 0.6


def evaluate_action(
    confidence: float,
    reason: str = "",
    suggest_threshold: float | None = None,
    prepare_threshold: float | None = None,
    suggestion_type: str | None = None,
    should_act: bool | None = None,
) -> ActionDecision:
    """根据置信度分数决定行动类型。

    Args:
        confidence: 0.0-1.0 的置信度分数
        reason: 判断理由
        suggest_threshold: 可选，覆盖默认 SUGGEST 门槛（成长系统信任感调制）
        prepare_threshold: 可选，覆盖默认 PREPARE 门槛
        suggestion_type: 可选，建议类型。当为 ``emotional_care`` 时启用关怀专属
            低门槛（THRESHOLD_SUGGEST_CARE），且低于门槛直接 NO_ACTION（不进
            PREPARE），避免关怀被静默积压弹不出。
        should_act: 可选，judge 的语义判定。``False`` 表示 judge 明确认为**不该
            介入**（如云端无桌面、未发现异常）——此时无论 confidence 多高都强制
            NO_ACTION，绝不走 SUGGEST 写台账/弹建议（2026-10-07 修复：原代码只
            看 confidence 无视 should_act，导致 "没发现异常" 以自报 0.9 的 SUGGEST
            被误录为 pending 并递出）。``True`` 或 ``None``（未提供）保持原行为。

    Returns:
        ActionDecision
    """
    confidence = max(0.0, min(1.0, confidence))

    # 语义闸门（2026-10-07）：judge 明确判定不介入 → 强制不行动。
    # 这是「判定有效才写台账」的设计前提（设计文档 §5.1）；不依赖 LLM 自报的
    # confidence（模型常把 should_act=false 也配上 0.9 高置信）。
    if should_act is False:
        return ActionDecision(
            action=ActionType.NO_ACTION,
            confidence=confidence,
            reason=reason or "judge 判定不介入（should_act=false），不主动打扰",
        )

    # P2-5 情感关怀：专属低门槛，且不做 PREPARE 过渡。
    if suggestion_type == "emotional_care":
        if confidence >= THRESHOLD_SUGGEST_CARE:
            return ActionDecision(
                action=ActionType.SUGGEST,
                confidence=confidence,
                reason=reason or f"情感关怀 ({confidence:.0%})，主动关心",
            )
        return ActionDecision(
            action=ActionType.NO_ACTION,
            confidence=confidence,
            reason=reason or f"情感关怀置信度不足 ({confidence:.0%})，不主动打扰",
        )

    thr_suggest = suggest_threshold if suggest_threshold is not None else THRESHOLD_SUGGEST
    thr_prepare = prepare_threshold if prepare_threshold is not None else THRESHOLD_PREPARE

    if confidence >= thr_suggest:
        return ActionDecision(
            action=ActionType.SUGGEST,
            confidence=confidence,
            reason=reason or f"高置信度 ({confidence:.0%})，建议主动干预",
        )
    elif confidence >= thr_prepare:
        return ActionDecision(
            action=ActionType.PREPARE,
            confidence=confidence,
            reason=reason or f"中置信度 ({confidence:.0%})，静默准备",
        )
    else:
        return ActionDecision(
            action=ActionType.NO_ACTION,
            confidence=confidence,
            reason=reason or f"低置信度 ({confidence:.0%})，不行动",
        )
