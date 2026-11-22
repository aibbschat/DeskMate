"""Bounded DAG runner with checkpoint/resume.

The runner is the execution foundation shared by interactive and automated
workflows. It does not know what a "task" is; callers provide handlers that
execute a step and return artifacts/results.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .leases import ConcurrencyLeases, LeaseConfig
from .models import Artifact, Job, JobStatus, StepKind, StepRun, StepSpec, StepStatus
from .store import OrchestrationStore

logger = logging.getLogger(__name__)

StepHandler = Callable[["StepSpec", "Job", "RunContext"], Awaitable[Any] | Any]


@dataclass
class RunContext:
    job: Job
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)

    def artifact(self, name: str) -> Optional[Artifact]:
        return self.artifacts.get(name)

    def result(self, step_id: str) -> Any:
        return self.results.get(step_id)


@dataclass
class StepOutput:
    result: Any = None
    artifacts: list[Artifact] = field(default_factory=list)


class OrchestrationRunner:
    def __init__(
        self,
        store: OrchestrationStore,
        leases: ConcurrencyLeases | None = None,
        *,
        lease_config: LeaseConfig | None = None,
    ) -> None:
        self.store = store
        self.leases = leases or ConcurrencyLeases(lease_config)

    async def run(
        self,
        job: Job,
        handler: StepHandler | dict[str, StepHandler],
        *,
        resume: bool = True,
        cancel_event: asyncio.Event | None = None,
    ) -> Job:
        """Execute a job until completion, failure, or cancellation.

        On resume, completed steps are reused and failed steps are retried when
        their retry budget allows. Steps whose dependencies failed are skipped.
        """
        self._validate_job(job)
        self.store.save_job(job)
        ctx = self._build_context(job)
        self._normalize_for_run(job, resume=resume)
        self.store.save_job(job)

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._mark_canceled(job)
                    self.store.save_job(job)
                    return job

                self._skip_blocked(job)
                ready = self._ready_steps(job)
                if not ready:
                    if any(r.status == StepStatus.RUNNING for r in job.runs.values()):
                        await asyncio.sleep(0.05)
                        continue
                    break

                job.status = JobStatus.RUNNING
                self.store.save_job(job)

                tasks = [
                    asyncio.create_task(
                        self._run_step(job, spec, handler, ctx, cancel_event)
                    )
                    for spec in ready
                ]
                if tasks:
                    await asyncio.gather(*tasks)
        except Exception:
            # A store failure (locked DB, disk full) must not leave the job
            # claiming to be RUNNING forever with no way to tell why.
            logger.exception("orchestration job %s aborted", job.id)
            job.status = JobStatus.FAILED
            try:
                self.store.save_job(job)
            except Exception:
                logger.debug("could not persist failed job %s", job.id, exc_info=True)
            raise

        self._finalize_job(job)
        self.store.save_job(job)
        return job

    @staticmethod
    def _validate_job(job: Job) -> None:
        """Fail fast on a job that can never complete: duplicate step ids,
        dependencies pointing at nothing, or a dependency cycle. Without
        this, a typo'd dep left the step PENDING forever and the job died
        FAILED with no hint which dep was wrong (2026-11-22)."""
        seen: set[str] = set()
        for step_id in (s.id for s in job.steps):
            if step_id in seen:
                raise ValueError(f"duplicate step id: {step_id}")
            seen.add(step_id)
        for spec in job.steps:
            for dep in spec.deps:
                if dep not in seen:
                    raise ValueError(
                        f"step {spec.id} depends on unknown step {dep}"
                    )
        # Kahn's algorithm: anything left over is on a cycle.
        indegree = {s.id: 0 for s in job.steps}
        dependents: dict[str, list[str]] = {s.id: [] for s in job.steps}
        for spec in job.steps:
            for dep in spec.deps:
                indegree[spec.id] += 1
                dependents[dep].append(spec.id)
        queue = [s.id for s in job.steps if indegree[s.id] == 0]
        visited = 0
        while queue:
            current = queue.pop()
            visited += 1
            for nxt in dependents[current]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(job.steps):
            cyclic = sorted(s for s in indegree if indegree[s] > 0)
            raise ValueError(f"dependency cycle involving: {', '.join(cyclic)}")

    def _build_context(self, job: Job) -> RunContext:
        ctx = RunContext(job=job)
        for step_id, run in job.runs.items():
            if run.status == StepStatus.OK:
                ctx.results[step_id] = run.result
                for art in run.artifacts or []:
                    ctx.artifacts[art.name] = art
        return ctx

    def _normalize_for_run(self, job: Job, *, resume: bool) -> None:
        known = {s.id for s in job.steps}
        job.runs = {k: v for k, v in job.runs.items() if k in known}
        for spec in job.steps:
            run = job.ensure_run(spec.id)
            if run.status == StepStatus.OK:
                continue
            if not resume:
                job.runs[spec.id] = StepRun(step_id=spec.id)
                continue
            if run.status in (StepStatus.FAILED, StepStatus.CANCELED):
                # Explicit resume: give failed steps a fresh attempt budget.
                run.status = StepStatus.PENDING
                run.error = None
                run.started_at = None
                run.finished_at = None
                run.attempt = 0
            elif run.status == StepStatus.RUNNING:
                # Process crashed mid-step; mark retryable/pending.
                run.status = StepStatus.PENDING
                run.error = run.error or "interrupted"
                run.started_at = None
                run.finished_at = None
        if job.status in (JobStatus.OK, JobStatus.FAILED, JobStatus.CANCELED):
            job.status = JobStatus.PENDING

    def _ready_steps(self, job: Job) -> list[StepSpec]:
        ready: list[StepSpec] = []
        for spec in job.steps:
            run = job.ensure_run(spec.id)
            if run.status != StepStatus.PENDING:
                continue
            deps_ok = True
            deps_blocked = False
            for dep in spec.deps:
                dr = job.runs.get(dep)
                if dr is None or dr.status != StepStatus.OK:
                    deps_ok = False
                if dr is not None and dr.status in (
                    StepStatus.FAILED,
                    StepStatus.SKIPPED,
                    StepStatus.CANCELED,
                ):
                    deps_blocked = True
            if deps_ok:
                ready.append(spec)
            elif deps_blocked:
                # Will be marked skipped in _skip_blocked.
                pass
        return ready

    def _skip_blocked(self, job: Job) -> None:
        changed = False
        for spec in job.steps:
            run = job.ensure_run(spec.id)
            if run.status != StepStatus.PENDING:
                continue
            for dep in spec.deps:
                dr = job.runs.get(dep)
                if dr is not None and dr.status in (
                    StepStatus.FAILED,
                    StepStatus.SKIPPED,
                    StepStatus.CANCELED,
                ):
                    run.status = StepStatus.SKIPPED
                    run.error = f"dependency not satisfied: {dep}"
                    run.finished_at = run.finished_at or _now()
                    changed = True
                    break
        if changed:
            self.store.save_job(job)

    def _finalize_job(self, job: Job) -> None:
        statuses = [job.ensure_run(s.id).status for s in job.steps]
        if not statuses:
            # An empty job has nothing to fail.
            job.status = JobStatus.OK
            return
        if any(s == StepStatus.CANCELED for s in statuses):
            job.status = JobStatus.CANCELED
        elif any(s in (StepStatus.FAILED, StepStatus.SKIPPED) for s in statuses):
            # SKIPPED only ever comes from an unsatisfied dependency (or an
            # explicit skip), so the job did not complete as planned.
            job.status = JobStatus.FAILED
        elif statuses and all(s == StepStatus.OK for s in statuses):
            job.status = JobStatus.OK
        else:
            job.status = JobStatus.FAILED

    def _mark_canceled(self, job: Job) -> None:
        for spec in job.steps:
            run = job.ensure_run(spec.id)
            if run.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.RUNNING):
                run.status = StepStatus.CANCELED
                run.finished_at = run.finished_at or _now()
        job.status = JobStatus.CANCELED

    async def _run_step(
        self,
        job: Job,
        spec: StepSpec,
        handler: StepHandler | dict[str, StepHandler],
        ctx: RunContext,
        cancel_event: asyncio.Event | None,
    ) -> None:
        run = job.ensure_run(spec.id)
        if cancel_event is not None and cancel_event.is_set():
            run.status = StepStatus.CANCELED
            run.finished_at = _now()
            self.store.save_job(job)
            return

        # Idempotent side effect: reuse committed result.
        if spec.kind == StepKind.SIDE_EFFECT and spec.idempotency_key:
            rec = self.store.get_side_effect(spec.idempotency_key)
            if rec and rec.get("status") == "ok":
                run.status = StepStatus.OK
                run.result = rec.get("result")
                run.finished_at = run.finished_at or _now()
                run.error = None
                ctx.results[spec.id] = run.result
                self.store.save_job(job)
                return

        run.status = StepStatus.RUNNING
        run.attempt += 1
        run.started_at = _now()
        run.finished_at = None
        run.error = None
        self.store.save_job(job)

        uses_llm = bool(spec.meta.get("uses_llm"))
        try:
            # Resolve inside the try so a missing handler fails only this step
            # instead of unwinding the whole job.
            fn = self._resolve_handler(spec, handler)
            async with self.leases.acquire(spec, uses_llm=uses_llm):
                if spec.kind == StepKind.SIDE_EFFECT and spec.idempotency_key:
                    rec = self.store.begin_side_effect(
                        spec.idempotency_key, job_id=job.id, step_id=spec.id
                    )
                    if rec.get("existing") and rec.get("status") == "ok":
                        run.status = StepStatus.OK
                        run.result = rec.get("result")
                        run.finished_at = _now()
                        ctx.results[spec.id] = run.result
                        self.store.save_job(job)
                        return
                    if rec.get("existing") and rec.get("status") == "running":
                        same_owner = (
                            rec.get("job_id") == job.id and rec.get("step_id") == spec.id
                        )
                        # 10-minute floor: an LLM publish step can legitimately
                        # run for minutes without touching the record. A 60s
                        # floor let a concurrent duplicate take over a LIVE
                        # side effect and double-publish.
                        stale = (_now() - float(rec.get("updated_at") or 0)) > max(
                            600.0, float(spec.timeout_s or 0) * 2
                        )
                        if same_owner or stale:
                            self.store.finish_side_effect(
                                spec.idempotency_key,
                                status="running",
                                result={"stale_takeover": True},
                            )
                        else:
                            raise RuntimeError(
                                f"side effect in progress: {spec.idempotency_key}"
                            )
                out = self._invoke(fn, spec, job, ctx)
                if inspect.isawaitable(out):
                    if spec.timeout_s and spec.timeout_s > 0:
                        # NOTE: wait_for only abandons the await; a handler that
                        # cannot be cancelled (e.g. one that delegates to a thread)
                        # keeps running after this returns. Callers that need a
                        # hard budget must enforce it inside the handler.
                        out = await asyncio.wait_for(out, timeout=spec.timeout_s)
                    else:
                        out = await out
            normalized = self._normalize_output(spec, out)
            run.result = normalized.result
            run.artifacts = normalized.artifacts
            run.status = StepStatus.OK
            run.finished_at = _now()
            ctx.results[spec.id] = run.result
            for art in run.artifacts:
                ctx.artifacts[art.name] = art
            if spec.kind == StepKind.SIDE_EFFECT and spec.idempotency_key:
                self.store.finish_side_effect(
                    spec.idempotency_key, status="ok", result=run.result
                )
        except Exception as e:
            run.error = str(e)
            run.finished_at = _now()
            if run.attempt <= max(0, int(spec.retries)):
                run.status = StepStatus.PENDING
                # Back off before the next round re-runs this step, or a
                # failing publish retries 3 times in a row with no pause
                # (same class of bug as the 11-21 provider retry note).
                await asyncio.sleep(min(2.0 ** (run.attempt - 1), 8.0))
                logger.info("step %s failed, will retry: %s", spec.id, e)
            else:
                run.status = StepStatus.FAILED
                if spec.kind == StepKind.SIDE_EFFECT and spec.idempotency_key:
                    self.store.finish_side_effect(
                        spec.idempotency_key, status="failed", result={"error": str(e)}
                    )
                logger.warning("step %s failed: %s", spec.id, e)
        finally:
            self.store.save_job(job)

    @staticmethod
    def _resolve_handler(
        spec: StepSpec, handler: StepHandler | dict[str, StepHandler]
    ) -> StepHandler:
        if isinstance(handler, dict):
            fn = handler.get(spec.id) or handler.get(spec.kind.value)
            if fn is None:
                raise KeyError(f"no handler for step {spec.id} ({spec.kind.value})")
            return fn
        return handler

    @staticmethod
    def _invoke(fn: StepHandler, spec: StepSpec, job: Job, ctx: RunContext):
        """Call a handler without ever blocking the event loop.

        Sync handlers used to run inline, which silently serialized every other
        ready step and skipped the step timeout entirely.
        """
        if inspect.iscoroutinefunction(fn):
            return fn(spec, job, ctx)
        return asyncio.to_thread(fn, spec, job, ctx)

    @staticmethod
    def _normalize_output(spec: StepSpec, out: Any) -> StepOutput:
        if isinstance(out, StepOutput):
            for a in out.artifacts:
                if not a.step_id:
                    a.step_id = spec.id
            return out
        if isinstance(out, Artifact):
            if not out.step_id:
                out.step_id = spec.id
            return StepOutput(result=out.value, artifacts=[out])
        if isinstance(out, list) and all(isinstance(x, Artifact) for x in out):
            for a in out:
                if not a.step_id:
                    a.step_id = spec.id
            return StepOutput(result=[a.to_dict() for a in out], artifacts=list(out))
        if isinstance(out, dict) and "result" in out and "artifacts" in out:
            arts = []
            for a in out.get("artifacts") or []:
                art = a if isinstance(a, Artifact) else Artifact.from_dict(a)
                if not art.step_id:
                    art.step_id = spec.id
                arts.append(art)
            return StepOutput(result=out.get("result"), artifacts=arts)
        return StepOutput(result=out, artifacts=[])


def _now() -> float:
    import time

    return time.time()
