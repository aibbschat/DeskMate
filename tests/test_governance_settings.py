"""Governance settings — persistence, Settings exposure, and hot-apply.

Covers the Settings-panel path for the sandbox + governance work:
  * The manager persists execution level + sandbox flags in prefs and exposes
    them via /v1/settings.
  * set_governance_settings hot-applies the execution level to every running
    engine (mirroring the WS set_mode handler) and rebuilds the tool guard.
  * build_engine honours UI overrides (execution_level flows into the
    PermissionEngine; sandbox_enabled=False yields an unsandboxed executor).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coworker.agent import build_engine
from coworker.agent import code_agent
from coworker.governance import RuleGuardian
from coworker.permissions import EXECUTION_LEVELS
from coworker.server.app import create_app
from coworker.server.manager import SessionManager


# -- build_engine overrides (no manager / HTTP needed) -----------------------
def test_build_engine_execution_level_override(tmp_path):
    strict = build_engine(agent=code_agent(), workspace=tmp_path, execution_level="strict")
    assert strict.permissions.execution_level == "strict"
    assert strict.permissions.tool_guard is not None

    off = build_engine(agent=code_agent(), workspace=tmp_path, execution_level="off")
    assert off.permissions.execution_level == "off"
    assert off.permissions.tool_guard is None


def test_build_engine_sandbox_override(tmp_path):
    # Disabling the sandbox yields an unsandboxed executor regardless of platform.
    eng = build_engine(agent=code_agent(), workspace=tmp_path, sandbox_enabled=False)
    assert eng.executor._sandbox is None

    # Enabled (default) delegates to build_sandbox: None only when unsupported.
    enabled = build_engine(agent=code_agent(), workspace=tmp_path, sandbox_enabled=True)
    if enabled.executor._sandbox is not None:
        # macOS w/ sandbox-exec: the profile must deny inbound network.
        assert "(deny network-inbound)" in enabled.executor._sandbox.compile_profile()


def test_build_engine_bad_execution_level_ignored(tmp_path):
    # An invalid level falls back to the Config default ("auto") rather than crashing.
    eng = build_engine(agent=code_agent(), workspace=tmp_path, execution_level="bogus")
    assert eng.permissions.execution_level in EXECUTION_LEVELS
    assert eng.permissions.tool_guard is not None  # "auto" keeps the guard on


# -- Manager: persistence + Settings exposure + hot-apply -------------------
def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    manager = SessionManager(data_dir=tmp_path / "data")
    return manager, TestClient(create_app(manager))


def test_settings_exposes_governance_defaults(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    s = client.get("/v1/settings").json()
    assert s["execution_level"] == "auto"
    assert s["sandbox_enabled"] is True
    assert s["sandbox_network"] is True
    assert s["sandbox_deny_paths"] == []


def test_governance_rest_roundtrip(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/v1/settings/governance",
        json={"execution_level": "strict", "sandbox_enabled": False, "sandbox_network": False},
    ).json()
    assert resp["ok"] is True
    assert resp["execution_level"] == "strict"
    assert resp["sandbox_enabled"] is False

    s = client.get("/v1/settings").json()
    assert s["execution_level"] == "strict"
    assert s["sandbox_enabled"] is False
    assert s["sandbox_network"] is False


def test_governance_rejects_bad_level(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)
    resp = client.post("/v1/settings/governance", json={"execution_level": "nope"}).json()
    assert resp["ok"] is False and "error" in resp


class _FakePerm:
    def __init__(self):
        self.execution_level = "auto"
        self.tool_guard = RuleGuardian()


class _FakeEngine:
    def __init__(self):
        self.permissions = _FakePerm()


def test_manager_hot_applies_execution_level(tmp_path, monkeypatch):
    manager, _ = _client(tmp_path, monkeypatch)
    fake = _FakeEngine()
    manager._engines = {"sid": fake}

    manager.set_governance_settings(execution_level="strict")
    assert fake.permissions.execution_level == "strict"
    assert fake.permissions.tool_guard is not None

    manager.set_governance_settings(execution_level="off")
    assert fake.permissions.execution_level == "off"
    assert fake.permissions.tool_guard is None


def test_manager_persists_governance(tmp_path, monkeypatch):
    manager, _ = _client(tmp_path, monkeypatch)
    out = manager.set_governance_settings(execution_level="strict", sandbox_network=False)
    assert out["ok"] is True
    assert manager.governance_settings()["execution_level"] == "strict"
    assert manager.governance_settings()["sandbox_network"] is False
