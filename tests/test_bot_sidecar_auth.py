"""Bot 群挂进主 sidecar 后的鉴权回归（2026-11-07 真机 401 事故）。

事故现象：桌面 GUI 点「Bot 群」，iframe 直接回
``{"error":"missing or invalid OpenWorker sidecar token"}``。

根因：``create_app`` 的 ``require_sidecar_token`` 白名单只放行 ``/roundtable/``，
漏了 ``/bots/``。上一轮接入时用 ``TestClient`` 验过 ``/bots/`` 200，但那次为了绕开
SessionManager 把 ``COWORKER_API_TOKEN`` 从 env 里 pop 掉了 —— 等于在**免鉴权环境**
测路由，真实 app 一起 token 就全被挡。

⇒ 本文件必须在**带 token** 的 ``create_app`` 上跑，锁住真实形态；同时验证放行范围
是收敛的（主 API 仍需 token），不能为了修一个页面把鉴权整片关掉。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coworker.server.app import create_app
from coworker.server.manager import SessionManager

TOKEN = "test-sidecar-token-1107"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("COWORKER_API_TOKEN", TOKEN)
    # 不跑 __init__（会建会话 / 连网关）；只验中间件与路由层。
    mgr = SessionManager.__new__(SessionManager)
    # 裸 manager 下部分 /v1 路由会因缺属性报 500；那是路由层的事，与中间件无关，
    # 不能让它以异常形式冒出来盖掉我们要验的 401 判定。
    return TestClient(create_app(mgr), raise_server_exceptions=False)


def test_bot_room_page_and_health_are_reachable_in_authenticated_sidecar(client) -> None:
    """iframe 页面与它的首个 API 都必须能在带 token 的主 app 里打开。"""
    page = client.get("/bots/")
    assert page.status_code == 200, page.text
    assert "Bot" in page.text or "bot" in page.text

    health = client.get("/bots/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True


def test_bot_mount_redirect_without_trailing_slash_is_reachable(client) -> None:
    """/bots → /bots/ 的 redirect 本身也不能被 401 挡住。"""
    r = client.get("/bots", follow_redirects=False)
    assert r.status_code in (200, 301, 307, 308), r.text


def test_main_api_still_requires_token(client) -> None:
    """放行 /bots 不能顺手把主 API 的鉴权关掉。"""
    r = client.get("/v1/settings")
    assert r.status_code == 401
    assert "missing or invalid OpenWorker sidecar token" in r.text


def test_wrong_token_still_rejected(client) -> None:
    r = client.get("/v1/settings", headers={"X-OpenWorker-Token": "wrong"})
    assert r.status_code == 401


def test_valid_token_passes_the_middleware(client) -> None:
    """带对 token 后中间件放行（路由可能因裸 manager 报别的错，但不该是 401）。"""
    r = client.get("/v1/settings", headers={"X-OpenWorker-Token": TOKEN})
    assert r.status_code != 401


def test_prefix_is_not_over_broad(client) -> None:
    """白名单按 /bots 前缀精确匹配，不能宽到 /bots-anything。"""
    r = client.get("/bots-elsewhere/api/health")
    assert r.status_code == 401
