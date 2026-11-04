"""自我驱动第一刀：让她自己安排下一次醒来，醒来的一轮仍在任务账本里（先红后绿）。

此前定时任务里 selfwake 四工具根本没注册（`_build_task_engine` 不传 wake_store），
节奏只能由创建时的 cron 决定；而就算接上，wake 醒来走的是 `deliver_to_session`，
那一轮不在任何 TaskRun 里——checkpoint 不更新、不投递、不结案，结果会静默丢失。

本文件锁住五件事：
1. 目标制任务的引擎注册 sleep_for/sleep_until/wake_on/wake_on_event；普通任务不注册（回归）；
2. 本轮自己登记了 wake → run.status=sleeping、不投递、不结案、保留引擎；
3. 有 pending wake 时，cron 再触发要 skipped，不能开第二条腿；
4. wake 到期 → 复用同一 session 续跑新的 TaskRun，attempts 继承，变化时正常送达结案；
5. wake 丢了（重启/被清）→ cron 兜底续跑，不能永远 skipped；暂停任务时取消睡眠。

测试库一律 tmp_path。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from coworker.automation.models import Schedule, ScheduledTask, TaskRun
from coworker.selfwake import WakeStore


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-sw-1",
        title="自己排节奏的盯梢",
        instructions="盯住目标；需要等待时自己安排下一次检查",
        workspace="/tmp/cw-autonomy-sw",
        agent="cowork",
        notify_on_completion=False,
        notify_target=None,
        goal_mode="watch",
    )
    base.update(kw)
    return SimpleNamespace(**base)


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
    """最小引擎：按脚本吐文本；sleep 步骤会真的往 WakeStore 里挂一个定时唤醒。"""

    def __init__(self, steps, openings, *, session_id="", wakes=None):
        self._steps = steps
        self._openings = openings
        self.session_id = session_id
        self._wakes = wakes
        self.messages: list[dict] = []

    async def run(self, opening):
        self._openings.append(opening)
        step = self._steps.pop(0) if self._steps else {"text": ""}
        if step.get("sleep"):
            self._wakes.add_timer(
                self.session_id,
                datetime.now(timezone.utc) + timedelta(seconds=int(step["sleep"])),
                note="self-paced recheck",
            )
            self.messages.append({"role": "assistant", "content": ""})
            yield SimpleNamespace(
                type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 1}
            )
            return
        self.messages.append({"role": "assistant", "content": step.get("text", "")})
        yield SimpleNamespace(
            type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 1}
        )


def _wired(tmp_path, monkeypatch, steps, openings, wakes):
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
            steps, openings, session_id=session_id, wakes=wakes
        ),
    )
    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr.wakes = wakes
    mgr._engines = {}
    mgr._running_sessions = set()
    mgr.save = lambda sid, engine: None
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast
    return mgr, pushed, calls


# -- 1. 工具面：目标制任务才有自唤醒 -------------------------------------------
def test_goal_task_engine_registers_selfwake_tools(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(data_dir=tmp_path / "data")

    def _real(goal_mode: str) -> ScheduledTask:
        return ScheduledTask(
            title="自己排节奏",
            instructions="盯住目标",
            schedule=Schedule(kind="cron", cron="*/10 * * * *"),
            workspace=str(ws),
            agent="cowork",
            goal_mode=goal_mode,
        )

    engine = mgr._build_task_engine(_real("watch"), session_id="__run__x")
    names = set(engine.registry.names())
    for tool in ("sleep_for", "sleep_until", "wake_on", "wake_on_event"):
        assert tool in names, f"目标制任务没注册 {tool}，她没法自己安排节奏"

    plain_engine = mgr._build_task_engine(_real(""), session_id="__run__y")
    plain_names = set(plain_engine.registry.names())
    assert "sleep_for" not in plain_names, "普通任务行为不能变"


# -- 2. 自己睡了：不投递、不结案、保留引擎 --------------------------------------
def test_sleeping_run_skips_delivery_and_stays_open(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = [{"sleep": 600}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    task = _task(workspace=str(tmp_path))

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert run.status == "sleeping", "自己安排了唤醒的轮次不能算完成"
    assert run.delivered_at is None, "中间轮不该投递"
    goal = run.checkpoint["goal"]
    assert goal.get("done") is False
    assert goal.get("sleeping") is True
    assert goal.get("wake_id"), "没把 wake_id 记进检查点，重启后接不上"
    assert calls == [] and _ledger_rows(tmp_path) == []
    assert run.session_id in mgr._engines, "引擎要留着，醒来才有上下文"
    assert len(wakes.pending(run.session_id)) == 1


# -- 3. cron 不能再开第二条腿 ---------------------------------------------------
def test_cron_tick_skips_while_selfsleeping(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = [{"sleep": 600}, {"text": "这一轮不该被跑到"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    task = _task(workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "sleeping"
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r2.status == "skipped"
    assert len(openings) == 1, "睡眠中 cron 不该再起引擎"
    assert "sleeping" in json.dumps(r2.checkpoint, ensure_ascii=False)


# -- 4. 醒来：同一 session 续跑新的 TaskRun -------------------------------------
def test_wake_resumes_same_session_and_keeps_attempts(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = [{"sleep": 600}, {"text": "读到 READY 了，报告变化"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    task = _task(workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "sleeping"
    wake = wakes.pending(r1.session_id)[0]

    r2 = asyncio.run(
        mgr._run_scheduled_task(task, "wake", session_id=r1.session_id, wake=wake)
    )
    assert r2.session_id == r1.session_id, "醒来必须复用同一会话，否则跨轮上下文又断了"
    assert r2.trigger == "wake"
    assert r2.status == "ok"
    assert r2.delivered_at is not None and r2.checkpoint["goal"]["done"] is True
    assert len(openings) == 2
    assert "wake" in openings[1].lower() or "醒来" in openings[1] or "timer" in openings[1].lower()
    attempts = r2.checkpoint["goal"]["attempts"]
    assert attempts, "醒来那轮应继承尝试链"
    assert any(a.get("outcome") == "sleeping" for a in attempts), "睡眠轮要留痕"
    assert r2.checkpoint["goal"].get("sleeping") is False


# -- 5. wake 丢了 → cron 兜底；暂停 → 取消睡眠 ----------------------------------
def test_cron_falls_back_when_wake_is_gone(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = [{"sleep": 600}, {"text": "wake 丢了，cron 接着跑"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    task = _task(workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    for w in wakes.pending(r1.session_id):
        wakes.mark_fired(w.id)
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r2.status == "ok", "wake 丢了必须能靠 cron 续上，不能永远 skipped"
    assert len(openings) == 2


def test_disabled_task_wake_is_cancelled(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = [{"sleep": 600}, {"text": "暂停了不该跑"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    task = _task(workspace=str(tmp_path), enabled=False)
    # _wired 用 SimpleNamespace 构造任务，这里手工把它绑到 run 会话上，验证暂停分支。
    mgr.task_store.task_for_run_session = lambda sid, _t=task: _t

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    wake = wakes.pending(r1.session_id)[0]
    asyncio.run(mgr._resume_wake(wake))
    assert len(openings) == 1, "任务已暂停，wake 不该再起一轮"
    assert wakes.pending(r1.session_id) == [], "暂停时要把睡眠取消掉"


# -- 6. 普通会话的 wake 仍走 deliver_to_session（回归） --------------------------
def test_plain_session_wake_still_uses_deliver(tmp_path, monkeypatch):
    wakes = WakeStore(tmp_path / "wakes.json")
    steps = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings, wakes)
    delivered: list[str] = []

    async def fake_deliver(session_id, message, **kw):
        delivered.append(session_id)

    mgr.deliver_to_session = fake_deliver
    wake = wakes.add_timer(
        "session-normal-1", datetime.now(timezone.utc) + timedelta(seconds=1)
    )
    asyncio.run(mgr._resume_wake(wake))
    assert delivered == ["session-normal-1"], "普通会话 wake 不能走任务续跑分支"
    assert openings == []

def test_real_engine_self_sleep_then_wake_resumes_same_session(tmp_path, monkeypatch):
    """真引擎证据：不 patch _build_task_engine / selfwake，模型真的调 sleep_for，
    引擎真的挂起，wake 到期后同一会话续跑并正常送达结案。"""
    import time as _time

    from coworker.providers import (
        AssistantTurn,
        ModelCapabilities,
        ProviderClient,
        ToolCall,
    )
    from coworker.server.manager import SessionManager

    class Provider(ProviderClient):
        def __init__(self):
            self.calls: list[list[dict]] = []

        def complete(self, *, model, messages, tools=None, **settings):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return AssistantTurn(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="sleep_for",
                            arguments={"seconds": 2, "note": "self-test recheck"},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return AssistantTurn(
                text="醒来：目标已变化，报告结果", finish_reason="stop"
            )

        def capabilities(self, model):
            return ModelCapabilities()

    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action

    original = delivery_mod.deliver_run_result
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)
    monkeypatch.setattr(
        delivery_mod,
        "deliver_run_result",
        lambda task, run, *, base_dir=None: original(task, run, base_dir=tmp_path),
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = Provider()
    manager = SessionManager(data_dir=tmp_path / "data", provider=provider)
    manager._maybe_autotitle = lambda sid: None  # 别让标题任务多打一次 provider
    task = ScheduledTask(
        title="自己排节奏",
        instructions="盯住目标；需要等待时自己安排下一次检查",
        schedule=Schedule(kind="cron", cron="*/10 * * * *"),
        workspace=str(ws),
        agent="cowork",
        goal_mode="watch",
    )
    manager.task_store.save(task)

    r1 = asyncio.run(manager._run_scheduled_task(task, "schedule"))
    assert r1.status == "sleeping", "模型自己调了 sleep_for，这轮不能算完成"
    assert r1.delivered_at is None, "睡眠轮不该投递"
    assert r1.checkpoint["goal"]["sleeping"] is True
    pending = manager.wakes.pending(r1.session_id)
    assert pending, "selfwake 工具没真的挂上唤醒"

    _time.sleep(2.2)
    asyncio.run(manager._resume_wake(pending[0]))

    wake_runs = [r for r in manager.task_store.runs(task.id) if r.trigger == "wake"]
    assert wake_runs, "wake 没有被包成 TaskRun —— 那她醒来干完活也会静默丢"
    r2 = wake_runs[0]
    assert r2.session_id == r1.session_id, "醒来必须复用同一会话"
    assert r2.delivered_at is not None and r2.checkpoint["goal"]["done"] is True
    joined = json.dumps(provider.calls[-1], ensure_ascii=False)
    assert "sleep_for" in joined or "wake scheduled" in joined, "第二轮的上下文没接上第一轮"