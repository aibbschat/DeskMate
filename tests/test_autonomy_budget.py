"""needs_you（缺信息回来问人）+ 硬预算（轮次/时长/失败退避）（先红后绿）。

第三刀补两条「傻等」缺口：
1. 定时任务里 ask_user 没注册（`_build_task_engine` 不传 question_asker）——缺登录态/
   要人拍板时她只能 [[STUCK]]，回不来问；
2. 自续期没有硬上限——理论上可以永久盯下去，失败轮也没有退避。

测试库一律 tmp_path。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

from coworker.automation.models import Schedule, ScheduledTask, TaskRun


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-budget-1",
        title="会问人的盯梢",
        instructions="盯住目标；缺信息就回来问萧盛，别自己猜",
        workspace="/tmp/cw-autonomy-budget",
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
    def __init__(self, steps, openings):
        self._steps = steps
        self._openings = openings
        self.messages: list[dict] = []

    async def run(self, opening):
        self._openings.append(opening)
        step = self._steps.pop(0) if self._steps else {"text": ""}
        if step.get("raise"):
            raise RuntimeError("boom")
        self.messages.append({"role": "assistant", "content": step.get("text", "")})
        yield SimpleNamespace(
            type=SimpleNamespace(name="ITERATION_END"), data={"iteration": 1}
        )


def _wired(tmp_path, monkeypatch, steps, openings):
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
        lambda self, task, *, session_id, messages=None: ScriptedEngine(steps, openings),
    )
    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    mgr._running_sessions = set()
    mgr.save = lambda sid, engine: None
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast
    return mgr, pushed, calls


def _seed_goal_run(mgr, **goal):
    base = {
        "state": "hash=abc 还没变",
        "last_checked": time.time() - 60,
        "done": False,
        "attempts": [{"at": time.time() - 60, "outcome": "quiet", "steps": ["run_shell(cat x)"], "result": "", "error": ""}],
    }
    base.update(goal)
    prior = TaskRun(
        task_id="task-budget-1",
        status="ok",
        finished_at=time.time() - 60,
        checkpoint={"goal": base},
    )
    mgr.task_store.add_run(prior)
    return prior


# -- 1. 问人通道：只有目标制任务注册 ask_user ---------------------------------
def test_goal_task_engine_registers_ask_user(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(data_dir=tmp_path / "data")

    def _real(goal_mode: str) -> ScheduledTask:
        return ScheduledTask(
            title="会问人",
            instructions="缺信息就问",
            schedule=Schedule(kind="cron", cron="*/10 * * * *"),
            workspace=str(ws),
            agent="cowork",
            goal_mode=goal_mode,
        )

    goal_engine = mgr._build_task_engine(_real("watch"), session_id="__run__q")
    assert "ask_user" in set(goal_engine.registry.names()), "目标制任务没接 ask_user，卡住只能干等"
    plain_engine = mgr._build_task_engine(_real(""), session_id="__run__p")
    assert "ask_user" not in set(plain_engine.registry.names()), "普通任务行为不能变"


def test_ask_user_pends_in_inbox_then_returns_answer(tmp_path, monkeypatch):
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    mgr = SessionManager(data_dir=tmp_path / "data")
    asker = mgr.inbox_question_asker("__run__ask", "cowork")

    async def scenario():
        async def answer_later():
            for _ in range(200):
                pending = mgr.inbox.pending()
                if pending:
                    mgr.inbox.resolve(pending[0].id, "萧盛的回答：用账号 A", by="test")
                    return
                await asyncio.sleep(0.01)

        task = asyncio.create_task(answer_later())
        result = await asyncio.wait_for(
            asker({"question": "用哪个账号？", "options": ["A", "B"]}), timeout=5
        )
        await task
        return result

    result = asyncio.run(scenario())
    assert "A" in json.dumps(result, ensure_ascii=False), "答案没回到 agent"


# -- 2. 硬预算：轮次 / 时长 ---------------------------------------------------
def test_note_attempt_counts_rounds(tmp_path):
    from deskmate.autonomy.goal import note_attempt

    run = TaskRun(task_id="t1")
    note_attempt(run, outcome="quiet", attempt={}, state="hash=abc", now=1000.0)
    note_attempt(run, outcome="quiet", attempt={}, state="hash=abc", now=1060.0)
    goal = run.checkpoint["goal"]
    assert goal["rounds"] == 2
    assert goal["started_at"] == 1000.0


def test_round_budget_expires_without_running_engine(tmp_path, monkeypatch):
    from deskmate.autonomy.goal import BUDGET_MAX_ROUNDS

    steps: list[dict] = []
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings)
    _seed_goal_run(mgr, rounds=BUDGET_MAX_ROUNDS, started_at=time.time() - 3600)
    task = _task(workspace=str(tmp_path))

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert run.status == "expired", "预算用尽要停下，不能无限自续期"
    assert run.delivered_at is not None, "停下也要把当前状态交出去"
    assert run.checkpoint["goal"]["done"] is True
    assert len(openings) == 0, "预算已尽就不该再起引擎"
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and "预算" in rows[0][2]


def test_time_budget_expires(tmp_path, monkeypatch):
    from deskmate.autonomy.goal import BUDGET_MAX_HOURS

    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, [], openings)
    _seed_goal_run(mgr, rounds=3, started_at=time.time() - (BUDGET_MAX_HOURS + 1) * 3600)
    task = _task(workspace=str(tmp_path))

    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert run.status == "expired"
    assert run.delivered_at is not None and run.checkpoint["goal"]["done"] is True
    assert len(openings) == 0


# -- 3. 失败退避：错误轮之后的 cron 要等一会儿 --------------------------------
def test_error_backoff_skips_next_cron_tick(tmp_path, monkeypatch):
    openings: list[str] = []
    mgr, pushed, calls = _wired(
        tmp_path, monkeypatch, [{"raise": True}, {"text": "恢复"}], openings
    )
    task = _task(workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "error"
    goal = r1.checkpoint["goal"]
    assert goal["backoff_until"] > time.time(), "失败轮必须置退避"

    # 退避期内：cron 再触发应 skipped，不再起引擎
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r2.status == "skipped"
    assert len(openings) == 1

    # 退避到期后恢复（改最近一轮的检查点，模拟时间过去了）
    latest = mgr.task_store.runs(task.id)[0]
    latest.checkpoint["goal"]["backoff_until"] = time.time() - 1
    mgr.task_store.add_run(latest)
    r3 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r3.status == "ok" and r3.delivered_at is not None
    assert "backoff_until" not in r3.checkpoint["goal"], "成功后要清掉退避"


def test_plain_task_has_no_budget_fields(tmp_path, monkeypatch):
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, [{"text": "普通结果"}], openings)
    task = _task(goal_mode="", workspace=str(tmp_path))
    run = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert "goal" not in run.checkpoint
    assert run.delivered_at is not None

def test_real_engine_ask_user_pends_then_continues_same_run(tmp_path, monkeypatch):
    """真引擎证据：模型真调 ask_user → Inbox 挂起等人 → 答在同一条 run 里继续 → 送达结案。"""
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
                            name="ask_user",
                            arguments={
                                "question": "用哪个账号继续？",
                                "options": ["A", "B"],
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return AssistantTurn(text="收到：用账号 A 继续，任务完成", finish_reason="stop")

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
    manager._maybe_autotitle = lambda sid: None
    task = ScheduledTask(
        title="会问人的盯梢",
        instructions="缺信息就问萧盛",
        schedule=Schedule(kind="cron", cron="*/10 * * * *"),
        workspace=str(ws),
        agent="cowork",
        goal_mode="watch",
    )
    manager.task_store.save(task)

    async def scenario():
        async def answer_later():
            for _ in range(600):
                pending = manager.inbox.pending()
                if pending:
                    manager.inbox.resolve(pending[0].id, "用账号 A", by="test")
                    return
                await asyncio.sleep(0.01)

        background = asyncio.create_task(answer_later())
        run = await manager._run_scheduled_task(task, "schedule")
        await background
        return run

    run = asyncio.run(scenario())
    assert run.status == "ok", f"run 没正常结束: {run.status} {run.error}"
    assert run.delivered_at is not None and run.checkpoint["goal"]["done"] is True
    assert len(provider.calls) == 2, "问人后没继续同一轮"
    tool_msgs = [m for m in provider.calls[-1] if m.get("role") == "tool"]
    answers = []
    for m in tool_msgs:
        try:
            answers.append(json.loads(m.get("content") or "{}").get("answer", ""))
        except Exception:
            pass
    assert "用账号 A" in " ".join(answers), "人的回答没有回到模型"
