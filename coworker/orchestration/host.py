"""Background job host: dispatch-and-return, parallel execution, crash recovery.

The orchestration runner and store already provide checkpoint/resume, step
fan-out, idempotent side effects and a process-wide LLM lease. What was missing
is the *host* that turns a Job into a fire-and-forget background task and
re-attaches to unfinished jobs after the process restarts — the same shape that
``deskmate.consult`` hand-rolls with daemon threads + ``consult_runs.json``.

This module generalizes that shape so any long-running read/check/merge task
can use it. Design constraints:

* No permission bypass, no new side-effect path — the runner owns execution.
* In-memory ``_ACTIVE`` dedups live threads; the SQLite checkpoint is the
  cross-process source of truth for *what remains to be done*.
* ``recover()`` is safe to call on every process start (and repeatedly): it
  only re-dispatches jobs whose threads are not alive locally, and the runner's
  ``resume=True`` skips completed steps and reuses their artifacts.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from .leases import ConcurrencyLeases, LeaseConfig
from .models import Job, JobStatus
from .runner import OrchestrationRunner, StepHandler
from .store import OrchestrationStore

logger = logging.getLogger(__name__)

Resolver = Callable[[Job], Optional[StepHandler]]
Notifier = Callable[[str, dict[str, Any]], None]


class JobHost:
    """Run checkpointed Jobs in background threads and recover them on restart.

    ``submit`` never awaits the job: it persists the checkpoint, starts a daemon
    thread and returns the job id immediately. ``wait`` joins all locally live
    job threads (used by graceful shutdown and tests).
    """

    def __init__(
        self,
        store: OrchestrationStore,
        *,
        notifier: Notifier | None = None,
        max_concurrent: int = 4,
        lease_config: LeaseConfig | None = None,
        leases: ConcurrencyLeases | None = None,
    ) -> None:
        self.store = store
        self._notifier = notifier
        self._max_concurrent = max(1, int(max_concurrent))
        self._runner = OrchestrationRunner(
            store, leases=leases or ConcurrencyLeases(lease_config)
        )
        # Exposed so synchronous/async callers (e.g. automation) can share the
        # exact same lease budget instead of building a private runner.
        self.runner = self._runner
        self._active: dict[str, threading.Thread] = {}
        self._slots = threading.BoundedSemaphore(self._max_concurrent)
        self._lock = threading.RLock()

    # -- dispatch ------------------------------------------------------------
    def submit(self, job: Job, handler: StepHandler | dict[str, StepHandler]) -> str:
        """Persist + dispatch a job in a background thread; return immediately.

        ``handler`` may be a single callable or the per-step handler mapping
        returned by ``build_read_check_merge_pipeline`` / ``build_pipeline_job``.
        """
        # Atomic check-and-register under the same lock recover uses, so a job
        # is never dispatched twice even if submit/recover race.
        with self._lock:
            if self._is_live(job.id):
                raise RuntimeError(f"job {job.id} already running")
            if not self._slots.acquire(blocking=False):
                raise RuntimeError(
                    f"并发上限 {self._max_concurrent} 已满，任务 {job.id} 未派发"
                )
            self.store.save_job(job)
            thread = threading.Thread(
                target=self._execute,
                args=(job.id, handler),
                name=f"orchjob-{job.id}",
                daemon=True,
            )
            self._active[job.id] = thread
            thread.start()
        self._notify("started", self._snapshot(job.id))
        return job.id

    def _execute(
        self, job_id: str, handler: StepHandler | dict[str, StepHandler]
    ) -> None:
        try:
            job = self.store.get_job(job_id)
            if job is None:
                logger.warning("job %s vanished before execution", job_id)
                return
            finished = asyncio.run(self._runner.run(job, handler, resume=True))
            status = finished.status.value
        except Exception as exc:  # noqa: BLE001 —— 线程兜底，绝不让 job 悬在 running
            logger.exception("background job %s failed", job_id)
            status = "failed"
            self._fail(job_id, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._active.pop(job_id, None)
                try:
                    self._slots.release()
                except ValueError:
                    pass
        self._notify("finished", self._snapshot(job_id))

    # -- crash recovery ------------------------------------------------------
    def recover(
        self,
        resolver: Resolver,
        *,
        limit: int = 200,
        sources: Optional[set[str]] = None,
        fail_unresolved: bool = False,
    ) -> list[str]:
        """Re-dispatch unfinished jobs left behind by a crashed process.

        Called on process start (and safe to call repeatedly). ``resolver`` maps
        a stored Job back to its handler.

        * ``sources`` — when given, ONLY these job sources are considered. This
          is the conservative default for managers that also store jobs whose
          handlers cannot be rebuilt (e.g. automation bound to a live session);
          such jobs must never be touched by a blind recovery.
        * ``fail_unresolved`` — if a *considered* job has no handler, mark it
          FAILED with a recorded reason. Defaults False (leave it untouched so
          a later resolver can pick it up). The direct-host test opts in.
        """
        resumed: list[str] = []
        with self._lock:
            candidates = [
                j
                for j in self.store.list_jobs(limit=limit)
                if j.status in (JobStatus.RUNNING, JobStatus.PENDING)
                and not self._is_live(j.id)
                and (sources is None or str(j.source) in sources)
            ]
        for job in candidates:
            handler = resolver(job)
            if handler is None:
                if fail_unresolved:
                    self._fail(
                        job.id,
                        "no handler available to resume orphaned job",
                        meta_extra={"recover_error": "no handler"},
                    )
                    logger.warning("orphaned job %s has no handler; marked failed", job.id)
                else:
                    logger.debug("orphaned job %s skipped: no handler yet", job.id)
                continue
            # submit itself re-checks liveness under the lock; a concurrent
            # recover that won the race simply raises here, which we ignore.
            try:
                self.submit(job, handler)
                resumed.append(job.id)
            except RuntimeError:
                continue
        return resumed

    # -- introspection -------------------------------------------------------
    def status(self, job_id: str) -> Optional[dict[str, Any]]:
        return self._snapshot(job_id)

    def wait(self, timeout: float = 120.0) -> bool:
        """Join all locally live job threads. Returns False if any timed out."""
        with self._lock:
            threads = list(self._active.values())
        deadline = time_monotonic() + max(0.0, timeout)
        ok = True
        for t in threads:
            remaining = deadline - time_monotonic()
            if remaining <= 0:
                t.join(timeout=0)
                ok = False
                continue
            t.join(timeout=remaining)
            if t.is_alive():
                ok = False
        return ok

    # -- internals -----------------------------------------------------------
    def _is_live(self, job_id: str) -> bool:
        t = self._active.get(job_id)
        return t is not None and t.is_alive()

    def _snapshot(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            return {"id": job_id, "status": "missing", "running": False}
        with self._lock:
            running = self._is_live(job_id)
        return {
            "id": job.id,
            "title": job.title,
            "source": job.source,
            "status": job.status.value,
            "running": running,
            "updated_at": job.updated_at,
            "meta": dict(job.meta),
        }

    def _fail(
        self,
        job_id: str,
        error: str,
        *,
        meta_extra: Optional[dict[str, Any]] = None,
    ) -> None:
        job = self.store.get_job(job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.meta["host_error"] = error
        if meta_extra:
            job.meta.update(meta_extra)
        self.store.save_job(job)

    def _notify(self, kind: str, run: dict[str, Any]) -> None:
        fn = self._notifier
        if fn is None:
            return
        try:
            fn(kind, dict(run))
        except Exception:  # noqa: BLE001 —— 通知是锦上添花，不能拖垮任务
            logger.debug("job notifier failed", exc_info=True)


def time_monotonic() -> float:
    import time

    return time.monotonic()
