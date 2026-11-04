"""第四刀：幽灵 run 对账 + 挂起后重启的续跑也进 TaskRun（先红后绿）。

三类「账本说不清」的洞：
1. kill -9 / 优雅退出时 run 的 finally 来不及改终态，留下 status=running 的幽灵行；
2. 这种行 cancel_run 会回「已结束」，停不掉；
3. 挂起等人答时进程重启，用户答了 → durable resume 把 turn 续上，但那一轮不在
   TaskRun 里，干完活结果静默丢。

测试库一律 tmp_path。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

from coworker.automation.models import Schedule, ScheduledTask, TaskRun


def _real_task(**kw) -> ScheduledTask:
    base = dict(
        title="重启续跑验收",
        instructions="盯住目标",
        schedule=Schedule(kind="cron", cron="*/10 * * * *"),
        workspace="/tmp/cw-autonomy-reconcile",
        agent="cowork",
        goal_mode="watch",
        notify_on_completion=False,
        notify_target=None,
    )
    base.update(kw)
    return ScheduledTask(**base)


def _ledger_rows(base_dir):
    db = base_dir / "deskmate.db"
    if not db.is_file():
        return []
    with sqlite3.connect(str(db)) as c:
        try:
            return c.execute("SELECT id, kind, content FROM proactive_ledger").fetchall()
        except sqlite3.OperationalError:
            return []


class ScriptedEngine:
    def __init__(self, steps, openings, *, resume_text=""):
        self._steps = steps
        self._openings = openings
        self._resume_text = resume_text
        self.messages: list[dict] = []
        self.resumed = 0

    async def run(self, opening):
        self._openings.append(opening)
        step = self._steps.pop(0) if self._steps else {"text": ""}
        self.messages.append({"role": "assistant", "content": step.get("text", "")})
        yield SimpleNamespace(
            type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 1}
        )

    async def resume(self):
        self.resumed += 1
        self.messages.append({"role": "assistant", "content": self._resume_text})
        yield SimpleNamespace(
            type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 0}
        )


def _wired(tmp_path, monkeypatch, steps, openings, resume_text=""):
    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action
    from coworker.automation.store import TaskStore
    from coworker.server.manager import SessionManager

    original = delivery_mod.deliver_run_result
    calls: list[str] = []

    def counting_deliver(task, run, *, base_dir=None):
        calls.append(run.run_id)
        return original(task, run, base_dir=tmp_path)

    monkeypatch.setattr(delivery_mod, "deliver_run_result", counting_deliver)
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)
    monkeypatch.setattr(
        SessionManager,
        "_build_task_engine",
        lambda self, task, *, session_id, messages=None: ScriptedEngine(
            steps, openings, resume_text=resume_text
        ),
    )
    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    mgr._running_sessions = set()
    mgr.save = lambda sid, engine: None
    # 替身不需要真实的会话客户端/自动标题钩子
    mgr.mark_running = lambda sid: mgr._running_sessions.add(sid)
    mgr.mark_idle = lambda sid: mgr._running_sessions.discard(sid)
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast
    return mgr, pushed, calls


# -- 1. 启动对账 ---------------------------------------------------------------
def test_reconcile_marks_orphan_running_runs_interrupted(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings)
    task = _real_task(workspace=str(tmp_path))
    mgr.task_store.save(task)
    orphan = TaskRun(task_id=task.id, status="running")
    mgr.task_store.add_run(orphan)

    fixed = mgr.reconcile_task_runs()
    assert fixed == 1
    stored = mgr.task_store.find_run(orphan.run_id)
    assert stored.status == "interrupted", "幽灵 run 必须落终态，否则永远显示在跑"
    assert stored.finished_at is not None
    assert stored.checkpoint.get("interrupted") is True
    assert mgr.reconcile_task_runs() == 0, "对账必须幂等"


def test_reconcile_skips_runs_with_live_engine(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings)
    task = _real_task(workspace=str(tmp_path))
    mgr.task_store.save(task)
    live = TaskRun(task_id=task.id, status="running")
    mgr.task_store.add_run(live)
    mgr._engines[live.session_id] = SimpleNamespace()

    assert mgr.reconcile_task_runs() == 0
    assert mgr.task_store.find_run(live.run_id).status == "running"


def test_reconcile_keeps_sleeping_runs(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings)
    task = _real_task(workspace=str(tmp_path))
    mgr.task_store.save(task)
    sleeping = TaskRun(
        task_id=task.id,
        status="sleeping",
        finished_at=time.time(),
        checkpoint={"goal": {"sleeping": True, "wake_id": "w1"}},
    )
    mgr.task_store.add_run(sleeping)
    assert mgr.reconcile_task_runs() == 0, "睡眠是合法中间态，别动它"


# -- 2. 僵尸 run 也要能叫停 ---------------------------------------------------
def test_cancel_zombie_run_lands_terminal_state(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings)
    task = _real_task(workspace=str(tmp_path))
    mgr.task_store.save(task)
    zombie = TaskRun(task_id=task.id, status="running", finished_at=time.time())
    mgr.task_store.add_run(zombie)

    out = mgr.cancel_run(task.id, zombie.run_id)
    assert out["ok"] is True
    stored = mgr.task_store.find_run(zombie.run_id)
    assert stored.status == "canceled"
    assert stored.cancel_requested is True


# -- 3. 挂起后重启的续跑必须进 TaskRun ----------------------------------------
def test_durable_resume_of_task_run_is_wrapped(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings, resume_text="答疑后继续：目标变了，报告")
    task = _real_task(workspace=str(tmp_path))
    mgr.task_store.save(task)
    interrupted = TaskRun(
        task_id=task.id,
        status="interrupted",
        finished_at=time.time(),
        checkpoint={"goal": {"state": "hash=abc", "done": False, "rounds": 1}},
    )
    mgr.task_store.add_run(interrupted)
    item = SimpleNamespace(session_id=interrupted.session_id, tool_call_id="c1")

    asyncio.run(mgr._durable_resume(item))

    runs = mgr.task_store.runs(task.id)
    resume_runs = [r for r in runs if r.trigger == "resume"]
    assert resume_runs, "重启续跑没包成 TaskRun —— 干完活没人知道"
    r = resume_runs[0]
    assert r.session_id == interrupted.session_id
    assert r.delivered_at is not None, "续跑的结果必须送达"
    assert r.checkpoint["goal"]["done"] is True
    assert len(_ledger_rows(tmp_path)) == 1


def test_durable_resume_plain_session_unchanged(tmp_path, monkeypatch):
    tasks = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, tasks, openings)
    plain_engine = ScriptedEngine([], [], resume_text="普通会话续跑")
    mgr.get_engine = lambda sid, **kw: plain_engine
    item = SimpleNamespace(session_id="session-plain-1", tool_call_id="c1")

    asyncio.run(mgr._durable_resume(item))
    assert plain_engine.resumed == 1
    assert mgr.task_store.list() == [], "普通会话不该被包成任务 run"
