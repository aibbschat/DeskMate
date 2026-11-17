"""小桔调用专家（2026-11-09；2026-11-17 改为后台任务）：目录、派发、审计、隔离。

本轮改的是**时序**：`consult_experts` 从"跑完才返回"变成"派发即返回"。所以断言也
跟着从「返回值里有专家意见」改成「返回值是派出回执 + 后台真的跑完了、审计与状态都落了」。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deskmate.bots.reading import BookShelf
from deskmate.consult import (
    CONSULT_GUIDANCE,
    EXPERTS,
    MAX_EXPERTS,
    _build_expert_engine,
    _collect,
    make_consult_tool,
    read_runs,
    set_notifier,
    wait_for_jobs,
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


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    """状态文件与通知器都按用例隔离；收尾等后台线程结束，避免串到下一个用例。"""
    monkeypatch.setenv("DESKMATE_CONSULT_RUNS", str(tmp_path / "consult_runs.json"))
    monkeypatch.setenv("DESKMATE_CONSULT_DIR", str(tmp_path / "audit"))
    set_notifier(None)
    yield
    wait_for_jobs(timeout=30)
    set_notifier(None)


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
    # 后台语义必须写进指引：否则模型会等结果、或把「已派出」当结论
    assert "后台任务" in CONSULT_GUIDANCE


def test_consult_dispatches_in_background_and_writes_audit(tmp_path: Path) -> None:
    """派发立刻返回；后台跑完后状态与审计都落盘，两位专家都在。"""
    shelf = _shelf(tmp_path)
    called: list[bool] = []
    events: list[str] = []
    set_notifier(lambda kind, _payload: events.append(kind))
    tool = make_consult_tool(
        provider=_ScriptedProvider(),
        model="scripted",
        shelf=shelf,
        audit_dir=tmp_path / "audit",
        timeout=30,
        on_consulted=lambda: called.append(True),
        session_id="sess-1",
    )
    out = tool(
        experts=["lit-editor", "brainstormer"],
        brief="这段情节是否可信？请给依据。",
        book="测试书",
    )
    # ① 立刻返回：是派出回执，不是结论
    assert "已派出" in out
    assert "不要等结果" in out
    assert "专家咨询结果" not in out
    assert called == [True]
    assert events[:1] == ["started"]

    # ② 后台真的跑完了
    assert wait_for_jobs(timeout=60)
    view = read_runs(session_id="sess-1")
    assert view["active"] == []
    assert len(view["recent"]) == 1
    run = view["recent"][0]
    assert run["status"] == "done"
    assert set(run["expert_names"]) == {"文学编辑", "脑洞专家"}
    assert {r["status"] for r in run["results"]} == {"ok"}
    assert "finished" in events

    audit = list((tmp_path / "audit").rglob("*.md"))
    assert audit and "这段情节是否可信" in audit[0].read_text(encoding="utf-8")
    assert "文学编辑：这是测试意见" in audit[0].read_text(encoding="utf-8")


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


def test_collect_keeps_partial_output_on_timeout() -> None:
    """超时不再清空：已流出来的分析原样返回并标 timeout（240s 白烧的根因之一）。"""
    from coworker.events import Event, EventType

    class _SlowEngine:
        def __init__(self) -> None:
            self.interrupted = False

        async def run(self, prompt):  # noqa: ANN001, ANN201
            yield Event(EventType.ASSISTANT_MESSAGE, {"text": "前七轮的分析都在这里。"})
            await asyncio.sleep(5)
            yield Event(EventType.ASSISTANT_MESSAGE, {"text": "永远不会到的第八轮。"})

        def request_interrupt(self) -> None:
            self.interrupted = True

    engine = _SlowEngine()
    text, status = asyncio.run(_collect(engine, "简报", timeout=0.5))
    assert status == "timeout"
    assert text == "前七轮的分析都在这里。"
    assert engine.interrupted is True


def test_delivery_claim_is_idempotent(tmp_path: Path) -> None:
    """状态文件是唯一事实源：「这条结果投过没有」只能被抢到一次。"""
    from deskmate.consult import _mark_delivered, _unmark_delivered, pending_delivery

    shelf = _shelf(tmp_path)
    tool = make_consult_tool(
        provider=_ScriptedProvider(),
        model="scripted",
        shelf=shelf,
        audit_dir=tmp_path / "audit",
        session_id="sess-x",
    )
    tool(experts=["outline"], brief="这个大纲的节奏有问题吗？", book="测试书")
    assert wait_for_jobs(timeout=60)
    pending = pending_delivery("sess-x")
    assert pending, "没有拿到待投递结果"
    run_id = pending[0]["id"]

    assert _mark_delivered(run_id) is True
    assert _mark_delivered(run_id) is False  # 第二次抢不到
    _unmark_delivered(run_id)
    assert _mark_delivered(run_id) is True  # 投递失败还回去后可以重投


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
    monkeypatch.setattr(
        agent_mod,
        "_deskmate_post_turn_hook",
        lambda u, a, base=None: calls.append((u, a)),
    )
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
    assert "已派出" in out

    eng._post_turn_hook("萧盛问", "小桔答")
    assert calls == []  # 咨询轮跳过
    eng._post_turn_hook("下一轮", "正常答")
    assert len(calls) == 1  # 下一轮恢复
