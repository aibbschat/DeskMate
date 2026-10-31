"""空回合（无正文、无工具调用）不再静默结束 — 2026-10-31 团队实测缺陷。

现场：三个 worker 的 assistant 轮次里 9/14、5/14、4/12 的正文为空，其中多条连工具
调用都没有（内容全进了 reasoning）。旧行为在 `not turn.tool_calls` 处直接 TURN_END，
于是 worker 表现为「领了活 → 一句『开工』→ 永久静止」，主编只能判零产出。

这些用例锁的是「空回合必须被追问一次，且有上限」；第 3 个用例把上限降为 0，
复现旧行为，证明前两个用例真的依赖新逻辑（不是本来就会绿）。
"""

from __future__ import annotations

import asyncio

import aisuite as ai
from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient, ToolCall
from coworker.tools import ToolRegistry


def _empty_turn(reasoning=None):
    """模型只思考、什么都不落地的一轮。"""
    return AssistantTurn(text=None, reasoning=reasoning, finish_reason="stop")


def _text_turn(text):
    return AssistantTurn(text=text, finish_reason="stop")


class ScriptedProvider(ProviderClient):
    def __init__(self, turns, *, loop=False):
        self._turns = list(turns)
        self._loop = loop
        self.calls = 0

    def complete(self, *, model, messages, tools=None, **settings):
        self.calls += 1
        return self._turns[0] if self._loop else self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, turns, *, loop=False, max_iterations=12):
    provider = ScriptedProvider(turns, loop=loop)
    registry = ToolRegistry()
    registry.register_all(ai.toolkits.files(root=str(tmp_path), allow_write=True))
    permissions = PermissionEngine(workspace_root=tmp_path)
    engine = TurnEngine(
        provider=provider,
        registry=registry,
        permissions=permissions,
        model="gpt-5.5",
        max_iterations=max_iterations,
    )
    return engine, provider


def _collect(engine, user_input):
    async def _run():
        return [ev async for ev in engine.run(user_input)]

    return asyncio.run(_run())


def test_thinking_only_round_is_asked_again(tmp_path):
    """正文为空、内容只在 reasoning 里 → 追问一次，并点名「思考只有你自己看得到」。"""
    engine, provider = _engine(
        tmp_path, [_empty_turn(reasoning="我先把骨架想清楚……"), _text_turn("骨架已落盘")]
    )
    events = _collect(engine, "出骨架")

    assert provider.calls == 2, "空回合后必须再问一次，而不是直接结束"
    texts = [ev.data.get("text") for ev in events if ev.type is EventType.ASSISTANT_MESSAGE]
    assert texts[-1] == "骨架已落盘"
    assert events[-1].type is EventType.TURN_END
    assert events[-1].data["status"] == "completed"

    nudges = [
        m["content"]
        for m in engine.messages
        if m.get("role") == "user" and "reasoning" in str(m.get("content", ""))
    ]
    assert nudges, "追问必须点名『内容写进了思考过程』这个真实成因"
    assert "看板" in nudges[0] or "工具" in nudges[0]


def test_empty_turn_gives_up_after_the_cap(tmp_path):
    """一直空 → 最多追问 _EMPTY_TURN_MAX_RETRIES 次就结束，并留下可见 notice。"""
    cap = TurnEngine._EMPTY_TURN_MAX_RETRIES
    engine, provider = _engine(tmp_path, [_empty_turn(reasoning="……")], loop=True)
    events = _collect(engine, "出骨架")

    assert provider.calls == cap + 1, "不能无限重试烧光迭代预算"
    assert events[-1].type is EventType.TURN_END
    notices = [m for m in engine.messages if m.get("role") == "notice"]
    assert any(n.get("kind") == "empty_turn" for n in notices), (
        "连续空回合要在 transcript 里留下诊断，而不是一个空白气泡"
    )


def test_cap_zero_reproduces_the_old_silent_end(tmp_path, monkeypatch):
    """把上限降到 0 就回到旧行为（静默结束）—— 证明上面两条锁的是新逻辑。"""
    monkeypatch.setattr(TurnEngine, "_EMPTY_TURN_MAX_RETRIES", 0)
    engine, provider = _engine(tmp_path, [_empty_turn(reasoning="……"), _text_turn("later")])
    events = _collect(engine, "出骨架")

    assert provider.calls == 1
    assert events[-1].type is EventType.TURN_END
    assert not [m for m in engine.messages if m.get("role") == "notice"]


def test_tool_only_round_is_not_treated_as_empty(tmp_path):
    """纯工具调用轮（无正文但有调用）是正常的，绝不能被当成空回合。"""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    engine, provider = _engine(
        tmp_path,
        [
            AssistantTurn(
                tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})],
                finish_reason="tool_calls",
            ),
            _text_turn("it says hello"),
        ],
    )
    events = _collect(engine, "read a.txt")

    assert provider.calls == 2
    assert EventType.TOOL_FINISHED in [ev.type for ev in events]
    assert not [
        m for m in engine.messages if m.get("role") == "user" and "reasoning" in str(m.get("content", ""))
    ]
