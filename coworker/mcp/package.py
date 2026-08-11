"""Plugin packages — install a self-contained bundle of MCP servers + personas.

A *plugin package* is the unit that lets a third party ship "a server **and** the
persona that knows how to use it" as one installable artifact — closing the loop the
older `catalog.py` (closed, platform-owned) could never: a persona can now declare
``tools: [mcp:<server>:<tool>, …]`` and the matching server lands alongside it.

Package shapes (either is accepted):
  • a ``.json``/``.yaml`` dict:
        {"name": str, "servers": [{name, config}], "personas": [<manifest text> | {meta, body}]}
  • a directory:
        mcp.json            — {"mcpServers": {name: config}}
        personas/*.md       — one manifest per file

Install is **atomic-ish**: all servers are written in a single global-file rewrite
(so a failure mid-way doesn't leave a half-installed server set), and personas go
through the existing registry install path (snapshot + disabled-pending-consent, same
governance as any third-party persona). On any error after servers were written, the
previously-present servers are restored.

This deliberately does NOT auto-enable anything — servers are added `enabled` (they're
just config; no process runs until a session connects) but personas land disabled
pending the user's consent, exactly like `PersonaRegistry.install_from_dir`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .config import read_global


def _load_raw(path: str | Path) -> dict[str, Any]:
    """Load a package file/dir into a normalized {name, servers, personas} dict."""
    p = Path(path)
    if p.is_dir():
        return _load_dir(p)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    return _normalize(data)


def _load_dir(d: Path) -> dict[str, Any]:
    mcp_file = d / "mcp.json"
    servers: dict[str, Any] = {}
    if mcp_file.is_file():
        servers = json.loads(mcp_file.read_text(encoding="utf-8")).get("mcpServers") or {}
    personas: list[Any] = []
    pdir = d / "personas"
    if pdir.is_dir():
        for md in sorted(pdir.glob("*.md")):
            personas.append(md.read_text(encoding="utf-8"))
    return {"name": d.name, "servers": list(servers.items()), "personas": personas}


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Accept either {servers:[{name,config}], …} or {mcpServers:{name:config}}.
    `personas` accepts either manifest *text* or {meta, body} dicts."""
    raw_servers = data.get("servers")
    if raw_servers is None and "mcpServers" in data:
        raw_servers = [{"name": n, **c} if "name" not in c else c for n, c in data["mcpServers"].items()]
    servers: list[tuple[str, dict]] = []
    for s in raw_servers or []:
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError("each server needs a `name`")
        # `config` may be nested or inlined at top level
        cfg = dict(s.get("config") or {k: v for k, v in s.items() if k != "name"})
        servers.append((name, cfg))
    personas: list[str] = []
    for p in data.get("personas", []) or []:
        if isinstance(p, str):
            personas.append(p)
        elif isinstance(p, dict):
            personas.append(_render_manifest(p))
        else:
            raise ValueError("each persona must be manifest text or {meta, body}")
    return {"name": str(data.get("name") or "plugin"), "servers": servers, "personas": personas}


def _render_manifest(p: dict[str, Any]) -> str:
    """Render a {meta:dict, body:str} persona into a frontmatter manifest string."""
    meta = p.get("meta", {})
    body = str(p.get("body", "")).strip()
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body + "\n"


def validate_plugin_package(path: str | Path) -> dict[str, Any]:
    """Dry-run: parse + structurally validate without writing. Returns a summary
    (servers/personas that *would* be installed, plus any errors)."""
    try:
        pkg = _load_raw(path)
    except Exception as exc:
        return {"ok": False, "error": f"cannot read package: {exc}"}
    errors: list[str] = []
    server_names = [n for n, _ in pkg["servers"]]
    if not server_names and not pkg["personas"]:
        errors.append("package has neither servers nor personas")
    seen = set()
    for name, cfg in pkg["servers"]:
        if name in seen:
            errors.append(f"duplicate server name: {name}")
        seen.add(name)
        if not (cfg.get("command") or cfg.get("url")):
            errors.append(f"server {name!r} needs a `command` or `url`")
    # Validate each persona manifest text parses (reuse the manifest parser's strictness).
    from ..personas.manifest import parse_manifest

    persona_ids: list[str] = []
    for i, text in enumerate(pkg["personas"]):
        try:
            m = parse_manifest(text)
            persona_ids.append(m.id)
        except Exception as exc:
            errors.append(f"persona #{i + 1} invalid: {exc}")
    return {
        "ok": not errors,
        "name": pkg["name"],
        "servers": server_names,
        "personas": persona_ids,
        "errors": errors,
    }


def install_plugin_package(path: str | Path) -> dict[str, Any]:
    """Install a plugin package: write its servers to the global MCP config and
    snapshot its personas (disabled pending consent). Best-effort atomic — rolls the
    server set back if persona install fails after servers were written.

    Returns {ok, name, servers, personas, errors}.
    """
    pkg = _load_raw(path)
    # Pre-validate before touching anything.
    pre = validate_plugin_package(path)
    if not pre["ok"]:
        return {"ok": False, "name": pkg["name"], "servers": [], "personas": [], "errors": pre["errors"]}

    # 1) Write servers in a single global-file rewrite (atomic-ish, easy rollback).
    prior = read_global()
    try:
        servers = read_global()
        for name, cfg in pkg["servers"]:
            servers[name] = cfg
        _write_global_servers(servers)
    except Exception as exc:
        # restore prior server set
        try:
            _write_global_servers(prior)
        except Exception:
            pass
        return {"ok": False, "name": pkg["name"], "servers": [], "personas": [], "errors": [f"server write failed: {exc}"]}

    # 2) Install personas via the existing registry path (snapshot + disabled).
    installed_personas: list[str] = []
    try:
        if pkg["personas"]:
            with tempfile.TemporaryDirectory() as td:
                pdir = Path(td) / "personas"
                pdir.mkdir()
                for i, text in enumerate(pkg["personas"]):
                    (pdir / f"persona-{i + 1}.md").write_text(text, encoding="utf-8")
                from ..personas.registry import get_registry

                reg = get_registry()
                summaries = reg.install_from_dir(pdir)
                installed_personas = [s.get("id") for s in summaries]
    except Exception as exc:
        # Roll back the servers we just wrote so the system isn't left half-installed.
        try:
            _write_global_servers(prior)
        except Exception:
            pass
        return {"ok": False, "name": pkg["name"], "servers": [], "personas": [], "errors": [f"persona install failed: {exc}"]}

    return {
        "ok": True,
        "name": pkg["name"],
        "servers": [n for n, _ in pkg["servers"]],
        "personas": installed_personas,
        "errors": [],
    }


def _write_global_servers(servers: dict[str, dict[str, Any]]) -> None:
    """Atomic single-file rewrite of the global mcpServers map (mirrors config._write_global)."""
    from .config import global_mcp_path

    path = global_mcp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    tmp.replace(path)
