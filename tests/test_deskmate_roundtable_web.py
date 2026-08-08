"""圆桌 Web 的 opencode A2A 适配层回归测试。

锁定行为：挂入主 app（COWORKER_PORT 语境，挂在 /roundtable 下）时，Agent Card 的
supportedInterfaces[0].url 必须指向真实可连地址（真实端口 + /roundtable 前缀 + 127.0.0.1），
否则 A2A 客户端按 card 里的地址去连会撞上空端口 / 被 IPv6 解析误导。
"""
import os

import pytest

from deskmate.roundtable.web import create_app


def _card_url():
    from starlette.testclient import TestClient

    client = TestClient(create_app())
    return client.get("/opencode-a2a/.well-known/agent-card.json").json()[
        "supportedInterfaces"
    ][0]["url"]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("COWORKER_PORT", raising=False)


def test_mounted_card_url_uses_real_port_and_prefix(monkeypatch):
    """挂入主 app：card 地址 = 真实端口 + /roundtable/opencode-a2a（127.0.0.1 可连）。"""
    monkeypatch.setenv("COWORKER_PORT", "64673")
    assert _card_url() == "http://127.0.0.1:64673/roundtable/opencode-a2a"


def test_standalone_card_url_has_no_roundtable_prefix(monkeypatch):
    """独立运行：无 COWORKER_PORT，card 地址 = 根 /opencode-a2a，不带 /roundtable 前缀。"""
    monkeypatch.delenv("COWORKER_PORT", raising=False)
    assert _card_url() == "http://127.0.0.1:9920/opencode-a2a"


def test_mounted_message_send_routes_under_prefix(monkeypatch):
    """挂载上下文里 A2A SendMessage 仍能经 /roundtable/opencode-a2a 路由。"""
    monkeypatch.setenv("COWORKER_PORT", "64673")
    from starlette.testclient import TestClient

    from coworker.server import SessionManager, create_app

    mgr = SessionManager(data_dir=os.path.join(os.environ.get("TMPDIR", "/tmp"), "ow_rt_web_test"))
    client = TestClient(create_app(mgr))
    resp = client.post(
        "/roundtable/opencode-a2a/",
        json={
            "jsonrpc": "2.0",
            "id": "t1",
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": "m1",
                    "role": "ROLE_USER",
                    "parts": [{"text": "hi", "mediaType": "text/plain"}],
                }
            },
        },
    )
    assert resp.status_code == 200
