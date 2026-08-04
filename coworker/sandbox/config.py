"""Sandbox configuration shared by every platform backend.

A ``SandboxConfig`` describes *what the agent may touch*: the workspace (always
read-write), extra mounted paths (ro/rw/executable), sensitive paths that are
explicitly denied, and whether the sandbox may reach the network. Each platform
backend (``macos_sandbox`` today; Linux/Windows later) compiles this into a
native policy — Seatbelt ``.sb`` on macOS, Landlock/Bubblewrap on Linux, etc.

The model is **deny-default + allow-list**, but reads are broad by default
(``allow_read_all``) so a local writing assistant can still open files anywhere
on the user's own machine; writes are confined to the workspace, ``/tmp``, and
any explicitly mounted path, and OS/system directories plus the ``deny_paths``
are always blocked. Destructive commands are caught one layer up by the
governance Tool Guard, so the sandbox's job is OS + secret confinement — a
kernel-level backstop, not the only gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Mount:
    """An extra path the sandbox may access beyond the workspace."""

    path: str
    writable: bool = False
    executable: bool = True  # may executables inside be run?


@dataclass
class SandboxConfig:
    workspace_dir: str
    mounts: list[Mount] = field(default_factory=list)
    # Paths (expanded via ~) that are denied read AND write regardless of mode.
    # Defaults are applied by `default_sandbox_config`; users may extend.
    deny_paths: list[str] = field(default_factory=list)
    # Allow the sandboxed process to make outbound network connections.
    # Inbound listen/bind is always denied (reverse-shell backstop).
    network_allow: bool = True
    # Reads are allowed broadly across the machine (except deny_paths). Set
    # False to switch to a strict allow-list of only workspace + mounts.
    allow_read_all: bool = True
    timeout_seconds: float = 600.0
    env_vars: dict[str, str] = field(default_factory=dict)
    # Admin-only escape hatch: extra native policy rules embedded verbatim.
    # MUST NOT come from user input or untrusted approval flows.
    extra_rules: str = ""


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0
    # Set when the native policy denied an operation (best-effort detection).
    sandbox_violation: Optional[str] = None


# Paths denied read+write by default: SSH keys, cloud creds, macOS keychains,
# and the local secrets store. These are the high-value exfiltration targets.
_DEFAULT_DENY_PATHS = [
    "~/.ssh",
    "~/.aws",
    "~/.config/openworker/secrets.json",
    "~/Library/Keychains",
    "~/Library/Application Support/Keychain",
]


def default_sandbox_config(workspace_dir: str | Path) -> SandboxConfig:
    """Build a safe, permissive-for-a-local-assistant sandbox config."""
    return SandboxConfig(
        workspace_dir=str(Path(workspace_dir).expanduser().resolve()),
        mounts=[],
        deny_paths=list(_DEFAULT_DENY_PATHS),
        network_allow=True,
        allow_read_all=True,
    )


def sandbox_config_from_dict(
    workspace_dir: str | Path, data: dict[str, Any]
) -> SandboxConfig:
    """Merge a (user) config dict over the defaults. Unknown keys are ignored."""
    cfg = default_sandbox_config(workspace_dir)
    if "network_allow" in data and isinstance(data["network_allow"], bool):
        cfg.network_allow = data["network_allow"]
    if "allow_read_all" in data and isinstance(data["allow_read_all"], bool):
        cfg.allow_read_all = data["allow_read_all"]
    if "timeout_seconds" in data and isinstance(data["timeout_seconds"], (int, float)):
        cfg.timeout_seconds = float(data["timeout_seconds"])
    if "extra_rules" in data and isinstance(data["extra_rules"], str):
        cfg.extra_rules = data["extra_rules"]
    extra_deny = data.get("deny_paths")
    if isinstance(extra_deny, list):
        cfg.deny_paths = list(dict.fromkeys([*_DEFAULT_DENY_PATHS, *extra_deny]))
    mounts = data.get("mounts")
    if isinstance(mounts, list):
        cfg.mounts = [
            Mount(
                path=str(m["path"]),
                writable=bool(m.get("writable", False)),
                executable=bool(m.get("executable", True)),
            )
            for m in mounts
            if isinstance(m, dict) and m.get("path")
        ]
    return cfg
