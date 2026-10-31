"""Team lead lane (owner ruling 2026-10-31, 「给 lead 开审批车道」).

现场（真专家实跑第一轮，16:04–16:43 卡死 39 分钟）：

- 16:03:42 dengying 请求 apply_unified_diff（改书稿目录文件）→ 正确弹审批；
- 16:04:37 主编被 board wake，正确发出 decide_worker_call(allow) 准备代批；
- **但 decide_worker_call 的 metadata 是 requires_approval=True（上游故意
  "Consequential on purpose"）→ 「审批者批人还要被批」→ 主编自己的调用也挂进
  信箱等用户 → 无人值守团队必死锁。**
- 之后 3 个 worker 的 write_file 审批全部 pending，主编回合 suspended 39 分钟。

修法：团队 lead 对「团队管理工具」开车道——decide_worker_call / steer_worker
自动放行。地板语义不变：lead 的文件写、shell、删除、越权操作照旧审批；
工具注册面只有 lead 挂载这两个工具（`if role == "lead"`），车道再按
manager 认定的 lead 身份双保险。
"""

import asyncio

import aisuite as ai
import pytest
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams.registry import TeamWorker

# 与 decide_worker_call_tool / steer_worker 注册时的 metadata 同构：
# requires_approval=True 正是现场把主编拦下的那道闸。
MD = ai.ToolMetadata(
    category="team",
    risk_level="medium",
    capabilities=["team"],
    requires_approval=True,
)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "repo").mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    for sid, agent in (("lead", "swe-lead"), ("worker", "swe-worker"), ("civilian", "code")):
        m.session_store.save(
            SessionRecord(
                session_id=sid,
                workspace=m.default_workspace,
                model="sample",
                mode="interactive",
                agent=agent,
            )
        )
    m.teams.create(
        space=m._board_space("lead"),
        lead_session="lead",
        lead_actor="lead",
        workers=[TeamWorker(actor="sam", persona="swe-worker", session_id="worker")],
    )
    yield m
    asyncio.run(m.aclose())


def test_lead_lane_allows_decide_worker_call(manager):
    """车道本体：lead 的 decide_worker_call（审批 worker 的动作本身）不再要人批。"""
    engine = manager.get_engine("lead")
    d = engine.permissions.evaluate(
        "decide_worker_call",
        {"worker": "sam", "call_id": "abc", "decision": "allow", "note": "在范围内，放行"},
        metadata=MD,
    )
    assert d.allowed, f"lead 的 decide_worker_call 仍要审批: {d.reason}"


def test_lead_lane_allows_steer_worker(manager):
    """steer_worker 是主编向下发纠偏文本的通道，同样属于职责本身。"""
    engine = manager.get_engine("lead")
    d = engine.permissions.evaluate(
        "steer_worker",
        {"worker": "sam", "message": "先交 #15，别动 #17"},
        metadata=MD,
    )
    assert d.allowed, f"lead 的 steer_worker 仍要审批: {d.reason}"


def test_lead_lane_does_not_widen_writes(manager, tmp_path):
    """车道只覆盖团队管理工具：lead 写任意路径（含工作区）、跑 shell 照旧审批。"""
    engine = manager.get_engine("lead")
    p = engine.permissions
    w = p.evaluate("write_file", {"path": str(tmp_path / "repo" / "x.md"), "content": "x"})
    assert not w.allowed and w.needs_user, "lead 的 write_file 被车道放宽了"
    s = p.evaluate("run_shell", {"command": "ls"})
    assert not s.allowed, "lead 的 run_shell 被车道放宽了"


def test_lead_lane_is_not_for_workers_or_civilians(manager):
    """非 lead 不享受车道：worker / 普通会话调同名工具照样要批。"""
    worker_engine = manager.get_engine("worker")
    wd = worker_engine.permissions.evaluate(
        "decide_worker_call",
        {"worker": "sam", "call_id": "abc", "decision": "allow", "note": ""},
        metadata=MD,
    )
    assert not wd.allowed and wd.needs_user, "worker 白拿了 lead 车道"

    civ_engine = manager.get_engine("civilian")
    cd = civ_engine.permissions.evaluate(
        "steer_worker", {"worker": "sam", "message": "hi"}, metadata=MD
    )
    assert not cd.allowed and cd.needs_user, "普通会话白拿了 lead 车道"


def test_lead_lane_survives_durable_resume(manager):
    """_durable_resume 重建引擎也走 get_engine → 主编批卡恢复后车道仍在。"""
    manager._engines.pop("lead", None)  # 模拟引擎被逐出（重启后 resume 的情形）
    engine = manager.get_engine("lead")
    d = engine.permissions.evaluate(
        "decide_worker_call",
        {"worker": "sam", "call_id": "abc", "decision": "deny", "note": "越权"},
        metadata=MD,
    )
    assert d.allowed, "durable resume 之后 lead 车道丢了"


def test_lead_lane_removed_when_team_disbands(manager):
    """团队解散（最后一个 worker 离队即整队拆除）后，旧 lead 不再享受车道。"""
    engine = manager.get_engine("lead")
    assert engine.permissions.evaluate(
        "decide_worker_call", {"worker": "sam", "call_id": "a", "decision": "allow"}, metadata=MD
    ).allowed
    manager.teams.detach_session("worker")  # 最后一个 worker 离队 → 整队解散
    manager._engines.pop("lead", None)
    engine = manager.get_engine("lead")
    d = engine.permissions.evaluate(
        "decide_worker_call", {"worker": "sam", "call_id": "a", "decision": "allow"}, metadata=MD
    )
    assert not d.allowed and d.needs_user, "团队没了 lead 车道还在"
