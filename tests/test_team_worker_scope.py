"""Team worker lane + the orphaned-team fix (owner ruling 2026-10-31).

Two defects behind "派活下去，下面的 Agent 产出不了东西":

1. A pre-spawned team worker has no human at a keyboard, so every consequential call
   parked it on an approval until somebody answered. Measured on 2026-10-31: two full
   rounds dispatched, zero worker output, and the lead quietly did the work itself.
   Fix (option A): the worker gets a lane — lookups and writes inside its OWN
   per-session output directory run without asking. Everything else (shell, deletes,
   writes outside its own directory i.e. your manuscript, persistent-authority tools,
   protected in-project files) still asks exactly as before.

2. Deleting a session left the team registry advertising a worker whose session no
   longer existed: `assign` validated against the ghost, the wake went to a session
   with no record and no engine, nothing ran, and nothing told the lead — the board
   kept showing in_progress work that was never going to happen. Fix: deleting a
   session detaches it from its team first, so further assignment fails loudly.
"""

import asyncio

import pytest
from coworker.permissions import Mode, PermissionEngine
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams.registry import TeamWorker


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "repo").mkdir()
    m = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    for sid, agent in (("lead", "swe-lead"), ("worker", "swe-worker")):
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


def _engine(tmp_path, *, worker_scope: bool):
    """An INTERACTIVE engine rooted at `repo`, with the worker's own output dir also
    writable (that is what a real worker session gets)."""
    ws = tmp_path / "repo"
    ws.mkdir(exist_ok=True)
    lane = tmp_path / "scratch" / "worker"
    lane.mkdir(parents=True, exist_ok=True)
    return PermissionEngine(
        workspace_root=ws,
        mode=Mode.INTERACTIVE,
        roots=[
            {"path": str(ws), "writable": True},
            {"path": str(lane), "writable": True},
        ],
        worker_scope=lane if worker_scope else None,
    )


# -- 1. the worker lane --------------------------------------------------------


def test_worker_lane_allows_lookups_that_used_to_park_it(tmp_path):
    """The exact call kanyu stalled on: web_search. In its lane it just runs."""
    assert _engine(tmp_path, worker_scope=True).evaluate(
        "web_search", {"query": "汉阳都城 五间水门"}
    ).allowed
    # ...and a human-facing session is untouched — it still asks.
    human = _engine(tmp_path, worker_scope=False).evaluate(
        "web_search", {"query": "x"}
    )
    assert not human.allowed and human.needs_user


def test_worker_lane_allows_writes_inside_its_own_output_dir(tmp_path):
    """The exact call gouzhang stalled on: write_file into its own directory."""
    lane = tmp_path / "scratch" / "worker"
    d = _engine(tmp_path, worker_scope=True).evaluate(
        "write_file", {"path": str(lane / "章纲-v2.md"), "content": "x"}
    )
    assert d.allowed and "own output directory" in d.reason


def test_worker_lane_still_asks_for_writes_outside_its_own_dir(tmp_path):
    """Your manuscript is not the worker's lane. A write there still needs a decision."""
    d = _engine(tmp_path, worker_scope=True).evaluate(
        "write_file", {"path": str(tmp_path / "repo" / "正文.md"), "content": "x"}
    )
    assert not d.allowed and d.needs_user


def test_worker_lane_still_asks_for_shell_and_deletes(tmp_path):
    """Exec stays gated: a worker running commands is a different risk class."""
    d = _engine(tmp_path, worker_scope=True).evaluate(
        "run_shell", {"command": "rm -rf something"}
    )
    assert not d.allowed and d.needs_user


def test_worker_lane_does_not_widen_by_relative_path(tmp_path):
    """A relative path is anchored to the workspace, same as root scoping — so a worker
    can't name `../正文.md` and land outside its lane."""
    lane = tmp_path / "scratch" / "worker"
    eng = _engine(tmp_path, worker_scope=True)
    assert eng.evaluate("write_file", {"path": "draft.md", "content": "x"}).allowed is False
    # sanity: the same engine does allow the lane's absolute path
    assert eng.evaluate(
        "write_file", {"path": str(lane / "draft.md"), "content": "x"}
    ).allowed


def test_worker_scope_is_only_set_for_the_worker_role(manager):
    """Wiring: a worker session's engine carries the lane; the lead's does not."""
    worker_engine = manager.get_engine("worker", agent="swe-worker")
    lead_engine = manager.get_engine("lead", agent="swe-lead")
    assert worker_engine.permissions.worker_scope is not None
    assert worker_engine.permissions.worker_scope.name == "worker"
    assert lead_engine.permissions.worker_scope is None


# -- 2. deleting a session no longer orphans the team --------------------------


def test_deleting_a_worker_detaches_it_from_the_team(manager):
    """The 2026-10-31 failure: after the delete, the registry still listed 'sam', so
    `assign` succeeded and the wake went to a session that no longer existed."""
    assert manager.teams.for_worker_session("worker") is not None
    out = manager.delete_session("worker")
    assert out["ok"] and out["team"]["role"] == "worker"
    assert manager.teams.for_worker_session("worker") is None


def test_deleting_the_last_worker_drops_the_team_so_the_lead_can_restaff(manager):
    """No workers left = nothing to dispatch to. The team is dropped, otherwise
    `propose_team` keeps answering 'this session already leads a team' forever."""
    manager.delete_session("worker")
    assert manager.teams.for_lead_session("lead") is None


def test_deleting_the_lead_drops_the_team(manager):
    out = manager.delete_session("lead")
    assert out["team"]["role"] == "lead" and out["team"]["team_removed"]
    assert manager.teams.for_lead_session("lead") is None


def test_deleting_a_plain_session_reports_no_team(manager):
    manager.session_store.save(
        SessionRecord(
            session_id="solo",
            workspace=manager.default_workspace,
            model="sample",
            mode="interactive",
            agent="cowork",
        )
    )
    out = manager.delete_session("solo")
    assert out["ok"] and "team" not in out


def test_ghost_members_are_pruned_on_startup(manager, tmp_path, monkeypatch):
    """The state萧哥 was left in: registry lists workers whose sessions were deleted
    out-of-band. A restart must clean the roster instead of needing a hand-edit."""
    # Simulate the orphan: drop the record without going through delete_session.
    manager.session_store.delete("worker")
    assert manager.teams.for_worker_session("worker") is not None  # still a ghost

    healed = SessionManager(data_dir=tmp_path / "data", workspace=tmp_path / "repo")
    try:
        # sam's session is gone, so the team has no members left and is dropped — the
        # lead can staff a fresh team instead of being told it already leads one.
        assert healed.teams.for_worker_session("worker") is None
        assert healed.teams.for_lead_session("lead") is None
    finally:
        asyncio.run(healed.aclose())


def test_prune_skips_pipeline_teams_with_synthetic_sessions(manager, tmp_path):
    """创作流水线用合成 key（`creation:<pid>`）当 board space，成员 session id 也是合成的
    （`creation:<pid>:<persona>`）——它们**本来就不在** session store 里。若照探会删掉
    活着的流水线名册（2026-10-31 第一次上线就把 26 个流水线队全清了）。"""
    manager.teams.create(
        space="creation:abc123",
        lead_session="creation:abc123:lead",
        lead_actor="editor-in-chief",
        workers=[
            TeamWorker(
                actor="screenwriter",
                persona="screenwriter",
                session_id="creation:abc123:screenwriter",
            )
        ],
    )
    pruned = manager.teams.prune_missing_sessions(
        lambda sid: manager.session_store.load(sid) is not None
    )
    assert pruned == []
    assert manager.teams.for_lead_session("creation:abc123:lead") is not None
    assert manager.teams.for_worker_session("creation:abc123:screenwriter") is not None


def test_prune_keeps_members_whose_sessions_are_alive(manager, tmp_path):
    """A healthy roster must survive the prune untouched."""
    pruned = manager.teams.prune_missing_sessions(
        lambda sid: manager.session_store.load(sid) is not None
    )
    assert pruned == []
    assert manager.teams.for_worker_session("worker") is not None


def test_a_worker_keeps_its_team_when_only_its_peer_is_deleted(manager):
    """Two workers: deleting one must not take the other (or the team) with it."""
    manager.session_store.save(
        SessionRecord(
            session_id="worker2",
            workspace=manager.default_workspace,
            model="sample",
            mode="interactive",
            agent="swe-worker",
        )
    )
    team = manager.teams.for_lead_session("lead")
    team.workers.append(
        TeamWorker(actor="kim", persona="swe-worker", session_id="worker2")
    )
    manager.delete_session("worker")
    assert manager.teams.for_worker_session("worker2") is not None
    assert manager.teams.for_lead_session("lead") is not None
    assert [w.actor for w in manager.teams.for_lead_session("lead").workers] == ["kim"]
