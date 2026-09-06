"""L2 风险闸 —— 自主动作「能不能直接干」的唯一路由点。

所有「该不该先问用户」的判断都汇到这里。现在只实现 low 分支（直接执行），
high 分支（产提议卡等用户确认）的接缝从 Day 1 就在：engine 调度时按这里返回的
等级决定走 executor 还是走 propose card。以后加高风险动作不必改这里的结构。
"""

from __future__ import annotations

from .registry import get_spec


def classify_risk(action_type: str) -> str:
    """返回 'low' / 'high' / 'unknown'。

    - low：只读或只写本地记忆，可自主执行
    - high：有外部/不可逆 side effect，必须走提议卡
    - unknown：未登记动作，默认按高风险处理（fail-safe，绝不默认放开）
    """
    spec = get_spec(action_type)
    if spec is None:
        return "unknown"
    return spec.risk


def is_low_risk(action_type: str) -> bool:
    return classify_risk(action_type) == "low"


def requires_approval(action_type: str) -> bool:
    """高风险或未知动作都要求先经用户确认（fail-safe）。"""
    return classify_risk(action_type) in ("high", "unknown")
