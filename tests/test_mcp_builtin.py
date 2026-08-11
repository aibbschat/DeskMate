"""Tests for the builtin MCP server bootstrap (preinstalled plugins).

Covers: first-seed, idempotency, non-destructive (user same-name), path-drift
repair (preserving user fields), and legacy hand-written entry migration.
"""

from __future__ import annotations

import sys

import pytest

from coworker.mcp import ensure_builtin_servers, read_global
from coworker.mcp.builtin import BUILTIN_SERVERS


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_first_seed_writes_all_builtins(state):
    touched = ensure_builtin_servers()
    assert set(touched) == set(BUILTIN_SERVERS)
    raw = read_global()
    for name, cfg in BUILTIN_SERVERS.items():
        entry = raw[name]
        assert entry["command"] == sys.executable
        assert entry["args"] == ["-m", cfg["module"]]
        assert entry.get("requires_approval") == bool(cfg["requires_approval"])
        assert entry["_builtin"] is True
        # cwd points at the package root (derivable, not hard-coded).
        assert entry["cwd"]


def test_idempotent_second_call_is_noop(state):
    ensure_builtin_servers()
    touched = ensure_builtin_servers()
    assert touched == []  # nothing drifted → nothing rewritten


def test_user_same_name_server_not_clobbered(state):
    # User added a custom server reusing a builtin name with a different module.
    from coworker.mcp.config import put_global_server

    put_global_server("novel-memory", {"command": "echo", "args": ["-m", "my.custom.server"]})
    touched = ensure_builtin_servers()
    assert "novel-memory" not in touched
    raw = read_global()
    assert raw["novel-memory"]["args"] == ["-m", "my.custom.server"]


def test_path_drift_repaired_preserving_user_fields(state):
    from coworker.mcp.config import put_global_server

    # Seed all builtins drifted (stale venv path), plus a user field on the
    # target that the repair must NOT clobber.
    for name, cfg in BUILTIN_SERVERS.items():
        entry = {
            "command": "/stale/venv/bin/python",
            "args": ["-m", cfg["module"]],
            "cwd": "/stale/workspace",
        }
        if name == "novel-memory":
            entry["env"] = {"MY_KEY": "keep-me"}
            entry["requires_approval"] = False
        put_global_server(name, entry)

    # First ensure repairs every drifted builtin.
    touched = ensure_builtin_servers()
    assert set(touched) == set(BUILTIN_SERVERS)

    # Second ensure is a no-op (path now matches).
    assert ensure_builtin_servers() == []

    entry = read_global()["novel-memory"]
    # Path fields repaired to current interpreter/root.
    assert entry["command"] == sys.executable
    assert entry["cwd"]
    # User field preserved (not clobbered by the repair).
    assert entry["env"] == {"MY_KEY": "keep-me"}


def test_legacy_real_mcpjson_migrates(state):
    """The current ~/.config/coworker/mcp.json has no `_builtin` marker and a
    hard-coded venv path. A fresh boot must repair it in place (keep enabled /
    requires_approval) and stamp the marker — without adding or dropping servers."""
    from coworker.mcp.config import put_global_server

    for name, cfg in BUILTIN_SERVERS.items():
        put_global_server(
            name,
            {
                "command": "/Users/xiaocheng/workspace/openworker/.venv/bin/python",
                "args": ["-m", cfg["module"]],
                "cwd": "/Users/xiaocheng/workspace/openworker",
                "enabled": True,
                "requires_approval": False,
            },
        )
    touched = ensure_builtin_servers()
    assert set(touched) == set(BUILTIN_SERVERS)
    raw = read_global()
    assert set(raw) == set(BUILTIN_SERVERS)  # no add/remove
    for name in BUILTIN_SERVERS:
        assert raw[name]["_builtin"] is True
        assert raw[name]["command"] == sys.executable
        assert raw[name]["enabled"] is True and raw[name]["requires_approval"] is False
