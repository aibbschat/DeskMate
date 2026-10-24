"""暂存待递 —— 把本来会被丢掉的建议留到开聊时递（2026-10-24 主动性改造阶段二）。

丢件点有两个：
- 决策为 PREPARE（中置信度）：原逻辑只返回 action=prepare，不进台账、不广播，
  前端也不处理，等于白判一轮。
- 决策为 SUGGEST 但专注度为 deep：原逻辑直接返回 NO_ACTION，不暂存。

暂存不等于“绕过门槛”：落库用 bypass_threshold=True 是因为暂存项的语义是
“待萧盛开聊时裁决的候选”，不是“已确认的高置信结论”；递送时仍受
build_handoff_opening 的 understanding 守卫与每日交付上限约束。

不暂存的情况（刻意保留）：judge 明确 should_act=False；防打扰拦截
（连续忽略 / 同 key 30 分钟冷却 / 静默模式）——那是用户信号，暂存会绕过它；
情绪类（emotional_care / self_expression）有时效性，过期再递没有意义。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_NON_DEFERRED_TYPES = {"emotional_care", "self_expression"}


def defer_judgment(base_dir, judgment, reason: str, *, original_action: str = "") -> str:
    """把一次判断暂存进主动台账（status=pending），返回台账 id，失败/不暂存返回空串。"""
    try:
        if judgment is None or not getattr(judgment, "should_act", False):
            return ""
        suggestion_type = str(getattr(judgment, "suggestion_type", "") or "")
        content = str(getattr(judgment, "suggestion_content", "") or "").strip()
        if not content or suggestion_type in _NON_DEFERRED_TYPES:
            return ""
        from .ledger import record_proactive
        from .tick import _finding_anchor

        evidence = str(getattr(judgment, "reasoning", "") or "")[:500]
        if not evidence.strip():
            evidence = reason or "暂存待递"
        rid = record_proactive(
            base_dir=Path(base_dir) if base_dir is not None else None,
            source="cloud_audit",
            kind=suggestion_type,
            content=content,
            evidence=evidence,
            anchor=_finding_anchor(judgment),
            confidence=float(getattr(judgment, "confidence", 0.0) or 0.0),
            bypass_threshold=True,
            payload={
                "deferred": True,
                "deferred_reason": reason or "",
                "original_action": original_action or "",
            },
        )
        return rid or ""
    except Exception as e:  # noqa: BLE001 —— 暂存失败绝不阻断 tick
        logger.debug("暂存待递失败（不阻断）: %s", e, exc_info=True)
        return ""
