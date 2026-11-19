"""工具 schema 预算必须保留查证/读稿核心工具（2026-11-19 回归）。

原 PR5 按注册顺序裁剪，把排在 100+ MCP 工具之后的 web_search/search_history
裁掉，导致模型以为"没挂工具"、转而凭记忆回答——违反"史料库过了才算数"红线。
这组用例锁死：pinned 工具无条件保留、顺序不变、其余仍受预算约束。
"""
from __future__ import annotations

import json

from coworker.engine import (
    _PINNED_TOOLS, _TOOL_SCHEMA_DEFAULT_CHARS,
    _is_pinned_tool, _select_tool_schemas, _tool_name,
)


def _schema(name: str, desc: str = "desc") -> dict:
    return {"function": {"name": name, "description": desc, "parameters": {"type": "object"}}}


def test_core_lookup_tools_survive_budget(monkeypatch):
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "20000")
    schemas = [_schema(f"mcp__noise__tool_{i}", "x" * 300) for i in range(140)]
    schemas += [_schema(n) for n in (
        "web_search", "search_history", "web_fetch",
        "history_library_check", "search_private", "read_file", "load_skill",
    )]
    kept = _select_tool_schemas(schemas)
    names = [_tool_name(s) for s in kept]
    for must in ("web_search", "search_history", "web_fetch",
                 "history_library_check", "search_private", "read_file", "load_skill"):
        assert must in names, f"{must} 被预算裁掉了（查证纪律被破坏）"


def test_order_preserved(monkeypatch):
    """保留列表必须是原列表的子序列（协议/缓存对顺序敏感）。"""
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "3000")
    schemas = [_schema(f"tool_{i}", "y" * 200) for i in range(60)]
    schemas += [_schema("search_history")]
    kept_names = [_tool_name(s) for s in _select_tool_schemas(schemas)]
    assert "search_history" in kept_names
    # 子序列校验
    it = iter([_tool_name(s) for s in schemas])
    assert all(any(n == k for k in it) for n in kept_names)


def test_budget_still_caps_non_pinned(monkeypatch):
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "2000")
    schemas = [_schema(f"noise_{i}", "z" * 300) for i in range(40)] + [_schema("web_search")]
    kept = _select_tool_schemas(schemas)
    assert len(kept) < len(schemas)  # 非核心工具确实被裁
    assert "web_search" in [_tool_name(s) for s in kept]


def test_zero_budget_disables_caps(monkeypatch):
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "0")
    schemas = [_schema(f"tool_{i}", "a" * 500) for i in range(50)]
    assert len(_select_tool_schemas(schemas)) == len(schemas)


def test_mcp_prefixed_core_tools_survive(monkeypatch):
    """MCP 工具名带 mcp__<server>__ 前缀，裸名白名单匹配不到—— Regression。"""
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "20000")
    schemas = [_schema(f"mcp__ai-circle__n_{i}", "x" * 300) for i in range(140)]
    core = [
        "mcp__perception__read_file", "mcp__perception__read_paragraphs",
        "mcp__deskmate-memory__memory_search", "mcp__deskmate-skills__skill_view",
        "search_history", "web_search",
    ]
    schemas += [_schema(n) for n in core]
    kept_names = [_tool_name(s) for s in _select_tool_schemas(schemas)]
    for n in core:
        assert n in kept_names, f"MCP 核心工具 {n} 被误裁"


def test_pinned_set_contains_lookups():
    for n in ("web_search", "search_history", "read_paragraphs", "load_skill"):
        assert n in _PINNED_TOOLS


# ---- 2026-11-19 二次修复：整组 MCP server pin 撑爆预算的回归 ----

def test_basic_file_tools_never_dropped(monkeypatch):
    """写文件/执行基础工具即使排在 100+ 工具之后也必须保住。

    回归：整组 pin 3 个 MCP server 后 pinned 自身 29827 字符 > 20000 预算，
    导致所有非 pinned（含 write_file/run_shell/apply_patch）100% 被裁 —— 模型
    看不到写文件工具，功能直接废掉。这些是 agent 干活的地基，必须 pin。
    """
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "20000")
    schemas = [_schema(f"mcp__noise__tool_{i}", "x" * 300) for i in range(140)]
    basics = (
        "write_file", "apply_patch", "apply_unified_diff", "replace_in_file",
        "run_shell", "shell_task_output", "shell_task_kill", "todo_write",
    )
    schemas += [_schema(n) for n in basics]
    kept = _select_tool_schemas(schemas)
    names = [_tool_name(s) for s in kept]
    for n in basics:
        assert n in names, f"基础文件/执行工具 {n} 被裁（agent 将无法正常干活）"


def test_mcp_server_not_wholesale_pinned():
    """MCP 工具不能按 server 整组 pin —— 只按末段裸名 pin 核心工具。

    整组 pin（perception/deskmate-memory/deskmate-skills）一次塞进 42 个工具、
    21391 字符，是撑爆预算的直接原因。改为末段裸名匹配后：核心（read_file/
    search_history 等）仍保住，非核心（get_active_window/vault_write 等）按预算。
    """
    # 核心 MCP 工具：末段裸名在白名单 -> 必须 pin
    assert _is_pinned_tool("mcp__perception__read_file")
    assert _is_pinned_tool("mcp__deskmate-memory__memory_search")
    assert _is_pinned_tool("mcp__any_server__search_history")
    # 非核心 MCP 工具：整组 server 不再无条件 pin
    assert not _is_pinned_tool("mcp__perception__get_active_window")
    assert not _is_pinned_tool("mcp__deskmate-memory__vault_write")
    assert not _is_pinned_tool("mcp__deskmate-skills__skill_create")


def test_non_pinned_tools_survive_when_budget_allows(monkeypatch):
    """pinned 收窄后必须给非核心工具留出预算空间，不能再次全灭。"""
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "20000")
    # 一个 pin 核心 + 若干普通工具，普通工具应能装下
    schemas = [_schema("read_file", "x" * 400)]
    schemas += [_schema(f"normal_{i}", "y" * 100) for i in range(20)]
    kept = _select_tool_schemas(schemas)
    names = [_tool_name(s) for s in kept]
    assert "read_file" in names
    assert sum(1 for n in names if n.startswith("normal_")) >= 10, "非核心工具被过度裁掉"


def test_default_budget_large_enough_for_pinned_plus_common(monkeypatch):
    """默认预算必须能容纳 pinned + 一批常用工具（原 20000 太小）。"""
    assert _TOOL_SCHEMA_DEFAULT_CHARS >= 40000
