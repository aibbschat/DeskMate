"""同题 A/B（有/无笔记本）离线测试（2026-11-09）。

锁住：A 组没有笔记本工具/摘要，B 组有；两条链路跑出不同结果；摘要来自真实笔记。
不调用真实模型。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deskmate.bots.abtest import build_engines, run_ab
from deskmate.bots.notebooks import NotebookSession
from deskmate.bots.reading import BookShelf


class _ScriptedProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        system = str(messages[0].get("content", "")) if messages else ""
        return AssistantTurn(text=("B 组" if "笔记本摘要" in system else "A 组"))

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def _shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


def test_ab_build_pair_and_run(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    s = NotebookSession("brainstormer", shelf)
    s.set_book("测试书")
    s.remember("旧判断：第三章要慢")

    eng_a, eng_b, digest = build_engines(
        expert="brainstormer", book="测试书", provider=_ScriptedProvider(),
        model="scripted", shelf=shelf,
    )
    system_a = str(eng_a.messages[0].get("content") or "")
    system_b = str(eng_b.messages[0].get("content") or "")
    assert "笔记本摘要" not in system_a
    assert "旧判断：第三章要慢" in system_b
    assert "notebook_read" not in set(eng_a.registry.names())
    assert "notebook_read" in set(eng_b.registry.names())
    assert "第三章要慢" in digest

    out = asyncio.run(
        run_ab(
            expert="brainstormer", book="测试书", question="第三章怎么走？",
            provider=_ScriptedProvider(), model="scripted", shelf=shelf,
        )
    )
    assert out["without_notebook"] == "A 组"
    assert out["with_notebook"] == "B 组"
    assert out["digest_chars"] > 0
