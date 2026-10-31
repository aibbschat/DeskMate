"""Team registry — which sessions form a team: one lead, its workers, their board.

A team is created at the staffing gate ("Create team & start"): worker sessions are
PRE-SPAWNED as durable state on disk (spawn ≠ first turn — an unassigned worker costs
zero tokens; its first model turn fires when the first assignment lands). The registry
is the roster the wake plumbing walks each tick, and the tie that scopes staleness
digests by role membership.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class TeamWorker:
    actor: str  # the lead-given NAME — board actor id, assignee handle, @mention target
    persona: str
    session_id: str
    model: str = ""
    reason: str = ""  # why the lead staffed it — surfaces in teammates' rosters
    approval_guidance: str = ""  # exact human-approved staffing text, not live steering


@dataclass
class Team:
    team_id: str
    space: str
    lead_session: str
    lead_actor: str
    workers: list[TeamWorker] = field(default_factory=list)
    chat_enabled: bool = False
    chat_group: str = ""  # ChatStore group_id when chat is enabled
    paused: bool = False  # budget/user pause: the wake gate skips a paused team
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Rolling budget gate: automatic wakes this hour (reset when the hour rolls).
    wake_hour: str = ""
    wakes_this_hour: int = 0


def _is_namespaced_space(space: str) -> bool:
    """True for a synthetic board key (`creation:<pid>`) rather than a real workspace
    path. Pipeline teams live on such keys and staff themselves with synthetic session
    ids, so a real-path check is what separates them from coworker teams."""
    s = (space or "").strip()
    return (not s.startswith("/")) or ":" in s


class TeamRegistry:
    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._teams: dict[str, Team] = {}
        self.refresh()

    def _load(self) -> dict[str, Team]:
        teams: dict[str, Team] = {}
        if self.path and self.path.is_file():
            for raw in json.loads(self.path.read_text(encoding="utf-8")).get(
                "teams", []
            ):
                workers = [TeamWorker(**w) for w in raw.pop("workers", [])]
                team = Team(**{**raw, "workers": []})
                team.workers = workers
                teams[team.team_id] = team
        return teams

    def refresh(self) -> None:
        """Re-read teams.json from disk.

        The same file is written by more than one registry instance in the process
        tree (deskmate's creation pipeline keeps its own TeamsClient), so a long-lived
        instance would otherwise serve a stale roster forever — a member it just
        appended would vanish from every read that goes through the other instance."""
        try:
            loaded = self._load()
        except Exception:  # noqa: BLE001 — a torn/partial read must not wipe live state
            return
        with self._lock:
            self._teams = loaded

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"teams": [asdict(t) for t in self._teams.values()]}, indent=2
            ),
            encoding="utf-8",
        )

    def create(
        self,
        *,
        space: str,
        lead_session: str,
        lead_actor: str,
        workers: list[TeamWorker],
        chat_enabled: bool = False,
        chat_group: str = "",
    ) -> Team:
        team = Team(
            team_id=uuid.uuid4().hex[:12],
            space=space,
            lead_session=lead_session,
            lead_actor=lead_actor,
            workers=workers,
            chat_enabled=chat_enabled,
            chat_group=chat_group,
        )
        with self._lock:
            self._teams[team.team_id] = team
            self._save()
        return team

    def add_worker(self, team_id: str, worker: TeamWorker) -> Optional[Team]:
        """Staff one MORE worker onto an existing team (the dynamic-add path).

        Teams used to be staffed exactly once — `propose_team` refuses a session
        that already leads a team — so a missing specialist (e.g. a plot-architecture
        expert) could only be had by tearing the whole team down. Adding a member
        keeps the existing roster, board and history intact. Returns the team, or
        None when the team is gone (deleted between the caller's check and here)."""
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return None
            team.workers.append(worker)
            self._save()
            return team

    def all(self) -> list[Team]:
        return list(self._teams.values())

    def get(self, team_id: str) -> Optional[Team]:
        return self._teams.get(team_id)

    def for_lead_session(self, session_id: str) -> Optional[Team]:
        for team in self._teams.values():
            if team.lead_session == session_id:
                return team
        return None

    def for_worker_session(self, session_id: str) -> Optional[tuple[Team, TeamWorker]]:
        for team in self._teams.values():
            for worker in team.workers:
                if worker.session_id == session_id:
                    return team, worker
        return None

    def detach_session(self, session_id: str) -> dict:
        """Remove a session from whatever team it belonged to. Called when the session is
        DELETED: without this, the registry kept advertising a worker whose session no
        longer existed, `assign` validated against that ghost, the wake went to a session
        with no record and no engine, and the dispatched work silently evaporated — the
        lead got no signal and quietly did the job itself (2026-10-31: two full rounds
        lost this way). Removing the member makes any further assignment fail LOUDLY, and
        a team left with no workers is dropped so the lead can staff a fresh one.

        Returns what was detached: {"role": "lead"|"worker"|None, "session_id",
        "team_id", "actor", "workers_left", "team_removed"}."""
        out = {
            "role": None,
            "session_id": session_id,
            "team_id": "",
            "actor": "",
            "workers_left": 0,
            "team_removed": False,
        }
        with self._lock:
            for team_id, team in list(self._teams.items()):
                if team.lead_session == session_id:
                    # The lead's session is gone — the team cannot function at all.
                    del self._teams[team_id]
                    out.update(
                        role="lead",
                        team_id=team_id,
                        actor=team.lead_actor,
                        workers_left=0,
                        team_removed=True,
                    )
                    self._save()
                    return out
                for worker in list(team.workers):
                    if worker.session_id != session_id:
                        continue
                    team.workers.remove(worker)
                    left = len(team.workers)
                    if left == 0:
                        # Nothing to dispatch to any more — drop it so `propose_team`
                        # isn't blocked by "this session already leads a team".
                        del self._teams[team_id]
                        out["team_removed"] = True
                    out.update(
                        role="worker",
                        team_id=team_id,
                        actor=worker.actor,
                        workers_left=left,
                    )
                    self._save()
                    return out
        return out

    def prune_missing_sessions(self, session_exists) -> list[dict]:
        """Drop every member whose session no longer exists; returns what was pruned.

        Self-heal for rosters orphaned before `detach_session` existed (and by any
        out-of-band record loss): a listed worker whose session is gone still validated
        for `assign`, so the board kept showing in_progress work that could never run.
        `session_exists` is a callable(str) -> bool supplied by the caller, because the
        registry has no access to the session store.

        Teams whose space is a NAMESPACED key are skipped: those are pipeline teams
        (deskmate/creation) whose board space is a synthetic key (`creation:<pid>`) and
        whose member sessions are synthetic too (`creation:<pid>:<persona>`) — they never
        exist in the coworker session store BY DESIGN, so probing would delete live
        pipeline rosters. Only coworker teams (space = a real workspace path) are probed.
        """
        with self._lock:
            session_ids = []
            for team in self._teams.values():
                if _is_namespaced_space(team.space):
                    continue
                session_ids.append(team.lead_session)
                session_ids.extend(w.session_id for w in team.workers)
        pruned = []
        for sid in session_ids:
            try:
                if session_exists(sid):
                    continue
            except Exception:  # noqa: BLE001 — a probe failure must not drop a member
                continue
            out = self.detach_session(sid)
            if out["role"] is not None:
                pruned.append(out)
        return pruned

    def set_paused(self, team_id: str, paused: bool) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team is not None:
                team.paused = paused
                self._save()

    def count_wake(self, team_id: str, *, cap: int) -> bool:
        """The budget gate at the wake gate: count one automatic wake against the
        team's rolling hour; False = over cap (the caller skips the wake and the
        team reads as paused-for-budget until the hour rolls). A runaway loop
        stops BETWEEN turns, never mid-flight."""
        hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return False
            if team.wake_hour != hour:
                team.wake_hour, team.wakes_this_hour = hour, 0
            if team.wakes_this_hour >= cap:
                self._save()
                return False
            team.wakes_this_hour += 1
            self._save()
            return True
