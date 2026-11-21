"""事件循环不许被同步 LLM 调用冻结（2026-10-13 事故回归锁）。

事故：桌面端跑几分钟后，全息屏/花园/日记/勋章所有数据端点集体"加载不出来"，
等一会儿又自己恢复。监控实锤：health 从 0.04s 飙到 2.57s/1.91s（事件循环冻结），
冻结期间积压请求在解冻瞬间集中完成。

根因类：async 处理器里**直接同步调用** LLM 助手（maybe_backfill_understanding /
try_inspire / store.generate_reply / advise / run_validation / ensure_today）。
LLM 健康时冻结 2-3 秒；多 provider 降级链全撞墙时冻结 75-120 秒——
正是"加载不出来、等一会儿又出现"的用户观感。

测试拓扑（与生产一致，否则假绿）：单 uvicorn 事件循环服务全部请求；
阻塞请求从**独立线程**打入；health 由主线程（外部观察者）计时。
曾用 ASGITransport + asyncio.create_task 的版本测不到冻结——阻塞调用把
测试协程自己的计时序列也一起冻住了。
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from coworker.providers import ModelCapabilities, ProviderClient
from coworker.server import SessionManager, create_app

BLOCK_SECONDS = 2.5


# 本机回环测试直连：忽略环境 HTTP(S)_PROXY，否则 127.0.0.1 请求被代理转成 502。
def _local_get(url, **kwargs):
    return httpx.get(url, trust_env=False, **kwargs)


def _local_post(url, **kwargs):
    return httpx.post(url, trust_env=False, **kwargs)


class _StubProvider(ProviderClient):
    def complete(self, **kwargs):  # pragma: no cover - engine never completes here
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """真 socket uvicorn（单事件循环）+ 外部可打的 base_url。"""

    def __init__(self, app: FastAPI):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(80):  # 最多等 8s 就绪
            try:
                if _local_get(f"{self.base}/v1/health", timeout=2).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("test server not ready")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _health_stays_fast_while(server: _Server, fire) -> None:
    """判据：fire 打出的阻塞请求在途期间，health 延迟必须 < 1s。"""
    result: dict = {}

    def _fire():
        try:
            r = fire(server.base)
            result["status"] = r.status_code
        except Exception as e:  # pragma: no cover
            result["error"] = repr(e)

    t = threading.Thread(target=_fire, daemon=True)
    t.start()
    time.sleep(0.5)  # 让请求真正进入慢调用
    t0 = time.monotonic()
    r = _local_get(f"{server.base}/v1/health", timeout=BLOCK_SECONDS + 5)
    dt = time.monotonic() - t0
    assert r.status_code == 200
    assert dt < 1.0, (
        f"health 被冻 {dt:.2f}s —— async 端点把同步 LLM 调用跑在了事件循环上"
    )
    t.join(timeout=30)
    assert result.get("status") == 200, f"阻塞请求本身失败: {result}"


def _make_server(tmp_path) -> _Server:
    manager = SessionManager(workspace=tmp_path, provider=_StubProvider())
    return _Server(create_app(manager))


# ---- ① handoff（GET，DeskHome 挂载即调；understanding 补齐带 10min 冷却）----


async def test_handoff_get_llm_does_not_block_loop(tmp_path, monkeypatch):
    import deskmate.proactivity.understanding as understanding

    def slow_backfill(*args, **kwargs):
        time.sleep(BLOCK_SECONDS)

    monkeypatch.setattr(understanding, "maybe_backfill_understanding", slow_backfill)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    server = _make_server(tmp_path)
    try:
        _health_stays_fast_while(
            server, lambda base: _local_get(f"{base}/v1/deskmate/proactive/handoff", timeout=30)
        )
    finally:
        server.stop()


# ---- ② inspire（聊天里聊写作自动触发；creative_suggest 走 chat_memory）----


async def test_inspire_llm_does_not_block_loop(tmp_path, monkeypatch):
    import deskmate.agency.behaviors.creative_suggest as creative_suggest

    def slow_inspire(*args, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return {"triggered": False, "reason": "slow"}

    monkeypatch.setattr(creative_suggest, "try_inspire", slow_inspire)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    server = _make_server(tmp_path)
    try:
        _health_stays_fast_while(
            server,
            lambda base: _local_post(
                f"{base}/v1/deskmate/inspire",
                json={"text": "我在写第三章", "concept": ""},
                timeout=30,
            ),
        )
    finally:
        server.stop()


# ---- ③ moments/comment（用户留言自动触发小桔回复；generate_reply 走 LLM 链）----


async def test_moment_comment_reply_does_not_block_loop(tmp_path, monkeypatch):
    from deskmate.growth.moments import MomentsStore

    def slow_generate_reply(self, text, *args, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return "慢速回复（测试）"

    monkeypatch.setattr(MomentsStore, "generate_reply", slow_generate_reply)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    # 先落一条日记，留言要有目标
    seed = MomentsStore(base_dir=str(tmp_path)).add_user_post("测试日记")
    server = _make_server(tmp_path)
    try:
        _health_stays_fast_while(
            server,
            lambda base: _local_post(
                f"{base}/v1/deskmate/moments/comment",
                json={"moment_id": seed["id"], "author": "me", "text": "写得真好"},
                timeout=30,
            ),
        )
    finally:
        server.stop()
