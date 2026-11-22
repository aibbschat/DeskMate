"""Automation adapters for the orchestration substrate.

The default automation path still runs a single agent session. This adapter
provides a checkpointed pipeline shape for long-running tasks that can be
decomposed into explicit steps (extract/review/write/publish). It is the
bridge that lets daily and automated flows share the same runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .models import Artifact, Job, StepKind, StepSpec

StepFn = Callable[..., Awaitable[Any] | Any]


@dataclass
class PipelineStep:
    id: str
    title: str
    kind: StepKind
    handler: StepFn
    deps: list[str] = field(default_factory=list)
    retries: int = 0
    timeout_s: float = 300.0
    idempotency_key: Optional[str] = None
    target: Optional[str] = None
    uses_llm: bool = True
    meta: dict[str, Any] = field(default_factory=dict)


def build_pipeline_job(
    *,
    job_id: str,
    title: str,
    source: str,
    steps: list[PipelineStep],
    meta: Optional[dict[str, Any]] = None,
) -> tuple[Job, dict[str, StepFn]]:
    specs: list[StepSpec] = []
    handlers: dict[str, StepFn] = {}
    for s in steps:
        spec_meta = dict(s.meta)
        if s.uses_llm:
            spec_meta.setdefault("uses_llm", True)
        specs.append(
            StepSpec(
                id=s.id,
                title=s.title,
                kind=s.kind,
                deps=list(s.deps),
                retries=s.retries,
                timeout_s=s.timeout_s,
                idempotency_key=s.idempotency_key,
                target=s.target,
                meta=spec_meta,
            )
        )
        handlers[s.id] = s.handler
    return Job(id=job_id, title=title, source=source, steps=specs, meta=meta or {}), handlers


def checkpoint_artifact(name: str, value: Any, *, kind: str = "json") -> Artifact:
    return Artifact(name=name, kind=kind, value=value)


def build_read_check_merge_pipeline(
    *,
    job_id: str,
    title: str,
    source: str,
    items: list[dict[str, Any]],
    extract_handler: StepFn,
    merge_handler: StepFn,
    check_handler: StepFn | None = None,
    merge_deps_extra: list[str] | None = None,
    meta: Optional[dict[str, Any]] = None,
) -> tuple[Job, dict[str, StepFn]]:
    """Representative pattern: parallel read/extract/check + single merge.

    Each item becomes a READ step; optional CHECK steps depend on extracts.
    A single COMPUTE merge step depends on all terminal read/check steps.
    """
    steps: list[PipelineStep] = []
    terminal_deps: list[str] = []
    for i, item in enumerate(items):
        sid = str(item.get("id") or f"extract-{i+1}")
        steps.append(
            PipelineStep(
                id=sid,
                title=str(item.get("title") or sid),
                kind=StepKind.READ,
                handler=extract_handler,
                uses_llm=bool(item.get("uses_llm", True)),
                meta={"item": item},
            )
        )
        if check_handler is not None:
            cid = f"check-{sid}"
            steps.append(
                PipelineStep(
                    id=cid,
                    title=f"Check {sid}",
                    kind=StepKind.COMPUTE,
                    handler=check_handler,
                    deps=[sid],
                    uses_llm=bool(item.get("check_uses_llm", False)),
                    meta={"item": item, "extract_step": sid},
                )
            )
            terminal_deps.append(cid)
        else:
            terminal_deps.append(sid)
    merge_deps = list(terminal_deps) + list(merge_deps_extra or [])
    steps.append(
        PipelineStep(
            id="merge",
            title="Merge",
            kind=StepKind.COMPUTE,
            handler=merge_handler,
            deps=merge_deps,
            uses_llm=True,
            meta={"pattern": "read_check_merge"},
        )
    )
    return build_pipeline_job(
        job_id=job_id, title=title, source=source, steps=steps, meta=meta
    )


def submit_read_check_merge(
    host: Any,
    *,
    job_id: str,
    title: str,
    source: str,
    items: list[dict[str, Any]],
    extract_handler: StepFn,
    merge_handler: StepFn,
    check_handler: StepFn | None = None,
    meta: Optional[dict[str, Any]] = None,
) -> str:
    """Submit a parallel read/check/merge job to a background host.

    This is the消费者-facing entry point: callers describe *what* to extract
    per item and how to merge, and get a job id back immediately. All steps run
    in background threads under the host's concurrency cap and the shared LLM
    lease; the CHECK steps fan out after their extract, MERGE waits for all
    terminal steps. A process restart re-dispatches unfinished steps via
    ``host.recover()`` without re-running completed ones.
    """
    job, handlers = build_read_check_merge_pipeline(
        job_id=job_id,
        title=title,
        source=source,
        items=items,
        extract_handler=extract_handler,
        merge_handler=merge_handler,
        check_handler=check_handler,
        meta={**(meta or {}), "pattern": "read_check_merge"},
    )
    return host.submit(job, handlers)


def build_checkpointed_publish_pipeline(
    *,
    job_id: str,
    title: str,
    source: str,
    produce_handler: StepFn,
    publish_handler: StepFn,
    publish_idempotency_key: str,
    publish_target: str = "publish",
    produce_retries: int = 0,
    publish_retries: int = 2,
    meta: Optional[dict[str, Any]] = None,
) -> tuple[Job, dict[str, StepFn]]:
    """Representative pattern: checkpointed produce + idempotent publish.

    Publish failure does not discard produce artifacts because the produce step
    is checkpointed and publish is an idempotent side effect.
    """
    steps = [
        PipelineStep(
            id="produce",
            title="Produce artifacts",
            kind=StepKind.COMPUTE,
            handler=produce_handler,
            retries=produce_retries,
            uses_llm=True,
            meta={"pattern": "checkpointed_publish"},
        ),
        PipelineStep(
            id="publish",
            title="Publish",
            kind=StepKind.SIDE_EFFECT,
            handler=publish_handler,
            deps=["produce"],
            retries=publish_retries,
            idempotency_key=publish_idempotency_key,
            target=publish_target,
            uses_llm=True,
            meta={"pattern": "checkpointed_publish"},
        ),
    ]
    return build_pipeline_job(
        job_id=job_id, title=title, source=source, steps=steps, meta=meta
    )
