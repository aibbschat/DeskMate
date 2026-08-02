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


def evaluate_action(
    confidence: float,
    reason: str = "",
    suggest_threshold: float | None = None,
    prepare_threshold: float | None = None,
) -> ActionDecision:
    """根据置信度分数决定行动类型。

    Args:
        confidence: 0.0-1.0 的置信度分数
        reason: 判断理由
        suggest_threshold: 可选，覆盖默认 SUGGEST 门槛（成长系统信任感调制）
        prepare_threshold: 可选，覆盖默认 PREPARE 门槛

    Returns:
        ActionDecision
    """
    confidence = max(0.0, min(1.0, confidence))
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
