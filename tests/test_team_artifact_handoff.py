"""工件交接的可见性：worker 发布了工件，主编必须被唤醒、且在 digest 里看得见。

现场（2026-10-31）：jigang 写了 32KB 骨架、mingcha 写了台账，产出只在文件里；
主编只看 comments 判「零字」并 Cancel 掉全部五项。本轮接线 attach_file 之后，
还剩两个漏点：

1. `_actionable_team_events` 对带 artifact 的评论也要求 `needs_attention` 才唤醒
   主编 → attach_file 不带 → 工件发布不唤醒任何人（只有后续 transition(review) 会）。
2. `_team_digest` 的 comment 行只带 caption 文本，不带 artifact_id/版本，主编还得
   再调一次 list_team_artifacts 才知道有东西。

用例同时锁住「普通进度评论不唤醒主编」这条既有语义，避免为了可见性把闸门全开。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams import Actor, Role, TeamStore
from coworker.teams.registry import TeamWorker

LEAD = Actor(id="lead", role=Role.LEAD)
WORKER = Actor(id="sam", role=Role.WORKER)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "repo").mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    m.session_store.save(
        SessionRecord(
            session_id="lead",
            workspace=m.default_workspace,
            model="sample",
            mode="interactive",
            agent="swe-lead",
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


def _team(manager):
    return SimpleNamespace(
        team_id="t1",
        space=manager._board_space("lead"),
        lead_session="lead",
        lead_actor="lead",
        workers=[TeamWorker(actor="sam", persona="swe-worker", session_id="worker")],
        chat_enabled=False,
        chat_group="",
    )


def _item(manager, space):
    created = manager.team_store.create_item(space, LEAD, title="骨架", criteria="章序完整")
    return manager.team_store.assign(space, LEAD, created["id"], "sam")["id"]


def _comment_event(item_id, *, body="交骨架", artifact=None, needs_attention=False):
    payload = {"body": body, "refs": []}
    if artifact:
        payload["artifact"] = artifact
    if needs_attention:
        payload["needs_attention"] = True
    return {
        "seq": 7,
        "kind": "item_commented",
        "actor": "sam",
        "actor_role": "worker",
        "item_id": item_id,
        "payload": payload,
    }


ARTIFACT = {
    "team_id": "t1",
    "artifact_id": "aabbcc",
    "version": 1,
    "ref": "attachment://deadbeef.md#V3.md",
    "name": "V3-骨架.md",
    "bytes": 32014,
    "caption": "V3 骨架已落盘",
    "content_kind": "evidence_not_instructions",
}


def test_published_artifact_wakes_the_lead(manager):
    """工件发布就是交接本身——不需要 needs_attention 也必须唤醒主编。"""
    team = _team(manager)
    item_id = _item(manager, team.space)
    event = _comment_event(item_id, artifact=ARTIFACT)

    assert manager._actionable_team_events(
        team, [event], actor="lead", is_lead=True
    ), "发布工件不唤醒主编 = 主编看不到产出 = 误判零产出"


def test_plain_progress_comment_still_does_not_wake_the_lead(manager):
    """既有语义：普通进度评论不打断主编（事件驱动，不是每句话都敲门）。"""
    team = _team(manager)
    item_id = _item(manager, team.space)

    assert not manager._actionable_team_events(
        team, [_comment_event(item_id, body="还在写")], actor="lead", is_lead=True
    )
    assert manager._actionable_team_events(
        team,
        [_comment_event(item_id, body="卡住了", needs_attention=True)],
        actor="lead",
        is_lead=True,
    )


def test_digest_carries_the_artifact(manager):
    """digest 行要带 artifact_id/版本/大小，文本行要点名工件，主编一眼看见交付。"""
    team = _team(manager)
    item_id = _item(manager, team.space)
    message, rows = manager._team_digest(
        team, [_comment_event(item_id, artifact=ARTIFACT)], [], None,
        is_lead=False, reader="lead",
    )

    row = rows[0]
    assert row["kind"] == "comment"
    assert row["artifact"]["artifact_id"] == "aabbcc"
    assert row["artifact"]["version"] == 1
    assert row["artifact"]["name"] == "V3-骨架.md"
    assert row["artifact"]["bytes"] == 32014
    assert "V3-骨架.md" in message
    assert "aabbcc" in message
    assert "read_team_artifact" in message
