"""B 方案契约：工具结果进入对话时，中文不得被 json.dumps 转成 \\uXXXX。

根因（2026-11-20 实盘定位）：`serialize_result` 与 `_tool_result_message` 都用
`json.dumps(result, default=str)`，默认 `ensure_ascii=True` —— 中文变 `\\uXXXX`、
换行变 `\\n`。模型补读落盘正文（`read_file` 返回 dict）时，得自己写
`json.loads` 解码脚本才能读全一章正文。

`toolresult.py:44` 的 docstring 明写 `serialize_result` 必须 "Exactly what
`_tool_result_message` puts in the message content" —— 两处**必须逐字一致**：
`bound_tool_result` 按 `serialize_result` 算字节决定是否截断/溢出，实发内容由
`_tool_result_message` 生成；只改一处会让字节口径与实发不符。

范围：只锁进入对话的两处。`_preview` / event 的 `result_preview` 是 GUI 短预览，
不进模型上下文，不在本契约内。
"""

from __future__ import annotations

import asyncio
import json

import aisuite as ai

from coworker import toolresult
from coworker.engine import TurnEngine, _tool_result_message
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient, ToolCall
from coworker.tools import ToolRegistry

_CN = "【覆盖率】第十六章：生意经\n陈冲的浑号本叫书呆子，后省为呆子。\n他说：「这买卖做得。」"


def _has_ascii_escape(s: str) -> bool:
    """是否含 json.dumps 的 \\uXXXX ASCII 转义。"""
    return "\\u" in s


def test_serialize_result_keeps_chinese_readable():
    text = toolresult.serialize_result({"path": "生意经.txt", "content": _CN})
    assert not _has_ascii_escape(text), "中文不得被转成 \\uXXXX"
    assert "生意经" in text and "陈冲" in text
    # ensure_ascii=False 输出仍是合法 JSON；控制字符（换行）照常转义为 \n
    assert json.loads(text)["content"] == _CN


def test_tool_result_message_keeps_chinese_readable():
    msg = _tool_result_message(
        ToolCall(id="c1", name="read_file", arguments={}),
        {"path": "生意经.txt", "content": _CN},
    )
    assert not _has_ascii_escape(msg["content"])
    assert "生意经" in msg["content"]
    assert msg["role"] == "tool" and msg["tool_call_id"] == "c1"
    assert json.loads(msg["content"])["content"] == _CN


def test_two_serializers_are_byte_identical():
    """核心契约：算字节的那处与实发的那处必须逐字一致（防只改一处）。"""
    samples = [
        _CN,  # str 分支
        {"content": _CN},  # 中文 dict
        {"content": _CN, "n": 1, "ok": True},  # 混合类型
        {"q": 'a"b\\c\n' + _CN},  # 引号 / 反斜杠 / 换行边界
        [{"content": _CN}, 2, None],  # list
        None,
    ]
    for x in samples:
        a = toolresult.serialize_result(x)
        b = _tool_result_message(ToolCall(id="x", name="t", arguments={}), x)["content"]
        assert a == b, f"两处序列化不一致: {type(x).__name__}"
        assert len(a.encode("utf-8")) == len(b.encode("utf-8"))


def test_no_ascii_size_bloat():
    """中文正文不再被 \\uXXXX 撑成 2 倍，且仍可 json.loads 还原。"""
    raw = _CN * 400
    got = toolresult.serialize_result({"content": raw})
    plain = json.dumps({"content": raw}, default=str, ensure_ascii=False)
    escaped = json.dumps({"content": raw}, default=str)
    assert got == plain, "实现必须用 ensure_ascii=False"
    assert len(got.encode("utf-8")) < len(escaped.encode("utf-8")) * 0.7
    assert json.loads(got)["content"] == raw


def test_spill_file_stays_plain_utf8(tmp_path):
    """回归保护：溢出落盘的正文一直是明文，改动不得破坏它。"""
    big = _CN * 1000
    out = toolresult.bound_tool_result(
        {"content": big}, max_bytes=10_000, spill_dir=tmp_path, step=9, tool_name="read_chapter"
    )
    assert "bytes omitted" in out["content"]
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    disk = files[0].read_text(encoding="utf-8")
    assert disk == big and not _has_ascii_escape(disk)


class _Scripted(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def test_engine_tool_message_chinese_is_readable(tmp_path):
    """端到端：一轮真实 TurnEngine，进入 messages 的 tool 消息是明文中文。"""

    def chinese_out():
        """Return a Chinese novel excerpt."""
        return {"path": "生意经.txt", "content": _CN}

    registry = ToolRegistry()
    registry.register(
        chinese_out,
        metadata=ai.ToolMetadata(category="search", risk_level="low", requires_approval=False),
    )
    engine = TurnEngine(
        provider=_Scripted([
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="chinese_out", arguments={})],
                finish_reason="tool_calls",
            ),
            AssistantTurn(text="done", finish_reason="stop"),
        ]),
        registry=registry,
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="m",
        approver=None,
        tool_result_max_bytes=10_000,
        tool_result_spill_dir=tmp_path / "spill",
    )

    async def go():
        return [ev async for ev in engine.run("go")]

    asyncio.run(go())
    tool_msgs = [m for m in engine.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert not _has_ascii_escape(tool_msgs[0]["content"])
    assert "生意经" in tool_msgs[0]["content"]
    assert json.loads(tool_msgs[0]["content"])["content"] == _CN
