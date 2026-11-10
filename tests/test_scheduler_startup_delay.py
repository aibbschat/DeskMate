"""Regression: the scheduler's first (catch-up) pass must not fire at cold boot.

2026-11-10 startup-slowness fix. The catch-up pass spawns due automation runs AND the
extra_tick hook (self-wake resume + DeskMate background proactive LLM turns) on the same
event loop that is still binding the HTTP socket, so at t=0 it delayed "Uvicorn running"
from ~1s to 6.4-9.7s and blocked first-screen requests for seconds at a time. The fix is
purely deferral: the pass still runs once, just later.
"""

from __future__ import annotations

import asyncio

from coworker.automation.scheduler import Scheduler
from coworker.server.manager import _startup_catchup_delay


class _NoStore:
    def due(self):
        return []


def test_start_without_delay_runs_catchup_immediately():
    async def run():
        hits = []

        async def extra():
            hits.append(1)

        sched = Scheduler(_NoStore(), runner=None, extra_tick=extra, tick_seconds=60.0)
        sched.start()  # historical behaviour: catch-up right away
        await asyncio.sleep(0.05)
        await sched.stop()
        assert hits == [1]

    asyncio.run(run())


def test_start_defers_catchup_until_initial_delay_elapses():
    async def run():
        hits = []

        async def extra():
            hits.append(1)

        sched = Scheduler(_NoStore(), runner=None, extra_tick=extra, tick_seconds=60.0)
        sched.start(initial_delay=0.25)
        await asyncio.sleep(0.12)
        assert hits == [], "catch-up must not run during the boot window"
        await asyncio.sleep(0.3)
        await sched.stop()
        assert hits == [1], "catch-up must still run exactly once after the delay"

    asyncio.run(run())


def test_stop_during_initial_delay_is_clean():
    """Shutting down inside the delay window must not hang or raise."""

    async def run():
        sched = Scheduler(_NoStore(), runner=None, tick_seconds=60.0)
        sched.start(initial_delay=30.0)
        await asyncio.sleep(0.02)
        await sched.stop()
        assert sched._task is None

    asyncio.run(run())


def test_manager_delay_is_env_tunable(monkeypatch):
    monkeypatch.delenv("COWORKER_SCHEDULER_CATCHUP_DELAY", raising=False)
    assert _startup_catchup_delay() == 30.0
    monkeypatch.setenv("COWORKER_SCHEDULER_CATCHUP_DELAY", "0")
    assert _startup_catchup_delay() == 0.0
    monkeypatch.setenv("COWORKER_SCHEDULER_CATCHUP_DELAY", "12.5")
    assert _startup_catchup_delay() == 12.5
    monkeypatch.setenv("COWORKER_SCHEDULER_CATCHUP_DELAY", "junk")
    assert _startup_catchup_delay() == 30.0
    monkeypatch.setenv("COWORKER_SCHEDULER_CATCHUP_DELAY", "-5")
    assert _startup_catchup_delay() == 0.0
