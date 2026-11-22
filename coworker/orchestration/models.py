"""Unified execution substrate models.

P0 foundation: long tasks are decomposed into checkpointed steps with explicit
artifacts. The same model is used by interactive agent jobs and scheduled
automations; only the trigger/adapter differs.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def _now() -> float:
    return time.time()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class StepKind(str, Enum):
    READ = "read"          # read-only gather/extract/check; safe to parallelize
    COMPUTE = "compute"    # pure local transform/synthesis; safe to parallelize
    WRITE = "write"        # workspace/file mutation; serialize per target
    SIDE_EFFECT = "side_effect"  # external/publish/send; idempotent, serialized


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELED = "canceled"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass
class Artifact:
    name: str
    kind: str = "text"  # text|json|file|dict
    value: Any = None
    path: Optional[str] = None
    step_id: str = ""
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "value": self.value,
            "path": self.path,
            "step_id": self.step_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Artifact":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in dict(d).items() if k in known})


@dataclass
class StepSpec:
    id: str
    title: str
    kind: StepKind = StepKind.READ
    deps: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 300.0
    retries: int = 0
    idempotency_key: Optional[str] = None
    target: Optional[str] = None  # serialize writes/side effects by target
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["kind"] = self.kind.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepSpec":
        data = dict(d)
        data["kind"] = StepKind(data.get("kind", StepKind.READ))
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class StepRun:
    step_id: str
    status: StepStatus = StepStatus.PENDING
    attempt: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    artifacts: list[Artifact] = field(default_factory=list)
    result: Any = None
    worker: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["artifacts"] = [a.to_dict() for a in self.artifacts]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepRun":
        data = dict(d)
        data["status"] = StepStatus(data.get("status", StepStatus.PENDING))
        data["artifacts"] = [Artifact.from_dict(a) for a in data.get("artifacts") or []]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Job:
    id: str = field(default_factory=lambda: _id("job"))
    title: str = ""
    source: str = ""  # interactive|automation|...
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    steps: list[StepSpec] = field(default_factory=list)
    runs: dict[str, StepRun] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "runs": {k: v.to_dict() for k, v in self.runs.items()},
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        return cls(
            id=d.get("id") or _id("job"),
            title=d.get("title", ""),
            source=d.get("source", ""),
            status=JobStatus(d.get("status", JobStatus.PENDING)),
            created_at=float(d.get("created_at", _now())),
            updated_at=float(d.get("updated_at", _now())),
            steps=[StepSpec.from_dict(s) for s in d.get("steps") or []],
            runs={k: StepRun.from_dict(v) for k, v in (d.get("runs") or {}).items()},
            meta=dict(d.get("meta") or {}),
        )

    def step(self, step_id: str) -> Optional[StepSpec]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def ensure_run(self, step_id: str) -> StepRun:
        run = self.runs.get(step_id)
        if run is None:
            run = StepRun(step_id=step_id)
            self.runs[step_id] = run
        return run
