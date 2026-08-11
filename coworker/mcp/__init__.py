"""MCP integration — our own async client on the official `mcp` SDK.

Public API: config loading/mutation, the connection manager, and tool wrapping.
"""

from __future__ import annotations

from .client import MCPManager
from .config import (
    MCPServerDef,
    delete_global_server,
    load_mcp_servers,
    patch_global_server,
    put_global_server,
    read_global,
)
from .builtin import BUILTIN_SERVERS, ensure_builtin_servers, render_builtin
from .package import install_plugin_package, validate_plugin_package
from .tools import build_callables, tool_name

__all__ = [
    "MCPManager",
    "MCPServerDef",
    "load_mcp_servers",
    "read_global",
    "put_global_server",
    "patch_global_server",
    "delete_global_server",
    "BUILTIN_SERVERS",
    "ensure_builtin_servers",
    "render_builtin",
    "install_plugin_package",
    "validate_plugin_package",
    "build_callables",
    "tool_name",
]
