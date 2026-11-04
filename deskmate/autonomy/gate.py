"""副作用闸门骨架 —— 按 risk class 决定 allow / propose / deny。

⚠️ A 阶段**故意不改变任何现有行为**：默认构造的 GatePolicy 恒返回 "allow"，
定时任务仍跑在原有的 ``Mode.BYPASS_APPROVALS`` 下。「收敛 BYPASS、把
EXTERNAL / WRITE_LOCAL 类动作留给人审」属于安全策略变更，须萧哥拍板
（施工图决策项 D3）后才启用。

启用方式（未来）：显式传入 mapping，例如
``GatePolicy({"EXTERNAL": "propose", "WRITE_LOCAL": "propose"})``，
再把 ``decide()`` 的结果接到提议卡 / PermissionEngine。风险口径以
``coworker/risk.py`` 的五级为单一事实源，L2 的两级只作展示别名。
"""

from __future__ import annotations

from typing import Any, Optional

VALID_DECISIONS = ("allow", "propose", "deny")


class GatePolicy:
    def __init__(self, mapping: Optional[dict[str, str]] = None) -> None:
        rules = dict(mapping or {})
        for key, value in rules.items():
            if value not in VALID_DECISIONS:
                raise ValueError(f"未知闸门决策 {key}={value}")
        self._mapping = rules
        # 没有显式规则＝闸门未启用＝完全保持现有 BYPASS 行为。
        self.enabled = bool(rules)

    def decide(self, *, task: Any, tool_name: str, risk_class: str) -> str:
        if not self.enabled:
            return "allow"
        decision = self._mapping.get(risk_class)
        if decision is None:
            decision = self._mapping.get(f"tool:{tool_name}", "allow")
        return decision if decision in VALID_DECISIONS else "allow"
