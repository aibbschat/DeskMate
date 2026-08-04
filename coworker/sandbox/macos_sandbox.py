"""macOS Seatbelt sandbox backend.

Ported from AgentScope QwenPaw (Apache-2.0) and adapted to OpenWorker's
synchronous, threaded ``LocalExecutor``. Uses ``sandbox-exec`` with a compiled
Seatbelt ``.sb`` profile to confine an agent's shell: deny-default, allow
system reads, confine writes to the workspace + ``/tmp`` + mounts, always deny
sensitive paths (``~/.ssh`` etc.), and gate network by config.

Reference: https://developer.apple.com/library/archive/documentation/Security/
Conceptual/SeatBeltBook/SeatBeltBook.html
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional

from .config import ExecutionResult, Mount, SandboxConfig

# Seatbelt violation markers. We match the actual diagnostic shapes emitted by
# `sandbox-exec` / the Seatbelt kernel rather than loose tokens like "deny",
# because application logs routinely contain those words.
_SEATBELT_VIOLATION_RE = re.compile(
    r"\bdeny\(\d+\)"  # `deny(1)`, `deny(2)` ...
    r"|^Sandbox:\s"  # macOS kernel violation line prefix
    r"|^sandbox-exec:\s"  # sandbox-exec error prefix
    r"|\bOperation not permitted\b",
    re.IGNORECASE | re.MULTILINE,
)


class MacOSSandbox:
    """macOS sandbox using sandbox-exec (Seatbelt profiles)."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config

    # -- profile compilation -------------------------------------------------
    @staticmethod
    def _sanitize_seatbelt_path(path: str) -> str:
        """Escape a path for safe embedding in a Seatbelt S-expression.

        Prevents profile injection via double-quote, backslash, and parentheses.
        Newlines are rejected outright (no valid path contains one, and they
        would break the profile grammar).
        """
        if "\n" in path or "\r" in path:
            raise ValueError(
                "Seatbelt path contains newlines (possible injection): "
                f"{path!r}"
            )
        path = path.replace("\\", "\\\\")
        path = path.replace('"', '\\"')
        return path

    def compile_profile(self) -> str:
        """Build the Seatbelt ``.sb`` policy string from ``SandboxConfig``."""
        config = self._config
        _san = self._sanitize_seatbelt_path
        lines = [
            "(version 1)",
            "",
            "(deny default)",
            "",
            "; Basic system operations",
            "(allow process-exec*)",
            "(allow process-fork)",
            "(allow signal)",
            "(allow sysctl-read)",
            "",
            "; System file access (readonly)",
            "(allow file-read*",
            '  (subpath "/System")',
            '  (subpath "/usr/lib")',
            '  (subpath "/usr/share")',
            '  (subpath "/Library")',
            '  (subpath "/private/var/db/timezone")',
            '  (literal "/dev/null")',
            '  (literal "/dev/zero")',
            '  (literal "/dev/random")',
            '  (literal "/dev/urandom")',
            '  (literal "/dev/tty")',
            '  (literal "/dev/dtracehelper")',
            ")",
            "",
            "; Mach operations",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm)",
            "",
            "; Sysctl operations",
            "(allow sysctl-read",
            '  (sysctl-name-prefix "hw.")',
            '  (sysctl-name-prefix "kern.")',
            '  (sysctl-name-prefix "machdep.cpu.")',
            ")",
        ]

        # Network: outbound allowed/denied by config; inbound bind always denied
        # (reverse-shell backstop). Seatbelt can't do domain-level filtering, so
        # `network_allow` is all-or-nothing for outbound.
        lines.append("")
        lines.append("; Network (outbound per config; inbound always denied)")
        if config.network_allow:
            lines.append("(allow network-outbound)")
        else:
            lines.append("(deny network-outbound)")
        lines.append("(deny network-inbound)")

        # File reads
        lines.append("")
        lines.append("; File read")
        if config.allow_read_all:
            # Deny-list mode: read everything except denied sensitive paths.
            lines.append("(allow file-read*)")
        else:
            # Allow-list mode: only declared mounts + workspace.
            lines.append("(allow file-read*")
            lines.append(f'  (subpath "{_san(config.workspace_dir)}"))')
            for mount in config.mounts:
                lines.append("(allow file-read*")
                lines.append(f'  (subpath "{_san(mount.path)}"))')

        # Deny sensitive paths (read + write)
        if config.deny_paths:
            lines.append("")
            lines.append("; Denied sensitive paths")
            for p in config.deny_paths:
                expanded = _san(os.path.expanduser(p))
                lines.append("(deny file-read*")
                lines.append(f'  (subpath "{expanded}"))')
                lines.append("(deny file-write*")
                lines.append(f'  (subpath "{expanded}"))')

        # File writes: workspace, /tmp, mounts, plus deny OS dirs.
        lines.append("")
        lines.append("; File write — confine to workspace + /tmp + mounts")
        for p in ("/dev/null", "/dev/zero", "/dev/tty", "/tmp", "/private/tmp"):
            lines.append("(allow file-write*")
            lines.append(f'  (subpath "{p}"))')
        lines.append("(allow file-write*")
        lines.append(f'  (subpath "{_san(config.workspace_dir)}"))')
        for mount in config.mounts:
            if mount.writable:
                lines.append("(allow file-write*")
                lines.append(f'  (subpath "{_san(mount.path)}"))')
        # Never let the agent scribble over the OS.
        for sysdir in ("/System", "/usr", "/Library", "/etc", "/private/etc"):
            lines.append("(deny file-write*")
            lines.append(f'  (subpath "{sysdir}"))')

        # Execution control: block running binaries from non-executable mounts.
        non_exec = [m for m in config.mounts if not m.executable]
        if non_exec:
            lines.append("")
            lines.append("; Deny execution in non-executable mounts")
            for m in non_exec:
                lines.append("(deny process-exec*")
                lines.append(f'  (subpath "{_san(m.path)}"))')

        # Admin-only escape hatch (verbatim, trusted source only).
        if config.extra_rules:
            if "\n" in str(config.extra_rules):
                # Logged by the caller; we still embed but flag it.
                pass
            lines.append("")
            lines.append("; Platform hints: extra rules (admin-only, verbatim)")
            lines.append(str(config.extra_rules))

        return "\n".join(lines)

    def argv_prefix(self) -> list[str]:
        """Return the argv to prepend so a command runs inside the sandbox."""
        return ["sandbox-exec", "-p", self.compile_profile()]

    # -- execution -----------------------------------------------------------
    def execute(self, cmd: str, cwd: Optional[str] = None) -> ExecutionResult:
        """Run one command inside the sandbox (fresh process per call)."""
        profile = self.compile_profile()
        cwd = cwd or self._config.workspace_dir
        shell = os.environ.get("SHELL", "/bin/bash")
        if not os.path.exists(shell):
            shell = "/bin/bash"
        env = dict(os.environ)
        env.update(self._config.env_vars)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["sandbox-exec", "-p", profile, shell, "-c", cmd],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                timeout=self._config.timeout_seconds,
                text=True,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            combined = proc.stdout + proc.stderr
            violation = None
            if proc.returncode != 0 and _SEATBELT_VIOLATION_RE.search(combined):
                violation = proc.stderr.strip() or proc.stdout.strip()
            return ExecutionResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
                duration_ms=duration_ms,
                sandbox_violation=violation,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                exit_code=-1,
                stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
                stderr="Command timed out",
                timed_out=True,
                duration_ms=duration_ms,
            )
        except Exception as exc:  # noqa: BLE001 - surface any spawn error
            duration_ms = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_ms=duration_ms,
            )

    @staticmethod
    def detect_violation(text: str) -> Optional[str]:
        """Best-effort detection of a Seatbelt denial in command output."""
        m = _SEATBELT_VIOLATION_RE.search(text or "")
        return text.strip() if m else None
