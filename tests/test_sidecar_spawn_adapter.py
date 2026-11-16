"""sidecar 划词适配器拉起——已退役（2026-11-16）的回归测试。

验证：无论 ``DESKMATE_SELECTION_DISABLE`` 与否，``_spawn_selection_adapter``
都不再 Popen 任何子进程（划词功能下线，萧哥 2026-11-16 拍板：外部划词客户端
OpenAI Translator 等所依赖的本地端点默认路由早已是死路——OmniRoute 2026-10-24
已删——实际无人可用）。

服务本体 ``deskmate/selection/`` 与其接口测试 ``tests/test_deskmate_selection.py``
保留供考古/复活，勿删。
"""
from __future__ import annotations

import subprocess

import coworker.server.run as run


def test_retired_never_spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("DESKMATE_SELECTION_DISABLE", raising=False)
    run._spawn_selection_adapter(8123)
    assert calls == [], "划词适配器已退役：任何情况下都不应再拉起子进程"


def test_retired_ignores_disable_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("DESKMATE_SELECTION_DISABLE", "1")
    run._spawn_selection_adapter(8123)
    assert calls == []
