"""Global error diagnostics for DeskMate — SQLite-backed, thread-safe.

Provides:
- Diagnostics class: record, query, and aggregate errors
- @diagnose(module, tool) decorator: auto-catch-and-record
- diagnostics_health() standalone: JSON-serialisable health report
- get_global_diagnostics() singleton factory

Uses only Python stdlib (sqlite3, threading, json, time, traceback).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DB = Path.home() / ".deskmate" / "diagnostics.db"

# ---------------------------------------------------------------------------
# Diagnostics class
# ---------------------------------------------------------------------------


class Diagnostics:
    """Thread-safe SQLite-backed error diagnostics store.

    Records errors with timestamp, module, tool, error message, and JSON
    context. Supports recent queries, stats aggregation, and health checks.
    """

    def __init__(self, db_path: str | Path = _DEFAULT_DB) -> None:
        self._db_path = Path(db_path).expanduser()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS diagnostics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                module_name TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                error_message TEXT NOT NULL,
                context TEXT DEFAULT '{}'
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_diagnostics_module
            ON diagnostics (module_name)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_diagnostics_timestamp
            ON diagnostics (timestamp DESC)
            """
        )
        self._conn.commit()

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_error(
        self,
        module: str,
        tool: str,
        message: str,
        context: Optional[dict[str, Any]] = None,
    ) -> int:
        """Record a single error and return its row id.

        Args:
            module:  Name of the module where the error occurred.
            tool:    Name of the tool or function that failed.
            message: Human-readable error message.
            context: Optional dict of contextual data (JSON-serialised).

        Returns:
            The auto-incremented id of the inserted row.
        """
        ctx_json = json.dumps(context or {}, ensure_ascii=False, default=str)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO diagnostics (timestamp, module_name, tool_name, error_message, context)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self._now(), module, tool, str(message), ctx_json),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent *limit* error records.

        Each record is a dict with keys: id, timestamp, module_name,
        tool_name, error_message, context (parsed from JSON).
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM diagnostics
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        """Return aggregated error counts.

        Returns:
            A dict with:
                - total_errors (int)
                - errors_by_module (dict[str, int])
                - errors_by_tool (dict[str, int])
        """
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM diagnostics"
            ).fetchone()["cnt"]

            by_module_rows = self._conn.execute(
                "SELECT module_name, COUNT(*) AS cnt FROM diagnostics GROUP BY module_name ORDER BY cnt DESC"
            ).fetchall()
            by_module: dict[str, int] = {r["module_name"]: r["cnt"] for r in by_module_rows}

            by_tool_rows = self._conn.execute(
                "SELECT tool_name, COUNT(*) AS cnt FROM diagnostics GROUP BY tool_name ORDER BY cnt DESC"
            ).fetchall()
            by_tool: dict[str, int] = {r["tool_name"]: r["cnt"] for r in by_tool_rows}

        return {
            "total_errors": total,
            "errors_by_module": by_module,
            "errors_by_tool": by_tool,
        }

    def get_health(self) -> dict[str, Any]:
        """Check health of all recorded modules.

        Returns:
            A dict with:
                - status: "healthy" if no errors, else "degraded"
                - total_errors (int)
                - module_status: list of dicts with module_name and error_count
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT module_name, COUNT(*) AS cnt FROM diagnostics GROUP BY module_name ORDER BY cnt DESC"
            ).fetchall()
            module_status = [
                {"module_name": r["module_name"], "error_count": r["cnt"]}
                for r in rows
            ]
            total = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM diagnostics"
            ).fetchone()["cnt"]

        return {
            "status": "healthy" if total == 0 else "degraded",
            "total_errors": total,
            "module_status": module_status,
        }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Row deserialisation
# ---------------------------------------------------------------------------


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["context"] = json.loads(d.get("context") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["context"] = {}
    return d


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_global_instance: Optional[Diagnostics] = None
_global_lock = threading.Lock()


def get_global_diagnostics() -> Diagnostics:
    """Return the global Diagnostics singleton.

    Creates it on first call; subsequent calls return the same instance.
    Thread-safe.
    """
    global _global_instance
    if _global_instance is None:
        with _global_lock:
            if _global_instance is None:
                _global_instance = Diagnostics()
    return _global_instance


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def diagnose(module: str, tool: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator that catches exceptions, records them to diagnostics, and re-raises.

    Usage::

        @diagnose(module="memory", tool="memory_read")
        def my_tool(...):
            ...

    The decorated function behaves identically — same return value on success,
    same exception on failure — but every exception is recorded in the
    diagnostics database before being re-raised.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception:
                msg = traceback.format_exc()
                diag = get_global_diagnostics()
                diag.record_error(
                    module=module,
                    tool=tool,
                    message=msg,
                    context={"args": _summarise_args(args, kwargs)},
                )
                raise
        return wrapper
    return decorator


def _summarise_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Build a safe, summarised representation of function arguments."""
    result: dict[str, Any] = {}
    if args:
        result["_positional"] = [str(a)[:200] for a in args[:10]]
    for k, v in kwargs.items():
        if any(s in k.lower() for s in ("token", "secret", "password", "key", "auth")):
            result[k] = "[redacted]"
        else:
            result[k] = str(v)[:500]
    return result


# ---------------------------------------------------------------------------
# Standalone health report
# ---------------------------------------------------------------------------


def diagnostics_health() -> dict[str, Any]:
    """Return a JSON-serialisable health report.

    Returns:
        A dict with:
            - total_errors (int)
            - errors_by_module (dict[str, int])
            - errors_by_tool (dict[str, int])
            - recent_errors (list[dict]): last 10 error records
            - module_status (list[dict]): each module with its error count

    Can be exposed as an MCP tool.
    """
    diag = get_global_diagnostics()
    stats = diag.get_stats()
    recent = diag.get_recent(limit=10)
    health = diag.get_health()
    return {
        "total_errors": stats["total_errors"],
        "errors_by_module": stats["errors_by_module"],
        "errors_by_tool": stats["errors_by_tool"],
        "recent_errors": recent,
        "module_status": health["module_status"],
    }