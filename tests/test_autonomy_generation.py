"""C1 契约（先红后绿）：目标代次 + 真叫停/暂停 + 前端接线。

施工图 §5 C1 / §B5 已知限制 1：
* 已结案目标重新启用会一直 ``skipped`` —— ``is_concluded()`` 只看最近一条带
  goal 的 run，而 ``skipped`` run 自己又带着 ``done=True``，形成自续链。
  正解是目标代次（generation）：run 打戳、判定只认同代、重启只 bump 计数器，
  **绝不改历史 run 的 done 伪装未结案**。
* ``TaskRun.cancel_requested / paused_at / resumed_at`` 全仓零读写点；停用任务
  只拦未来 tick，正在跑的 turn 照跑完，且 finally 会把它当成功投台账。
  C1 补「停得下」：跨协程控制标记 + 引擎中断 + finally 的 halt gate。

最后两条静态接线检查是刻意加的：本项目多次踩过「模块写好了但没挂进调用链、
被 fail-open 吞掉静默失效」，必须锁住 manager / app / 前端真的接上了。
测试库一律放 tmp_path，绝不碰 ~/.deskmate/deskmate.db。
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from coworker.automation.models import ScheduledTask, Schedule, TaskRun


def _ns_task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-c1-1",
        title="盯一个公开页面",
        instructions="盯住这个页面，有变化再报告",
        workspace="/tmp/cw-autonomy-c1",
        agent="deskmate",
        notify_on_completion=False,
        notify_target=None,
        goal_mode="watch",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _real_task(tmp_path, **kw) -> ScheduledTask:
    # notify_on_completion=False: the scripted manager has no real broadcast_session
    # (same fixture convention as tests/test_autonomy_goal.py). Delivery is still
    # exercised through deliver_run_result / the ledger rows.
    kw.setdefault("notify_on_completion", False)
    return ScheduledTask(
        title="盯一个公开页面",
        instructions="盯住这个页面，有变化再报告",
        schedule=Schedule(kind="cron", cron="0 9 * * *"),
        workspace=str(tmp_path),
        goal_mode="watch",
        **kw,
    )


def _run(**kw) -> TaskRun:
    kw.setdefault("task_id", "task-c1-1")
    return TaskRun(**kw)


def _ledger_rows(base_dir):
    db = base_dir / "deskmate.db"
    if not db.is_file():
        return []
    with sqlite3.connect(str(db)) as c:
        try:
            return c.execute("SELECT id, kind, content FROM proactive_ledger").fetchall()
        except sqlite3.OperationalError:
            return []


# -- 1. 数据契约：代次字段、旧 blob 双向兼容 -----------------------------------
def test_scheduled_task_has_goal_generation_default_zero():
    task = _real_task(Path("/tmp"))
    assert task.goal_generation == 0, "默认 0 = 与既有所有任务同代，行为一字不变"
    assert task.public()["goal_generation"] == 0, "UI 要能显示当前代次"


def test_task_blob_tolerates_missing_and_unknown_keys():
    """旧库缺 generation、新库多未知键，都不能把整个自动化列表读炸。"""
    old = {
        "title": "旧任务",
        "instructions": "干活",
        "schedule": {"kind": "cron", "cron": "0 * * * *"},
        "workspace": "/tmp",
        "id": "task-old",
    }
    task = ScheduledTask.from_dict(old)
    assert task.goal_generation == 0
    assert task.goal_mode == ""

    newer = dict(old, goal_generation=3, some_future_key="x")
    assert ScheduledTask.from_dict(newer).goal_generation == 3


def test_task_run_has_generation_stamp():
    run = _run()
    assert run.goal_generation == 0
    assert TaskRun.from_dict({**run.to_dict(), "future_key": 1}).goal_generation == 0


# -- 2. 纯函数：按代次过滤历史 --------------------------------------------------
def test_generation_filters_concluded_and_previous_state():
    from deskmate.autonomy.goal import is_concluded, previous_goal_state

    old_done = _run(
        goal_generation=0,
        checkpoint={"goal": {"state": "hash=old", "done": True, "generation": 0}},
    )
    old_quiet = _run(
        goal_generation=0,
        checkpoint={"goal": {"state": "hash=quiet", "done": False, "generation": 0}},
    )
    new_quiet = _run(
        goal_generation=1,
        checkpoint={"goal": {"state": "hash=new", "done": False, "generation": 1}},
    )
    # 默认（不传 generation）保持 B2 语义，旧测试全绿
    assert is_concluded([old_done, old_quiet]) is True
    # 新代次：旧代的 done 不得再让目标 skipped
    assert is_concluded([old_done, old_quiet], generation=1) is False
    assert previous_goal_state([old_done, old_quiet], generation=1) == {}
    assert previous_goal_state([new_quiet, old_quiet], generation=1)["state"] == "hash=new"
    # 同代有 done 仍要结案
    assert is_concluded([new_quiet, old_done], generation=0) is True


# -- 3. 重启目标：bump 计数、不改历史 -------------------------------------------
def test_update_automation_restart_goal_bumps_generation(tmp_path):
    from coworker.automation.store import TaskStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    task = _real_task(tmp_path)
    mgr.task_store.save(task)

    out = mgr.update_automation(task.id, {"restart_goal": True})
    assert out["ok"] is True
    assert out["task"]["goal_generation"] == 1
    assert mgr.task_store.get(task.id).goal_generation == 1
    again = mgr.update_automation(task.id, {"restart_goal": True})
    assert again["task"]["goal_generation"] == 2
    # 关掉目标制后重启，应重新声明而非保持 skipped
    mgr.update_automation(task.id, {"goal_mode": ""})
    reopened = mgr.update_automation(task.id, {"restart_goal": True})
    assert reopened["task"]["goal_mode"] == "watch"


def test_scheduling_tool_can_restart_goal(tmp_path):
    """小桔自己重启目标：工具 schema 要声明，否则 LLM 不会传。"""
    from coworker.automation.store import TaskStore
    from coworker.automation.tools import scheduling_tools

    store = TaskStore(str(tmp_path / "automation.db"))
    tools = scheduling_tools(
        store, origin={"workspace": str(tmp_path)}, default_workspace=str(tmp_path)
    )
    update = next(t for t in tools if t.__name__ == "update_scheduled_task")
    props = update.__coworker_schema__["function"]["parameters"]["properties"]
    assert "restart_goal" in props and "goal_mode" in props

    task = _real_task(tmp_path)
    store.save(task)
    out = update(task.id, restart_goal=True)
    assert out["ok"] is True
    assert store.get(task.id).goal_generation == 1


# -- 4. 静态接线检查 -----------------------------------------------------------
def test_manager_wires_generation_and_halt_gate():
    from coworker.server.manager import SessionManager

    src = inspect.getsource(SessionManager._run_scheduled_task)
    for name in (
        "goal_generation", "generation=run.goal_generation",
        "_control_marks", "cancel_requested", "canceled", "paused",
    ):
        assert name in src, f"_run_scheduled_task 没接 {name}"


def test_app_has_run_cancel_route():
    repo = Path(__file__).resolve().parents[1]
    app = (repo / "coworker/server/app.py").read_text(encoding="utf-8")
    assert "/runs/{run_id}/cancel" in app, "叫停没有 HTTP 入口"


def test_frontend_wires_goal_mode_and_run_controls():
    repo = Path(__file__).resolve().parents[1]
    api = (repo / "surfaces/gui/src/api.ts").read_text(encoding="utf-8")
    view = (repo / "surfaces/gui/src/components/ScheduledView.tsx").read_text(encoding="utf-8")
    app_tsx = (repo / "surfaces/gui/src/App.tsx").read_text(encoding="utf-8")
    assert "goal_mode" in api and "goal_generation" in api
    assert "cancelAutomationRun" in api, "前端没有叫停入口"
    assert "goal_mode" in view and "restartAutomationGoal" in view
    # 全局事件在 App.tsx 的 connectEvents 消费，详情页靠轮询刷新（不额外开 WS）
    assert "automation_run_stopped" in app_tsx, "前端没有消费停止事件"


# -- 5. 集成：真跑多次 run ------------------------------------------------------
class ScriptedRun:
    """最小引擎：记录 opening，按脚本吐文本 + 3 个 ITERATION_END；可被中断。"""

    def __init__(self, texts, openings, on_start=None):
        self._texts = texts
        self._openings = openings
        self._on_start = on_start
        self.messages: list[dict] = []
        self.interrupted = False

    async def run(self, opening):
        self._openings.append(opening)
        if self._on_start:
            self._on_start()
        text = self._texts.pop(0) if self._texts else ""
        self.messages = [{"role": "assistant", "content": text}]
        for i in range(1, 4):
            yield SimpleNamespace(
                type=SimpleNamespace(name="ITERATION_END"), data={"iteration": i}
            )

    def request_interrupt(self):
        self.interrupted = True


def _wired_manager(tmp_path, monkeypatch, texts, openings, on_start=None):
    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action
    from coworker.automation.store import TaskStore
    from coworker.server.manager import SessionManager

    original = delivery_mod.deliver_run_result
    calls: list[str] = []
    holder: dict = {}

    def counting_deliver(task, run, *, base_dir=None):
        calls.append(run.run_id)
        return original(task, run, base_dir=tmp_path)

    monkeypatch.setattr(delivery_mod, "deliver_run_result", counting_deliver)
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)

    def make_engine(self, task, *, session_id):
        cb = (lambda: on_start(holder["mgr"])) if on_start else None
        eng = ScriptedRun(texts, openings, cb)
        holder["engine"] = eng
        return eng

    monkeypatch.setattr(SessionManager, "_build_task_engine", make_engine)

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    mgr.save = lambda sid, engine: None
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast
    holder["mgr"] = mgr
    return mgr, pushed, calls, holder


def test_restarted_goal_runs_again_and_delivers_once(tmp_path, monkeypatch):
    """结案 → skipped → 重启目标 → 新一轮不再 skipped、不带旧状态、只多投一次。"""
    from coworker.automation.store import TaskStore

    texts = [
        "[[NO_CHANGE]]\n[[STATE]]hash=abc 未变",
        "第一轮变化：截止日延后。",
        "[[NO_CHANGE]]\n[[STATE]]hash=xyz 新代次",
        "第二轮变化：新增补充通知。",
    ]
    openings: list[str] = []
    mgr, pushed, calls, _ = _wired_manager(tmp_path, monkeypatch, texts, openings)

    task = _real_task(tmp_path)
    mgr.task_store.save(task)

    def once(t):
        return asyncio.run(mgr._run_scheduled_task(t, "schedule"))

    r1 = once(task)
    assert r1.checkpoint["goal"]["generation"] == 0
    r2 = once(task)
    assert r2.checkpoint["goal"]["done"] is True
    r3 = once(task)
    assert r3.status == "skipped", "结案后本应 skipped"
    assert len(_ledger_rows(tmp_path)) == 1

    # 重启：bump 代次，历史 done run 留在第 0 代，绝不被改写
    bumped = mgr.update_automation(task.id, {"restart_goal": True})
    assert bumped["task"]["goal_generation"] == 1
    assert mgr.task_store.runs(task.id)[0].checkpoint["goal"]["done"] is True

    fresh = mgr.task_store.get(task.id)
    r4 = once(fresh)
    assert r4.status == "ok", "重启后仍被旧 done run 挡住"
    assert r4.goal_generation == 1
    assert "hash=abc" not in openings[-1], "新代次不该继承上一代状态"
    assert r4.checkpoint["goal"]["generation"] == 1

    r5 = once(fresh)
    assert r5.checkpoint["goal"]["done"] is True
    assert len(_ledger_rows(tmp_path)) == 2, "重启后应恰好再送达一次"
    assert len(calls) == 2
    assert [e["type"] for e in pushed].count("automation_run_finished") == 2


def test_cancel_run_halts_without_delivery_or_conclusion(tmp_path, monkeypatch):
    """叫停正在跑的 run：真中断、不改状态为 ok、不投台账、不误结案。"""

    def on_start(mgr):
        running = [r for r in mgr.task_store.runs("task-c1-1") if r.finished_at is None]
        assert running, "引擎启动时 run 应已落库"
        out = mgr.cancel_run("task-c1-1", running[0].run_id)
        assert out["ok"] is True

    texts = ["本轮已完成的报告内容"]
    openings: list[str] = []
    mgr, pushed, calls, holder = _wired_manager(
        tmp_path, monkeypatch, texts, openings, on_start
    )
    task = _ns_task(workspace=str(tmp_path))

    r = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert holder["engine"].interrupted is True, "没有真的中断引擎"
    assert r.status == "canceled"
    assert r.cancel_requested is True
    assert r.delivered_at is None and calls == [], "叫停不该被当成功投递"
    assert _ledger_rows(tmp_path) == []
    assert [e["type"] for e in pushed] == ["automation_run_stopped"]
    assert not r.checkpoint.get("goal", {}).get("done"), "叫停不该把目标误结案"
    stored = mgr.task_store.runs("task-c1-1")[0]
    assert stored.status == "canceled" and stored.cancel_requested is True


def test_disabling_task_pauses_running_run_and_resume_marks_it(tmp_path, monkeypatch):
    """暂停 = 停用未来 tick + 中断当前 turn；恢复 = 重新启用并留痕。"""

    def on_start(mgr):
        mgr.update_automation("task-c1-1", {"enabled": False})

    texts = ["[[NO_CHANGE]]\n[[STATE]]hash=abc 未变"]
    openings: list[str] = []
    mgr, pushed, calls, _ = _wired_manager(
        tmp_path, monkeypatch, texts, openings, on_start
    )
    task = _real_task(tmp_path, id="task-c1-1", notify_on_completion=False)
    mgr.task_store.save(task)

    r = asyncio.run(mgr._run_scheduled_task(mgr.task_store.get(task.id), "schedule"))
    assert r.status == "paused"
    assert r.paused_at is not None
    assert mgr.task_store.get(task.id).enabled is False
    assert _ledger_rows(tmp_path) == [] and calls == []

    mgr.update_automation(task.id, {"enabled": True})
    assert mgr.task_store.get(task.id).enabled is True
    assert mgr.task_store.runs(task.id)[0].resumed_at is not None


def test_plain_task_and_generation_zero_behaviour_unchanged(tmp_path, monkeypatch):
    """回归护栏：普通任务 / 第 0 代目标，行为与 B2 完全一致。"""
    texts = ["第一次结果", "第二次结果"]
    openings: list[str] = []
    mgr, pushed, calls, _ = _wired_manager(tmp_path, monkeypatch, texts, openings)
    task = _ns_task(goal_mode="", workspace=str(tmp_path))
    assert getattr(task, "goal_generation", 0) == 0

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.delivered_at is not None and r2.delivered_at is not None
    assert len(_ledger_rows(tmp_path)) == 2
    assert len(calls) == 2
    assert [e["type"] for e in pushed].count("automation_run_started") == 2
