"""Exit-with-parent guard, shared by the desktop sidecar and any child adapter.

When a process is launched as a desktop helper (the sidecar under the GUI, or an
adapter spawned BY the sidecar), set ``COWORKER_EXIT_WITH_PARENT=1`` and pass the
watcher's own PID via ``COWORKER_PARENT_PID``. We then poll that PID and
``os._exit(0)`` the moment it dies — covering abrupt kills (Tauri restart, crash)
that skip the shell's graceful child-kill and would otherwise leak the process.

POSIX: probe the PID with ``kill(pid, 0)`` every ~1.5s. Windows has no re-parenting
semantics, so we block on a process handle and exit when it signals.
"""
from __future__ import annotations

import os
import sys
import threading


def _watch_parent_windows(parent: int) -> None:
    """Block on a handle to the parent process; exit only when it actually terminates.

    Best-effort — any failure leaves the parent's RunEvent::ExitRequested kill as the primary
    cleanup path. Two correctness points that bit us before:
      - `OpenProcess` returns a 64-bit HANDLE; ctypes defaults the return type to a 32-bit int,
        which truncates the handle to garbage. Declare restype/argtypes so the handle is valid.
      - Only `os._exit` on WAIT_OBJECT_0 (the parent genuinely died). A bad handle yields
        WAIT_FAILED immediately — treating that as "parent died" would kill a perfectly healthy
        server seconds after startup (exactly the freeze we saw)."""
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x0010_0000
    INFINITE = 0xFFFF_FFFF
    WAIT_OBJECT_0 = 0x0000_0000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent)
    if not handle:
        return

    def watch() -> None:
        if kernel32.WaitForSingleObject(handle, INFINITE) == WAIT_OBJECT_0:
            os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def _exit_when_orphaned() -> None:
    """If launched with ``COWORKER_EXIT_WITH_PARENT=1``, exit when the given parent PID dies.

    The parent PID comes from ``COWORKER_PARENT_PID`` (set explicitly by the spawner), falling
    back to ``os.getppid()`` for a direct spawn. A call that returns normally means the guard is
    not active (standalone run) — the caller simply continues.
    """
    if os.environ.get("COWORKER_EXIT_WITH_PARENT") != "1":
        return

    try:
        parent = int(os.environ.get("COWORKER_PARENT_PID") or 0)
    except ValueError:
        parent = 0
    parent = parent or os.getppid()  # standalone fallback: our direct spawner

    if sys.platform == "win32":
        _watch_parent_windows(parent)
        return

    import time

    original_ppid = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(1.5)
            try:
                os.kill(parent, 0)  # liveness probe only; signal 0 delivers nothing
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass  # alive, but owned by someone else (shouldn't happen) — keep waiting
            # Secondary signal: our direct parent died (covers PID-reuse edge cases).
            if os.getppid() != original_ppid:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
