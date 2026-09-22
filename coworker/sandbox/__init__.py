"""Sandbox package — confines an agent's shell to a native OS policy.

Entry points:
  * ``default_sandbox_config(workspace)`` / ``sandbox_config_from_dict(...)``
    build a :class:`SandboxConfig`.
  * ``build_sandbox(config)`` returns a backend (``MacOSSandbox`` on macOS) or
    ``None`` when no backend is available (caller then runs unsandboxed).

The :class:`~coworker.tools.shell.LocalExecutor` calls ``build_sandbox`` and,
when it gets a backend, prepends ``sandbox.argv_prefix()`` to the shell argv so
the *whole* persistent shell (and every command it runs) is confined. This is
the minimal-touch integration: no per-command decision needed, because the
Seatbelt profile already denies OS writes, secrets, and inbound network.
"""

from __future__ import annotations

import logging
import shutil
import sys
from typing import Optional

from .config import (
    ExecutionResult,
    Mount,
    SandboxConfig,
    default_sandbox_config,
    mounts_from_write_roots,
    parse_mounts,
    sandbox_config_from_dict,
)
from .macos_sandbox import MacOSSandbox

logger = logging.getLogger(__name__)

__all__ = [
    "SandboxConfig",
    "Mount",
    "ExecutionResult",
    "default_sandbox_config",
    "mounts_from_write_roots",
    "parse_mounts",
    "sandbox_config_from_dict",
    "build_sandbox",
    "MacOSSandbox",
]


def sandbox_available() -> bool:
    """True when a native sandbox backend exists for this platform."""
    if sys.platform == "darwin":
        return shutil.which("sandbox-exec") is not None
    # Linux (Landlock/Bubblewrap) and Windows (AppContainer) backends are not
    # wired yet; fail open so the agent keeps running.
    return False


def build_sandbox(config: SandboxConfig) -> Optional[MacOSSandbox]:
    """Return a sandbox backend for ``config``, or ``None`` if unavailable.

    Returns ``None`` (not an error) on unsupported platforms or when the
    required native binary is missing — the caller runs unsandboxed and logs a
    warning. We fail *open* deliberately: a missing sandbox must never wedge a
    local assistant session.
    """
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            logger.warning(
                "sandbox-exec not found; running shell WITHOUT a macOS "
                "Seatbelt sandbox. Install Xcode CLT or set sandbox_enabled=false."
            )
            return None
        return MacOSSandbox(config)
    logger.warning(
        "No sandbox backend for platform %r; running shell unsandboxed.",
        sys.platform,
    )
    return None
