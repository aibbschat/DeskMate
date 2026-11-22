"""Scripted end-to-end tests for _run_orchestrated_automation (2026-11-22).

Locks the contracts fixed in the concurrency review:

- the orchestrated path owns its session claim AND cleanup (control marks +
  mark_idle) — it returns straight to the caller, skipping the legacy finally;
- a publish failure keeps the produce draft (the whole point of checkpointing);
- the idempotent publish side effect is committed exactly once;
- skipped runs checkpoint as SKIPPED/CANCELED, not FAILED.

No network, no LLM: the engine is scripted per step, delivery runs against a
tmp ledger, after-action reflection is stubbed (same convention as
tests/test_autonomy_goal.py).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from coworker.automation import Schedule, ScheduledTask, TaskRun
from coworker.orchestration import JobStatus, StepStatus


def _task(ws, **kw) -> ScheduledTask:
    kw.setdefault("notify_on_completion", False)
    return ScheduledTask(
        title="产出并发布",
        instructions="写一份草稿",
        schedule=Schedule(kind="cron", cron="0 9 * * *"),
        workspace=str(ws),
        orchestration={
            "mode": "produce_publish",
            "publish_prompt": "把草稿发布出去：{{produce_result}}",
        },
        **kw,
    )


def _wired(tmp_path, monkeypatch, responses):
    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action
    from coworker.automation.store import TaskStore
    from coworker.orchestration import OrchestrationStore
    from coworker.server.manager import SessionManager

    ws = tmp_path / "ws"
    ws.mkdir()

    class Engine:
        def __init__(self, session_id):
            self.session_id = session_id
            self.messages: list[dict] = []

        async def run(self, opening):
            step = self.session_id.rsplit(":", 1)[-1]
            resp = responses.get(step, "")
            if isinstance(resp, Exception):
                raise resp
            if step == "produce":
                (ws / "draft.md").write_text(str(resp), encoding="utf-8")
            self.messages = [{"role": "assistant", "content": str(resp)}]
            yield SimpleNamespace(type="done")

    monkeypatch.setattr(
        SessionManager,
        "_build_task_engine",
        lambda self, task, *, session_id: Engine(session_id),
    )
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)
    delivered: list[str] = []
    original = delivery_mod.deliver_run_result

    def counting_deliver(task, run, *, base_dir=None):
        delivered.append(run.run_id)
        return original(task, run, base_dir=tmp_path)

    monkeypatch.setattr(delivery_mod, "deliver_run_result", counting_deliver)

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr.orchestration_store = OrchestrationStore(tmp_path / "orch.db")
    mgr._engines = {}
    mgr._engine_touched = {}
    mgr._running_sessions = set()
    mgr._foreground_sessions = set()
    mgr._promotion_rebuild = set()
    mgr._loop = None
    mgr.teams = SimpleNamespace(
        for_lead_session=lambda sid: None, for_worker_session=lambda sid: None
    )
    mgr._maybe_autotitle = lambda sid: None
    events: list[dict] = []

    async def fake_broadcast(payload):
        events.append(payload)

    mgr.broadcast_event = fake_broadcast
    return mgr, ws, delivered, events


def test_orchestrated_run_produce_publish_ok(tmp_path, monkeypatch):
    mgr, ws, delivered, events = _wired(
        tmp_path, monkeypatch, {"produce": "草稿内容", "publish": "已发布"}
    )
    task = _task(ws)
    mgr.task_store.save(task)

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))

    assert run.status == "ok"
    assert run.result_text == "已发布"
    assert "draft.md" in run.artifacts
    assert run.delivered_at is not None
    assert delivered == [run.run_id]
    # Cleanup owned by this path: session released, control mark popped.
    assert run.session_id not in mgr._running_sessions
    assert run.run_id not in mgr._control_marks()

    job = mgr.orchestration_store.get_job(
        f"automation-pipeline:{task.id}:{run.run_id}"
    )
    assert job is not None and job.status is JobStatus.OK
    assert job.runs["produce"].status is StepStatus.OK
    assert job.runs["publish"].status is StepStatus.OK
    side = mgr.orchestration_store.get_side_effect(f"publish:{task.id}:{run.run_id}")
    assert side is not None and side["status"] == "ok"

    # The run-level checkpoint (automation:{task}:{run}) marks deliver OK too.
    cp = mgr.orchestration_store.get_job(f"automation:{task.id}:{run.run_id}")
    assert cp is not None
    assert cp.runs["deliver"].status is StepStatus.OK
    assert "automation_run_started" in [e["type"] for e in events]


def test_publish_failure_keeps_produce_draft(tmp_path, monkeypatch):
    mgr, ws, delivered, events = _wired(
        tmp_path,
        monkeypatch,
        {"produce": "草稿内容", "publish": RuntimeError("publish boom")},
    )
    task = _task(ws)
    mgr.task_store.save(task)

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))

    assert run.status == "error"
    assert "publish boom" in (run.error or "")
    # The draft must survive the publish failure — no re-run of produce.
    assert run.result_text == "草稿内容"
    assert "draft.md" in run.artifacts
    assert run.session_id not in mgr._running_sessions
    assert run.run_id not in mgr._control_marks()

    job = mgr.orchestration_store.get_job(
        f"automation-pipeline:{task.id}:{run.run_id}"
    )
    assert job is not None and job.status is JobStatus.FAILED
    assert job.runs["produce"].status is StepStatus.OK
    assert job.runs["publish"].status is StepStatus.FAILED
    # Default publish retries=2 → three attempts total, then FAILED.
    assert job.runs["publish"].attempt == 3


def test_busy_session_yields_as_skipped(tmp_path, monkeypatch):
    mgr, ws, delivered, events = _wired(
        tmp_path, monkeypatch, {"produce": "x", "publish": "y"}
    )
    task = _task(ws)
    mgr.task_store.save(task)
    mgr.try_mark_running = lambda sid: False

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))

    assert run.status == "skipped"
    assert delivered == []
    assert mgr.orchestration_store.list_jobs(source="automation") == []


def test_checkpoint_maps_skipped_run_to_skipped_not_failed(tmp_path, monkeypatch):
    mgr, ws, delivered, events = _wired(tmp_path, monkeypatch, {})
    task = _task(ws)
    run = TaskRun(task_id=task.id, status="skipped")

    mgr._checkpoint_automation_run(task, run, phase="produce")

    job = mgr.orchestration_store.get_job(f"automation:{task.id}:{run.run_id}")
    assert job is not None
    assert job.runs["produce"].status is StepStatus.SKIPPED
    assert job.runs["deliver"].status is StepStatus.SKIPPED
    assert job.status is JobStatus.CANCELED
