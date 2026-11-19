"""工具 schema 预算必须保留查证/读稿核心工具（2026-11-19 回归）。

原 PR5 按注册顺序裁剪，把排在 100+ MCP 工具之后的 web_search/search_history
裁掉，导致模型以为"没挂工具"、转而凭记忆回答——违反"史料库过了才算数"红线。
这组用例锁死：pinned 工具无条件保留、顺序不变、其余仍受预算约束。
"""
from __future__ import annotations

import json

from coworker.engine import _PINNED_TOOLS, _select_tool_schemas, _tool_name


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


def test_pinned_set_contains_lookups():
    for n in ("web_search", "search_history", "read_paragraphs", "load_skill"):
        assert n in _PINNED_TOOLS
