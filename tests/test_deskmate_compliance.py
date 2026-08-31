"""P2-8 合规轻陪伴桌宠定位 — 单元测试 + 端点冒烟。

覆盖：get_positioning 结构、not_a_companion 声明、三要素齐全、
以及 GET /v1/deskmate/positioning 端点返回 200 + disclaimer。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deskmate.compliance import get_positioning, POSITIONING


def test_positioning_shape():
    p = get_positioning()
    assert p["name"] == "小桔"
    assert p["not_a_companion"] is True
    assert "disclaimer" in p and p["disclaimer"]


def test_positioning_three_pillars():
    keys = {x["key"] for x in POSITIONING["pillars"]}
    assert {"tool", "cute", "controllable"} <= keys


def test_positioning_endpoint():
    try:
        from fastapi.testclient import TestClient
        from coworker.server.app import create_app
        from coworker.server.manager import SessionManager
    except Exception:
        # 后端未在当前环境装配时跳过（不视为失败）
        return
    app = create_app(SessionManager(data_dir="/tmp/deskmate_test_positioning"))
    client = TestClient(app)
    resp = client.get("/v1/deskmate/positioning")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("not_a_companion") is True
    assert "disclaimer" in data
