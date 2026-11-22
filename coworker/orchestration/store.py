"""SQLite-backed checkpoint store for orchestration jobs.

Jobs and step runs are stored as JSON blobs so the model can evolve without
schema migrations. A separate side_effects table records idempotency keys to
make external/publish steps safe to retry.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import Job, JobStatus, StepRun, StepStatus


def _now() -> float:
    return time.time()


class OrchestrationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        try:
            from deskmate.memory._sqlite import connect as _sqlite_connect

            self._conn = _sqlite_connect(self.path, check_same_thread=False)
        except Exception:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orchestration_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orch_jobs_status
                    ON orchestration_jobs(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS orchestration_side_effects (
                    key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orch_side_effects_job
                    ON orchestration_side_effects(job_id, updated_at DESC);
                """
            )
            self._conn.commit()

    # -- jobs -----------------------------------------------------------------
    def save_job(self, job: Job) -> Job:
        job.updated_at = _now()
        data = json.dumps(job.to_dict(), ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO orchestration_jobs (id, status, source, updated_at, data)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job.id, job.status.value, job.source, job.updated_at, data),
            )
            self._conn.commit()
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM orchestration_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return Job.from_dict(json.loads(row["data"])) if row else None

    def list_jobs(
        self, *, source: str | None = None, limit: int = 50
    ) -> list[Job]:
        q = "SELECT data FROM orchestration_jobs"
        args: list[Any] = []
        if source:
            q += " WHERE source=?"
            args.append(source)
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [Job.from_dict(json.loads(r["data"])) for r in rows]

    def save_step_run(self, job_id: str, run: StepRun) -> Optional[Job]:
        """Persist a single step run inside its job checkpoint."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM orchestration_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                return None
            job = Job.from_dict(json.loads(row["data"]))
            job.runs[run.step_id] = run
            job.updated_at = _now()
            self._conn.execute(
                """
                UPDATE orchestration_jobs SET status=?, updated_at=?, data=? WHERE id=?
                """,
                (
                    job.status.value,
                    job.updated_at,
                    json.dumps(job.to_dict(), ensure_ascii=False, default=str),
                    job_id,
                ),
            )
            self._conn.commit()
            return job

    # -- side effects / idempotency ------------------------------------------
    def get_side_effect(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_side_effects WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        result = row["result"]
        try:
            result = json.loads(result) if result else None
        except Exception:
            pass
        return {
            "key": row["key"],
            "job_id": row["job_id"],
            "step_id": row["step_id"],
            "status": row["status"],
            "result": result,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def begin_side_effect(
        self, key: str, *, job_id: str, step_id: str
    ) -> dict[str, Any]:
        """Reserve an idempotency key. Returns existing record if present."""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_side_effects WHERE key=?", (key,)
            ).fetchone()
            if row:
                result = row["result"]
                try:
                    result = json.loads(result) if result else None
                except Exception:
                    pass
                return {
                    "key": row["key"],
                    "job_id": row["job_id"],
                    "step_id": row["step_id"],
                    "status": row["status"],
                    "result": result,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "existing": True,
                }
            self._conn.execute(
                """
                INSERT INTO orchestration_side_effects
                    (key, job_id, step_id, status, result, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, job_id, step_id, "running", "null", now, now),
            )
            self._conn.commit()
        return {
            "key": key,
            "job_id": job_id,
            "step_id": step_id,
            "status": "running",
            "result": None,
            "created_at": now,
            "updated_at": now,
            "existing": False,
        }

    def finish_side_effect(
        self, key: str, *, status: str, result: Any = None
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                UPDATE orchestration_side_effects
                SET status=?, result=?, updated_at=? WHERE key=?
                """,
                (status, json.dumps(result, ensure_ascii=False, default=str), now, key),
            )
            self._conn.commit()

    def prune(self, *, max_age_days: float = 30.0) -> int:
        """Delete old finished job checkpoints. Returns the row count removed.

        Jobs and side-effect records otherwise accumulate forever: every consult
        run and every automation run writes one. Side-effect records are kept —
        they are tiny and are what makes a late retry idempotent.
        """
        try:
            days = max(0.0, float(max_age_days))
        except (TypeError, ValueError):
            days = 30.0
        cutoff = _now() - days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM orchestration_jobs "
                "WHERE updated_at < ? AND status NOT IN ('running', 'pending')",
                (cutoff,),
            )
            removed = cur.rowcount or 0
            self._conn.commit()
        return removed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
