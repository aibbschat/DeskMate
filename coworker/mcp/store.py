"""MCP Skill Store — one-click install, post-config.

The skill catalog is a curated list of MCP servers bundled with the software.
Each entry describes how to install (stdio command or HTTP URL) and what
configuration (API key) is needed after install.

Installation is lightweight: stdio servers are launched via ``npx -y`` (npm
downloads the package on first run, cached thereafter). HTTP servers just
record the URL. No heavy binaries are bundled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import MCPServerDef, global_mcp_path, load_mcp_servers
from ..secrets import state_dir, write_private_text


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class SkillConfig:
    """Post-install configuration needed by a skill."""
    type: str  # "api_key" | "oauth" | "none"
    key_name: str = ""
    key_hint: str = ""
    env_key: str = ""


@dataclass
class SkillInstall:
    """How to install a skill."""
    type: str  # "stdio" | "http" | "sse"
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    url: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SkillEntry:
    """A single skill in the catalog."""
    id: str
    name: str
    description: str
    category: str  # search | document | database | development | productivity | other
    icon: str = ""
    version: str = "1.0.0"
    install: SkillInstall = field(default_factory=lambda: SkillInstall(type="none"))
    config: SkillConfig = field(default_factory=lambda: SkillConfig(type="none"))
    tags: list[str] = field(default_factory=list)
    author: str = ""
    homepage: str = ""


# ---------------------------------------------------------------------------
# Builtin catalog
# ---------------------------------------------------------------------------

def _clean_description(raw: str) -> str:
    """Strip the YAML frontmatter noise that upstream community catalogs embed
    in the ``description`` field, e.g.::

        name: timeline-report
        description: >
          Generate a chronological report...

    or, on a single line (some upstream dumps inline the whole frontmatter)::

        name: rtl-chat description: | Multi-language RTL formatting skill...

    The frontend cards render the description verbatim, so this turns the raw
    dump into a single clean paragraph without losing upstream content.
    """
    if not raw:
        return ""
    # Upstream community catalogs sometimes embed raw YAML frontmatter in the
    # description field — potentially several ``name:`` / ``description:`` lines
    # (some entries even repeat ``name:``). Walk line by line, drop every
    # ``name:`` noise line and strip the ``description:`` prefix (with its
    # optional ``>`` / ``|`` block-scalar marker), then flatten to one paragraph.
    out = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if s.startswith("name:"):
            continue
        if s.startswith("description:"):
            rest = re.sub(r"^description:\s*(>|\|)?\s*", "", s)
            if rest:
                out.append(rest)
            continue
        out.append(ln)
    text = " ".join(x.strip() for x in out).strip()
    # Unwrap a single surrounding double-quoted string.
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    # Collapse YAML block-scalar newlines + repeated whitespace into spaces.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _builtin_skills() -> list[SkillEntry]:
    """Curated skills bundled with the software."""
    # Read from the builtin catalog JSON (contains merged MSApps open-source skills)
    import json
    from pathlib import Path
    catalog_path = Path(__file__).parent / "../skills/catalog.json"
    with open(catalog_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Base skills from Anthropic
    base = [
        SkillEntry(
            id="brave-search",
            name="Brave 搜索",
            description="联网搜索，获取实时信息",
            category="search",
            icon="🔍",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-brave-search"],
            ),
            config=SkillConfig(
                type="api_key",
                key_name="Brave Search API Key",
                key_hint="从 brave.com/search/api 免费申请",
                env_key="BRAVE_API_KEY",
            ),
            tags=["搜索", "联网"],
        ),
        SkillEntry(
            id="filesystem",
            name="文件系统",
            description="读写本地文件，支持目录浏览",
            category="development",
            icon="📁",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-filesystem", "${workspace}"],
            ),
            config=SkillConfig(type="none"),
            tags=["文件", "本地"],
        ),
        SkillEntry(
            id="sqlite",
            name="SQLite",
            description="查询本地 SQLite 数据库",
            category="database",
            icon="🗄️",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-sqlite"],
            ),
            config=SkillConfig(type="none"),
            tags=["数据库", "SQL"],
        ),
        SkillEntry(
            id="github",
            name="GitHub",
            description="GitHub 操作：仓库、Issue、PR",
            category="development",
            icon="🐙",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-github"],
            ),
            config=SkillConfig(
                type="api_key",
                key_name="GitHub Personal Access Token",
                key_hint="从 github.com/settings/tokens 创建",
                env_key="GITHUB_PERSONAL_ACCESS_TOKEN",
            ),
            tags=["代码", "版本控制"],
        ),
        SkillEntry(
            id="fetch",
            name="网页抓取",
            description="抓取网页内容，转为 Markdown",
            category="document",
            icon="🌐",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-fetch"],
            ),
            config=SkillConfig(type="none"),
            tags=["网页", "爬虫"],
        ),
        SkillEntry(
            id="gaode-maps",
            name="高德地图",
            description="地图搜索、路线规划、POI 查询",
            category="other",
            icon="🗺️",
            version="1.0.0",
            install=SkillInstall(
                type="stdio",
                command="npx",
                args=["-y", "@anthropic/mcp-server-gaode"],
            ),
            config=SkillConfig(
                type="api_key",
                key_name="高德开放平台 API Key",
                key_hint="从 lbs.amap.com 免费申请",
                env_key="AMAP_API_KEY",
            ),
            tags=["地图", "导航"],
        ),
    ]
    # Merge with MSApps open-source community skills
    for source in data["sources"]:
        for item in source["skills"]:
            # Convert catalog entry from JSON to SkillEntry dataclass.
            # The catalog stores post-install config under a nested "config"
            # object — read that, never the (non-existent) top-level
            # config_type / config_env_key fields.
            cfg = item.get("config") or {}
            se = SkillConfig(
                type=cfg.get("type") or "none",
                key_name=cfg.get("key_name") or "",
                key_hint=cfg.get("key_hint") or "",
                env_key=cfg.get("env_key") or "",
            )
            si = SkillInstall(
                type=item["install"]["type"],
                command=item["install"].get("command"),
                args=item["install"].get("args", []),
                url=item["install"].get("url"),
                env=item["install"].get("env", {}),
                headers=item["install"].get("headers", {}),
            )
            base.append(SkillEntry(
                id=item["id"],
                name=item["name"],
                description=_clean_description(item["description"]),
                category=item["category"],
                icon=item.get("icon") or "🔧",
                version=item.get("version") or "1.0.0",
                install=si,
                config=se,
                tags=item.get("tags") or [],
                author=item.get("author"),
                homepage=item.get("homepage"),
            ))
    return base


# ---------------------------------------------------------------------------
# Store API
# ---------------------------------------------------------------------------

_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


def _resolve_workspace_tokens(args: list[str]) -> list[str]:
    """Expand the `${workspace}` placeholder used by the filesystem skill to a
    concrete, valid directory (the user's home). The MCP filesystem server
    would otherwise treat the literal string as a (non-existent) path.
    """
    home = str(Path.home())
    return [a.replace("${workspace}", home) for a in args]


def _set_env_secret(env_key: str, value: str) -> None:
    """Persist a secret into the local `.env` (SecretStore dotenv) as KEY=VALUE.

    Secrets live in `.env`, never in mcp.json — mcp.json stores a `${KEY}`
    reference that ``secrets.resolve`` expands at load time. This matches the
    rest of the MCP connector stack.
    """
    env_path = state_dir() / ".env"
    existing: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()
    existing[env_key] = value
    content = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    write_private_text(env_path, content)


def _skill_to_mcp_config(skill: SkillEntry, config_values: dict[str, str] | None = None) -> dict[str, Any]:
    """Convert a SkillEntry to the mcpServers JSON format."""
    cfg: dict[str, Any] = {"enabled": True}

    if skill.install.type == "stdio":
        cfg["command"] = skill.install.command
        cfg["args"] = _resolve_workspace_tokens(skill.install.args)
        if skill.install.env:
            cfg["env"] = dict(skill.install.env)
    elif skill.install.type in ("http", "sse"):
        cfg["url"] = skill.install.url
        if skill.install.headers:
            cfg["headers"] = dict(skill.install.headers)

    # Merge user-provided config values (API keys) into env — but as `${VAR}`
    # references, with the real value parked in `.env`.
    if config_values and skill.config.env_key:
        env_key = skill.config.env_key
        _set_env_secret(env_key, config_values[env_key])
        cfg.setdefault("env", {})[env_key] = f"${{{env_key}}}"

    return cfg


def install_skill(skill: SkillEntry, config_values: dict[str, str] | None = None) -> None:
    """Install a skill by writing its config to mcp.json."""
    mcp_path = global_mcp_path()
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

    servers = data.setdefault("mcpServers", {})
    servers[skill.id] = _skill_to_mcp_config(skill, config_values)

    mcp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def uninstall_skill(skill_id: str) -> None:
    """Remove a skill from mcp.json."""
    mcp_path = global_mcp_path()
    if not mcp_path.exists():
        return

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    servers = data.get("mcpServers", {})
    servers.pop(skill_id, None)

    mcp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def configure_skill(skill_id: str, config_values: dict[str, str]) -> None:
    """Update configuration values (API keys) for an installed skill.

    Each ``config_values`` entry is keyed by the real env var name (e.g.
    ``BRAVE_API_KEY``). The plaintext value is parked in ``.env`` and a
    ``${VAR}`` reference is written into mcp.json, so secrets never live in
    the (synced) mcp.json file.
    """
    mcp_path = global_mcp_path()
    if not mcp_path.exists():
        return

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    servers = data.get("mcpServers", {})
    server = servers.get(skill_id)
    if server is None:
        return

    env = server.setdefault("env", {})
    for env_key, value in config_values.items():
        if not env_key:
            continue
        _set_env_secret(env_key, value)
        env[env_key] = f"${{{env_key}}}"

    mcp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_installed() -> dict[str, dict[str, Any]]:
    """Return installed skills with their status."""
    mcp_path = global_mcp_path()
    if not mcp_path.exists():
        return {}

    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    return data.get("mcpServers", {})


def get_skill_status(skill_id: str) -> str:
    """Check if a skill is installed and configured.

    Returns: "not_installed" | "installed" | "configured"
    """
    servers = list_installed()
    server = servers.get(skill_id)
    if server is None:
        return "not_installed"

    # Find the skill in catalog to check if config is needed
    catalog = {s.id: s for s in _builtin_skills()}
    skill = catalog.get(skill_id)
    if skill is None:
        return "installed"

    if skill.config.type == "none":
        return "configured"

    # Check if env key has a value
    env = server.get("env", {})
    if skill.config.env_key and env.get(skill.config.env_key):
        return "configured"

    return "installed"


def get_builtin_skills() -> list[SkillEntry]:
    """Return the builtin skill catalog."""
    return _builtin_skills()
