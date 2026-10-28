"""/v1/team-roster —— GUI 团队面板唯一的读接口。

守的是三件事：
  1. 没有团队时给出明确的空形状（前端据此渲染"这个会话还没有团队"，而不是报错）；
  2. 有团队时把 worker 的**运行态**折进去（run 中的引擎 / 已派发未开跑的 drain），
     以及每人手里的活和整块看板的计数——这些是面板的全部信息量；
  3. 纯读：连调用两次不会往板上多出任何东西（面板 3s 轮询一次，绝不能有副作用）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.server import SessionManager, create_app
from coworker.teams import Actor, Role
from coworker.teams.model import space_for_workspace
from coworker.teams.registry import TeamWorker

USER = Actor(id="user", role=Role.USER)


class _Provider(ProviderClient):
    def complete(self, *, model, messages, tools=None, **settings):
        return AssistantTurn(text="ok", finish_reason="stop")

    def capabilities(self, model):
        return ModelCapabilities()


def _manager(tmp_path) -> SessionManager:
    return SessionManager(workspace=tmp_path, provider=_Provider())


def test_roster_for_a_session_without_a_team(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    body = client.get("/v1/team-roster", params={"session_id": "nope"}).json()
    assert body == {"team": None, "role": None}


def test_roster_folds_workers_liveness_and_the_board(tmp_path):
    manager = _manager(tmp_path)
    space = space_for_workspace(tmp_path)
    manager.teams.create(
        space=space,
        lead_session="lead1",
        lead_actor="cowork:lead",
        workers=[
            TeamWorker(actor="writer", persona="historian", session_id="w1", reason="查称谓"),
            TeamWorker(actor="editor", persona="lit-editor", session_id="w2", reason="校错字"),
        ],
    )
    a = manager.team_store.create_item(space, USER, title="顺时间线", criteria="锚点对齐")
    b = manager.team_store.create_item(space, USER, title="核对称谓", criteria="前后一致")
    manager.team_store.assign(space, USER, a["id"], "writer")
    manager.team_store.transition(space, USER, a["id"], "in_progress")
    manager.team_store.assign(space, USER, b["id"], "editor")

    body = TestClient(create_app(manager)).get(
        "/v1/team-roster", params={"session_id": "lead1"}
    ).json()

    assert body["role"] == "lead"
    assert body["team"]["team_id"]
    assert body["team"]["lead_actor"] == "cowork:lead"
    # 两个 worker 都在名单里，各自带着"我手里有什么"
    by_actor = {w["actor"]: w for w in body["workers"]}
    assert set(by_actor) == {"writer", "editor"}
    assert by_actor["writer"]["current"] == "顺时间线"
    assert by_actor["writer"]["counts"] == {"in_progress": 1}
    assert by_actor["editor"]["counts"] == {"open": 1}
    # 没有引擎在跑 → 不能谎报在跑
    assert by_actor["writer"]["running"] is False
    assert body["team"]["lead_running"] is False
    # 看板计数与列表
    assert body["counts"] == {"in_progress": 1, "open": 1}
    assert {i["title"] for i in body["items"]} == {"顺时间线", "核对称谓"}


def test_roster_is_read_only(tmp_path):
    manager = _manager(tmp_path)
    space = space_for_workspace(tmp_path)
    manager.teams.create(space=space, lead_session="lead1", lead_actor="cowork:lead", workers=[])
    manager.team_store.create_item(space, USER, title="只有一条", criteria="—")
    client = TestClient(create_app(manager))
    for _ in range(3):
        client.get("/v1/team-roster", params={"session_id": "lead1"})
    assert len(manager.team_store.list_items(space, USER)) == 1


def test_roster_from_a_worker_session_resolves_its_team(tmp_path):
    # worker 自己打开这个面板时也要能看见整支队伍（role 标成 worker）
    manager = _manager(tmp_path)
    space = space_for_workspace(tmp_path)
    manager.teams.create(
        space=space,
        lead_session="lead1",
        lead_actor="cowork:lead",
        workers=[TeamWorker(actor="writer", persona="historian", session_id="w1")],
    )
    body = TestClient(create_app(manager)).get(
        "/v1/team-roster", params={"session_id": "w1"}
    ).json()
    assert body["role"] == "worker"
    assert body["team"]["lead_session"] == "lead1"
    assert [w["actor"] for w in body["workers"]] == ["writer"]
