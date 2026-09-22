"""macOS Seatbelt sandbox — integration tests (they really launch ``sandbox-exec``).

Skipped unless the host is macOS with ``sandbox-exec`` on PATH.

Why these exist: the unit tests in ``test_sandbox.py`` only assert that the
compiled profile *contains* the right rules. That is not the same as the kernel
enforcing them, and it cannot catch the class of bug where two rules of the
profile disagree — e.g. a path that is both a writable mount and a ``deny_path``.

⚠ Probes deliberately do NOT live under pytest's ``tmp_path``: the profile
blanket-allows writes to ``/tmp`` + ``/private/tmp``, so a probe there comes back
writable with or without a mount and every assertion silently turns into a
tautology. ``scratch`` below allocates outside both.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coworker.roots import RootDir
from coworker.sandbox import (
    MacOSSandbox,
    Mount,
    SandboxConfig,
    mounts_from_write_roots,
    sandbox_config_from_dict,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS + sandbox-exec",
)


@pytest.fixture
def scratch():
    """A throwaway dir outside ``/tmp`` + ``/private/tmp`` (see module docstring)."""
    base = Path.home() / "Library" / "Caches" / "ow-sandbox-it"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="t-", dir=str(base)))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _try_touch(cfg: SandboxConfig, target: Path | str) -> tuple[bool, str]:
    """Attempt to create a file at ``target`` from inside the sandbox."""
    target = str(target)
    os.makedirs(target, exist_ok=True)
    profile = MacOSSandbox(cfg).compile_profile()
    proc = subprocess.run(
        ["sandbox-exec", "-p", profile, "/bin/bash", "-c", f"touch '{target}/probe.txt'"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0, (proc.stderr or "").strip()


def test_workspace_writable_but_unmounted_sibling_is_not(scratch):
    ws = scratch / "ws"
    outside = scratch / "outside"
    ws.mkdir()
    outside.mkdir()

    cfg = SandboxConfig(workspace_dir=str(ws))
    ok, err = _try_touch(cfg, ws)
    assert ok, err

    blocked, err2 = _try_touch(cfg, outside)
    assert not blocked
    assert "Operation not permitted" in err2


def test_write_root_is_writable_through_the_shell(scratch):
    """Regression: a read-write root must work for the *shell*, not just write_file.

    Observed in the wild: a scheduled task delivering into ~/Documents died with
    "Operation not permitted" while ``write_file`` into that same folder succeeded —
    roots reached the file tools and the permission engine but not the sandbox.
    """
    ws = scratch / "ws"
    docs = scratch / "docs"
    ws.mkdir()
    docs.mkdir()

    roots = [
        RootDir(path=ws, writable=True, label="workspace"),
        RootDir(path=docs, writable=True, label="docs"),
    ]
    cfg = sandbox_config_from_dict(ws, {"mounts": mounts_from_write_roots(roots)})
    ok, err = _try_touch(cfg, docs)
    assert ok, err


def test_read_only_root_stays_unwritable(scratch):
    ws = scratch / "ws"
    ro = scratch / "ro"
    ws.mkdir()
    ro.mkdir()

    cfg = sandbox_config_from_dict(
        ws, {"mounts": mounts_from_write_roots([RootDir(path=ro, writable=False)])}
    )
    blocked, _ = _try_touch(cfg, ro)
    assert not blocked, "只读 root 不得获得写权限"


def test_deny_path_still_wins_over_a_mount(scratch):
    """Security regression: mounting a deny_path must not re-open it.

    Verified against the real kernel rather than by reading the profile, because
    Seatbelt rule precedence is not simply "last one wins".
    """
    ws = scratch / "ws"
    secret = scratch / "secret"
    ws.mkdir()
    secret.mkdir()

    cfg = SandboxConfig(
        workspace_dir=str(ws),
        mounts=[Mount(path=str(secret), writable=True)],
        deny_paths=[str(secret)],
    )
    blocked, _ = _try_touch(cfg, secret)
    assert not blocked, "deny_paths 必须压过同路径的 mount"


def test_default_deny_paths_block_ssh_even_when_mounted(scratch):
    ws = scratch / "ws"
    ws.mkdir()
    ssh = os.path.expanduser("~/.ssh")
    if not os.path.isdir(ssh):
        pytest.skip("no ~/.ssh on this host")

    probe = os.path.join(ssh, ".ow_probe")
    cfg = SandboxConfig(
        workspace_dir=str(ws),
        mounts=[Mount(path=ssh, writable=True)],
        deny_paths=["~/.ssh"],
    )
    try:
        blocked, _ = _try_touch(cfg, probe)
        assert not blocked, "~/.ssh 即便被挂载也必须仍然拒绝写入"
    finally:
        shutil.rmtree(probe, ignore_errors=True)
