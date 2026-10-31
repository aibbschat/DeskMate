"""Acceptance tests for the team delegated-approval port.

This is the fix for the reported defect: a worker's tool-call approval used to be
routed only to the user's personal inbox (route_for had no team awareness), so the
lead (editor-in-chief) could neither see nor approve the worker's parked requests —
the task stalled until the human opened the inbox manually.

The port makes the worker's stall *visible and actionable* to the lead:
  * ``note_worker_waiting`` emits a WORKER_WAITING board event (lead sees it).
  * the lead session gets ``decide_worker_call``, which resolves the worker's parked
    prompt via ``_lead_decide_worker_call`` (lead has authority).
  * the Inbox cascade keeps the lead's proxy card and the worker's original prompt
    consistent (atomic deny / supersede), so neither can be silently inverted or stuck.

Ports the relevant subset of the upstream tests/test_team_live_gaps.py +
tests/test_team_rehearsal_fixes.py that exercises the local API. A few upstream
tests that depend on reviewer hot-apply / durable-resume helpers outside this port
are intentionally omitted; the core defect (visibility + authority) is covered here.
"""

import asyncio

import pytest
from coworker.engine import ApprovalOutcome, PermissionRequest
from coworker.inbox import InboxStore
from coworker.server.manager import SessionManager
from coworker.sessions import SessionRecord
from coworker.teams.registry import TeamWorker
from coworker.teams.store import WORKER_WAITING
from coworker.teams.summary import all_events


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


def request_for(manager):
    """Mirror the upstream helper: a worker parked on an approval, and the lead's
    decide_worker_call that answers it."""
    original = manager.inbox.add_approval(
        "worker",
        "Run tests",
        data={"tool": "run_shell", "arguments": {"command": "python -m unittest -v"}},
    )
    request = PermissionRequest(
        tool_name="decide_worker_call",
        arguments={"worker": "sam", "call_id": original.id, "decision": "allow"},
        metadata=None,
        reason="requires approval",
        tool_call_id="lead-call",
    )
    return original, request


@pytest.mark.parametrize("resolve_first", [False, True])
@pytest.mark.parametrize("answer", ["allow", "deny"])
def test_worker_resolution_retires_linked_gate_before_or_after_creation(
    manager, resolve_first, answer
):
    """Resolving the worker's own prompt must retire the lead's linked proxy gate,
    whether the worker is resolved before or after the lead's gate is created."""
    original, request = request_for(manager)
    data = manager.approval_prompt_data("lead", request)
    if resolve_first:
        manager.inbox.resolve(original.id, answer, by="human")

    async def run():
        task = asyncio.create_task(manager.inbox_approver("lead", "swe-lead")(request))
        await asyncio.sleep(0)
        if not resolve_first:
            manager.inbox.resolve(original.id, answer, by="human")
        assert await asyncio.wait_for(task, 1) is ApprovalOutcome.SUPERSEDED

    asyncio.run(run())
    parent = manager.inbox.for_tool_call("lead", "lead-call")
    assert parent.state == "resolved" and parent.resolution == "superseded"
    assert parent.resolved_by == "system:worker-resolution"
    assert parent.data["worker_call"]["state"] == "resolved"
    assert parent.data["worker_call"]["resolution"] == answer
    assert not manager.inbox.pending()
    assert not manager.inbox.resolve(original.id, "opposite")
    assert original.resolution == answer
    loaded = InboxStore(manager.inbox.path)
    assert loaded.get(parent.id).resolution == "superseded"
    assert data["worker_prompt_id"] == original.id


def test_cross_team_or_mismatched_worker_cannot_link_or_disclose_prompt(manager):
    """A decide_worker_call that isn't owned by this lead's team must not leak the
    worker's prompt onto the card, and a stale 'superseded' must be downgraded to DENY
    when the original prompt is still pending."""
    original, request = request_for(manager)
    assert manager.approval_outcome("superseded", request, "lead") is ApprovalOutcome.DENY
    assert "worker_call" not in manager.approval_prompt_data("not-the-lead", request)
    request.arguments["worker"] = "maya"
    data = manager.approval_prompt_data("lead", request)
    assert "worker_prompt_id" not in data and "worker_call" not in data
    assert original.state == "pending"


def test_denied_lead_allow_atomically_denies_worker_and_retires_other_proxies(manager):
    """Denying the lead's proxy that proposed to ALLOW the worker must atomically deny
    the worker's original call (never leave it parked) and supersede any sibling proxy."""
    original, request = request_for(manager)
    data = manager.approval_prompt_data("lead", request)
    parent = manager.inbox.add_approval("lead", "Decision", data=data)
    sibling = manager.inbox.add_approval("lead", "Other window", data=data)

    async def run():
        waits = [
            asyncio.create_task(manager.inbox.wait(i.id)) for i in (original, parent, sibling)
        ]
        await asyncio.sleep(0)
        assert manager.inbox.resolve(parent.id, "deny", by="human")
        assert await asyncio.wait_for(asyncio.gather(*waits), 1) == [
            "deny",
            "deny",
            "superseded",
        ]

    asyncio.run(run())
    assert original.resolved_by == "human"
    assert not manager.inbox.pending()
    assert not manager.inbox.resolve(original.id, "allow")
    loaded = InboxStore(manager.inbox.path)
    assert loaded.get(original.id).resolution == "deny"
    assert loaded.get(parent.id).resolution == "deny"


@pytest.mark.parametrize("answer", ["allow", "interrupted", "session deleted"])
def test_non_denial_of_proxy_does_not_resolve_worker(manager, answer):
    """Anything that isn't a denial of the lead's proxy leaves the worker's prompt parked."""
    original, request = request_for(manager)
    parent = manager.inbox.add_approval(
        "lead", "Decision", data=manager.approval_prompt_data("lead", request)
    )
    manager.inbox.resolve(parent.id, answer)
    assert original.state == "pending"


def test_rejecting_a_lead_denial_never_implicitly_allows_worker(manager):
    """The lead proxy itself being denied (decision=deny) must not resolve the worker."""
    original, request = request_for(manager)
    request.arguments["decision"] = "deny"
    parent = manager.inbox.add_approval(
        "lead", "Decision", data=manager.approval_prompt_data("lead", request)
    )
    manager.inbox.resolve(parent.id, "deny")
    assert original.state == "pending"


def test_worker_wait_digest_is_informational_and_wait_only_batch_does_not_wake(
    manager, monkeypatch
):
    """A worker-only WORKER_WAITING batch is informational for the worker (no decision
    authority) and must not repeatedly wake them — it is consumed and returns 0."""
    team = manager.teams.for_lead_session("lead")
    events = [
        {
            "seq": 10,
            "item_id": None,
            "kind": WORKER_WAITING,
            "actor": "maya",
            "payload": {"tool": "run_shell", "prompt_id": "sample-prompt"},
        }
    ]
    body, rows = manager._team_digest(team, events, [], is_lead=False, reader="sam")
    assert "decide_worker_call" not in body and "your decision" not in body
    assert "lead or user" in body and rows[0]["kind"] == "waiting"
    # The GUI renders rows, not the prose. A waiting row carries NO item/title
    # (a session is not a task identity), so it must still carry enough to render:
    # tool + decision flag. Without them the row text is empty -> a styled card
    # with blank bullets (shipped 2026-10-31, fixed same day).
    assert rows[0]["tool"] == "run_shell"
    assert rows[0]["decision"] is False
    consumed = []
    monkeypatch.setattr(manager.team_store, "feed_for", lambda *a: events)
    monkeypatch.setattr(
        manager.team_store, "consume_feed", lambda *a: consumed.append(a)
    )
    assert (
        asyncio.run(
            manager._drain_team_member(
                team, session_id="worker", actor="sam", is_lead=False
            )
        )
        == 0
    )
    assert consumed == [(team.space, "sam", 10)]


def test_note_worker_waiting_emits_board_event_for_lead(manager):
    """Core defect fix: a parked worker tool call becomes visible to the lead as a
    WORKER_WAITING board event, and the lead's digest names it as a decision to make."""
    manager.note_worker_waiting(
        "worker", "run_shell", prompt_id="p1", preview="python -m unittest -v"
    )
    space = manager.teams.for_lead_session("lead").space
    waiting = [e for e in all_events(manager.team_store, space) if e["kind"] == WORKER_WAITING]
    assert len(waiting) == 1
    assert waiting[0]["payload"]["prompt_id"] == "p1"
    assert waiting[0]["payload"]["tool"] == "run_shell"
    team = manager.teams.for_lead_session("lead")
    body, rows = manager._team_digest(team, waiting, [], is_lead=True, reader="lead")
    assert "decide_worker_call" in body
    assert rows[0]["kind"] == "waiting"
    # The lead's row must be renderable AND marked as its decision to make —
    # `decision` is what makes the GUI say "waiting for YOUR decision" instead of
    # the informational "waiting for the lead or user".
    assert rows[0]["tool"] == "run_shell"
    assert rows[0]["prompt_id"] == "p1"
    assert rows[0]["decision"] is True
    assert "python -m unittest" in (rows[0].get("note") or "")


def test_lead_decide_worker_call_resolves_worker_prompt(manager):
    """Core defect fix: the lead can directly resolve a worker's parked approval, and a
    second decision is a no-op (already handled)."""
    original, request = request_for(manager)
    result = manager._lead_decide_worker_call("lead", "sam", original.id, "allow", "go ahead")
    assert result["ok"] is True
    assert result["resolution"] == "allow"
    item = manager.inbox.get(original.id)
    assert item.state == "resolved"
    assert item.resolution == "allow"
    assert item.resolved_by == "lead:sam"

    again = manager._lead_decide_worker_call("lead", "sam", original.id, "deny", "")
    assert "error" in again


def test_lead_decide_worker_call_deny_resolves_as_deny(manager):
    original, request = request_for(manager)
    result = manager._lead_decide_worker_call("lead", "sam", original.id, "deny", "not now")
    assert result["ok"] is True
    assert result["resolution"] == "deny"
    assert manager.inbox.get(original.id).resolution == "deny"


def test_lead_approval_card_carries_worker_call_details(manager):
    """The human's approval card for a decide_worker_call shows WHAT is being allowed
    (the worker's tool + arguments), not just an opaque id."""
    original, request = request_for(manager)
    data = manager.approval_prompt_data("lead", request)
    assert "worker_call" in data
    assert data["worker_call"]["tool"] == "run_shell"
    assert data["worker_call"]["arguments"] == {"command": "python -m unittest -v"}
    assert data["worker_prompt_id"] == original.id
