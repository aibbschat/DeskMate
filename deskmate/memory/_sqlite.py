"""统一的 SQLite 连接参数（2026-11-19 PR14 · #29）。

问题：全仓 100+ 处裸 ``sqlite3.connect()``，``busy_timeout`` / WAL / synchronous
各写各的；多进程（本机 daemon + 云端窗口 + MCP server）并发写时容易出现
``database is locked``，而且同一库被不同 pragma 反复切换 journal_mode。

本模块只做**参数收敛**，不接管连接生命周期（谁开的谁关）：

    from deskmate.memory._sqlite import connect
    conn = connect(db_path)            # WAL + synchronous=NORMAL + busy_timeout=5000
    conn = connect(db_path, readonly=True)

``DESKMATE_SQLITE_BUSY_TIMEOUT_MS`` 可调超时（默认 5000）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 5000


def _busy_timeout() -> int:
    try:
        return max(0, int(os.environ.get("DESKMATE_SQLITE_BUSY_TIMEOUT_MS", BUSY_TIMEOUT_MS)))
    except ValueError:
        return BUSY_TIMEOUT_MS


_UNSET = object()


def connect(
    path: str | Path,
    *,
    timeout: float | None = None,
    check_same_thread: bool = True,
    wal: bool = True,
    synchronous: str | None = "NORMAL",
    readonly: bool = False,
    isolation_level: str | None | object = _UNSET,
) -> sqlite3.Connection:
    """按统一口径打开一个连接（WAL + busy_timeout，可选只读）。"""
    kwargs = {"check_same_thread": check_same_thread}
    if isolation_level is not _UNSET:
        kwargs["isolation_level"] = isolation_level
    if timeout is not None:
        kwargs["timeout"] = timeout
    if readonly:
        kwargs["uri"] = True
        target = f"file:{Path(path).as_posix()}?mode=ro"
    else:
        target = str(path)
    conn = sqlite3.connect(target, **kwargs)
    try:
        conn.execute(f"PRAGMA busy_timeout={_busy_timeout()}")
        if not readonly:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            if synchronous:
                conn.execute(f"PRAGMA synchronous={synchronous}")
    except sqlite3.Error:
        pass  # pragma 失败不阻断（例如只读挂载 / 内存库）
    return conn
