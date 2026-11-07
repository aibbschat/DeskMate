"""主聊天 WS 与「按需语音朗读」的接线契约（集成测试）。

单元测试锁模块内部逻辑；这里锁的是**真的接上了**：
- 工具在 WS 会话的 registry 里可见（含复用 engine 的补挂路径）
- 模型调工具 → `tool_finished` → 后台朗读拿到的是**当轮最终正文**
- 普通轮次完全静默
- 非 macOS / 模块不可用时整条分支短路

朗读本体不真出网、不真播音：`speak_reply` 在测试里被替换成记录调用 + threading.Event。
"""
from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient, ToolCall
from coworker.server import SessionManager, create_app
from coworker.server import app as app_mod
from deskmate import tts as real_tts


class ScriptedProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        # `turn_start` 的自动标题是旁路调用（不带 tools），不能吃掉主对话脚本：
        # 否则第二轮会 `pop from empty list` → error 事件，测试「假绿」在错误路径上。
        if not tools:
            return AssistantTurn(text="会话标题", finish_reason="stop")
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def _text(text):
    return AssistantTurn(text=text, finish_reason="stop")


def _voice_call(call_id="call_voice_1"):
    return AssistantTurn(
        tool_calls=[ToolCall(id=call_id, name=real_tts.VOICE_TOOL_NAME, arguments={})]
    )


def _make_client(tmp_path, turns):
    manager = SessionManager(workspace=tmp_path, provider=ScriptedProvider(turns))
    return manager, TestClient(create_app(manager))


def _drain(ws):
    types: list[str] = []
    final_texts: list[str] = []
    while True:
        event = ws.receive_json()
        types.append(event["type"])
        if event["type"] == "assistant_message":
            data = event.get("data") or {}
            if not data.get("tool_calls"):
                final_texts.append(data.get("text") or "")
        if event["type"] == "turn_done":
            return types, final_texts


def _install_fake_speech(monkeypatch, *, module=real_tts):
    """替换朗读本体；返回 (spoken, done)。"""
    spoken: list[str] = []
    done = threading.Event()

    def fake_speak(text, **kwargs):
        spoken.append(text)
        done.set()
        return {"ok": True, "reason": ""}

    monkeypatch.setattr(app_mod, "_voice_reply_module", lambda: module)
    monkeypatch.setattr(module, "speak_reply", fake_speak)
    return spoken, done


def test_ws_voice_tool_is_registered_and_speaks_final_reply(tmp_path, monkeypatch):
    spoken, done = _install_fake_speech(monkeypatch)
    # 第 1 轮模型调意图工具（中间消息带 tool_calls），第 2 轮才是屏幕上的最终回复。
    manager, client = _make_client(tmp_path, [_voice_call(), _text("这是屏幕上那条最终回复。")])
    with client.websocket_connect("/ws/session/voice1") as ws:
        assert ws.receive_json()["type"] == "ready"
        engine = manager.get_engine("voice1")
        assert real_tts.VOICE_TOOL_NAME in engine.registry.names()
        ws.send_json({"type": "user_message", "text": "语音回复"})
        types, final_texts = _drain(ws)
        assert "tool_finished" in types
        assert "turn_end" in types
        assert final_texts == ["这是屏幕上那条最终回复。"]  # 主 turn 真的走完了，不是 error 分支
        assert done.wait(5), "朗读后台任务没有执行"
    assert spoken == ["这是屏幕上那条最终回复。"]


def test_ws_ordinary_turn_stays_silent(tmp_path, monkeypatch):
    """平时不自动朗读：没调意图工具，连 TTS 都不该碰。"""
    spoken, done = _install_fake_speech(monkeypatch)
    _manager, client = _make_client(tmp_path, [_text("普通文字回复")])
    with client.websocket_connect("/ws/session/voice_silent") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "user_message", "text": "随便聊聊"})
        types, final_texts = _drain(ws)
        assert "turn_end" in types
        assert final_texts == ["普通文字回复"]
        assert "tool_finished" not in types
        assert not done.wait(0.5)
    assert spoken == []


def test_ws_reused_engine_gets_voice_tool_backfilled(tmp_path, monkeypatch):
    """get_engine 命中缓存时忽略 extra_tools；WS 必须补挂，否则老会话里没有这个工具。"""
    spoken, done = _install_fake_speech(monkeypatch)
    manager, client = _make_client(tmp_path, [_voice_call(), _text("补挂后能念。")])
    # 先用一个不带 extra_tools 的路径把 engine 建出来（模拟后台任务 / 旧会话）
    existing = manager.get_engine("voice_reuse")
    assert existing is not None
    assert real_tts.VOICE_TOOL_NAME not in existing.registry.names()
    with client.websocket_connect("/ws/session/voice_reuse") as ws:
        assert ws.receive_json()["type"] == "ready"
        assert real_tts.VOICE_TOOL_NAME in existing.registry.names()
        ws.send_json({"type": "user_message", "text": "念给我听"})
        types, final_texts = _drain(ws)
        assert "tool_finished" in types
        assert "turn_end" in types
        assert final_texts == ["补挂后能念。"]
        assert done.wait(5)
    assert spoken == ["补挂后能念。"]


def test_ws_set_voice_tool_is_registered_and_persists_choice(tmp_path, monkeypatch):
    """换音色必须走完整链路：模型调工具 → 写 prefs.json → 下一轮朗读用新音色。

    用 `COWORKER_STATE_DIR` 把 prefs 隔离到 tmp，绝不写真实的
    `~/.config/coworker/prefs.json`（那会改掉萧哥正在用的音色）。
    """
    state = tmp_path / "state"
    monkeypatch.setenv("COWORKER_STATE_DIR", str(state))
    _install_fake_speech(monkeypatch)
    turns = [
        AssistantTurn(
            tool_calls=[
                ToolCall(
                    id="call_voice_set",
                    name=real_tts.SET_VOICE_TOOL_NAME,
                    arguments={"voice": "御姐"},
                )
            ]
        ),
        _text("音色换好了。"),
    ]
    manager, client = _make_client(tmp_path, turns)
    with client.websocket_connect("/ws/session/voice_set") as ws:
        assert ws.receive_json()["type"] == "ready"
        engine = manager.get_engine("voice_set")
        assert real_tts.SET_VOICE_TOOL_NAME in engine.registry.names()
        ws.send_json({"type": "user_message", "text": "以后用御姐音念"})
        types, _final = _drain(ws)
        assert "tool_finished" in types
        assert "turn_end" in types
    prefs = state / "prefs.json"
    assert prefs.is_file(), "换音色没有落盘"
    assert real_tts.load_saved_voice(prefs_path=prefs) == (
        "zh_female_gaolengyujie_uranus_bigtts"
    )


def test_ws_voice_message_tool_sends_bubble_payload(tmp_path, monkeypatch):
    """「发条语音」= 聊天里的语音气泡：合成后随事件带 base64 走，服务端不出声。"""
    import base64 as _b64

    import pytest

    carved = {}

    def fake_build(text, **kwargs):
        carved["text"] = text
        return {
            "ok": True,
            "reason": "",
            "audio": b"MP3DATA",
            "text": text,
            "seconds": 4.2,
            "truncated": False,
        }

    monkeypatch.setattr(app_mod, "_voice_reply_module", lambda: real_tts)
    monkeypatch.setattr(real_tts, "build_voice_message", fake_build)
    monkeypatch.setattr(
        real_tts, "speak_reply", lambda *a, **k: pytest.fail("气泡不该走扬声器")
    )
    turns = [
        AssistantTurn(
            tool_calls=[
                ToolCall(id="call_msg_1", name=real_tts.MESSAGE_TOOL_NAME, arguments={})
            ]
        ),
        _text("这是要发成语音的那条正文。"),
    ]
    _manager, client = _make_client(tmp_path, turns)
    with client.websocket_connect("/ws/session/voice_msg") as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "user_message", "text": "发条语音给我"})
        _drain(ws)  # 吃掉到 turn_done 为止的事件
        # 气泡事件在 turn_done 之后单独到（前端先看到文字，再冒出语音条，像真微信）
        event = ws.receive_json()
    assert event["type"] == "voice_message"
    data = event["data"]
    assert _b64.b64decode(data["audio_b64"]) == b"MP3DATA"
    assert data["mime"] == "audio/mpeg"
    assert data["seconds"] == 4.2
    assert data["text"] == "这是要发成语音的那条正文。"
    assert carved["text"] == "这是要发成语音的那条正文。"


def test_ws_voice_branch_is_short_circuited_when_module_unavailable(tmp_path, monkeypatch):
    """云端 Linux / 显式关闭时：工具不注册、朗读不发生，主聊天照常。"""
    monkeypatch.setattr(app_mod, "_voice_reply_module", lambda: None)
    manager, client = _make_client(tmp_path, [_text("照常回复")])
    with client.websocket_connect("/ws/session/voice_off") as ws:
        assert ws.receive_json()["type"] == "ready"
        names = manager.get_engine("voice_off").registry.names()
        assert real_tts.VOICE_TOOL_NAME not in names
        assert real_tts.MESSAGE_TOOL_NAME not in names
        assert real_tts.SET_VOICE_TOOL_NAME not in names
        ws.send_json({"type": "user_message", "text": "语音回复"})
        types, final_texts = _drain(ws)
        assert "assistant_message" in types
        assert "turn_end" in types
        assert final_texts == ["照常回复"]
        assert "tool_finished" not in types
