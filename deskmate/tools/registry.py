"""deskmate 侧工具注册表（四段前置）。

以前加一个工具要改三处：工具文件、``build_engine`` 里的注册、系统提示词。
这里把「有哪些工具、怎么构造」收敛到一张表，``build_engine`` 只调一次
``build_tools()``；新工具只需在 ``FACTORIES`` 里加一行。

约定
----
- 只注册**可安全默认开启**的工具；需要审批/写权限的仍显式注册。
- 工厂函数接收 ``base``（记忆根目录），并在**构造期**固化路径——不在调用期
  读环境变量（daemon 线程/测试 teardown 后会回落到真实目录）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from deskmate.tools.memory_search import make_memory_search_tool

#: 工具名 → 工厂。工厂签名：``(**ctx) -> Callable``。
FACTORIES: dict[str, Callable[..., Callable[..., Any]]] = {
    "memory_search": make_memory_search_tool,
}

#: 默认开启的只读工具（四段的范围：只开检索，不开写入）。
DEFAULT_READONLY: tuple[str, ...] = ("memory_search",)


def build_tools(
    names: Optional[tuple[str, ...]] = None,
    base: Optional[Path | str] = None,
) -> list[Callable[..., Any]]:
    """按名字构造工具列表；未知名字直接跳过（不抛，避免拖垮会话）。"""
    picked = DEFAULT_READONLY if names is None else tuple(names)
    out: list[Callable[..., Any]] = []
    for name in picked:
        factory = FACTORIES.get(name)
        if factory is None:
            continue
        try:
            out.append(factory(base=base))
        except Exception:  # noqa: BLE001 —— 单个工具起不来不影响其它工具
            continue
    return out
