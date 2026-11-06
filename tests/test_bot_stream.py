"""Bot 群原生流式回归 —— 逐 token 事件 / SSE 帧 / 落库一致 / 断开掐流。

对应 docs/bot-mode-redesign.md 阶段 2 的流式补齐：
旧的 runtime._drive 把 TurnEngine 的 ASSISTANT_DELTA 全丢了（只留整段文本），
本组测试锁住「不丢事件」之后的四条契约：

1. delta 拼接 == final 文本（前端逐字生长与落库文本一致）；
2. bot 自主发起的 read_book 会作为 tool 事件可见（解释等待时间）；
3. say_stream 只在 bot 说完后落库一次，且 author_role 只有 user / bot（无纪要）；
4. /stream 是标准 SSE，带 X-Accel-Buffering: no，客户端断开时 engine 真的停流。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from coworker.providers.base import AssistantTurn, ModelCapabilities, StreamChunk, ToolCall
from coworker.teams.chat import ChatStore
from deskmate.bots import rooms
from deskmate.bots.runtime import BotRuntime
from deskmate.bots.web import create_app

_REPLY = "我读了《夜不收》，烧仓这段建议从仓场则例切入。"


async def _collect(agen) -> list[dict[str, Any]]:
    return [ev async for ev in agen]


class _StreamingScriptedProvider:
    """离线 provider：分段 yield text_delta，最后给完整 turn。

    与 test_bot_mode_runtime.py 的 _ScriptedProvider 区别：那个只 yield 一个含
    完整 turn 的 chunk（拿不到逐 token），这个真正模拟流式，用来验证事件转发。
    """

    def __init__(self, *, tool_first: bool = False, text: str = _REPLY) -> None:
        self.step = 0
        self.tool_first = tool_first
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def capabilities(self, model):
        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):
        return AssistantTurn(text=self.text)

    def stream(self, *, model, messages, tools=None, **settings):
        self.calls.append({"messages": messages, "tools": tools})
        self.step += 1
        if self.tool_first and self.step == 1:
            # 模型自主决定先读稿（真 Agent，不是外部解析规则）
            yield StreamChunk(
                turn=AssistantTurn(
                    text=None,
                    tool_calls=[
                        ToolCall(id="c1", name="read_book", arguments={"title": "《夜不收》"})
                    ],
                )
            )
            return
        step = max(1, len(self.text) // 3)
        for i in range(0, len(self.text), step):
            yield StreamChunk(text_delta=self.text[i : i + step])
        yield StreamChunk(turn=AssistantTurn(text=self.text))


class _FakeStreamRuntime:
    """不依赖真 engine 的编排层替身，用于测 @ 定向 / pass / SSE 端点。"""

    async def speak_stream(self, bot_id: str, text: str, *, history=None):
        yield {"type": "delta", "text": "一半"}
        yield {"type": "final", "text": "完整发言"}


@pytest.fixture()
def shelf_dir(tmp_path: Path) -> Path:
    book = tmp_path / "16.夜不收"
    book.mkdir()
    (book / "第三章 烧仓.txt").write_text(
        "叶鸣秋推开柴门，见董文正立在廊下，手里攥着一封未拆的信。", encoding="utf-8"
    )
    return tmp_path


def _runtime(shelf_dir: Path, tmp_path: Path, provider=None) -> BotRuntime:
    return BotRuntime(
        books_root=shelf_dir,
        workspace_root=tmp_path / "ws",
        provider=provider or _StreamingScriptedProvider(),
        model="test-model",
    )


# ---------------------------------------------------------------- runtime 层

def test_speak_stream_deltas_join_into_final_text(shelf_dir, tmp_path) -> None:
    """逐 token 事件的拼接结果必须等于 final，前端覆盖气泡时才不会跳字。"""
    rt = _runtime(shelf_dir, tmp_path)
    events = asyncio.run(_collect(rt.speak_stream("historian", "聊聊烧仓", history=[])))
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    finals = [e["text"] for e in events if e["type"] == "final"]
    assert len(deltas) >= 2, "必须真的逐 token 吐事件，而不是一次给整段"
    assert "".join(deltas) == _REPLY
    assert finals == [_REPLY]
    assert not any(e["type"] == "error" for e in events)
    rt.close()


def test_speak_stream_surfaces_self_decided_tool_call(shelf_dir, tmp_path) -> None:
    """bot 自主读稿要作为 tool 事件可见（用户能解释「为什么等这么久」）。"""
    prov = _StreamingScriptedProvider(tool_first=True)
    rt = _runtime(shelf_dir, tmp_path, prov)
    events = asyncio.run(
        _collect(rt.speak_stream("historian", "先读《夜不收》，说说烧仓", history=[]))
    )
    tools = [e for e in events if e["type"] == "tool"]
    assert tools and tools[0]["name"] == "read_book"
    assert "".join(e["text"] for e in events if e["type"] == "delta") == _REPLY
    assert [e["text"] for e in events if e["type"] == "final"] == [_REPLY]
    assert prov.step == 2, "应当发生一次自主工具调用 + 一次流式发言"
    rt.close()


def test_speak_stream_interrupts_engine_when_aclosed(shelf_dir, tmp_path) -> None:
    """客户端断开 → aclose → engine.request_interrupt()，不能继续烧 token。"""
    rt = _runtime(shelf_dir, tmp_path)

    async def run():
        agen = rt.speak_stream("historian", "聊聊", history=[])
        eng = rt.engine("historian")
        first = await agen.__anext__()
        assert first["type"] == "delta"
        assert not eng._cancel.is_set()
        await agen.aclose()
        assert eng._cancel.is_set(), "断开必须真正掐断 provider 流"

    asyncio.run(run())
    rt.close()


# ---------------------------------------------------------------- rooms 层

def test_say_stream_event_order_and_single_persist(shelf_dir, tmp_path) -> None:
    """start → bot_start/delta/bot_end × N → done；只落库一次，无纪要。"""
    rt = _runtime(shelf_dir, tmp_path)
    cs = ChatStore(tmp_path / "chat.db")
    store = rooms.RoomStore(tmp_path, chat_store=cs)
    room = store.create_room("烧仓怎么往下写", ["historian", "lit-editor"])
    events = asyncio.run(_collect(store.say_stream(room.room_id, "大家聊聊", runtime=rt)))

    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done"
    assert types.count("bot_start") == 2
    assert types.count("bot_end") == 2
    assert types.count("delta") >= 2

    msgs = cs.messages(room.room_id)
    assert [m["author_role"] for m in msgs] == ["user", "bot", "bot"]
    assert all(m["text"] == _REPLY for m in msgs[1:])
    assert not any(
        kw in m["text"] for m in msgs for kw in ("纪要", "本轮总结", "主持人")
    )
    ends = [e for e in events if e["type"] == "bot_end"]
    assert [e["text"] for e in ends] == [msgs[1]["text"], msgs[2]["text"]], (
        "bot_end 的权威文本必须与落库一致，不能双写"
    )
    rt.close()
    store.close()
    cs.close()


def test_say_stream_respects_mentions_and_pass(tmp_path) -> None:
    """@ 只唤醒被点名的 bot；空 final = pass，不落库、不生成占位消息。"""

    class Fake:
        async def speak_stream(self, bot_id, text, *, history=None):
            if bot_id == "reader":
                yield {"type": "final", "text": ""}
                return
            async for ev in _FakeStreamRuntime().speak_stream(
                bot_id, text, history=history
            ):
                yield ev

    cs = ChatStore(tmp_path / "chat.db")
    store = rooms.RoomStore(tmp_path, chat_store=cs)
    room = store.create_room("话题", ["historian", "lit-editor", "reader"])
    events = asyncio.run(
        _collect(store.say_stream(room.room_id, "@historian @reader 聊聊", runtime=Fake()))
    )
    seq = [e["type"] for e in events if e["type"] in ("bot_start", "bot_end", "bot_pass")]
    assert seq == ["bot_start", "bot_end", "bot_start", "bot_pass"]
    msgs = cs.messages(room.room_id)
    assert [m["author_role"] for m in msgs] == ["user", "bot"]
    store.close()
    cs.close()


# ---------------------------------------------------------------- web / SSE 层

def test_stream_endpoint_emits_sse_frames_and_headers(tmp_path) -> None:
    """POST /api/rooms/{id}/stream 是标准 SSE，逐 delta 成帧，落库文本一致。"""
    client = TestClient(create_app(str(tmp_path), runtime=_FakeStreamRuntime()))
    room_id = client.post(
        "/api/rooms", json={"topic": "话题", "bot_ids": ["historian", "lit-editor"]}
    ).json()["room_id"]

    resp = client.post(f"/api/rooms/{room_id}/stream", json={"text": "说句话"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no", "反代缓冲会把逐字变一次性"
    assert resp.headers["cache-control"] == "no-cache"

    frames = [
        json.loads(line[5:].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    types = [f["type"] for f in frames]
    assert types[0] == "start" and types[-1] == "done"
    assert types.count("delta") >= 2
    assert types.count("bot_end") == 2
    assert all("turn_id" in f for f in frames if f["type"] in ("bot_start", "delta", "bot_end"))

    msgs = client.get(f"/api/rooms/{room_id}/messages").json()
    assert [m["author_role"] for m in msgs] == ["user", "bot", "bot"]
    assert msgs[-1]["text"] == "完整发言", "final 覆盖后落库的是权威文本，不是半截 delta"


def test_stream_endpoint_with_real_turn_engine(shelf_dir, tmp_path) -> None:
    """SSE 端点接真 TurnEngine（离线 provider）也能逐 token 出帧，不是只测替身。"""
    rt = _runtime(shelf_dir, tmp_path)
    client = TestClient(create_app(str(tmp_path), runtime=rt))
    room_id = client.post(
        "/api/rooms", json={"topic": "话题", "bot_ids": ["historian", "reader"]}
    ).json()["room_id"]

    resp = client.post(f"/api/rooms/{room_id}/stream", json={"text": "聊聊烧仓"})
    assert resp.status_code == 200
    frames = [
        json.loads(line[5:].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    types = [f["type"] for f in frames]
    assert types.count("delta") >= 2, "真 engine 的 ASSISTANT_DELTA 必须透到 SSE"
    assert types.count("bot_end") == 2
    msgs = client.get(f"/api/rooms/{room_id}/messages").json()
    assert msgs[-1]["text"] == _REPLY
    rt.close()


def test_stream_endpoint_rejects_empty_and_missing_room(tmp_path) -> None:
    client = TestClient(create_app(str(tmp_path), runtime=_FakeStreamRuntime()))
    assert client.post("/api/rooms/nope/stream", json={"text": "hi"}).status_code == 404
    room_id = client.post(
        "/api/rooms", json={"topic": "话题", "bot_ids": ["historian", "reader"]}
    ).json()["room_id"]
    assert client.post(f"/api/rooms/{room_id}/stream", json={"text": "  "}).status_code == 400


def test_legacy_say_endpoint_still_works(tmp_path) -> None:
    """旧 /say 一次性契约不能被流式改造破坏（现有调用方与 21 例回归）。"""

    class Fake:
        def speak(self, bot_id, text, *, history=None):
            return "旧契约发言"

    client = TestClient(create_app(str(tmp_path), runtime=Fake()))
    room_id = client.post(
        "/api/rooms", json={"topic": "话题", "bot_ids": ["historian", "reader"]}
    ).json()["room_id"]
    out = client.post(f"/api/rooms/{room_id}/say", json={"text": "说句话"})
    assert out.status_code == 200
    assert [r["text"] for r in out.json()["replies"]] == ["旧契约发言", "旧契约发言"]
