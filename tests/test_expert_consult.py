"""小桔调用专家（2026-11-09）：目录、并行、审计、隔离。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deskmate.bots.reading import BookShelf
from deskmate.consult import (
    CONSULT_GUIDANCE,
    EXPERTS,
    MAX_EXPERTS,
    _build_expert_engine,
    make_consult_tool,
)


class _ScriptedProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        system = str(messages[0].get("content", "")) if messages else ""
        for name in ("文学编辑", "脑洞专家", "大纲专家", "人物专家", "影视改编"):
            if name in system:
                return AssistantTurn(text=f"{name}：这是测试意见。")
        return AssistantTurn(text="专家：这是测试意见。")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def _shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


def test_directory_has_five_experts() -> None:
    assert {item["id"] for item in EXPERTS} == {
        "lit-editor", "brainstormer", "outline", "character", "adaptation",
    }
    assert MAX_EXPERTS == 3
    assert "你可以调用专家" in CONSULT_GUIDANCE
    assert "看不到你的记忆" in CONSULT_GUIDANCE


def test_consult_two_experts_parallel_and_audit(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    called = []
    tool = make_consult_tool(
        provider=_ScriptedProvider(),
        model="scripted",
        shelf=shelf,
        audit_dir=tmp_path / "audit",
        timeout=30,
        on_consulted=lambda: called.append(True),
    )
    out = tool(
        experts=["lit-editor", "brainstormer"],
        brief="这段情节是否可信？请给依据。",
        book="测试书",
    )
    assert "文学编辑：这是测试意见" in out
    assert "脑洞专家：这是测试意见" in out
    assert "专家咨询结果" in out
    assert called == [True]
    audit = list((tmp_path / "audit").rglob("*.md"))
    assert audit and "这段情节是否可信" in audit[0].read_text(encoding="utf-8")


def test_consult_rejects_bad_requests(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    tool = make_consult_tool(
        provider=_ScriptedProvider(), model="scripted", shelf=shelf, audit_dir=tmp_path / "audit"
    )
    assert "没指定专家" in tool(experts=[], brief="这是一个足够长的简报", book="测试书")
    assert "没有这些专家" in tool(experts=["nope"], brief="这是一个足够长的简报")
    assert "最多 3 位" in tool(
        experts=["lit-editor", "brainstormer", "outline", "character"],
        brief="这是一个足够长的简报",
        book="测试书",
    )
    assert "简报太短" in tool(experts=["lit-editor"], brief="短")
    assert "找不到" in tool(experts=["lit-editor"], brief="这是一个足够长的简报", book="不存在")


def test_expert_engine_is_isolated_and_can_read(tmp_path: Path) -> None:
    shelf = _shelf(tmp_path)
    eng = _build_expert_engine("outline", _ScriptedProvider(), "scripted", shelf)
    names = set(eng.registry.names())
    assert {"read_book", "list_books", "notebook_read", "notebook_remember"} <= names
    assert not (names & {"memory_read", "memory_update", "memory_forget"})
    assert eng._post_turn_hook is None


def test_deskmate_has_consult_but_expert_does_not(tmp_path: Path) -> None:
    from coworker.agent import build_engine
    from coworker.memory.sqlite_store import SQLiteMemoryStore
    from coworker.personas.registry import get_registry

    store = SQLiteMemoryStore(tmp_path / "m.db")
    desk = build_engine(
        agent=get_registry().get("deskmate").agent(),
        provider=_ScriptedProvider(),
        model="scripted",
        workspace=None,
        memory_store=store,
    )
    assert "consult_experts" in set(desk.registry.names())
    assert "你可以调用专家" in str(desk.messages[0].get("content", ""))

    expert = build_engine(
        agent=get_registry().get("character").agent(),
        provider=_ScriptedProvider(),
        model="scripted",
        workspace=None,
    )
    assert "consult_experts" not in set(expert.registry.names())
    assert "你可以调用专家" not in str(expert.messages[0].get("content", ""))

def test_consult_turn_skips_auto_memory_extraction(tmp_path: Path, monkeypatch) -> None:
    """咨询轮不自动抽小桔记忆；下一轮恢复。"""
    import coworker.agent as agent_mod
    from coworker.agent import build_engine
    from coworker.memory.sqlite_store import SQLiteMemoryStore
    from coworker.personas.registry import get_registry

    calls = []
    monkeypatch.setattr(agent_mod, "_deskmate_post_turn_hook", lambda u, a: calls.append((u, a)))
    monkeypatch.setenv("DESKMATE_CONSULT_DIR", str(tmp_path / "consult"))

    store = SQLiteMemoryStore(tmp_path / "m.db")
    eng = build_engine(
        agent=get_registry().get("deskmate").agent(),
        provider=_ScriptedProvider(),
        model="scripted",
        workspace=None,
        memory_store=store,
    )
    out = eng.registry.execute(
        "consult_experts",
        {"experts": ["character"], "brief": "这个人的行为是否合理？请给依据。"},
    )
    assert "专家咨询结果" in out

    eng._post_turn_hook("萧盛问", "小桔答")
    assert calls == []  # 咨询轮跳过
    eng._post_turn_hook("下一轮", "正常答")
    assert len(calls) == 1  # 下一轮恢复
