"""Board verbs: state-machine legality and role authority (the capability firebreak)."""

import pytest

from coworker.teams import Actor, AuthorityError, BoardError, Role, TeamStore
from coworker.teams.dialect import LocalDialect
from coworker.teams.tools import board_tools

USER = Actor(id="user", role=Role.USER)
LEAD = Actor(id="lead-1", role=Role.LEAD)
WORKER = Actor(id="worker-1", role=Role.WORKER)
OTHER = Actor(id="worker-2", role=Role.WORKER)
SPACE = "proj"


@pytest.fixture
def store(tmp_path):
    store = TeamStore(tmp_path / "teams.db")
    yield store
    store.close()


def assigned_item(store, assignee="worker-1"):
    item = store.create_item(SPACE, LEAD, title="Task", criteria="tests pass")
    store.assign(SPACE, LEAD, item["id"], assignee)
    return item["id"]


# ------------------------------------------------------------------ create_item

def test_acceptance_criteria_are_required(store):
    with pytest.raises(BoardError, match="criteria"):
        store.create_item(SPACE, LEAD, title="Vague hope", criteria="  ")


def test_workers_file_items_open_and_unassigned(store):
    mine = assigned_item(store)
    filed = store.create_item(
        SPACE, WORKER, title="Rounding bug in invoices", criteria="repro + fix",
        parent=mine,
    )
    assert filed["state"] == "open"
    assert filed["assignee"] == ""
    assert filed["creator"] == "worker-1"
    # the worker sees its own filing; nothing runs until the lead/user assigns it,
    # and the filer can't assign it to itself
    visible = {item["id"] for item in store.list_items(SPACE, WORKER)}
    assert filed["id"] in visible
    with pytest.raises(AuthorityError):
        store.assign(SPACE, WORKER, filed["id"], "worker-1")
    # open + unassigned = claimable, so OTHER sees it in the pool (open-claims
    # default); under lead-only policy the strict slice rule returns
    other_worker = {item["id"] for item in store.list_items(SPACE, OTHER)}
    assert filed["id"] in other_worker
    store.set_policy(SPACE, LEAD, claims="lead-only")
    other_worker = {item["id"] for item in store.list_items(SPACE, OTHER)}
    assert filed["id"] not in other_worker


def test_child_inherits_parent_case(store):
    parent = store.create_item(
        SPACE, LEAD, title="Root", criteria="c", case="findings"
    )
    child = store.create_item(
        SPACE, LEAD, title="Child", criteria="c", parent=parent["id"]
    )
    assert child["case_id"] == "findings"
    assert {"kind": "parent", "item": parent["id"]} in child["links"]


def test_worker_cannot_expand_its_slice_through_a_hidden_parent(store):
    hidden = assigned_item(store, assignee="worker-2")
    before = store.event_count(SPACE)

    with pytest.raises(BoardError):
        store.create_item(
            SPACE,
            WORKER,
            title="Bridge into another worker's task",
            criteria="must not be created",
            parent=hidden,
        )

    assert store.event_count(SPACE) == before
    assert store.list_items(SPACE, WORKER) == []


# ------------------------------------------------------------------- transitions

def test_full_happy_path(store):
    item_id = assigned_item(store)
    store.transition(SPACE, WORKER, item_id, "in_progress")
    store.transition(SPACE, WORKER, item_id, "review", comment="branch green")
    done = store.transition(SPACE, LEAD, item_id, "done")
    assert done["state"] == "done"


def test_illegal_edges_rejected(store):
    item = store.create_item(SPACE, LEAD, title="T", criteria="c")
    with pytest.raises(BoardError, match="illegal transition"):
        store.transition(SPACE, USER, item["id"], "done")
    with pytest.raises(BoardError, match="illegal transition"):
        store.transition(SPACE, USER, item["id"], "review")


def test_workers_never_mark_done(store):
    item_id = assigned_item(store)
    store.transition(SPACE, WORKER, item_id, "in_progress")
    store.transition(SPACE, WORKER, item_id, "review")
    with pytest.raises(AuthorityError, match="review"):
        store.transition(SPACE, WORKER, item_id, "done")


def test_worker_cannot_touch_someone_elses_item(store):
    item_id = assigned_item(store, assignee="worker-1")
    with pytest.raises(AuthorityError, match="not assigned"):
        store.transition(SPACE, OTHER, item_id, "in_progress")


def test_worker_cannot_cancel(store):
    item_id = assigned_item(store)
    with pytest.raises(AuthorityError):
        store.transition(SPACE, WORKER, item_id, "canceled")


def test_cancel_is_a_board_verb_and_reopen_works(store):
    item_id = assigned_item(store)
    store.transition(SPACE, LEAD, item_id, "canceled")
    reopened = store.transition(SPACE, LEAD, item_id, "open")
    assert reopened["state"] == "open"
    assert reopened["assignee"] == "worker-1"  # reassignable — assignment survives


def test_done_is_terminal(store):
    item_id = assigned_item(store)
    store.transition(SPACE, WORKER, item_id, "in_progress")
    store.transition(SPACE, WORKER, item_id, "review")
    store.transition(SPACE, LEAD, item_id, "done")
    with pytest.raises(BoardError, match="illegal transition"):
        store.transition(SPACE, USER, item_id, "in_progress")


def test_rework_loop(store):
    item_id = assigned_item(store)
    store.transition(SPACE, WORKER, item_id, "in_progress")
    store.transition(SPACE, WORKER, item_id, "review")
    store.transition(SPACE, LEAD, item_id, "in_progress", comment="criteria 2 unmet")
    item = store.get_item(SPACE, item_id, actor=LEAD)
    assert item["state"] == "in_progress"
    assert item["comments"][-1]["body"] == "criteria 2 unmet"


# ---------------------------------------------------------------- assign / link

def test_cannot_assign_closed_items(store):
    item_id = assigned_item(store)
    store.transition(SPACE, LEAD, item_id, "canceled")
    with pytest.raises(BoardError, match="reopen"):
        store.assign(SPACE, LEAD, item_id, "worker-2")


def test_workers_cannot_assign_or_link(store):
    item_id = assigned_item(store)
    with pytest.raises(AuthorityError):
        store.assign(SPACE, WORKER, item_id, "worker-2")
    other = store.create_item(SPACE, LEAD, title="B", criteria="c")
    with pytest.raises(AuthorityError):
        store.link(SPACE, WORKER, item_id, "blocks", other["id"])


def test_parent_cycles_rejected(store):
    a = store.create_item(SPACE, LEAD, title="A", criteria="c")
    b = store.create_item(SPACE, LEAD, title="B", criteria="c", parent=a["id"])
    with pytest.raises(BoardError, match="cycle"):
        store.link(SPACE, LEAD, a["id"], "parent", b["id"])


def test_blocked_by_shows_on_the_other_side(store):
    a = store.create_item(SPACE, LEAD, title="A", criteria="c")
    b = store.create_item(SPACE, LEAD, title="B", criteria="c")
    store.link(SPACE, LEAD, a["id"], "blocks", b["id"])
    assert {"kind": "blocked_by", "item": a["id"]} in store.get_item(
        SPACE, b["id"], actor=LEAD
    )["links"]


def test_artifact_refs_accumulate_on_the_item(store):
    item_id = assigned_item(store)
    store.transition(SPACE, WORKER, item_id, "in_progress")
    store.comment(SPACE, WORKER, item_id, "fix branch up", refs=["branch:fix/acl"])
    store.transition(
        SPACE, WORKER, item_id, "review",
        comment="done", refs=["branch:fix/acl", "report:posture.html"],
    )
    item = store.get_item(SPACE, item_id, actor=WORKER)
    assert item["refs"] == ["branch:fix/acl", "report:posture.html"]  # deduped, ordered
    store.rebuild(SPACE)
    assert store.get_item(SPACE, item_id, actor=WORKER)["refs"] == item["refs"]


# ------------------------------------------------------------- worker visibility

def test_worker_sees_only_its_slice(store):
    mine = assigned_item(store, assignee="worker-1")
    theirs = assigned_item(store, assignee="worker-2")
    linked = store.create_item(SPACE, LEAD, title="Dep", criteria="c")
    store.link(SPACE, LEAD, linked["id"], "blocks", mine)
    visible = {item["id"] for item in store.list_items(SPACE, WORKER)}
    assert visible == {mine, linked["id"]}
    assert theirs not in visible
    # the user sees everything
    assert len(store.list_items(SPACE, USER)) == 3


def test_worker_item_detail_matches_list_visibility(store):
    mine = assigned_item(store, assignee="worker-1")
    theirs = assigned_item(store, assignee="worker-2")
    claimable = store.create_item(SPACE, LEAD, title="Available", criteria="c")
    linked = store.create_item(SPACE, LEAD, title="Dependency", criteria="c")
    store.link(SPACE, LEAD, linked["id"], "blocks", mine)
    worker = LocalDialect(store, journal=None, actor=WORKER)

    assert worker.get_item(SPACE, mine)["id"] == mine
    assert worker.get_item(SPACE, linked["id"])["id"] == linked["id"]
    assert worker.get_item(SPACE, claimable["id"])["id"] == claimable["id"]
    assert store.get_item(SPACE, mine, actor=WORKER)["id"] == mine

    with pytest.raises(BoardError):
        worker.get_item(SPACE, theirs)
    with pytest.raises(BoardError):
        store.get_item(SPACE, theirs, actor=WORKER)

    store.claim(SPACE, OTHER, claimable["id"])
    with pytest.raises(BoardError):
        worker.get_item(SPACE, claimable["id"])

    held = store.create_item(SPACE, LEAD, title="Held", criteria="c")
    store.set_policy(SPACE, LEAD, claims="lead-only")
    with pytest.raises(BoardError):
        worker.get_item(SPACE, held["id"])

    assert worker.get_item(SPACE, mine)["id"] == mine


def test_worker_comments_only_on_its_slice(store):
    theirs = assigned_item(store, assignee="worker-2")
    with pytest.raises(AuthorityError, match="assigned"):
        store.comment(SPACE, WORKER, theirs, "drive-by")


# ------------------------------------------------------------------- tool layer

def test_tool_sets_are_role_filtered(store):
    lead_names = {tool.__name__ for tool in board_tools(store, space=SPACE, actor=LEAD)}
    worker_names = {
        tool.__name__ for tool in board_tools(store, space=SPACE, actor=WORKER)
    }
    readers = {"get_item_comments", "get_item_comment", "get_proposal"}
    assert lead_names == {"create_item", "list_items", "get_item", "transition", "comment", "assign", "link"} | readers
    assert worker_names == {"create_item", "list_items", "get_item", "transition", "comment", "claim", "set_status"} | readers


def test_tools_return_errors_instead_of_raising(store):
    tools = {t.__name__: t for t in board_tools(store, space=SPACE, actor=WORKER)}
    result = tools["transition"](item=99, to="in_progress")
    assert "error" in result


def test_comment_timestamps_render_with_a_local_offset(store):
    """The model does time arithmetic against its own clock, so a bare UTC `ts`
    made a lead read a 21-minute gap as "eight hours" (2026-10-31 incident).
    Tool output carries the local clock and its offset; the store keeps UTC."""
    item = assigned_item(store)
    store.transition(SPACE, WORKER, item, "in_progress")
    store.comment(SPACE, WORKER, item, "hand-off")
    tools = {t.__name__: t for t in board_tools(store, space=SPACE, actor=WORKER)}
    page = tools["get_item_comments"](item=item)
    assert page["comments"], page
    stamp = page["comments"][-1]["ts"]
    assert stamp[-6] in "+-" and stamp[-3] == ":", stamp
    # the stored value stays UTC — only the model-facing rendering moved
    assert store.comments(SPACE, item)[-1]["ts"].endswith("+00:00")


def test_create_item_tool_schema_keeps_the_title_parameter(store):
    from coworker.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register_all(board_tools(store, space=SPACE, actor=LEAD))
    schema = registry.get("create_item").schema
    properties = schema["function"]["parameters"]["properties"]
    # The auto-generator strips dict keys named "title" (pydantic metadata cleanup),
    # which would delete this parameter; the explicit schema must keep it.
    assert "title" in properties


# ------------------------------------------- 只读证据通道（2026-10-31 萧哥拍板）


def test_worker_can_read_another_workers_item_as_evidence(store):
    """主人的活要用到队友的材料时，队友的工作项必须读得到。

    现场：worker 的产出开头写着「#1 后半部地图考据、#2 三方冲突落地，在我这边不可见」，
    另一个 worker 直接拿到 `no visible item #1 in space`（同一个 space 里明明有）。
    """
    mine = assigned_item(store, assignee="worker-1")
    theirs = assigned_item(store, assignee="worker-2")
    store.comment(SPACE, Actor(id="worker-2", role=Role.WORKER), theirs, "考据：仓廒十二")

    evidence = store.get_item_evidence(SPACE, theirs, actor=WORKER)
    assert evidence["id"] == theirs
    assert any("考据" in c["body"] for c in evidence["comments"])


def test_evidence_read_does_not_widen_the_authored_slice(store):
    """证据只读：`list_items` 与 `get_item` 的作用域语义一字不改。"""
    mine = assigned_item(store, assignee="worker-1")
    theirs = assigned_item(store, assignee="worker-2")

    assert {i["id"] for i in store.list_items(SPACE, WORKER)} == {mine}
    with pytest.raises(BoardError):
        store.get_item(SPACE, theirs, actor=WORKER)


def test_evidence_read_grants_no_write(store):
    """读到 ≠ 能写：评论、附件、状态变更仍各自独立把关。"""
    theirs = assigned_item(store, assignee="worker-2")
    with pytest.raises(AuthorityError, match="assigned"):
        store.comment(SPACE, WORKER, theirs, "drive-by")
    with pytest.raises(AuthorityError):
        store.require_attachment_write(SPACE, WORKER, theirs)
    # set_status 先过可见性再过归属，落在 BoardError —— 一样是写不进去
    with pytest.raises(BoardError):
        store.set_status(SPACE, WORKER, theirs, "帮忙改一下")


def test_evidence_read_is_bounded_by_space(store, tmp_path):
    """别的团队看板照样读不到——开口只在自己这支队伍里。"""
    other = TeamStore(tmp_path / "other.db")
    try:
        item = other.create_item("elsewhere", Actor(id="lead-2", role=Role.LEAD),
                                 title="别队的活", criteria="c")
        with pytest.raises(BoardError):
            store.get_item_evidence("elsewhere", item["id"], actor=WORKER)
        with pytest.raises(BoardError):
            store.get_item_evidence(SPACE, 9999, actor=WORKER)
    finally:
        other.close()


def test_worker_tool_layer_reads_cross_team_evidence(store):
    """工具层：worker 的 get_item / get_item_comments 走证据通道。"""
    theirs = assigned_item(store, assignee="worker-2")
    store.comment(SPACE, Actor(id="worker-2", role=Role.WORKER), theirs, "龙山仓：仓廒十二")

    tools = {t.__name__: t for t in board_tools(store, space=SPACE, actor=WORKER)}
    got = tools["get_item"](item=theirs)
    assert "error" not in got, got
    assert got["id"] == theirs

    page = tools["get_item_comments"](item=theirs)
    assert any("龙山仓" in c["body"] for c in page["comments"]), page

    # 主编的视图不受影响（作用域本来就是全开）
    lead_tools = {t.__name__: t for t in board_tools(store, space=SPACE, actor=LEAD)}
    assert lead_tools["get_item"](item=theirs)["id"] == theirs
