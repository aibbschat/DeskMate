"""B2 目标制任务契约（先红后绿）。

施工图 §4 B2 改道方案：不补 selfwake 接线，改用现有 scheduler cron 重复触发
＋ ``TaskRun.checkpoint`` 存上次状态，实现「盯到变化再报告」。

锁住五件事：
1. ``ScheduledTask.goal_mode`` 是可选、默认关闭的显式声明（下达时授权，不预设类别）；
2. 跨 run 的 ``checkpoint`` 能继承（含崩溃时 unfinished run 的状态）；
3. 无变化的静默检查：不投台账、不发完成/开始事件、不写 after_action 记忆；
4. 变化时**只送达一次**并结案，后续 cron run 直接 skipped；
5. 普通任务（goal_mode 为空）行为一字不变 —— 回归护栏。

最后一条接线检查是刻意加的：本项目多次踩过「模块写好了但没挂进调用链、
被 fail-open 吞掉静默失效」，必须锁住 manager 真的调用了 goal 模块。
测试库一律放 tmp_path，绝不碰 ~/.deskmate/deskmate.db。
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from types import SimpleNamespace

from coworker.automation.models import ScheduledTask, Schedule, TaskRun


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-goal-1",
        title="盯一个公开页面",
        instructions="盯住这个页面，有变化再报告",
        workspace="/tmp/cw-autonomy-goal",
        agent="deskmate",
        notify_on_completion=False,
        notify_target=None,
        goal_mode="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run(**kw) -> TaskRun:
    kw.setdefault("task_id", "task-goal-1")
    return TaskRun(**kw)


def _ledger_rows(base_dir):
    db = base_dir / "deskmate.db"
    if not db.is_file():
        return []
    with sqlite3.connect(str(db)) as c:
        try:
            return c.execute(
                "SELECT id, kind, content FROM proactive_ledger"
            ).fetchall()
        except sqlite3.OperationalError:
            return []


# -- 1. 显式声明，默认关闭 ------------------------------------------------------
def test_scheduled_task_has_goal_mode_default_off():
    task = ScheduledTask(
        title="t",
        instructions="i",
        schedule=Schedule(kind="cron", cron="0 * * * *"),
        workspace="/tmp",
    )
    assert task.goal_mode == "", "默认必须是关闭的，不能改变任何既有任务"
    assert task.public()["goal_mode"] == ""


def test_old_task_json_without_goal_mode_still_loads():
    """旧库 blob 没有 goal_mode，必须仍能读回（回归护栏）。"""
    old = {
        "title": "旧任务",
        "instructions": "干活",
        "schedule": {"kind": "cron", "cron": "0 * * * *"},
        "workspace": "/tmp",
        "id": "task-old",
        "task_session_id": "__task__task-old",
    }
    task = ScheduledTask.from_dict(old)
    assert task.goal_mode == ""
    assert task.id == "task-old"


def test_goal_enabled_only_when_declared():
    from deskmate.autonomy.goal import goal_enabled

    for off in ("", None, "off", "false", "0", "no"):
        assert goal_enabled(_task(goal_mode=off)) is False
    assert goal_enabled(_task(goal_mode="watch")) is True
    assert goal_enabled(_task()) is False  # 属性都没有也不能炸


# -- 2. 纯函数：状态解析与跨 run 继承 -------------------------------------------
def test_parse_quiet_and_state():
    from deskmate.autonomy.goal import parse_quiet

    quiet, state = parse_quiet("[[NO_CHANGE]]\n[[STATE]]hash=abc 截止日未变")
    assert quiet is True
    assert state == "hash=abc 截止日未变"
    assert parse_quiet("页面补了一则通知，差异已标出。") == (False, "")
    # fail-safe：只有 STATE 没有 NO_CHANGE 不算静默，宁可多投不漏投
    assert parse_quiet("[[STATE]]hash=abc")[0] is False


def test_build_goal_opening_carries_previous_state():
    from deskmate.autonomy.goal import build_goal_opening

    text = build_goal_opening({"state": "hash=abc", "last_checked": 1730000000.0})
    assert "hash=abc" in text, "上轮状态必须进 opening，否则无法跨次比对"
    assert "[[NO_CHANGE]]" in text and "[[STATE]]" in text
    assert build_goal_opening({})  # 首次检查也要有协议说明


def test_previous_goal_state_and_concluded():
    """store.runs() 按 started_at DESC，列表第一个是最近一次。"""
    from deskmate.autonomy.goal import is_concluded, previous_goal_state

    finished = _run(
        status="ok", finished_at=1.0,
        checkpoint={"goal": {"state": "hash=abc", "done": False}},
    )
    older = _run(
        status="ok", finished_at=0.0,
        checkpoint={"goal": {"state": "hash=old", "done": False}},
    )
    plain = _run(status="ok", finished_at=2.0, checkpoint={"last_iteration": 3})
    assert previous_goal_state([plain, finished, older])["state"] == "hash=abc"
    assert previous_goal_state([older])["state"] == "hash=old"
    assert previous_goal_state([]) == {}
    assert is_concluded([plain, finished, older]) is False

    done = _run(
        status="ok", finished_at=3.0,
        checkpoint={"goal": {"state": "hash=abc", "done": True}},
    )
    assert is_concluded([done, finished, older]) is True


def test_record_check_keeps_other_checkpoint_keys():
    from deskmate.autonomy.goal import record_check, record_concluded

    run = _run(checkpoint={"last_iteration": 3, "event_count": 6})
    record_check(run, state="hash=abc", now=1730000000.0)
    assert run.checkpoint["last_iteration"] == 3, "不能抹掉已有的迭代检查点"
    assert run.checkpoint["goal"]["state"] == "hash=abc"
    assert run.checkpoint["goal"]["done"] is False
    record_concluded(run)
    assert run.checkpoint["goal"]["done"] is True


# -- 3. 接线检查 ---------------------------------------------------------------
def test_manager_goal_path_is_wired():
    from coworker.server.manager import SessionManager

    src = inspect.getsource(SessionManager._run_scheduled_task)
    for name in (
        "goal_enabled", "previous_goal_state", "is_concluded",
        "build_goal_opening", "parse_quiet", "record_check", "record_concluded",
    ):
        assert name in src, f"_run_scheduled_task 没接 {name}"


# -- 4. 真跑多次 run（集成证据） ------------------------------------------------
class ScriptedRun:
    """最小引擎：记录 opening，按脚本吐 assistant 文本 + 3 个 ITERATION_END。"""

    def __init__(self, texts, openings):
        self._texts = texts
        self._openings = openings
        self.messages: list[dict] = []

    async def run(self, opening):
        self._openings.append(opening)
        text = self._texts.pop(0) if self._texts else ""
        self.messages = [{"role": "assistant", "content": text}]
        for i in range(1, 4):
            yield SimpleNamespace(
                type=SimpleNamespace(name="ITERATION_END"),
                data={"iteration": i},
            )


def _wired_manager(tmp_path, monkeypatch, texts, openings):
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
        lambda self, task, *, session_id: ScriptedRun(texts, openings),
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


def test_watch_task_two_quiet_then_change_then_skip(tmp_path, monkeypatch):
    """B2 主验收：无变化零打扰 → 跨 run 继承 → 变化只送达一次 → 结案后 skip。"""
    texts = [
        "[[NO_CHANGE]]\n[[STATE]]hash=abc 截止日未变",
        "[[NO_CHANGE]]\n[[STATE]]hash=abc 截止日未变",
        "页面补了一则补充通知：截止日延后两周。原文链接：https://example.com/n",
    ]
    openings: list[str] = []
    mgr, pushed, calls = _wired_manager(tmp_path, monkeypatch, texts, openings)
    task = _task(goal_mode="watch", workspace=str(tmp_path))

    def once():
        return asyncio.run(mgr._run_scheduled_task(task, "schedule"))

    # 第一轮：静默检查，什么都不能打扰用户
    r1 = once()
    assert r1.status == "ok"
    assert r1.delivered_at is None, "无变化不该投台账"
    assert calls == [], "静默检查不该调用投递函数"
    assert _ledger_rows(tmp_path) == [], "无变化期间台账零新增"
    assert [e["type"] for e in pushed] == [], "静默检查不该弹开始/完成事件"
    assert r1.checkpoint["goal"]["state"].startswith("hash=abc")
    assert r1.checkpoint["last_iteration"] == 3, "ITERATION_END 不该抹掉 goal 键"
    assert "hash=abc" not in openings[0], "首次检查没有历史状态"

    # 第二轮：静默，但必须继承第一轮状态
    r2 = once()
    assert "hash=abc" in openings[1], "跨 run 没继承 checkpoint，无法比对"
    assert r2.checkpoint["goal"]["state"].startswith("hash=abc")
    assert _ledger_rows(tmp_path) == []
    assert len(calls) == 0

    # 第三轮：有变化 → 一次送达 + 结案
    r3 = once()
    assert r3.delivered_at is not None
    assert r3.checkpoint["goal"]["done"] is True, "变化后必须结案"
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0][1] == "task_result"
    assert "补充通知" in rows[0][2]
    assert len(calls) == 1
    assert [e["type"] for e in pushed] == ["automation_run_finished"]

    # 第四轮：cron 还会触发，但已结案 → skipped，不重复投递、不再起引擎
    r4 = once()
    assert r4.status == "skipped"
    assert len(openings) == 3, "结案后不该再跑引擎"
    assert _ledger_rows(tmp_path) and len(_ledger_rows(tmp_path)) == 1
    assert len(calls) == 1, "结案后重复打扰"


def test_plain_task_behaviour_unchanged(tmp_path, monkeypatch):
    """回归护栏：goal_mode 为空时，每次 run 照旧投递（A 阶段语义不变）。"""
    texts = ["第一次结果", "第二次结果"]
    openings: list[str] = []
    mgr, pushed, calls = _wired_manager(tmp_path, monkeypatch, texts, openings)
    task = _task(goal_mode="", workspace=str(tmp_path))

    r1 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    r2 = asyncio.run(mgr._run_scheduled_task(task, "schedule"))
    assert r1.delivered_at is not None and r2.delivered_at is not None
    assert len(_ledger_rows(tmp_path)) == 2
    assert len(calls) == 2
    assert [e["type"] for e in pushed].count("automation_run_started") == 2


def test_crash_resume_inherits_unfinished_checkpoint(tmp_path, monkeypatch):
    """kill -9 后：上次 running/未 finished 的 run 状态要被新一轮继承。"""
    from coworker.automation.store import TaskStore

    texts = ["[[NO_CHANGE]]\n[[STATE]]hash=abc 仍未变"]
    openings: list[str] = []
    mgr, pushed, calls = _wired_manager(tmp_path, monkeypatch, texts, openings)
    interrupted = _run(
        task_id="task-goal-1",
        status="running",
        finished_at=None,
        checkpoint={"goal": {"state": "hash=pre 崩溃前", "done": False}},
    )
    mgr.task_store.add_run(interrupted)

    r = asyncio.run(
        mgr._run_scheduled_task(_task(goal_mode="watch", workspace=str(tmp_path)), "catchup")
    )
    assert "hash=pre 崩溃前" in openings[0], "崩溃前的状态没续上"
    assert r.checkpoint["goal"]["state"].startswith("hash=abc")
    assert _ledger_rows(tmp_path) == []


# -- 5. 下达入口：GUI 与 Agent 工具都要能声明目标制 ------------------------------
def test_create_and_update_automation_accept_goal_mode(tmp_path):
    """没有下达入口，PoC 就只能靠测试构造 —— 必须打通 GUI 创建/更新。"""
    from coworker.automation.store import TaskStore
    from coworker.server.manager import SessionManager

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._provision_scratch = lambda sid: str(tmp_path / "scratch")
    mgr._engines = {}

    payload = dict(
        title="盯页面",
        instructions="有变化再报告",
        cron="0 9 * * *",
        goal_mode="watch",
    )
    out = mgr.create_automation(payload)
    assert out["ok"] is True
    assert out["task"]["goal_mode"] == "watch"
    task_id = out["task"]["id"]
    assert mgr.task_store.get(task_id).goal_mode == "watch"

    changed = mgr.update_automation(task_id, {"goal_mode": ""})
    assert changed["task"]["goal_mode"] == ""
    assert mgr.task_store.get(task_id).goal_mode == ""


def test_scheduling_tool_declares_and_passes_goal_mode(tmp_path):
    """小桔自己下达目标：工具的 schema 要声明这个参数，否则 LLM 不会传。"""
    from coworker.automation.store import TaskStore
    from coworker.automation.tools import scheduling_tools

    store = TaskStore(str(tmp_path / "automation.db"))
    tools = scheduling_tools(
        store, origin={"workspace": str(tmp_path)}, default_workspace=str(tmp_path)
    )
    create = next(t for t in tools if t.__name__ == "create_scheduled_task")
    props = create.__coworker_schema__["function"]["parameters"]["properties"]
    assert "goal_mode" in props, "LLM 看不到这个参数就无法自主下达目标制任务"

    out = create(
        title="盯页面",
        instructions="有变化再报告",
        cron="0 9 * * *",
        goal_mode="watch",
    )
    assert out["ok"] is True
    assert store.get(out["id"]).goal_mode == "watch"
