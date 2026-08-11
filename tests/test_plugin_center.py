"""Tests for the plugin-center work: catalog mcp-references + plugin packages."""

from __future__ import annotations

import json

import pytest

from coworker.personas.manifest import parse_manifest
from coworker.personas.registry import PersonaRegistry, set_registry


def _catalog():
    # Lazy import: importing coworker.catalog at module top triggers an agent→catalog
    # circular import (same reason manifest.py imports it lazily).
    from coworker.catalog import expand, is_mcp_ref, parse_mcp_ref

    return expand, is_mcp_ref, parse_mcp_ref


# ---------------------------------------------------------------------------
# 改造三A: catalog opens an `mcp:<server>:<tool>` reference branch
# ---------------------------------------------------------------------------


def test_is_mcp_ref_and_parse():
    expand, is_mcp_ref, parse_mcp_ref = _catalog()
    assert is_mcp_ref("mcp:foo:bar")
    assert not is_mcp_ref("files")
    assert parse_mcp_ref("mcp:my-server:search") == ("my-server", "search")
    with pytest.raises(ValueError):
        parse_mcp_ref("mcp:no-tool")
    with pytest.raises(ValueError):
        parse_mcp_ref("not-mcp")


def test_expand_skips_mcp_refs():
    # expand() must not choke on mcp refs and must still expand catalog ids.
    expand, is_mcp_ref, parse_mcp_ref = _catalog()

    class _Ctx:
        workspace = None
        executor = None
        todo = None
        roots = None

    tools = expand(["files", "mcp:external:do_thing", "git"], _Ctx())
    # 'files'/'git' have unmet context prereqs in this fake ctx → skipped;
    # the key assertion is that mcp refs don't raise and produce no callables here.
    assert all("mcp__" not in getattr(t, "__name__", "") for t in tools)


def test_validate_tools_allows_well_formed_mcp_ref():
    # A persona manifest with a valid mcp ref must NOT be rejected.
    md = (
        "---\n"
        "id: researcher\n"
        "name: Researcher\n"
        "tools: [search, mcp:wikipedia:lookup]\n"
        "family: knowledge\n"
        "---\n"
        "You research things.\n"
    )
    m = parse_manifest(md)  # raises ManifestError if validation rejects the ref
    assert "mcp:wikipedia:lookup" in m.tools


def test_validate_tools_rejects_malformed_mcp_ref():
    md = (
        "---\n"
        "id: bad\n"
        "name: Bad\n"
        "tools: [mcp:missing-tool-segment]\n"
        "family: knowledge\n"
        "---\n"
        "x\n"
    )
    with pytest.raises(Exception):
        parse_manifest(md)


# ---------------------------------------------------------------------------
# 改造三B: plugin packages (server + persona bundles)
# ---------------------------------------------------------------------------


@pytest.fixture
def pkg_dir(tmp_path):
    d = tmp_path / "my-plugin"
    d.mkdir()
    (d / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather": {
                        "command": "python",
                        "args": ["-m", "weather_server"],
                        "enabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pdir = d / "personas"
    pdir.mkdir()
    (pdir / "weather-bot.md").write_text(
        "---\n"
        "id: weather-bot\n"
        "name: Weather Bot\n"
        "tools: [mcp:weather:get_forecast]\n"
        "family: knowledge\n"
        "---\n"
        "I tell you the weather.\n",
        encoding="utf-8",
    )
    return d


def test_validate_package_ok(pkg_dir):
    from coworker.mcp import validate_plugin_package

    r = validate_plugin_package(pkg_dir)
    assert r["ok"] is True
    assert r["servers"] == ["weather"]
    assert "weather-bot" in r["personas"]


def test_validate_package_rejects_no_servers_no_personas(tmp_path):
    from coworker.mcp import validate_plugin_package

    d = tmp_path / "empty"
    d.mkdir()
    r = validate_plugin_package(d)
    assert r["ok"] is False


def test_install_package_writes_server_and_persona(pkg_dir, tmp_path, monkeypatch):
    from coworker.mcp import config as mcp_config
    from coworker.mcp import install_plugin_package

    # Isolate the global mcp.json into the temp area.
    monkeypatch.setattr(mcp_config, "global_mcp_path", lambda: tmp_path / "mcp.json")
    # Isolate + inject a temp persona registry.
    reg = PersonaRegistry(state_path=tmp_path / "personas.json")
    set_registry(reg)

    r = install_plugin_package(pkg_dir)
    assert r["ok"] is True
    assert r["servers"] == ["weather"]
    assert "weather-bot" in r["personas"]

    # Server actually written to the (temp) global file.
    servers = mcp_config.read_global()
    assert "weather" in servers
    # Persona registered, but disabled pending consent (governance).
    entry = reg.get("weather-bot")
    assert entry is not None
    assert reg.is_enabled("weather-bot") is False


def test_install_package_rolls_back_on_bad_persona(tmp_path, monkeypatch):
    from coworker.mcp import config as mcp_config
    from coworker.mcp import install_plugin_package

    monkeypatch.setattr(mcp_config, "global_mcp_path", lambda: tmp_path / "mcp.json")
    reg = PersonaRegistry(state_path=tmp_path / "personas.json")
    set_registry(reg)

    # Package with a valid server but a malformed persona manifest.
    d = tmp_path / "bad-plugin"
    d.mkdir()
    (d / "mcp.json").write_text(
        json.dumps({"mcpServers": {"svc": {"command": "x"}}}), encoding="utf-8"
    )
    pdir = d / "personas"
    pdir.mkdir()
    (pdir / "broken.md").write_text(
        "---\nid: broken\ntools: [mcp:not-a-ref]\n---\nbody\n", encoding="utf-8"
    )
    r = install_plugin_package(d)
    assert r["ok"] is False
    # Server must be rolled back (not left half-installed).
    assert "svc" not in mcp_config.read_global()


# ---------------------------------------------------------------------------
# prepare_mcp_tools persona tool-pinning (pure-function parts)
# ---------------------------------------------------------------------------


def test_mcp_tool_local_name_decodes():
    from coworker.server.manager import SessionManager

    class _Fn:
        __name__ = "mcp__weather__get_forecast"

    assert SessionManager._mcp_tool_local_name(_Fn()) == "get_forecast"
    # a non-mcp callable name passes through unchanged
    class _Plain:
        __name__ = "read_file"

    assert SessionManager._mcp_tool_local_name(_Plain()) == "read_file"


def test_prepare_mcp_tools_pinning_filters_to_refs():
    """The persona-pinning branch in prepare_mcp_tools should keep only callables
    whose local tool name is in the persona's declared refs for that server."""
    from coworker.server.manager import SessionManager

    kept = SessionManager._mcp_tool_local_name

    class _Fn:
        def __init__(self, local):
            self.__name__ = f"mcp__weather__{local}"

    server = type("S", (), {"name": "weather"})()
    callables = [_Fn("get_forecast"), _Fn("get_alerts"), _Fn("radar")]
    refs = {"weather:get_forecast", "weather:radar"}  # persona borrowed 2 of 3

    # Replicate the exact filter from prepare_mcp_tools (kept in sync on purpose).
    allowed = {r.split(":", 1)[1] for r in refs if r.startswith(server.name + ":")}
    pinned = [fn for fn in callables if kept(fn) in allowed]
    names = [kept(fn) for fn in pinned]
    assert names == ["get_forecast", "radar"]

