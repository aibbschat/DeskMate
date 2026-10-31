"""C+ 动态加人：建团后仍能临时补一位专家（不重建团队），含创作流水线真会话。

覆盖：
- TeamRegistry.add_worker 的追加/落盘/跨实例重读；
- SessionManager.add_to_team 的校验（worker trait、callname 去重、无团队拒绝）与预建会话；
- 专家桥在主 app 缺席时 fail-open（不抛、不假装成功）；
- 创作流水线建团即为 worker 预建真会话，add_expert 之后花名册立刻可见。
"""

import asyncio

import pytest

from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams.registry import TeamRegistry, TeamWorker
from deskmate.creation import agent_bridge
from deskmate.creation.executor import StubExecutor
from deskmate.creation.pipeline import CreativePipeline


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "repo").mkdir(exist_ok=True)
    m = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    yield m
    agent_bridge.set_manager(None)
    asyncio.run(m.aclose())


def staff_lead(m, *, session_id="lead", actor="sam", persona="swe-worker"):
    m.session_store.save(
        SessionRecord(
            session_id=session_id,
            workspace=m.default_workspace,
            model="sample",
            mode="interactive",
            agent="swe-lead",
        )
    )
    m.teams.create(
        space=m._board_space(session_id),
        lead_session=session_id,
        lead_actor="lead",
        workers=[TeamWorker(actor=actor, persona=persona, session_id="worker")],
    )


# --------------------------------------------------------------- registry 层

def test_add_worker_appends_and_persists(tmp_path):
    path = tmp_path / "teams.json"
    reg = TeamRegistry(path)
    team = reg.create(
        space="proj",
        lead_session="lead",
        lead_actor="lead",
        workers=[TeamWorker(actor="sam", persona="swe-worker", session_id="w1")],
    )
    reg.add_worker(team.team_id, TeamWorker(actor="plot", persona="design-worker", session_id="w2"))
    assert [w.actor for w in reg.get(team.team_id).workers] == ["sam", "plot"]

    # 另一个实例（deskmate 侧）读盘后也应看到新成员
    other = TeamRegistry(path)
    assert [w.actor for w in other.get(team.team_id).workers] == ["sam", "plot"]


def test_add_worker_unknown_team_returns_none(tmp_path):
    reg = TeamRegistry(tmp_path / "teams.json")
    assert reg.add_worker("nope", TeamWorker(actor="x", persona="y", session_id="z")) is None


# --------------------------------------------------------------- manager 层

def test_add_to_team_spawns_real_worker_session(manager):
    staff_lead(manager)
    team = manager.teams.for_lead_session("lead")
    res = manager.add_to_team("lead", {"persona": "design-worker", "name": "plot"})
    assert res["approved"], res
    worker = res["worker"]
    assert worker["actor"] == "plot"

    roster = manager.teams.for_lead_session("lead")
    assert [w.actor for w in roster.workers] == ["sam", "plot"]
    rec = manager.session_store.load(worker["session_id"])
    assert rec is not None and rec.agent == "design-worker"
    assert rec.team["team_id"] == team.team_id
    assert rec.team["role"] == "worker"
    assert rec.team["lead_session"] == "lead"


def test_add_to_team_dedupes_callname(manager):
    staff_lead(manager)
    first = manager.add_to_team("lead", {"persona": "design-worker", "name": "sam"})
    second = manager.add_to_team("lead", {"persona": "design-worker", "name": "sam"})
    assert first["worker"]["actor"] == "sam-2"
    assert second["worker"]["actor"] == "sam-3"


def test_add_to_team_rejects_non_worker_persona(manager):
    staff_lead(manager)
    res = manager.add_to_team("lead", {"persona": "swe-lead"})
    assert res["approved"] is False
    assert "team-capable" in res["error"]


def test_add_to_team_requires_existing_team(manager):
    res = manager.add_to_team("nobody", {"persona": "design-worker"})
    assert res["approved"] is False
    assert "no team" in res["error"]


# --------------------------------------------------------------- 专家桥 fail-open

def test_bridge_is_inert_without_manager():
    agent_bridge.set_manager(None)
    assert agent_bridge.dispatch_task("creation:x", "screenwriter", "写一集") is None
    assert agent_bridge.reserve_worker_sessions("creation:x", ["screenwriter"]) == {}
    with pytest.raises(RuntimeError):
        agent_bridge.add_expert("creation:x", "design-worker")


# --------------------------------------------------------------- 创作流水线 C+ 

def test_pipeline_staffs_real_sessions_and_can_add_expert(manager, tmp_path):
    agent_bridge.set_manager(manager)
    pl = CreativePipeline(
        space="creation:addtest",
        base_dir=tmp_path / "data",
        executor=StubExecutor(),
    )
    pl.start_screenplay_project("一个关于诏狱的故事")
    assert pl._team is not None
    for worker in pl._team.workers:
        rec = manager.session_store.load(worker.session_id)
        assert rec is not None, f"{worker.actor} 应有真会话"
        assert rec.agent == worker.persona
        assert rec.team["team_id"] == pl._team.team_id

    res = pl.add_expert("design-worker", name="plot", reason="情节架构")
    assert res["approved"], res
    assert [w.actor for w in pl._team.workers].count("plot") == 1
    rec = manager.session_store.load(res["worker"]["session_id"])
    assert rec is not None and rec.team["space"] == pl.space


# --------------------------------------------------------------- 对话式加人（对主编说）

def test_parse_intent_recognizes_add_expert():
    from deskmate.creation.chat_flow import parse_intent

    i = parse_intent("加个情节架构专家", {})
    assert i.type == "add_expert" and i.expert_name == "情节架构"
    i = parse_intent("帮我把情节架构专家加进来", {})
    assert i.type == "add_expert" and i.expert_name == "情节架构"
    i = parse_intent("加人", {})
    assert i.type == "add_expert" and i.expert_name == ""
    # 圆桌不能误吞成加人
    assert parse_intent("请七专家终审", {}).type == "roundtable"


def test_chat_add_expert_staffs_the_team(manager, tmp_path):
    agent_bridge.set_manager(manager)
    pl = CreativePipeline(
        space="creation:chattest",
        base_dir=tmp_path / "data",
        executor=StubExecutor(),
    )
    pl.start_screenplay_project("一个关于诏狱的故事")

    from deskmate.creation.chat_flow import CreationChat

    chat = CreationChat(pl, "chattest")

    async def _drain():
        return [m async for m in chat.post("加个 design-worker 专家")]

    msgs = asyncio.run(_drain())
    assert any("已加入团队" in (m.get("title") or m.get("text") or "") for m in msgs), msgs
    assert any(w.persona == "design-worker" for w in pl._team.workers)
