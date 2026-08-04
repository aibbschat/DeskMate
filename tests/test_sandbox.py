"""macOS Seatbelt sandbox — backend tests.

Covers the parts that don't require actually launching ``sandbox-exec``:

  * :meth:`MacOSSandbox.compile_profile` builds a deny-default Seatbelt ``.sb``
    that confines writes to the workspace + ``/tmp`` + mounts, blocks OS dirs
    and the ``deny_paths`` (secrets), and gates network by config.
  * :meth:`MacOSSandbox._sanitize_seatbelt_path` prevents profile injection.
  * :meth:`MacOSSandbox.argv_prefix` wraps a command in ``sandbox-exec -p``.
  * :func:`build_sandbox` returns a backend on macOS (when ``sandbox-exec``
    exists) or ``None`` otherwise — the fail-open contract.

The real confinement (OS writes / secrets / inbound net actually denied) is
covered by ``tests/test_sandbox_exec.py`` when run on a macOS host with
sandbox-exec available.
"""

from __future__ import annotations

import os
import shutil

import pytest

from coworker.sandbox import (
    ExecutionResult,
    Mount,
    MacOSSandbox,
    SandboxConfig,
    build_sandbox,
    default_sandbox_config,
    sandbox_config_from_dict,
)


def test_default_config_has_secret_deny_paths():
    cfg = default_sandbox_config("/tmp/ow")
    assert any("ssh" in p for p in cfg.deny_paths)
    assert any("aws" in p for p in cfg.deny_paths)
    assert any("Keychains" in p for p in cfg.deny_paths)
    assert cfg.allow_read_all is True
    assert cfg.network_allow is True


def test_sandbox_config_from_dict_merges():
    cfg = sandbox_config_from_dict(
        "/tmp/ow",
        {
            "network_allow": False,
            "allow_read_all": False,
            "timeout_seconds": 30,
            "deny_paths": ["/secret/project"],
            "mounts": [{"path": "/data", "writable": True, "executable": False}],
        },
    )
    assert cfg.network_allow is False
    assert cfg.allow_read_all is False
    assert cfg.timeout_seconds == 30.0
    # user deny path merged with defaults (deduped, order preserved)
    assert "/secret/project" in cfg.deny_paths
    assert any("ssh" in p for p in cfg.deny_paths)
    assert cfg.mounts == [Mount(path="/data", writable=True, executable=False)]


def test_compiled_profile_confines_writes_and_secrets(tmp_path):
    cfg = default_sandbox_config(tmp_path)
    ws = cfg.workspace_dir
    profile = MacOSSandbox(cfg).compile_profile()

    assert "(version 1)" in profile
    assert "(deny default)" in profile
    # Workspace allowed for read + write.
    assert f'(subpath "{ws}")' in profile
    # OS directories denied for write (kernel-level backstop).
    for d in ("/System", "/usr", "/Library", "/etc", "/private/etc"):
        assert f'(subpath "{d}")' in profile
    # Sensitive paths expanded from ~ and denied read+write.
    home = os.path.expanduser("~")
    assert f'(subpath "{home}/.ssh")' in profile
    assert f'(subpath "{home}/.aws")' in profile
    # Network gating.
    assert "(allow network-outbound)" in profile
    assert "(deny network-inbound)" in profile


def test_profile_network_denied_when_disabled(tmp_path):
    cfg = default_sandbox_config(tmp_path)
    cfg.network_allow = False
    profile = MacOSSandbox(cfg).compile_profile()
    assert "(deny network-outbound)" in profile


def test_profile_allowlist_read_mode(tmp_path):
    cfg = default_sandbox_config(tmp_path)
    cfg.allow_read_all = False
    cfg.mounts = [Mount(path="/data", writable=False, executable=True)]
    profile = MacOSSandbox(cfg).compile_profile()
    # Deny-list mode emits a broad allow; allow-list mode must NOT.
    assert "(allow file-read*)" not in profile
    assert '(subpath "/data")' in profile


def test_profile_non_executable_mount_blocked(tmp_path):
    cfg = default_sandbox_config(tmp_path)
    cfg.mounts = [Mount(path="/data", writable=True, executable=False)]
    profile = MacOSSandbox(cfg).compile_profile()
    assert '(subpath "/data")' in profile
    # Non-executable mount forbids running binaries inside it.
    assert "(deny process-exec*" in profile


def test_sanitize_rejects_injection_and_newlines():
    sb = MacOSSandbox(default_sandbox_config("/tmp/ow"))
    # A double-quote is backslash-escaped (not removed) so it cannot close the
    # Seatbelt string literal and inject a `)` to terminate the subpath expr.
    safe = sb._sanitize_seatbelt_path('/tmp/x "foo"')
    assert '\\"' in safe
    assert '"' not in safe.replace('\\"', "")  # no raw, unescaped quote remains
    # Newlines are rejected outright (invalid path + grammar break).
    with pytest.raises(ValueError):
        sb._sanitize_seatbelt_path("/tmp/x\nfoo")


def test_argv_prefix_wraps_command():
    sb = MacOSSandbox(default_sandbox_config("/tmp/ow"))
    prefix = sb.argv_prefix()
    assert prefix[0] == "sandbox-exec"
    assert prefix[1] == "-p"
    assert "(deny default)" in prefix[2]


def test_build_sandbox_contract():
    # Fail-open: either a backend or None, never raises.
    result = build_sandbox(default_sandbox_config("/tmp/ow"))
    assert result is None or isinstance(result, MacOSSandbox)


def test_execution_result_dataclass():
    r = ExecutionResult(exit_code=0, stdout="ok", stderr="")
    assert r.exit_code == 0
    assert r.stdout == "ok"
    assert r.timed_out is False
    assert r.sandbox_violation is None


@pytest.mark.skipif(
    not (os.uname().sysname == "Darwin" and shutil.which("sandbox-exec")),
    reason="requires macOS with sandbox-exec to exercise build_sandbox backend",
)
def test_build_sandbox_returns_backend_on_macos():
    assert isinstance(build_sandbox(default_sandbox_config("/tmp/ow")), MacOSSandbox)
