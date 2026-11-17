"""专家结果回投（2026-11-17，C 方案）：后台跑完要把原文送回小桔的会话。

这条链路是这次改造的核心——没它，后台任务就只是「派出去、没有下文」。
钉四件事：
① 结果能投进会话（她的 messages 里真的出现了专家原文）；
② 投递是幂等的（第二次抢不到投递权，不会把同一份结果说两遍）；
③ 会话还没有引擎时不硬投（留给 tick 兜底，不丢也不重复）；
④ 投递带的 source 是 deskmate 而不是用户气泡（用户没说过这句话）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deskmate.bots.reading import BookShelf
from deskmate.consult import make_consult_tool, pending_delivery, wait_for_jobs


class _ScriptedProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="人物专家看过之后，我这样看……")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete(model=model, messages=messages, tools=tools, **settings))


def _shelf(tmp_path: Path) -> BookShelf:
    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    (book / "正文.txt").write_text("第一章：开头。", encoding="utf-8")
    return BookShelf(tmp_path / "books")


@pytest.fixture
def prepared(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DESKMATE_CONSULT_RUNS", str(tmp_path / "consult_runs.json"))
    monkeypatch.setenv("DESKMATE_CONSULT_DIR", str(tmp_path / "audit"))
    tool = make_consult_tool(
        provider=_ScriptedProvider(),
        model="scripted",
        shelf=_shelf(tmp_path),
        audit_dir=tmp_path / "audit",
        session_id="sess-d",
    )
    out = tool(experts=["character"], brief="这个人的行为是否合理？请给依据。", book="测试书")
    assert "已派出" in out
    assert wait_for_jobs(timeout=60)
    yield tmp_path
    wait_for_jobs(timeout=10)


def test_result_is_delivered_into_session_once(prepared, monkeypatch):
    from coworker.server.manager import SessionManager

    mgr = SessionManager(
        data_dir=prepared / "data", provider=_ScriptedProvider(), model="scripted"
    )
    engine = mgr.get_engine("sess-d", agent="deskmate")
    assert engine is not None

    pending = pending_delivery("sess-d")
    assert pending, "后台完成的结果没有进入待投递队列"

    first = asyncio.run(mgr._deliver_consult_result(pending[0]))
    assert first is True

    # ① 她真的看到了专家原文（下一轮她会据此汇报）
    joined = "\n".join(str(m.get("content") or "") for m in engine.messages)
    assert "【专家任务结束】" in joined
    assert "人物专家" in joined
    assert "人物专家看过之后" in joined
    # ④ 是「专家回投」而不是用户气泡（前端据此渲染成卡片而非用户消息）
    sources = [m.get("source") for m in engine.messages if isinstance(m.get("source"), dict)]
    assert any(s.get("connector") == "deskmate" for s in sources), "投递消息缺少 deskmate source"

    # ② 幂等：同一份结果不会被投第二遍
    assert pending_delivery("sess-d") == []
    assert asyncio.run(mgr._deliver_consult_result(pending[0])) is False


def test_no_engine_means_no_delivery(prepared):
    """会话没有引擎时不能把结果标成「已投」——那等于把它吞了。"""
    from coworker.server.manager import SessionManager

    mgr = SessionManager(
        data_dir=prepared / "data2", provider=_ScriptedProvider(), model="scripted"
    )
    pending = pending_delivery("sess-d")
    assert pending
    assert asyncio.run(mgr._deliver_consult_result(pending[0])) is False
    assert pending_delivery("sess-d"), "没有引擎却被标成已投递，结果就永久丢了"
