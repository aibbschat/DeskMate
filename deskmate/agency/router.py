"""L2 代理决策路由 —— 「这个任务该由谁来做决定/执行」。

设计（2026-09-05 拍板）：代理决策路由 = 圆桌 7 persona 泛化延伸。即某些自主任务
可由某个「角色视角」来决策或执行，而不是永远小桔自己拍板。

当前 minimal 版：所有内置自主动作（world_search 等）都是小桔自身能力，
路由直接返回 ``"self"``，无需外部 persona。路由接缝从 Day 1 就在——以后接
roundtable persona 时，只需在此按 action_type / 上下文返回某个 persona id，
engine 把执行委派给该 persona，其余代码不变。

fail-open：任何异常都回落 "self"（绝不因路由失败而卡住自主层）。
"""

from __future__ import annotations

from typing import Any


def route_task(action_type: str, context: dict[str, Any] | None = None) -> str:
    """返回处理该任务的处理者标识。

    - "self"：小桔自身执行（默认）
    - "<persona_id>"：交由某圆桌角色视角决策/执行（未来扩展）

    当前实现：所有已知自主动作都归 self。
    """
    try:
        # 未来：若 action_type 注册了专属 persona，或 context 指定了 @角色，则返回该 id。
        # 例：if action_type in PERSONA_OWNED: return PERSONA_OWNED[action_type]
        return "self"
    except Exception:
        return "self"
