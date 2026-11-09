"""同题 A/B：有 / 无笔记本（1v1 专用，手动触发，不每轮问）。

用途：验证「笔记本到底有没有用」——同一个问题、同一位专家，跑两遍：
A 不带笔记本（去掉笔记本工具与指引）；B 带笔记本（工具 + 摘要）。
不是产品内常驻功能；CLI 或后台脚本手动跑，出结果再决定要不要继续加记忆机制。
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any


def _strip_notebook_section(prompt: str) -> str:
    """去掉人设里的「## 你的笔记本」一节，A 组不该看到用不了的指引。"""
    return re.sub(r"\n## 你的笔记本.*?(?=\n## |\Z)", "\n", prompt, flags=re.S).strip() + "\n"


def build_engines(
    *,
    expert: str,
    book: str = "",
    provider: Any,
    model: str,
    shelf: Any = None,
):
    """构造 A（无笔记）/ B（有笔记）两个 1v1 engine，返回 (eng_a, eng_b, digest)。"""
    from coworker.agent import build_engine
    from coworker.catalog import expand
    from coworker.personas.registry import get_registry
    from deskmate.bots.notebooks import NotebookSession
    from deskmate.bots.reading import default_shelf

    entry = get_registry().get(expert)
    base = entry.agent()
    tools_a = [t for t in entry.manifest.tools if t != "notebook"]
    tools_b = list(entry.manifest.tools)

    digest = ""
    if book:
        session = NotebookSession(expert, shelf or default_shelf())
        if session.ensure_book(book):
            digest = session.read() or ""
            if len(digest) > 8000:
                digest = digest[:8000] + "\n……（截断）"

    def _factory(tool_ids):
        return lambda ctx: expand(tool_ids, ctx)

    agent_a = dataclasses.replace(
        base,
        system_prompt=_strip_notebook_section(base.system_prompt),
        tool_factory=_factory(tools_a),
    )
    prompt_b = base.system_prompt
    if digest:
        prompt_b += "\n\n# 你的笔记本摘要（本轮自动带入）\n\n" + digest
    agent_b = dataclasses.replace(
        base,
        system_prompt=prompt_b,
        tool_factory=_factory(tools_b),
    )

    eng_a = build_engine(agent=agent_a, provider=provider, model=model, workspace=None)
    eng_b = build_engine(agent=agent_b, provider=provider, model=model, workspace=None)
    return eng_a, eng_b, digest


async def run_engine(engine: Any, question: str) -> str:
    """跑一轮，取最终助手文本（工具调用中间消息不算）。"""
    from coworker.events import EventType

    last = ""
    async for event in engine.run(question):
        if event.type == EventType.ASSISTANT_MESSAGE:
            text = (event.data or {}).get("text") or ""
            if text:
                last = text
    return last


async def run_ab(
    *,
    expert: str,
    book: str,
    question: str,
    provider: Any,
    model: str,
    shelf: Any = None,
) -> dict[str, Any]:
    eng_a, eng_b, digest = build_engines(
        expert=expert, book=book, provider=provider, model=model, shelf=shelf
    )
    without = await run_engine(eng_a, question)
    with_nb = await run_engine(eng_b, question)
    return {
        "expert": expert,
        "book": book,
        "question": question,
        "digest_chars": len(digest),
        "without_notebook": without,
        "with_notebook": with_nb,
    }
