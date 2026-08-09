"""sidecar 自动拉起划词适配器的回归测试。

验证：
- ``DESKMATE_SELECTION_DISABLE=1`` 时完全不拉起（不调用 Popen）。
- 正常时调用 Popen，且子进程环境带上 sidecar 的 port/token 与「随父退出」标记。
不依赖真实进程。
"""
from __future__ import annotations

import os
import subprocess

import pytest

import coworker.server.run as run


class _FakePopen:
    def __init__(self, cmd, env=None, **kw):
        self.cmd = cmd
        self.env = dict(env or {})


def test_disable_flag_skips_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or _FakePopen("x"))
    monkeypatch.setenv("DESKMATE_SELECTION_DISABLE", "1")
    run._spawn_selection_adapter(8123)
    assert calls == [], "disabled 时不应拉起适配器"


def test_spawn_passes_port_token_and_orphan_flag(monkeypatch):
    captured = {}

    def fake_popen(cmd, env=None, **kw):
        captured["cmd"] = cmd
        captured["env"] = dict(env or {})
        return _FakePopen(cmd, env)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.delenv("DESKMATE_SELECTION_DISABLE", raising=False)
    monkeypatch.setenv("COWORKER_API_TOKEN", "secret-token")
    monkeypatch.setenv("COWORKER_PORT", "8123")

    run._spawn_selection_adapter(8123)

    assert captured, "enabled 时应调用 Popen"
    assert captured["cmd"][:3] == [
        os.sys.executable,
        "-m",
        "deskmate.selection",
    ], captured["cmd"]
    env = captured["env"]
    assert env["COWORKER_PORT"] == "8123"
    assert env["COWORKER_API_TOKEN"] == "secret-token"
    assert env["COWORKER_EXIT_WITH_PARENT"] == "1"
    assert env["COWORKER_PARENT_PID"] == str(os.getpid())
