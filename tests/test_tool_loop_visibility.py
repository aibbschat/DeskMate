"""工具空转可见性与读稿工具 pin（2026-11-21）。

现场：模型连续多轮只吐 tool_call、正文为空，GUI 只剩工具卡片 +
"Waiting for agent..."。本文件锁定两件事：
1. 首轮只发一次非持久 ASSISTANT_DELTA，后续工具轮不重复刷屏；
2. 新增的 read_chapters 与 read_chapter 一样是核心工具，永不被 schema 预算裁掉。
"""

from __future__ import annotations

import aisuite as ai
from coworker.engine import (
    _is_pinned_tool,
    _select_tool_schemas,
    TurnEngine,
)
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ToolCall
from coworker.tools import ToolRegistry

from tests.test_engine import ScriptedProvider, _collect  # 复用既有脚本桩


def _engine(tmp_path, turns):
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    return TurnEngine(
        provider=ScriptedProvider(turns),
        registry=registry,
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="gpt-5.5",
        max_iterations=12,
    )


def _tool_turn(name, args, call_id):
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
        finish_reason="tool_calls",
    )


def test_visibility_progress_once_across_many_tool_rounds(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    engine = _engine(
        tmp_path,
        [
            _tool_turn("read_file", {"path": "a.txt"}, "c1"),
            _tool_turn("read_file", {"path": "a.txt"}, "c2"),
            AssistantTurn(text="done", finish_reason="stop"),
        ],
    )
    events = _collect(engine, "read twice")
    progress = [e for e in events if e.type == EventType.ASSISTANT_DELTA]
    assert len(progress) == 1, "两轮工具只有首轮提示；每 5 轮才更新一次，不能每轮刷屏"
    assert "read_file" in progress[0].data["text"]
    # 非持久：绝不污染 prompt/cache 或工具配对
    assert not any(
        "我先处理这一步" in str(m.get("content", "")) for m in engine.messages
    )


def test_read_chapters_never_dropped_by_schema_budget(monkeypatch):
    monkeypatch.setenv("COWORKER_TOOL_SCHEMA_MAX_CHARS", "1")
    schemas = [
        {"type": "function", "function": {"name": "mcp__perception__read_chapters"}},
        {"type": "function", "function": {"name": "mcp__perception__read_chapter"}},
        {"type": "function", "function": {"name": "some_connector_tool"}},
    ]
    kept = _select_tool_schemas(schemas)
    names = {s["function"]["name"] for s in kept}
    assert "mcp__perception__read_chapters" in names
    assert "mcp__perception__read_chapter" in names
    assert "some_connector_tool" not in names
    assert _is_pinned_tool("read_chapters")
