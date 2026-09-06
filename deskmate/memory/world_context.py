"""把小桔「看世界」得来的观察回灌进对话 / 主动推理上下文。

背景（2026-09-06 复盘）：world_ingestion（豆包搜索）与 digest（AI 圈网文观察日记）
都把外部观察写进了 learned_history（带「（外部·」溯源标）。但全代码库里
``LearnedStore.relevant()`` / ``active_recent()`` 除了单元测试、日记素材（``diary_inspiration``），
**聊天与主动决策路径一条都没调用**——她"看"了，但既不会在聊天里主动提及，也不能据此决策。
本模块就是那条缺失的"回灌"管：让「她观察到的世界」从只落库、无人消费，
变成聊天与主动推理可读的依据。

2026-09-06 萧哥追加「理解层」：除了**事实**（她观察到的世界），还回灌**她的理解**
（LLM 消化观察后的判断，见 ``agency/behaviors/understand_observations.py``，落
``agency/observation_understanding.jsonl``）。事实是可信依据，理解是参考视角——
决策时两者分层呈现，高风险动作仍须人审。

设计：
- 只取带「（外部·」溯源标的事实（世界观察），不碰用户口述事实，避免混淆来源。
- 理解段来自独立 jsonl（最近 7 天、最多 3 条），与事实解耦，理解缺失不影响事实回灌。
- fail-open：任何异常返回已有的可用段（或空串），绝不阻断主流程（聊天 / 主动 tick 都比记忆重要）。
- 7 天过期的条目在 ``relevant()`` 里已被自动过滤，不会注入过期世界。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_EXTERNAL_PREFIX = "（外部"


def build_world_context(
    base_dir: str | Path | None = None,
    limit: int = 25,
) -> str:
    """取近期活跃的「世界观察」事实 + 小桔的理解，格式化为一段注入文本。

    Args:
        base_dir: 记忆库目录；None 走默认（~/.deskmate）。
        limit: 最多取多少条活跃事实做筛选（默认 25）。

    Returns:
        形如「## 她观察到的世界……」+「## 她对世界的理解……」的多行文本；
        无任何世界观察返回空串。
    """
    fact_block = _build_fact_block(base_dir, limit)
    understand_block = _build_understand_block(base_dir)
    parts = [p for p in (fact_block, understand_block) if p]
    if not parts:
        return ""
    return "\n\n".join(parts)


def _build_fact_block(base_dir: str | Path | None, limit: int) -> str:
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


def _build_understand_block(base_dir: str | Path | None) -> str:
    """读理解层 jsonl，回灌「她的理解」段（决策参考，可能带主观）。"""
    root = (
        Path(base_dir)
        if base_dir
        else Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
    )
    path = root / "agency" / "observation_understanding.jsonl"
    if not path.is_file():
        return ""
    try:
        cutoff = time.time() - 7 * 86400  # 只取近 7 天
        blocks: list[str] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = float(rec.get("ts") or 0)
                if ts < cutoff:
                    continue
                text = str(rec.get("understanding") or "").strip()
                if text:
                    blocks.append(text)
        if not blocks:
            return ""
        # 最多 3 条，避免注入过长；倒序（最新的在前）
        blocks = blocks[-3:][::-1]
        header = (
            "## 她对世界的理解（小桔消化观察后的判断，供决策参考；"
            "这是她的视角、可能带主观，高风险动作仍须人审）"
        )
        return header + "\n" + "\n".join(f"- {b}" for b in blocks)
    except Exception:
        return ""
