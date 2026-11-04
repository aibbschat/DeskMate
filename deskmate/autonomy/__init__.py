"""小桔自主能力地基：任务结果送达与副作用闸门。

A 阶段只做两件纯逻辑：把 task_runs 的完成结果可靠送进 proactive_ledger，
以及提供一个默认不改变现有权限行为的闸门骨架。不引入新引擎、不改 BYPASS。
"""

from .delivery import (
    build_completion_event,
    build_ledger_payload,
    deliver_run_result,
    delivery_anchor,
)
from .gate import GatePolicy

__all__ = [
    "build_completion_event",
    "build_ledger_payload",
    "deliver_run_result",
    "delivery_anchor",
    "GatePolicy",
]
