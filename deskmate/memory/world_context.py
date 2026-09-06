"""把小桔「看世界」得来的观察回灌进对话 / 主动推理上下文。

背景（2026-09-06 复盘）：world_ingestion（豆包搜索）与 digest（AI 圈网文观察日记）
都把外部观察写进了 learned_history（带「（外部·」溯源标）。但全代码库里
``LearnedStore.relevant()`` / ``active_recent()`` 除了单元测试、日记素材（``diary_inspiration``），
**聊天与主动决策路径一条都没调用**——她"看"了，但既不会在聊天里主动提及，也不能据此决策。
本模块就是那条缺失的"回灌"管：让「她观察到的世界」从只落库、无人消费，
变成聊天与主动推理可读的依据。

设计：
- 只取带「（外部·」溯源标的事实（世界观察），不碰用户口述事实，避免混淆来源。
- fail-open：任何异常返回空串，绝不阻断主流程（聊天 / 主动 tick 都比记忆重要）。
- 7 天过期的条目在 ``relevant()`` 里已被自动过滤，不会注入过期世界。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_EXTERNAL_PREFIX = "（外部"


def build_world_context(
    base_dir: str | Path | None = None,
    limit: int = 25,
) -> str:
    """取近期活跃的「世界观察」事实，格式化为一段注入文本。

    Args:
        base_dir: 记忆库目录；None 走默认（~/.deskmate）。
        limit: 最多取多少条活跃事实做筛选（默认 25）。

    Returns:
        形如「## 她观察到的世界……」的多行文本；无世界观察返回空串。
    """
    try:
        from .learned_store import LearnedStore

        items = LearnedStore(base_dir=base_dir).relevant(limit=limit)
    except Exception:
        return ""
    if not items:
        return ""

    lines: list[str] = []
    for it in items:
        fact = (it.get("fact") or "").strip()
        if not fact.startswith(_EXTERNAL_PREFIX):
            continue  # 只回灌世界观察，不碰用户事实
        ts = (it.get("ts") or "")[:10]
        lines.append(f"- [{ts}] {fact}")
    if not lines:
        return ""

    header = (
        "## 她观察到的世界（小桔近期主动摄入的外部信息，供你回应时参考；"
        "这是她看到的，不是用户原话，引用时勿当成萧哥说过的）"
    )
    return header + "\n" + "\n".join(lines[:limit])
