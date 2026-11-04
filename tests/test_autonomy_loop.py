"""自主循环 —— 让跨轮真的记得上轮干了什么，会拐弯、会报卡点（先红后绿）。

用户口径（2026-11-04）：「不是自我驱动，就不是真自主执行，错了或遇到问题了就不会
拐弯，傻傻卡住。」

本文件锁四件事：
1. 每轮把「实际动作 + 结果/错误」记进 checkpoint.goal.attempts，下一轮 opening
   必须带上（这是跨轮唯一能让模型知道「上次试过什么、为什么失败」的通道）；
2. 模型自述 [[STUCK]]<原因> 时累计卡住轮次；连续卡住到阈值 → 投一次「卡点」并
   结案，不再每 10 分钟空转；
3. 结论（done=True）必须等投递拿到非空台账 id 之后才落 —— 投递失败/空产出时保持
   未结案，下一轮还能重试（修 A 阶段「先结案后投递」的静默丢结果缺陷）；
4. 普通任务（goal_mode=""）行为一字不变（回归护栏）。

测试库一律 tmp_path，绝不碰 ~/.deskmate / ~/.config/coworker。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from coworker.automation.models import Schedule, ScheduledTask, TaskRun


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-loop-1",
        title="盯一个会出错的目标",
        instructions="盯住目标，有变化再报告；遇到问题自己想办法，实在不行说明卡在哪",
        workspace="/tmp/cw-autonomy-loop",
        agent="deskmate",
        notify_on_completion=False,
        notify_target=None,
        goal_mode="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _ledger_rows(base_dir):
    db = base_dir / "deskmate.db"
    if not db.is_file():
        return []
    with sqlite3.connect(str(db)) as c:
        try:
            return c.execute(
                "SELECT id, kind, content, evidence FROM proactive_ledger"
            ).fetchall()
        except sqlite3.OperationalError:
            return []


# -- 1. 纯函数：尝试提取 / 卡点解析 / 计数 --------------------------------------
def test_attempt_from_messages_extracts_tool_calls_and_result():
    """跨轮记忆的原料：从本轮真实消息里抠出「调了什么工具、结果是什么」。"""
    from deskmate.autonomy.goal import attempt_from_messages

    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "arguments": json.dumps({"command": "cat /tmp/x/flag.txt"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"exit_code": 1}'},
        {"role": "assistant", "content": "读不到，文件不存在"},
    ]
    attempt = attempt_from_messages(
        msgs, result="读不到，文件不存在", error="", at=1.0
    )
    assert attempt["steps"], "必须留下工具动作，否则下一轮还会原样重试"
    assert "run_shell" in attempt["steps"][0]
    assert "cat /tmp/x/flag.txt" in attempt["steps"][0]
    assert "读不到" in attempt["result"]
    # 空消息不能炸，也不能编出一个动作
    empty = attempt_from_messages([], result="", error="boom", at=2.0)
    assert empty["steps"] == [] and "boom" in empty["error"]


def test_parse_stuck_marker():
    """模型自己说「卡住了」要有明确协议，不能靠关键词猜。"""
    from deskmate.autonomy.goal import parse_stuck

    ok, reason = parse_stuck("[[STUCK]]登录态过期，需要萧哥重新登录一次")
    assert ok is True and "登录态过期" in reason
    assert parse_stuck("[[NO_CHANGE]]\n[[STATE]]x")[0] is False
    assert parse_stuck("我觉得有点卡")[0] is False, "没有标记就不算卡住"


def test_opening_carries_attempts_and_stuck_hint():
    """下一轮的 opening 必须带上「上次试过什么、错在哪、卡在哪」。"""
    from deskmate.autonomy.goal import build_goal_opening

    prev = {
        "state": "hash=abc 仍未变",
        "last_checked": 1730000000.0,
        "attempts": [
            {
                "at": 1730000000.0,
                "outcome": "error",
                "steps": ["run_shell(cat /tmp/x/flag.txt)"],
                "result": "",
                "error": "No such file or directory",
            },
            {
                "at": 1730000100.0,
                "outcome": "stuck",
                "steps": ["web_fetch(https://example.com)"],
                "result": "",
                "blocker": "登录态过期",
            },
        ],
        "stuck_rounds": 2,
        "flat_rounds": 4,
        "last_blocker": "登录态过期",
    }
    text = build_goal_opening(prev)
    assert "hash=abc 仍未变" in text, "旧语义不能丢"
    assert "run_shell" in text and "No such file" in text, "失败动作没带到下一轮"
    assert "登录态过期" in text
    assert "不要再原样重复" in text, "必须明说别重复已失败的动作"
    assert "[[STUCK]]" in text, "得告诉她卡住时怎么表达"


def test_note_attempt_counts_stuck_and_resets_on_progress():
    from deskmate.autonomy.goal import note_attempt

    run = TaskRun(task_id="t1")
    note_attempt(
        run, outcome="error", attempt={"steps": ["run_shell(x)"]}, error="boom", now=1.0
    )
    assert run.checkpoint["goal"]["stuck_rounds"] == 1
    note_attempt(run, outcome="stuck", attempt={}, blocker="登录失效", now=2.0)
    assert run.checkpoint["goal"]["stuck_rounds"] == 2
    assert run.checkpoint["goal"]["last_blocker"] == "登录失效"
    # 恢复一次正常检查 → 卡住计数归零，但尝试历史要保留
    note_attempt(run, outcome="quiet", attempt={}, state="hash=abc", now=3.0)
    goal = run.checkpoint["goal"]
    assert goal["stuck_rounds"] == 0
    assert len(goal["attempts"]) == 3, "尝试链不能因为恢复就清空"


# -- 2. 集成：两轮之间必须记得上一轮 -------------------------------------------
class ScriptedEngine:
    """最小引擎：按脚本吐 tool_calls + 最终文本；可 raise 以模拟执行失败。"""

    def __init__(self, steps, openings):
        # 共享同一个步骤队列：每轮 run 消费一条，模拟「第二轮」拿到第二条脚本。
        self._steps = steps
        self._openings = openings
        self.messages: list[dict] = []

    async def run(self, opening):
        self._openings.append(opening)
        step = self._steps.pop(0) if self._steps else {"text": ""}
        self.messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "run_shell",
                            "arguments": json.dumps(
                                {"command": "cat /tmp/x/flag.txt"}
                            ),
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"exit_code": 1}'},
        ]
        if step.get("raise"):
            raise RuntimeError("provider exploded")
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
        lambda self, task, *, session_id: ScriptedEngine(steps, openings),
    )

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    mgr.save = lambda sid, engine: None
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast
    return mgr, pushed, calls


def test_second_round_opening_carries_first_round_failure(tmp_path, monkeypatch):
    """核心红测试：第一轮执行失败，第二轮的 opening 必须带着失败动作和原因。"""
    steps = [
        {"raise": True},
        {"text": "[[NO_CHANGE]]\n[[STATE]]hash=abc 仍未变"},
    ]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings)
    task = _task(goal_mode="watch", workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "error"
    assert openings[0] and "hash=abc" not in openings[0], "首轮没有历史"

    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    opening2 = openings[1]
    assert "run_shell" in opening2, "下一轮不知道上次调了什么工具"
    assert "provider exploded" in opening2, "下一轮不知道上次为什么失败"
    assert "不要再原样重复" in opening2
    assert r2.status == "ok"
    assert r2.checkpoint["goal"]["stuck_rounds"] == 0, "恢复成功后卡住计数要归零"


def test_stuck_rounds_are_delivered_once_then_closed(tmp_path, monkeypatch):
    """连续卡住到阈值 → 投一次卡点并结案，之后 skipped，不无限空转。"""
    stuck_text = "[[STUCK]]登录态过期，需要萧哥重新登录一次"
    steps = [{"text": stuck_text} for _ in range(3)]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings)
    task = _task(goal_mode="watch", workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "stuck"
    assert r1.delivered_at is None, "卡住预警轮不该投递"
    assert _ledger_rows(tmp_path) == []

    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert "登录态过期" in openings[1], "第二轮 opening 没带卡点原因"
    assert r2.delivered_at is None
    assert r2.checkpoint["goal"]["done"] is False

    r3 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r3.status == "stuck"
    assert r3.delivered_at is not None, "到阈值必须投递卡点"
    assert r3.checkpoint["goal"]["done"] is True, "投出去之后才结案"
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and "登录态过期" in rows[0][2]

    # 结案后 cron 再触发：skipped，不再起引擎
    r4 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r4.status == "skipped"
    assert len(openings) == 3


def test_conclusion_waits_for_successful_delivery(tmp_path, monkeypatch):
    """修「先结案后投递」：投递没拿到台账 id 就不能 done，否则永久丢。"""
    import deskmate.autonomy.delivery as delivery_mod

    original = delivery_mod.deliver_run_result
    steps = [{"text": "有变化了：READY"}, {"text": "有变化了：READY（重试）"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings)
    task = _task(goal_mode="watch", workspace=str(tmp_path))

    monkeypatch.setattr(delivery_mod, "deliver_run_result", lambda *a, **k: "")
    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.status == "ok"
    assert r1.checkpoint["goal"]["done"] is False, "没投出去却结案 = 结果永久丢"
    assert r1.delivered_at is None
    assert _ledger_rows(tmp_path) == []

    # 投递恢复 → 下一轮还能补投并结案
    monkeypatch.setattr(
        delivery_mod,
        "deliver_run_result",
        lambda task, run, *, base_dir=None: original(task, run, base_dir=tmp_path),
    )
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r2.delivered_at is not None
    assert r2.checkpoint["goal"]["done"] is True
    assert len(_ledger_rows(tmp_path)) == 1


def test_plain_task_behaviour_unchanged(tmp_path, monkeypatch):
    """回归护栏：普通任务不写 attempts、每轮照旧投递，A 阶段语义不变。"""
    steps = [{"text": "第一次结果"}, {"text": "第二次结果"}]
    openings: list[str] = []
    mgr, pushed, calls = _wired(tmp_path, monkeypatch, steps, openings)
    task = _task(goal_mode="", workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.delivered_at is not None and r2.delivered_at is not None
    assert len(_ledger_rows(tmp_path)) == 2
    assert "goal" not in r1.checkpoint, "普通任务不该被写入目标制状态"

def test_real_engine_receives_previous_failure_memory(tmp_path, monkeypatch):
    """真引擎证据：不 patch _build_task_engine，用真 TurnEngine + RecordingProvider，
    断言第二轮发给模型的 messages 里确实带着第一轮的失败原因。

    既有的三个集成测试都把引擎换成了假货；这条补上「真引擎 × 目标制」的组合。
    """
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
    from coworker.server.manager import SessionManager

    class RecordingProvider(ProviderClient):
        def __init__(self):
            self.calls: list[list[dict]] = []

        def complete(self, *, model, messages, tools=None, **settings):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                raise RuntimeError("boom-first-round")
            return AssistantTurn(
                text="[[NO_CHANGE]]\n[[STATE]]hash=abc 仍未变", finish_reason="stop"
            )

        def capabilities(self, model):
            return ModelCapabilities()

    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)
    monkeypatch.setattr(delivery_mod, "deliver_run_result", lambda *a, **k: "")
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = RecordingProvider()
    manager = SessionManager(data_dir=tmp_path / "data", provider=provider)
    task = ScheduledTask(
        title="盯一个会出错的目标",
        instructions="盯住目标，有变化再报告；遇到问题自己想办法，实在不行说明卡在哪",
        schedule=Schedule(kind="cron", cron="*/10 * * * *"),
        workspace=str(ws),
        agent="cowork",
        goal_mode="watch",
    )
    manager.task_store.save(task)

    r1 = asyncio.run(manager._run_scheduled_task(task, "schedule"))
    # 真引擎把 provider 异常吞成 error 事件（不会抛），所以 status 仍是 ok——
    # 但错误必须被记进 run.error 与 attempt，下一轮才看得到。
    assert "boom-first-round" in (r1.error or ""), "引擎错误没被记下来"
    assert r1.checkpoint["goal"]["attempts"][-1]["outcome"] == "error"

    r2 = asyncio.run(manager._run_scheduled_task(task, "schedule"))
    assert r2.status == "ok"
    assert len(provider.calls) >= 2, "第二轮没真的调用模型"
    sent = "\n".join(str(m.get("content") or "") for m in provider.calls[-1])
    assert "boom-first-round" in sent, "真实引擎下一轮没收到上一轮失败记忆"
    assert "run_shell" in sent or "尝试" in sent, "没把上一轮的动作带进 prompt"
    assert r2.checkpoint["goal"]["attempts"], "检查点里没有尝试链"
    assert r2.checkpoint["goal"]["attempts"][0]["outcome"] == "error"
