"""服务端 keep-alive 时长锁（2026-10-13 13:45 第三起事故根因：面板全空 ~3 分钟自愈）。

事故机制（证据：13:45:53 发日记 POST 秒回 200 → 13:46:23 起前端所有轮询消失 →
13:48:21 五个 OPTIONS 预检完成但对应 GET 拖到 13:51:14 才完成——173 秒，
恰为 TCP 重传超时量级；13:51:14 几十个请求同秒涌出＝排队洪峰）：

uvicorn 默认 ``timeout_keep_alive=5`` 秒——服务端 5 秒就关闭空闲连接；
WKWebView 的连接池按自己的节奏复用连接，请求落在已被服务端 FIN 的半死连接上
→ 卡到 TCP 超时（~2-3 分钟）→ 占住 WebView 同 host 6 连接槽 → 面板数据 GET
全部排队 → 全空；TCP 超时后连接重置、排队请求一次性涌出 →「自动恢复」。
「操作频繁就堵」＝每次操作落在不同连接上，命中竞态的概率随操作密度上升。

修复：``KEEP_ALIVE_SECONDS = 300``（远长于前端最长轮询间隔 30s），
服务端在会话期内永不先关连接，竞态不复存在。
"""

from __future__ import annotations

import inspect
import socket
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI

from coworker.server import run as run_mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    return app


class _Server:
    def __init__(self, keep_alive: float):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(
                _make_app(),
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                timeout_keep_alive=keep_alive,
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(80):
            try:
                if httpx.get(f"{self.base}/v1/health", timeout=2).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("test server not ready")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _get_on(sock: socket.socket) -> bytes:
    """同一 TCP 连接上发一个 GET，读完整响应（headers + content-length body）。"""
    sock.sendall(
        b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"连接已被服务端关闭（半死连接竞态复现）：{buf[:120]!r}")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


def test_run_py_sets_long_keepalive():
    """run.py 必须定义 ≥60s 的 KEEP_ALIVE_SECONDS 且真的传给 uvicorn.run。"""
    ka = getattr(run_mod, "KEEP_ALIVE_SECONDS", None)
    assert ka is not None and ka >= 60, (
        "run.py 缺 KEEP_ALIVE_SECONDS——uvicorn 默认 5s 空闲即关连接，"
        "与 WebView 连接复用构成半死连接竞态（面板全空 ~3 分钟自愈事故根因）"
    )
    src = inspect.getsource(run_mod)
    assert "timeout_keep_alive=KEEP_ALIVE_SECONDS" in src, (
        "KEEP_ALIVE_SECONDS 已定义但未接线到 uvicorn.run（wiring 断点）"
    )


def test_idle_connection_survives_default_uvicorn_gap():
    """空闲 6.5s（> uvicorn 默认 5s）后，同一连接的下一击必须仍有响应。

    修复前（默认 5s）：服务端已 FIN，第二击读到 EOF——正是事故里
    『OPTIONS 过了、GET 挂 173 秒』的微观机制。
    """
    ka = getattr(run_mod, "KEEP_ALIVE_SECONDS", 5)  # 修复前＝uvicorn 默认 5s
    server = _Server(keep_alive=ka)
    try:
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=15)
        first = _get_on(sock)
        assert b"200 OK" in first
        time.sleep(6.5)  # 跨过 uvicorn 默认 5s 空闲线
        second = _get_on(sock)  # 修复前在此断言失败（EOF）
        assert b"200 OK" in second
        sock.close()
    finally:
        server.stop()
