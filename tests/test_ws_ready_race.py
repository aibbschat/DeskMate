"""ws_session 僵尸连接修复（2026-11-21）。

今晨事故（会话 3bb34d79-964）：客户端在 accept 之后、ready 发送之前的一瞬断开
（GUI 切会话 / React 重挂载）。`send_json` 抛 RuntimeError 把 ws_session 协程炸死、
WS 关闭握手中途夭折、TCP 残留 ESTABLISHED——GUI 侧 onclose 永不触发，用户消息发进
死管道，界面无限 "Waiting for agent..."。修复：ready 发送断连安全（吞异常干净返回），
让 uvicorn 完成 close 握手，GUI 才能走重连 + outbox 补发。
"""
from __future__ import annotations

from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.server import SessionManager, create_app


class CapturingProvider(ProviderClient):
    def __init__(self, turns):
        self._turns = list(turns)

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


def test_ready_send_on_closed_socket_returns_cleanly(tmp_path, monkeypatch):
    manager = SessionManager(
        workspace=tmp_path, provider=CapturingProvider([AssistantTurn(text="hi")])
    )
    client = TestClient(create_app(manager))

    def boom(self, *args, **kwargs):
        raise RuntimeError(
            "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."
        )

    # 客户端瞬断：ready 发送撞上已关闭的 socket。修复前这里会把 handler 炸死
    # （TestClient 会把应用异常原样抛出）；修复后干净返回、连接正常收尾。
    monkeypatch.setattr(WebSocket, "send_json", boom)
    with client.websocket_connect("/ws/session/race1") as ws:
        ws.receive_json  # 不读——客户端已"断开"，直接退出即可
    monkeypatch.undo()

    # 服务器必须毫发无损：新连接照常拿到 ready，会话可继续使用。
    with client.websocket_connect("/ws/session/race1") as ws:
        ev = ws.receive_json()
        assert ev["type"] == "ready"
        assert ev["data"]["session_id"] == "race1"
