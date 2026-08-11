"""Tests for MCPManager hot-swap (build-before-swap) — roundtable/agent hot-plug.

Verifies the core guarantee: a failed new connection does NOT drop the live one,
and a healthy new connection atomically replaces the old without taking the whole
tool surface offline.
"""
import asyncio
from types import SimpleNamespace

import pytest

from coworker.mcp.client import MCPManager
from coworker.mcp.config import MCPServerDef


class _FakeSession:
    def __init__(self, tools):
        self._tools = tools

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)


class _Transport:
    """Async context manager standing in for stdio/streamable transports."""

    def __init__(self, session, fail=False):
        self.session = session
        self.fail = fail

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("boom: transport failed")
        return ("read", "write")

    async def __aexit__(self, *exc):
        return False


class _SessionCtx:
    """Async context manager wrapping the fake ClientSession."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _tool(name):
    return SimpleNamespace(name=name, description=f"tool {name}")


def _session_for(tool_names):
    return _FakeSession([_tool(t) for t in tool_names])


def _patch_transport(monkeypatch, *, connect_ok=True, tool_names=("alpha", "beta")):
    session = _session_for(tool_names)
    transport = _Transport(session, fail=not connect_ok)

    def fake_stdio(params):
        return transport

    def fake_http(url, **kw):
        return transport

    monkeypatch.setattr("coworker.mcp.client.stdio_client", fake_stdio)
    monkeypatch.setattr("coworker.mcp.client.streamablehttp_client", fake_http)
    monkeypatch.setattr("coworker.mcp.client.ClientSession", lambda r, w: _SessionCtx(session))


def _server(name, command="echo", args=None):
    return MCPServerDef(
        name=name, transport="stdio", command=command, args=args or [], enabled=True
    )


@pytest.mark.asyncio
async def test_ensure_then_reload_swaps_live_connection(monkeypatch):
    _patch_transport(monkeypatch, connect_ok=True, tool_names=("old_a", "old_b"))
    mgr = MCPManager()
    first = await mgr.ensure(_server("s1"))
    assert {t.name for t in first.tools} == {"old_a", "old_b"}

    # Reload with a NEW tool set; build-before-swap should replace in place.
    _patch_transport(monkeypatch, connect_ok=True, tool_names=("new_a",))
    swapped = await mgr.reload_server(_server("s1"))
    assert {t.name for t in swapped.tools} == {"new_a"}
    # The slot now holds the new connection.
    assert {t.name for t in (await mgr.tools(_server("s1")))} == {"new_a"}
    await mgr.aclose()


@pytest.mark.asyncio
async def test_reload_failure_keeps_old_connection(monkeypatch):
    _patch_transport(monkeypatch, connect_ok=True, tool_names=("live_a",))
    mgr = MCPManager()
    old = await mgr.ensure(_server("s1"))
    assert {t.name for t in old.tools} == {"live_a"}

    # New config fails to connect → old connection must survive (no dark surface).
    _patch_transport(monkeypatch, connect_ok=False)
    with pytest.raises(Exception):
        await mgr.reload_server(_server("s1"))
    # Old connection still live.
    assert {t.name for t in (await mgr.tools(_server("s1")))} == {"live_a"}
    await mgr.aclose()


@pytest.mark.asyncio
async def test_reload_all_isolates_failures(monkeypatch):
    # Both start healthy.
    _patch_transport(monkeypatch, connect_ok=True, tool_names=("ok",))
    mgr = MCPManager()
    await mgr.ensure(_server("good"))
    await mgr.ensure(_server("also_good"))

    # Per-server failure: "good" (command=="FAIL") fails, "also_good" succeeds.
    good_session = _session_for(("ok",))
    bad_transport = _Transport(good_session, fail=True)
    ok_transport = _Transport(good_session, fail=False)

    def failing_stdio(params):
        return bad_transport if params.command == "FAIL" else ok_transport

    def failing_http(url, **kw):
        return ok_transport

    monkeypatch.setattr("coworker.mcp.client.stdio_client", failing_stdio)
    monkeypatch.setattr("coworker.mcp.client.streamablehttp_client", failing_http)
    monkeypatch.setattr("coworker.mcp.client.ClientSession", lambda r, w: _SessionCtx(good_session))

    good = _server("good", command="FAIL")
    also = _server("also_good", command="ok")
    # reload_all should not raise even though "good" fails; "also_good" stays live.
    await mgr.reload_all([good, also])
    assert {t.name for t in (await mgr.tools(also))} == {"ok"}
    await mgr.aclose()
