"""小桔调用专家（2026-11-09；2026-11-17 改为后台任务）：目录、派发、审计、隔离。

本轮改的是**时序**：`consult_experts` 从"跑完才返回"变成"派发即返回"。所以断言也
跟着从「返回值里有专家意见」改成「返回值是派出回执 + 后台真的跑完了、审计与状态都落了」。
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest

import deskmate.consult as consult_mod
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


# ---------------------------------------------------------------------------
# 进度（2026-11-17）：状态条不能永远写着「正在读《夜不收》」
# ---------------------------------------------------------------------------


def _seed_running(run_id: str) -> threading.Event:
    """造一条"正在跑"的任务，并给它配一个真的活线程。

    必须配活线程：`_mutate` 每次都会 reap 僵尸，running 但 `_ACTIVE` 里没有活线程的
    记录会被立刻标成 interrupted——这本身是本仓「不许静默说谎」的设计，测试要顺着它走。
    """
    import json
    import time as _time

    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    consult_mod._ACTIVE[run_id] = thread
    path = Path(os.environ["DESKMATE_CONSULT_RUNS"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "id": run_id,
                        "session_id": "sess-p",
                        "experts": ["character"],
                        "expert_names": ["人物专家"],
                        "book": "夜不收",
                        "status": "running",
                        "started_at": _time.time(),
                        "results": [],
                        "delivered": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return stop


def test_reading_progress_is_recorded_and_broadcast(tmp_path: Path) -> None:
    """读稿的每一步都要落成事实：在跑什么、哪一章、第几次、累计多少字。"""
    import json

    run_id = "c-progress-1"
    stop = _seed_running(run_id)
    seen_busy: list[str] = []
    events: list[str] = []
    set_notifier(lambda kind, _payload: events.append(kind))

    def read_book(title: str, chapter: str = "") -> str:
        """假读稿工具（名字必须叫 read_book 才会被记账）。"""
        # 进入函数时，状态文件里应该已经写着「正在读这一章」
        state = json.loads(Path(os.environ["DESKMATE_CONSULT_RUNS"]).read_text(encoding="utf-8"))
        seen_busy.append(state["runs"][0]["progress"]["character"]["busy"])
        return f"读到{chapter}的正文"

    expect_text = "读到第十一章的正文"

    try:
        wrapped = consult_mod._instrument_reading(
            read_book, run_id=run_id, expert_id="character"
        )
        out = wrapped("夜不收", chapter="第十一章")
        # 正文照旧，但开头多一行预算（模型据此知道还剩多少时间、读了几次）
        assert expect_text in out
        assert out.startswith("【读稿预算】"), "预算行必须在最前面，塞末尾会被长正文淹掉"
        assert "这是第 1 次读稿" in out
        # ② 读完后：章节、次数、字数、busy 清空
        # （必须在停线程之前读：线程一停，reap 就会把 running 标成 interrupted）
        progress = consult_mod.read_runs(session_id="sess-p")["active"][0]["progress"]["character"]
    finally:
        stop.set()

    # ① 读的当下：busy=read_book（不是等读完才补记）
    assert seen_busy == ["read_book"]
    assert progress["busy"] == ""
    assert progress["chapter"] == "第十一章"
    assert progress["reads"] == 1
    assert progress["chars"] == len(expect_text)
    # ③ 进度也走通知（前端靠它实时刷新，不必轮询）
    assert "progress" in events


def test_instrumented_tool_keeps_its_schema(tmp_path: Path) -> None:
    """包装不能改签名/文档：注册表靠它生成工具描述，模型据此决定怎么调。"""
    import json

    from coworker.tools.registry import _schema_for
    from deskmate.bots.reading import make_reading_tools

    shelf = _shelf(tmp_path)
    for fn in make_reading_tools(shelf):
        raw = _schema_for(fn)
        wrapped = _schema_for(
            consult_mod._instrument_reading(fn, run_id="c-any", expert_id="character")
        )
        assert json.dumps(raw, sort_keys=True, ensure_ascii=False) == json.dumps(
            wrapped, sort_keys=True, ensure_ascii=False
        ), fn.__name__


# ---------------------------------------------------------------------------
# 读稿硬闸（2026-11-17）：两次实盘超时都是「书读完了还在读，从不落笔」
# ---------------------------------------------------------------------------

def _seed_reading_state(
    run_id: str, *, reads: int, elapsed: float, budget: int = 900
) -> threading.Event:
    """造一条已读 N 次、已跑了 elapsed 秒的 running 任务。"""
    import json
    import time as _time

    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    consult_mod._ACTIVE[run_id] = thread
    path = Path(os.environ["DESKMATE_CONSULT_RUNS"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "runs": [
                    {
                        "id": run_id,
                        "session_id": "sess-cap",
                        "experts": ["character"],
                        "expert_names": ["人物专家"],
                        "book": "夜不收",
                        "status": "running",
                        "started_at": _time.time() - elapsed,
                        "timeout": budget,
                        "progress": {
                            "character": {
                                "busy": "",
                                "chapter": "",
                                "reads": reads,
                                "chars": reads * 20_000,
                            }
                        },
                        "results": [],
                        "delivered": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return stop


def test_read_tool_refuses_after_call_cap() -> None:
    """读够次数就断供：不真的去读，只回一句「现在必须开始交付」。

    实测一本 15 章 / 13.7 万字的书读 4 次就覆盖全书；第 9 次还在读的那两次实盘，
    最后交出的都是一句「我要再补读第 X 到 Y 章」——零字正文。
    """
    run_id = "c-cap-1"
    stop = _seed_reading_state(
        run_id, reads=consult_mod._READ_CALLS_CAP_DEFAULT, elapsed=60
    )
    real_calls: list[str] = []

    def read_book(title: str, chapter: str = "") -> str:
        real_calls.append(chapter)
        return "正文"

    try:
        wrapped = consult_mod._instrument_reading(
            read_book, run_id=run_id, expert_id="character"
        )
        out = wrapped("夜不收", chapter="第十二章")
    finally:
        stop.set()

    assert real_calls == [], "到顶后不许真的去读书"
    assert "现在必须开始交付" in out
    assert "次数已达上限" in out


def test_read_tool_refuses_when_time_runs_out() -> None:
    """剩余预算不足 35% 时断供——把最后那段时间强制留给「写」。"""
    run_id = "c-time-1"
    stop = _seed_reading_state(run_id, reads=1, elapsed=700)  # 700/900，剩 22%
    real_calls: list[str] = []

    def read_book(title: str, chapter: str = "") -> str:
        real_calls.append(chapter)
        return "正文"

    try:
        wrapped = consult_mod._instrument_reading(
            read_book, run_id=run_id, expert_id="character"
        )
        out = wrapped("夜不收", chapter="第十一章")
    finally:
        stop.set()

    assert real_calls == [], "时间见底后不许再读"
    assert "读稿时间段已用完" in out


def test_read_tool_head_shows_budget() -> None:
    """还没到极值时照常供料，但开头必须带「已用/剩余/第几次」。"""
    run_id = "c-head-1"
    stop = _seed_reading_state(run_id, reads=2, elapsed=100)

    def read_book(title: str, chapter: str = "") -> str:
        """假读稿：函数名必须是 read_book，包装只认这个名字。"""
        return "正文内容"

    try:
        wrapped = consult_mod._instrument_reading(
            read_book, run_id=run_id, expert_id="character"
        )
        out = wrapped("夜不收", chapter="第五章到第九章")
    finally:
        stop.set()

    assert out.startswith("【读稿预算】")
    assert "这是第 3 次读稿" in out, "读之前计数，所以是第 3 次"
    assert "累计 40,000 字" in out
    assert out.endswith("正文内容")
