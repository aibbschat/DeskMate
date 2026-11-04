"""A 阶段地基契约（先红后绿）。

锁住四件事：
1. TaskRun 有可持久化的状态字段，且旧 JSON / 脏键都能安全读回；
2. 任务结果有确定性的投递锚点与全局完成事件；
3. 完成投递真的写进 proactive_ledger（含失败任务），且幂等不重复；
4. 副作用闸门骨架默认不改变现有 BYPASS 行为。

最后一条「接线检查」是刻意加的：本项目多次踩过「模块写好了但没挂进调用链 /
没进传输清单，被 fail-open 吞掉静默失效」，所以必须锁住 manager 与前端的接线。
测试库一律放 tmp_path，绝不碰 ~/.deskmate/deskmate.db。
"""

from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace

from coworker.automation.models import TaskRun

DURABLE_FIELDS = (
    "progress",
    "current_step",
    "checkpoint",
    "paused_at",
    "resumed_at",
    "cancel_requested",
    "delivered_at",
    "event_count",
)


def _run(**kw) -> TaskRun:
    kw.setdefault("task_id", "task-autonomy-1")
    return TaskRun(**kw)


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id="task-autonomy-1",
        title="盯一个公开页面",
        instructions="盯住这个页面，有变化再报告",
        workspace="/tmp/cw-autonomy",
        agent="deskmate",
        notify_on_completion=True,
        notify_target=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# -- 1. TaskRun 状态契约 -------------------------------------------------------
def test_run_has_durable_state_fields():
    run = _run()
    for name in DURABLE_FIELDS:
        assert hasattr(run, name), f"TaskRun 缺字段 {name}"
    assert run.progress == 0.0
    assert run.checkpoint == {}
    assert run.cancel_requested is False
    assert run.delivered_at is None
    assert run.event_count == 0


def test_old_run_json_loads_with_new_fields():
    """旧库里存的 blob 没有新字段，必须仍能读回且保留原状态。回归护栏。"""
    old = {
        "task_id": "task-autonomy-1",
        "run_id": "run-old",
        "started_at": 1730000000.0,
        "finished_at": 1730000001.0,
        "status": "ok",
        "result_text": "done",
        "artifacts": [],
        "error": None,
        "trigger": "schedule",
        "session_id": "__run__run-old",
    }
    run = TaskRun.from_dict(old)
    assert run.status == "ok"
    assert run.result_text == "done"
    for name in DURABLE_FIELDS:
        assert hasattr(run, name)


def test_unknown_keys_in_run_json_are_ignored():
    """前向兼容：将来/脏数据多出来的键不能让整个 run 读不回来。"""
    blob = _run().to_dict()
    blob["future_field_from_another_build"] = {"x": 1}
    run = TaskRun.from_dict(blob)
    assert run.task_id == "task-autonomy-1"


# -- 2. 完成事件与锚点 ---------------------------------------------------------
def test_completion_event_contract():
    from deskmate.autonomy.delivery import build_completion_event

    task, run = _task(), _run()
    ev = build_completion_event(task, run, status="ok")
    assert ev["type"] == "automation_run_finished"
    data = ev["data"]
    for key in (
        "task_id", "task_title", "run_id", "session_id", "status",
        "text", "ledger_id", "workspace", "agent", "finished_at",
    ):
        assert key in data, f"事件缺字段 {key}"
    assert data["run_id"] == run.run_id
    assert data["task_id"] == task.id
    assert data["status"] == "ok"


def test_delivery_anchor_is_stable_and_scoped():
    from deskmate.autonomy.delivery import delivery_anchor

    a = delivery_anchor("task-1", "run-a")
    assert a == delivery_anchor("task-1", "run-a")          # 同一次投递幂等
    assert a != delivery_anchor("task-1", "run-b")          # 不同 run 不串
    assert "run-a" in a


# -- 3. 真的写台账、幂等、失败也投 ---------------------------------------------
def _ledger_rows(base_dir):
    db = base_dir / "deskmate.db"
    if not db.is_file():
        return []  # no delivery happened at all — no table is the correct outcome
    with sqlite3.connect(str(db)) as c:
        try:
            return c.execute(
                "SELECT id, kind, content, evidence FROM proactive_ledger"
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def test_deliver_run_result_writes_ledger(tmp_path):
    from deskmate.autonomy.delivery import deliver_run_result

    run = _run(result_text="页面补了一则通知，已标出差异。", status="ok")
    ledger_id = deliver_run_result(_task(), run, base_dir=tmp_path)
    assert ledger_id, "投递成功应返回台账 id"
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][1] == "task_result"
    assert "页面补了一则通知" in rows[0][2]
    assert rows[0][3].strip(), "evidence 不能为空（写入门槛 + 可追溯）"
    assert run.delivered_at is not None, "投递后要留凭证"


def test_deliver_run_result_idempotent(tmp_path):
    from deskmate.autonomy.delivery import deliver_run_result

    task, run = _task(), _run(result_text="同一条结果", status="ok")
    first = deliver_run_result(task, run, base_dir=tmp_path)
    second = deliver_run_result(task, run, base_dir=tmp_path)
    assert second == first
    assert len(_ledger_rows(tmp_path)) == 1, "已投递的 run 不能重复写台账"


def test_failed_run_is_still_delivered(tmp_path):
    from deskmate.autonomy.delivery import deliver_run_result

    run = _run(status="error", error="provider 429，已重试 3 次")
    assert deliver_run_result(_task(), run, base_dir=tmp_path)
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert "429" in rows[0][2], "失败也要说清卡在哪，不能静默"


def test_empty_result_is_not_delivered(tmp_path):
    """三层空内容守卫的源头：没有实产就不要制造一张空卡。"""
    from deskmate.autonomy.delivery import deliver_run_result

    run = _run(result_text="   ", status="ok")
    assert deliver_run_result(_task(), run, base_dir=tmp_path) == ""
    assert _ledger_rows(tmp_path) == []


# -- 4. 闸门骨架：默认不越界 ---------------------------------------------------
def test_gate_default_preserves_bypass():
    from deskmate.autonomy.gate import GatePolicy

    policy = GatePolicy()
    for risk in ("READ", "EGRESS", "EXTERNAL", "WRITE_LOCAL", "EXEC", "unknown"):
        assert policy.decide(task=_task(), tool_name="send_message", risk_class=risk) == "allow"


# -- 5. 接线检查（防「写了没挂上」） -------------------------------------------
def test_manager_completion_path_is_wired():
    from coworker.server.manager import SessionManager

    src = (
        inspect.getsource(SessionManager._run_scheduled_task)
        + inspect.getsource(SessionManager._notify_task_done)
    )
    assert "deliver_run_result" in src, "完成路径没有接投递函数"
    assert "automation_run_finished" in src, "完成路径没有发全局事件"


def test_frontend_consumes_completion_event():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    app = (repo / "surfaces/gui/src/App.tsx").read_text(encoding="utf-8")
    assert "automation_run_finished" in app, "前端没有消费完成事件（窗口没开就收不到结果）"


def test_run_scheduled_task_closes_delivery_loop(tmp_path, monkeypatch):
    """真实跑一次完成路径：事件发出 + 台账落行 + 凭证持久化 + 检查点保留。

    静态「接线检查」只能证明源码里有字符串；这条用最小 manager 实例走完
    _run_scheduled_task，验收「做完了用户确实收得到」。所有库都放 tmp_path，
    并 patch 掉 after_action 的后台线程，绝不碰 ~/.deskmate 生产库。
    """
    import asyncio

    import deskmate.autonomy.delivery as delivery_mod
    import deskmate.memory.after_action as after_action
    from coworker.automation.store import TaskStore
    from coworker.server.manager import SessionManager

    original = delivery_mod.deliver_run_result
    monkeypatch.setattr(
        delivery_mod,
        "deliver_run_result",
        lambda task, run, *, base_dir=None: original(task, run, base_dir=tmp_path),
    )
    monkeypatch.setattr(after_action, "reflect_and_persist", lambda *a, **k: None)

    class FakeEngine:
        messages = [{"role": "assistant", "content": "页面补了一则通知，差异已标出。"}]

        async def run(self, opening):
            for i in range(1, 7):
                yield SimpleNamespace(
                    type=SimpleNamespace(name="ITERATION_END"),
                    data={"iteration": i},
                )

    monkeypatch.setattr(
        SessionManager,
        "_build_task_engine",
        lambda self, task, *, session_id: FakeEngine(),
    )

    mgr = SessionManager.__new__(SessionManager)
    mgr.task_store = TaskStore(str(tmp_path / "automation.db"))
    mgr._engines = {}
    mgr.save = lambda sid, engine: None
    pushed: list[dict] = []

    async def fake_broadcast(payload):
        pushed.append(payload)

    mgr.broadcast_event = fake_broadcast

    task = _task(notify_on_completion=False, workspace=str(tmp_path))
    run = asyncio.run(mgr._run_scheduled_task(task, "manual"))

    assert run.status == "ok"
    assert run.event_count == 6
    assert run.checkpoint.get("last_iteration") == 6, "中途检查点没留住"
    assert run.delivered_at is not None, "投递凭证没写"

    finished = [e for e in pushed if e.get("type") == "automation_run_finished"]
    assert len(finished) == 1, "完成事件没有发到全局 /ws/events"
    assert finished[0]["data"]["ledger_id"], "事件里没带台账 id"

    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0][1] == "task_result"
    assert "页面补了一则通知" in rows[0][2]

    stored = mgr.task_store.find_run(run.run_id)
    assert stored is not None and stored.delivered_at is not None, "凭证没落库，重启会重复投递"
