"""Contract tests for the generic background sub-task delegation tool.

Goal (owner ruling 2026-11-22): an agent, in the middle of a long task, judges
"this chunk is independent / I'm not confident / there are too many things" and
hands a self-contained sub-task to a read-only subagent running in the background;
when it finishes the result is delivered back into the SAME session as a NEW
message (the proven consult delivery shape), so the agent can keep going.

These tests pin the shape WITHOUT a live LLM: the host is a real JobHost over a
temp SQLite store; the subagent handler is injected. Type-safety and idempotency
contracts are asserted directly.
"""

from __future__ import annotations

import os
import time

import pytest

from coworker.orchestration import ConcurrencyLeases, JobHost, OrchestrationStore


def _host(tmp_path):
    store = OrchestrationStore(str(tmp_path / "delegation.db"))
    events: list[tuple] = []
    host = JobHost(
        store,
        notifier=lambda kind, payload: events.append((kind, payload)),
        leases=ConcurrencyLeases(),
    )
    return host, store, events


def test_delegate_dispatches_background_and_returns_receipt(tmp_path):
    """delegate_task returns immediately; the job runs on the host in background."""
    from deskmate.delegation import delegate_builder

    host, store, events = _host(tmp_path)
    seen: list[str] = []

    builder = delegate_builder(
        host=host,
        run_subtask=lambda task, job_id: seen.append(task) or f"report: {task}",
        session_id="s-main",
    )
    tool = builder()
    receipt = tool(task="查这三处引文是否见于正文")
    assert "已派出" in receipt

    # The tool must return before the handler necessarily finished; poll briefly.
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen == ["查这三处引文是否见于正文"]
    host.wait(timeout=5)
    jobs = store.list_jobs(limit=10)
    assert len(jobs) == 1
    assert jobs[0].source == "delegation"
    assert jobs[0].status.value == "ok"
    assert any(kind == "started" for kind, _ in events)
    assert any(kind == "finished" for kind, _ in events)


def test_delegate_result_is_delivered_back_as_new_message(tmp_path):
    """Finished sub-task result is delivered to the originating session."""
    from deskmate.delegation import delegate_builder

    host, _, _ = _host(tmp_path)
    delivered: list[tuple] = []

    builder = delegate_builder(
        host=host,
        run_subtask=lambda task, job_id: {"conclusion": "引文 1 未见原句"},
        session_id="s-main",
        deliver=lambda sid, message, job_id: delivered.append((sid, message, job_id)),
    )
    builder()(task="核对这三处引文是否见于正文")
    host.wait(timeout=5)

    assert len(delivered) == 1
    sid, message, job_id = delivered[0]
    assert sid == "s-main"
    assert "引文 1 未见原句" in message
    assert job_id


def test_delegate_result_delivery_is_idempotent(tmp_path):
    """A replayed 'finished' notification must not deliver the same result twice."""
    from deskmate.delegation import delegate_builder

    host, store, _ = _host(tmp_path)
    delivered: list[tuple] = []
    builder = delegate_builder(
        host=host,
        run_subtask=lambda task, job_id: "answer",
        session_id="s-main",
        deliver=lambda sid, message, job_id: delivered.append((sid, message, job_id)),
    )
    builder()(task="核对引文是否见于正文原始稿件")
    host.wait(timeout=5)
    first = len(delivered)
    assert first == 1

    # Simulate a duplicate notification (e.g. tick fallback replay) with the same job.
    job = store.list_jobs(limit=10)[0]
    from deskmate.delegation import _make_result_notifier

    notify = _make_result_notifier(
        store, lambda sid, msg, jid: delivered.append((sid, msg, jid)), session_id="s-main"
    )
    notify("finished", {"id": job.id})
    notify("finished", {"id": job.id})
    assert len(delivered) == first  # deduped via delivered marker


def test_delegate_rejects_short_task_and_empty(tmp_path):
    """A vague/empty sub-task is rejected up front (garbage in, garbage out)."""
    from deskmate.delegation import delegate_builder

    host, _, _ = _host(tmp_path)
    tool = delegate_builder(host=host, run_subtask=lambda t, j: "", session_id="s")()
    assert "太短" in tool(task="x")
    assert "任务" in tool(task="   ")


def test_delegate_dedups_same_task_while_running(tmp_path):
    """The same sub-task must not be dispatched twice while one is in flight."""
    from deskmate.delegation import delegate_builder

    host, store, _ = _host(tmp_path)
    entered = []

    def slow(task, job_id):
        entered.append(job_id)
        time.sleep(0.3)
        return "done"

    tool = delegate_builder(
        host=host, run_subtask=slow, session_id="s"
    )()
    r1 = tool(task="同一件独立的事，别重复派")
    r2 = tool(task="同一件独立的事，别重复派")
    assert "已派出" in r1
    assert "重复" in r2 or "已经在" in r2 or "别再" in r2
    host.wait(timeout=5)
    assert len(entered) == 1
    assert len(store.list_jobs(limit=10)) == 1


def test_build_delegate_engine_is_read_only(tmp_path):
    """The background subagent engine has NO write/shell/ask_user tools."""
    from coworker.permissions import Mode
    from deskmate.delegation import build_delegate_engine

    engine = build_delegate_engine(
        provider=object(),
        model="fake-model",
        workspace=str(tmp_path),
    )
    names = set(engine.registry.names())
    assert "ask_user" not in names
    assert "write_file" not in names
    assert "run_shell" not in names
    assert "save_skill" not in names
    assert engine.permissions.mode == Mode.PLAN


def test_delegate_multiple_sessions_route_by_job_meta(tmp_path):
    """Two sessions sharing one host must each get their own result back."""
    from deskmate.delegation import delegate_builder

    host, _, _ = _host(tmp_path)
    delivered: list[tuple] = []
    deliver = lambda sid, msg, jid: delivered.append((sid, jid))

    # Build for session A, then for session B — both share the same host.
    tool_a = delegate_builder(
        host=host, run_subtask=lambda t, j: "A-result", session_id="sess-A", deliver=deliver
    )()
    tool_b = delegate_builder(
        host=host, run_subtask=lambda t, j: "B-result", session_id="sess-B", deliver=deliver
    )()
    tool_a(task="会话甲派的一段独立查证工作")
    tool_b(task="会话乙派的另一段独立查证工作")
    host.wait(timeout=5)

    assert len(delivered) == 2
    routes = dict(delivered)
    jobs = {j.meta.get("session_id"): j.id for j in host.store.list_jobs(limit=10)}
    assert routes["sess-A"] == jobs["sess-A"]
    assert routes["sess-B"] == jobs["sess-B"]


def test_build_engine_registers_delegate_when_wired(tmp_path):
    """build_engine registers delegate_task only when host+deliver are injected."""
    from coworker.agent import build_engine
    from coworker.agents.code import code_agent

    host, _, _ = _host(tmp_path)
    calls = []
    engine = build_engine(
        agent=code_agent(),
        workspace=str(tmp_path),
        model="fake",
        provider=object(),
        session_id="s-test",
        orchestration_host=host,
        delegate_deliver=lambda *a: calls.append(a),
    )
    assert "delegate_task" in engine.registry.names()
    # Without injection, no delegate tool (CLI/back-compat).
    engine2 = build_engine(
        agent=code_agent(), workspace=str(tmp_path), model="fake", provider=object()
    )
    assert "delegate_task" not in engine2.registry.names()


def test_collect_subtask_report_drives_async_generator(tmp_path):
    """collect_subtask_report drives an async-generator engine off-loop."""
    from coworker.events import Event, EventType
    from deskmate.delegation import collect_subtask_report

    class FakeEngine:
        async def run(self, task):
            yield Event(EventType.ASSISTANT_MESSAGE, {"text": "第一版"})
            yield Event(EventType.ASSISTANT_MESSAGE, {"text": "结论：未见原句"})
            yield Event(EventType.TURN_END, {"status": "completed"})

    out = collect_subtask_report(FakeEngine(), "核引文")
    assert out["report"] == "结论：未见原句"
    assert "note" not in out


def test_collect_subtask_report_captures_error(tmp_path):
    """A child ERROR event becomes a structured error, not an empty success."""
    from coworker.events import Event, EventType
    from deskmate.delegation import collect_subtask_report

    class ErrEngine:
        async def run(self, task):
            yield Event(EventType.ERROR, {"error": "provider down"})

    out = collect_subtask_report(ErrEngine(), "x")
    assert out == {"error": "provider down"}
