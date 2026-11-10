"""后台 turn 不得阻塞事件循环（2026-11-10 回归锁）。

实测根因（startup-doctor/latency-doctor）：后台 turn 的第一圈 _compaction_due 在事件循环
线程上调用 _outbound_messages → context_provider，冷会话要跑完 DeskMate 记忆上下文组装，
一次 2407ms 的 loop stall（faulthandler 主线程栈：engine.py:727 → engine.py:2577 →
agent.py:854 → deskmate/memory/user_fact_context.py:409 re.sub）。同一个组装在 _astream 里
也有一份。这里锁住三件事：
  1. compaction 判定不再组装上下文（用上轮真实 token + 快照粗估）；
  2. 出站消息组装在 produce 线程里做，不在事件循环线程；
  3. 前台 turn 在跑时 scheduler 整个 tick 让路（不丢，下个 tick 补）。
"""

from __future__ import annotations

import asyncio
import os
import threading

from coworker.automation import Schedule, ScheduledTask, Scheduler, TaskRun
from coworker.engine import TurnEngine
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.tools import ToolRegistry


class _ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, *, context_provider=None, turns=None):
    return TurnEngine(
        provider=_ScriptedProvider(turns or [AssistantTurn(text="ok", finish_reason="stop")]),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        context_provider=context_provider,
    )


def _mk_engine_with_big_history(tmp_path):
    engine = _engine(tmp_path)
    engine.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 4000},
    ]
    engine.compaction_settings = lambda: {
        "cap_tokens": 400,
        "threshold_pct": 0.8,
        "context_window": 100_000,
    }
    return engine


def test_compaction_gate_does_not_assemble_context(tmp_path):
    """判定本身是廉价的：绝不能为估 token 触发 context_provider（事件循环上的 2.4s）。"""
    calls = []
    engine = _engine(tmp_path, context_provider=lambda msgs: calls.append(1) or "ctx")
    engine.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    engine.compaction_settings = lambda: {
        "cap_tokens": 400,
        "threshold_pct": 0.8,
        "context_window": 100_000,
    }
    assert engine._compaction_due() is False
    assert engine._compaction_due() is False
    assert calls == []


def test_compaction_gate_still_triggers_on_real_usage(tmp_path):
    engine = _mk_engine_with_big_history(tmp_path)
    engine._last_context_tokens = 10_000_000  # provider reported real usage
    assert engine._compaction_due() is True


def test_compaction_gate_counts_appended_tail_and_context_block(tmp_path):
    """快照之后的追加消息 + 下一次组装会再贴上的 <system-context> 块都要计入。"""
    engine = _mk_engine_with_big_history(tmp_path)
    engine._outbound_estimate_tokens = 100
    engine._outbound_estimate_len = 1
    engine._last_context_block_chars = 200_000  # 未来块 ~50k token
    engine.messages.append({"role": "user", "content": "new question"})
    signal = engine._compaction_signal_tokens()
    assert signal >= 50_000
    assert engine._compaction_due() is True


def test_compaction_gate_estimates_raw_history_on_first_turn(tmp_path):
    engine = _mk_engine_with_big_history(tmp_path)
    assert engine._outbound_estimate_tokens is None
    assert engine._compaction_signal_tokens() > 0


def test_outbound_assembly_runs_off_the_event_loop(tmp_path):
    """_astream 的上下文组装必须在 produce 线程，不在事件循环线程。"""
    seen = []

    def ctx(messages):
        seen.append(threading.current_thread())
        return "ctx"

    engine = _engine(tmp_path, context_provider=ctx)

    async def _run():
        return [ev async for ev in engine.run("hi")]

    events = asyncio.run(_run())
    assert events  # turn ran
    assert seen, "context_provider was never called"
    loop_thread = threading.main_thread()
    assert all(t is not loop_thread for t in seen)
    assert engine._outbound_estimate_tokens is not None


class _FakeStore:
    def __init__(self, task):
        self._task = task
        self.advanced = []

    def due(self, *, now=None):
        return [self._task]

    def get(self, task_id):
        return self._task

    def save(self, task):
        self.advanced.append(task.id)
        return task


def _cron_task():
    return ScheduledTask(
        title="brief",
        instructions="do it",
        schedule=Schedule(kind="cron", cron="* * * * *"),
        workspace="/tmp/cw-auto",
    )


def test_scheduler_defers_tick_while_foreground_turn_runs():
    ran = []
    store = _FakeStore(_cron_task())

    async def runner(task, trigger):
        ran.append(trigger)
        return TaskRun(task_id=task.id, status="ok", trigger=trigger)

    async def scenario():
        sched = Scheduler(store, runner, foreground_busy=lambda: True)
        await sched._tick(trigger="schedule")
        await asyncio.sleep(0.05)
        assert ran == []  # 让路：本 tick 一个都不跑
        sched.foreground_busy = lambda: False
        await sched._tick(trigger="schedule")
        await asyncio.sleep(0.05)
        assert ran == ["schedule"]  # 下个 tick 补上
        assert store.advanced == [store._task.id]

    asyncio.run(scenario())


def test_scheduler_tick_runs_when_no_gate():
    ran = []
    store = _FakeStore(_cron_task())

    async def runner(task, trigger):
        ran.append(trigger)
        return TaskRun(task_id=task.id, status="ok", trigger=trigger)

    async def scenario():
        sched = Scheduler(store, runner)
        await sched._tick(trigger="catchup")
        await asyncio.sleep(0.05)
        assert ran == ["catchup"]

    asyncio.run(scenario())


def test_manager_foreground_gate(monkeypatch):
    from coworker.server import SessionManager

    mgr = object.__new__(SessionManager)
    mgr._running_sessions = {"s1"}
    mgr._session_clients = {"s1": set()}
    monkeypatch.setenv("COWORKER_BACKGROUND_YIELD", "1")
    assert mgr._foreground_turn_running() is True
    monkeypatch.setenv("COWORKER_BACKGROUND_YIELD", "0")
    assert mgr._foreground_turn_running() is False
    monkeypatch.setenv("COWORKER_BACKGROUND_YIELD", "1")
    mgr._session_clients = {}  # 无前台客户端（纯后台 run）→ 不让路
    assert mgr._foreground_turn_running() is False
