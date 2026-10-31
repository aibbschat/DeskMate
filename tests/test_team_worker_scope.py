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
import threading
from pathlib import Path

import pytest
from coworker.permissions import Mode, PermissionEngine
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams.registry import TeamWorker
from deskmate.creation import agent_bridge


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


def test_worker_lane_holds_when_the_scratch_base_is_a_symlink(manager, tmp_path, monkeypatch):
    """判定级：写自己的输出目录必须真的放行——哪怕 scratch 基址是个符号链接。

    之前的用例只断言 `worker_scope.name`，从不 evaluate 一次真实写，于是这条溜了过去：
    车道赋的是未 resolve 的 `scratch_base()/sid`，而判定侧会把路径 resolve，
    `/tmp → /private/tmp` 一错位就整个车道失效，写文件重新回到「等审批」，
    表现就是「派下去的活产出不了」。默认 `~/OpenWorker` 不含软链，所以平时不发作。
    """
    real = tmp_path / "real-scratch"
    real.mkdir()
    link = tmp_path / "linked-scratch"
    link.symlink_to(real)  # scratch 基址在软链之后
    monkeypatch.setenv("COWORKER_SCRATCH_BASE", str(link))

    engine = manager.get_engine("worker", agent="swe-worker")
    own = Path(manager.scratch_base()) / "worker" / "成稿.md"
    d = engine.permissions.evaluate("write_file", {"path": str(own), "content": "x"})
    assert d.allowed, f"软链基址下车道失效：{d.reason}"
    assert "own output directory" in d.reason


def test_pipeline_team_worker_can_write_the_shared_scratch(manager, tmp_path):
    """判定级：流水线团队的共享盘必须可写，而不是被 root 作用域提前挡掉。

    创作流水线的主编是合成的 `creation:<pid>:lead`，没有 session record；旧代码据此
    整个跳过 team-scratch root，写共享盘在车道之前就被判「path is not in a writable
    directory」——等于「交工件给主编」这条路对流水线是死的。
    """
    agent_bridge.set_manager(manager)
    try:
        space = "creation:sharedroot"
        res = agent_bridge.reserve_worker_sessions(space, ["screenwriter"])
        sid = res["screenwriter"]
        team = manager.teams.create(
            space=space,
            lead_session=f"{space}:lead",
            lead_actor="lead",
            workers=[TeamWorker(actor="screenwriter", persona="screenwriter", session_id=sid)],
        )
        agent_bridge.bind_team(space, team)
        engine = manager.get_engine(sid, agent="screenwriter")
        labels = [getattr(r, "label", "") for r in (engine.roots or [])]
        assert "team scratch" in labels, f"流水线团队缺共享盘 root：{labels}"
        shared = Path(manager.scratch_base()) / f"{space}:lead" / "交接.md"
        d = engine.permissions.evaluate("write_file", {"path": str(shared), "content": "x"})
        assert d.allowed, f"共享盘仍被拦：{d.reason}"
    finally:
        agent_bridge.set_manager(None)


def test_dispatch_rejects_a_pure_acknowledgement(manager, tmp_path):
    """判定级：只回一句「已写入」而文件没写出来，必须算交付失败。

    派活 prompt 明确要求「回复只写一句『已写入』」，正文在文件里。所以文件缺失时，
    兜底拿到的最后一条 assistant 文本极可能就是这句确认语；若照单全收，流水线会把
    三个字当成大纲/剧本正文存进去——无报错、无告警，比失败更糟。
    """
    agent_bridge.set_manager(manager)
    space = "creation:ackprobe"
    res = agent_bridge.reserve_worker_sessions(space, ["screenwriter"])
    sid = res["screenwriter"]
    team = manager.teams.create(
        space=space,
        lead_session=f"{space}:lead",
        lead_actor="lead",
        workers=[TeamWorker(actor="screenwriter", persona="screenwriter", session_id=sid)],
    )
    agent_bridge.bind_team(space, team)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    manager._loop = loop
    try:
        async def only_confirms(session_id, message, *, source=None):
            rec = manager.session_store.load(session_id)
            rec.messages.append({"role": "user", "content": message})
            rec.messages.append({"role": "assistant", "content": "已写入"})
            manager.session_store.save(rec)

        manager.deliver_to_session = only_confirms
        assert (
            agent_bridge.dispatch_task(space, "screenwriter", "写第一场", timeout=10) is None
        ), "确认语不能当成交稿正文"

        async def writes_the_file(session_id, message, *, source=None):
            p = Path(manager.scratch_base()) / session_id / "creation" / "draft.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("第一场 诏狱走廊 夜 内\n苏源: 你还记得那道门吗？", encoding="utf-8")

        manager.deliver_to_session = writes_the_file
        got = agent_bridge.dispatch_task(space, "screenwriter", "写第二场", timeout=10)
        assert got and "诏狱走廊" in got, "真成稿仍须照常取回"
    finally:
        manager._loop = None
        loop.call_soon_threadsafe(loop.stop)
        agent_bridge.set_manager(None)


def _shared_engine(tmp_path, shared: str):
    """A worker-lane engine that also owns the team's shared scratch."""
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
            {"path": shared, "writable": True},
        ],
        worker_scope=lane,
        worker_shared_scopes=(shared,),
    )


def test_worker_lane_allows_writes_into_the_team_shared_scratch(tmp_path):
    """共享盘是可写 root，就必须也能写——否则无人值守的 worker 会卡在审批上。

    现场：root 里给了 lead 的 team scratch，但作用域只认 worker 自己的目录，
    写共享盘会弹卡，而团队场景没有人在旁边点允许。
    """
    shared = tmp_path / "scratch" / "lead"
    shared.mkdir(parents=True, exist_ok=True)
    d = _shared_engine(tmp_path, str(shared)).evaluate(
        "write_file", {"path": str(shared / "交接.md"), "content": "x"}
    )
    assert d.allowed and "shared scratch" in d.reason
    # 自己的目录仍然是「自己的目录」这条口径，不被共享盘顶掉
    own = _shared_engine(tmp_path, str(shared)).evaluate(
        "write_file",
        {"path": str(tmp_path / "scratch" / "worker" / "草稿.md"), "content": "x"},
    )
    assert own.allowed and "own output directory" in own.reason


def test_worker_shared_scratch_still_refuses_everything_else(tmp_path):
    """共享盘不是提权：稿子目录、别人的会话目录照样要问。"""
    shared = tmp_path / "scratch" / "lead"
    shared.mkdir(parents=True, exist_ok=True)
    eng = _shared_engine(tmp_path, str(shared))
    assert not eng.evaluate(
        "write_file", {"path": str(tmp_path / "repo" / "正文.md"), "content": "x"}
    ).allowed
    assert not eng.evaluate(
        "write_file",
        {"path": str(tmp_path / "scratch" / "someone-else" / "x.md"), "content": "x"},
    ).allowed


def test_shared_scratch_is_wired_only_for_the_worker(manager):
    """接线：worker 的 engine 拿到团队共享盘（lead 的 scratch），lead 自己没有。"""
    worker_engine = manager.get_engine("worker", agent="swe-worker")
    lead_engine = manager.get_engine("lead", agent="swe-lead")
    shared = worker_engine.permissions.worker_shared_scopes
    assert shared, "worker 必须拿到团队共享盘，否则写交接文件会被拦"
    assert shared[0].name == "lead"
    assert not lead_engine.permissions.worker_shared_scopes


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
