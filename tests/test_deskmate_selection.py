"""划词适配器测试：用 httpx.MockTransport 模拟 sidecar，验证代理/流式/搜索注入。

不依赖真实 OpenWorker 进程。通过 monkeypatch 注入 (port, token) 与 mock 客户端。
"""
from __future__ import annotations

import json
import subprocess

import httpx
import pytest

import deskmate.selection.server as srv
from deskmate.selection.server import build_app, SEARCH_SENTINEL


def _make_client(capture: dict, sidecar_port: int = 18795):
    """构造 MockTransport 客户端，记录最后一次转发给 sidecar 的请求体。"""

    async def _sse_chunks():
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode() if request.content else ""
        if path == "/v1/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/v1/chat/completions":
            capture["body"] = body
            # 流式：回一段 SSE（用生成器体，避免 MockTransport 预消费）；非流式：回 OpenAI JSON
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            if payload.get("stream"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_sse_chunks(),
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "模拟回答"}}]},
            )
        return httpx.Response(404, json={"error": "not found"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


@pytest.fixture
def app(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(srv, "resolve_sidecar", lambda: (18795, "test-token"))
    monkeypatch.setattr(srv, "_client", _make_client(capture))
    return build_app(), capture


def test_health_ok(app):
    from fastapi.testclient import TestClient

    application, _ = app
    with TestClient(application) as c:
        r = c.get("/v1/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_proxy_nonstream_explain(app):
    from fastapi.testclient import TestClient

    application, capture = app
    with TestClient(application) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "stream": False,
                "messages": [{"role": "user", "content": "焚书坑儒是什么意思"}],
            },
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "模拟回答"
        # 代理是透传：客户端消息原样转发，并带上 sidecar token（由 upstream 校验）
        sent = json.loads(capture["body"])
        assert sent["messages"][-1]["content"] == "焚书坑儒是什么意思"


def test_default_routes_to_free_model(app):
    from fastapi.testclient import TestClient

    application, capture = app
    with TestClient(application) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "stream": False,
                "messages": [{"role": "user", "content": "焚书坑儒是什么意思"}],
            },
        )
        assert r.status_code == 200
        # 不填模型 -> 默认路由到 OmniRoute 免费模型（model=auto），不烧付费 DeepSeek
        sent = json.loads(capture["body"])
        assert sent["model"] == "auto"


def test_explicit_omniroute_alias_routes_free(app):
    from fastapi.testclient import TestClient

    application, capture = app
    with TestClient(application) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "omniroute",
                "stream": False,
                "messages": [{"role": "user", "content": "解释：垂帘听政"}],
            },
        )
        assert r.status_code == 200
        sent = json.loads(capture["body"])
        assert sent["model"] == "auto"


def test_proxy_stream_passthrough(app, monkeypatch):
    import asyncio

    from starlette.requests import Request

    application, capture = app

    # FakeClient：跳过真实 HTTP，直接回完整 JSON，验证 gen() 的 SSE 包裹逻辑。
    class _FakeResp:
        status_code = 200
        text = '{"choices":[{"message":{"content":"叔夺侄位"}}]}'

        def json(self):
            return json.loads(self.text)

    class _FakeClient:
        async def post(self, url, content=None, headers=None):
            capture["body"] = (
                content.decode() if isinstance(content, (bytes, bytearray)) else content
            )
            return _FakeResp()

    monkeypatch.setattr(srv, "_client", _FakeClient())

    async def run():
        body = json.dumps(
            {
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "翻译这段"}],
            }
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "app": application,
        }
        req = Request(scope, receive=receive)
        resp = await srv._proxy_chat(req)
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)
        return b"".join(chunks).decode()

    out = asyncio.run(run())
    assert "data: [DONE]" in out
    # 适配器始终以非流式请求上游（再包成 SSE），并归一化模型名为 provider:model 全名
    sent = json.loads(capture["body"])
    assert sent["stream"] is False
    assert sent["model"] == "deepseek:deepseek-v4-flash"


def test_selection_explain(app):
    from fastapi.testclient import TestClient

    application, _ = app
    with TestClient(application) as c:
        r = c.post(
            "/v1/deskmate/selection",
            json={"action": "explain", "text": "靖难之役"},
        )
        assert r.status_code == 200
        assert r.json()["result"] == "模拟回答"


def test_search_sentinel_injects_web_results(app, monkeypatch):
    from fastapi.testclient import TestClient

    application, capture = app
    # 让搜索返回假结果
    monkeypatch.setattr(
        srv,
        "_run_web_search",
        lambda q: {"provider": "mock", "results": [
            {"title": "明成祖本纪", "url": "http://x/1", "snippet": "朱棣发动靖难"}
        ]},
    )
    with TestClient(application) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "stream": False,
                "messages": [{"role": "user", "content": SEARCH_SENTINEL + "靖难之役背景"}],
            },
        )
        assert r.status_code == 200
        sent = json.loads(capture["body"])
        # 哨兵前缀已被清掉，且检索结果被注入到 system 消息
        assert sent["messages"][-1]["content"].endswith("靖难之役背景")
        sys_txt = sent["messages"][0]["content"]
        assert "明成祖本纪" in sys_txt
        assert "朱棣发动靖难" in sys_txt
