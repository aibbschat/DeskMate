"""In-process concurrency leases for the orchestration runner.

This is the P0 foundation: bound fan-out and serialize dangerous steps. The
store provides durable checkpoints; leases prevent a single process from
stampeding provider pools or mutating the same target concurrently.

Two layers of bounding:

* **Per-runner** (``LeaseConfig``): ``max_parallel`` / ``max_read`` /
  ``max_compute`` / ``max_llm``. A single job may not fan out wider than this.
  These gates are loop-free too (``_CrossLoopLimiter``), so one shared runner
  keeps its budget even when the manager loop and JobHost worker threads use
  it at the same time.
* **Per-process** (``_CrossLoopLimiter``): the provider pool is shared by the
  manager loop, the consult worker threads (each runs its own loop) and any
  future automation runner. ``asyncio.Semaphore`` is loop-bound, so a shared
  semaphore cannot bound that traffic — three concurrent consult runs would
  each believe it owns the pool. The cross-loop gate hands slots over with
  ``call_soon_threadsafe`` so the cap stays honest across loops/threads.
"""

from __future__ import annotations

import asyncio
import collections
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from coworker.providers.timeouts import provider_pool_size

from .models import StepKind, StepSpec


@dataclass
class LeaseConfig:
    max_parallel: int = 4
    max_llm: int = 0  # 0 => derive from provider pool
    max_read: int = 8
    max_compute: int = 8
    # Share the process-wide provider-pool gates. Tests that want to isolate a
    # single runner can turn this off; production should keep it on.
    share_process_gates: bool = True

    def resolved_max_llm(self) -> int:
        if self.max_llm and self.max_llm > 0:
            return max(1, int(self.max_llm))
        return _process_llm_limit()

    def resolved_process_parallel(self) -> int:
        return max(1, int(provider_pool_size()))


def _process_llm_limit() -> int:
    """LLM slots for the whole process: the pool minus headroom for chat traffic."""
    pool = max(1, int(provider_pool_size()))
    return max(1, min(pool, max(2, pool - 4)))


class _CrossLoopLimiter:
    """Admission gate usable from several event loops / threads at once.

    Slots are handed to the next waiter by scheduling the wakeup on the
    waiter's own loop, so no loop ever touches another loop's primitives.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._waiters: collections.deque[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = collections.deque()
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._active < self._limit and not self._waiters:
                self._active += 1
                return
            fut: asyncio.Future[None] = loop.create_future()
            self._waiters.append((loop, fut))
        try:
            await fut
        except asyncio.CancelledError:
            # Either we were still queued (drop ourselves) or a slot had already
            # been handed to us (give it back, or capacity leaks permanently).
            handed = False
            with self._lock:
                for i, (_loop, pending) in enumerate(self._waiters):
                    if pending is fut:
                        del self._waiters[i]
                        break
                else:
                    handed = fut.done() and not fut.cancelled()
            if handed:
                self.release()
            raise

    def release(self) -> None:
        with self._lock:
            while self._waiters:
                loop, fut = self._waiters.popleft()
                try:
                    loop.call_soon_threadsafe(self._handoff, fut)
                except RuntimeError:
                    # Waiter's loop is gone; drop it and try the next one.
                    continue
                return
            self._active = max(0, self._active - 1)

    def _handoff(self, fut: asyncio.Future[None]) -> None:
        if fut.cancelled():
            # The waiter bailed between handoff and wakeup — pass the slot on.
            self.release()
            return
        if not fut.done():
            fut.set_result(None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"limit": self._limit, "active": self._active, "waiting": len(self._waiters)}

    @property
    def _value(self) -> int:
        """Remaining capacity, mirroring ``asyncio.Semaphore._value``.

        Tests read this to assert a cancelled waiter did not inflate capacity.
        Invariant: waiters are only queued while ``_active == _limit``, so
        ``limit - active`` is 0 exactly when the gate is saturated.
        """
        with self._lock:
            return max(0, self._limit - self._active)


_PROCESS_GLOBAL: Optional[_CrossLoopLimiter] = None
_PROCESS_LLM: Optional[_CrossLoopLimiter] = None
_PROCESS_LOCK = threading.Lock()


def process_gates() -> tuple[_CrossLoopLimiter, _CrossLoopLimiter]:
    """Return the process-wide (parallel, llm) gates, creating them once."""
    global _PROCESS_GLOBAL, _PROCESS_LLM
    with _PROCESS_LOCK:
        if _PROCESS_GLOBAL is None:
            pool = max(1, int(provider_pool_size()))
            _PROCESS_GLOBAL = _CrossLoopLimiter(pool)
            _PROCESS_LLM = _CrossLoopLimiter(_process_llm_limit())
        assert _PROCESS_LLM is not None
        return _PROCESS_GLOBAL, _PROCESS_LLM


def reset_process_gates() -> None:
    """Drop the process gates (tests only). Never call while work is in flight."""
    global _PROCESS_GLOBAL, _PROCESS_LLM
    with _PROCESS_LOCK:
        _PROCESS_GLOBAL = None
        _PROCESS_LLM = None


class ConcurrencyLeases:
    """Per-runner concurrency caps that hold across event loops.

    All gates here are :class:`_CrossLoopLimiter` instances, not ``asyncio``
    primitives: one runner is shared between the manager loop and the JobHost
    worker threads (each runs its own loop), and an ``asyncio.Semaphore`` is
    bound to the loop that first awaited it. The old ``_ensure`` rebuilt the
    semaphores whenever a second loop showed up — under in-flight
    acquisitions, which silently inflated capacity and dropped the per-target
    serialization (2026-11-22 fix). Loop-free gates keep one honest budget
    per instance, so the shared runner in ``manager.orchestration_runner``
    is now safe to use from both loops at once.
    """

    def __init__(self, config: LeaseConfig | None = None) -> None:
        self.config = config or LeaseConfig()
        self._global = _CrossLoopLimiter(max(1, int(self.config.max_parallel)))
        self._llm = _CrossLoopLimiter(self.config.resolved_max_llm())
        self._read = _CrossLoopLimiter(max(1, int(self.config.max_read)))
        self._compute = _CrossLoopLimiter(max(1, int(self.config.max_compute)))
        self._targets: dict[str, _CrossLoopLimiter] = {}
        self._targets_guard = threading.Lock()

    def _target_limiter(self, target: str) -> _CrossLoopLimiter:
        # Plain dict get-or-create under a threading lock: the critical
        # section is microseconds and never awaits, so it cannot stall a loop.
        with self._targets_guard:
            limiter = self._targets.get(target)
            if limiter is None:
                limiter = _CrossLoopLimiter(1)
                self._targets[target] = limiter
            return limiter

    @asynccontextmanager
    async def acquire(
        self, spec: StepSpec, *, uses_llm: bool = False
    ) -> AsyncIterator[None]:
        # Target serialization comes FIRST. Acquiring it after the global/llm
        # slots meant a write step parked on a contended target kept a global
        # slot and an LLM slot for the whole wait — four same-target writes
        # could starve every read in the job.
        target_lock: Optional[_CrossLoopLimiter] = None
        if spec.target:
            target_lock = self._target_limiter(f"{spec.kind.value}:{spec.target}")

        kind_sem: Optional[_CrossLoopLimiter] = None
        if spec.kind == StepKind.READ:
            kind_sem = self._read
        elif spec.kind == StepKind.COMPUTE:
            kind_sem = self._compute
        acquired_kind = False
        acquired_global = acquired_instance_global = False
        acquired_llm = acquired_process_llm = False
        acquired_target = False
        try:
            if target_lock is not None:
                await target_lock.acquire()
                acquired_target = True
            proc_global, proc_llm = process_gates()
            if self.config.share_process_gates:
                await proc_global.acquire()
                acquired_global = True
            await self._global.acquire()
            acquired_instance_global = True
            if kind_sem is not None:
                await kind_sem.acquire()
                acquired_kind = True
            if uses_llm:
                await self._llm.acquire()
                acquired_llm = True
                if self.config.share_process_gates:
                    await proc_llm.acquire()
                    acquired_process_llm = True
            yield
        finally:
            if acquired_process_llm:
                proc_llm.release()
            if acquired_llm:
                self._llm.release()
            if acquired_kind and kind_sem is not None:
                kind_sem.release()
            if acquired_instance_global:
                self._global.release()
            if acquired_global:
                proc_global.release()
            if acquired_target and target_lock is not None:
                target_lock.release()
