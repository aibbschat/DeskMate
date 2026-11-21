"""吞吐隔离回归锁（2026-10-13 第二起事故：信箱操作频繁 → 全站面板空）。

事故：用户频繁处理信箱（标记已读/删除）→ 面板全空、过一会儿自动恢复；
早上写日记也复现同款。此时事件循环 8 处同步 LLM 已修（to_thread 化），
仍堵——说明堵点不是（只是）事件循环。

机制链（熔断表 12:37:41 local 通道熔断记录为证）：
1. GET handoff 里 `await maybe_backfill_understanding`（10min 冷却到期时真跑
   LLM）→ 付费组全半开 → 逐个真打撞墙 12-75s → **handoff 请求挂起**；
2. WebView 对同 host 的 HTTP/1.1 并发连接上限 6——挂起的 LLM 请求把连接占满；
3. 面板数据 GET（勋章/日记/花园/个人中心）排在连接池后面 → 全空；
4. LLM 链走完连接释放 → 自动恢复。

修复（本文件锁两条）：
A. handoff 的 understanding 补齐改 `_spawn_llm`（fire-and-forget 专用池）——
   信箱永远秒回，连接立即释放；
B. LLM 长任务全部走 `LLM_EXECUTOR`（2 工人专用池），与 to_thread 默认池隔离——
   防线程池饥饿型拥堵（数据端点若未来走 to_thread 也不被 LLM 挤）。

拓扑：与 test_event_loop_not_blocked.py 同——真 socket uvicorn 单事件循环，
外部线程打请求（同 loop 的假绿坑见该文件注释）。
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

SLOW_SECONDS = 3.0


# 本机回环测试直连：忽略环境 HTTP(S)_PROXY，否则 127.0.0.1 请求被代理转成 502。
def _local_get(url, **kwargs):
    return httpx.get(url, trust_env=False, **kwargs)


def _local_post(url, **kwargs):
    return httpx.post(url, trust_env=False, **kwargs)


class _StubProvider(ProviderClient):
    def complete(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def capabilities(self, model):
        return ModelCapabilities()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app: FastAPI):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        for _ in range(80):
            try:
                if _local_get(f"{self.base}/v1/health", timeout=2).status_code == 200:
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("test server not ready")

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _make_server(tmp_path) -> _Server:
    manager = SessionManager(workspace=tmp_path, provider=_StubProvider())
    return _Server(create_app(manager))


# ---- A. handoff 秒回（understanding 补齐慢也不许拖住取件）----


async def test_handoff_fast_when_backfill_slow(tmp_path, monkeypatch):
    """understanding 补齐 LLM 撞墙（sleep 3s）时，GET handoff 必须 <1.5s 返回。

    旧行为（await to_thread 补齐）：handoff 挂到补齐完成（≈3s+），
    WebView 连接被占满 → 全站面板空。新行为（_spawn_llm 后台补齐）：秒回。
    """
    import deskmate.proactivity.understanding as understanding

    def slow_backfill(*args, **kwargs):
        time.sleep(SLOW_SECONDS)

    monkeypatch.setattr(understanding, "maybe_backfill_understanding", slow_backfill)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    server = _make_server(tmp_path)
    try:
        t0 = time.monotonic()
        r = _local_get(f"{server.base}/v1/deskmate/proactive/handoff", timeout=30)
        dt = time.monotonic() - t0
        assert r.status_code == 200
        assert dt < 1.5, (
            f"handoff 被补齐拖住 {dt:.2f}s —— 补齐必须 fire-and-forget，信箱要秒回"
        )
    finally:
        server.stop()


# ---- B. LLM 专用池与数据端点隔离（线程池饥饿防御）----


async def test_llm_pool_isolated_from_data_endpoints(tmp_path, monkeypatch):
    """16 个慢 LLM 请求在途（超过默认池工人），数据端点 GET 必须亚秒。

    旧行为（LLM 走 to_thread 默认池）：14 工人被占满 → 走线程的端点排队。
    新行为（run_llm 专用 2 工人池）：LLM 在专用池排队，默认池空闲。
    """
    import deskmate.agency.behaviors.creative_suggest as creative_suggest

    def slow_inspire(*args, **kwargs):
        time.sleep(SLOW_SECONDS)
        return {"triggered": False, "reason": "slow"}

    monkeypatch.setattr(creative_suggest, "try_inspire", slow_inspire)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    server = _make_server(tmp_path)
    try:
        # 并发打 16 个慢 inspire（每个占一个执行器工人 3s）
        def _fire():
            try:
                _local_post(
                    f"{server.base}/v1/deskmate/inspire",
                    json={"text": "压测", "concept": ""},
                    timeout=60,
                )
            except Exception:
                pass

        threads = [threading.Thread(target=_fire, daemon=True) for _ in range(16)]
        for t in threads:
            t.start()
        time.sleep(1.0)  # 让它们进入慢调用

        # 数据端点必须不受影响（diary 纯 async 读文件；moments 走 DB）
        for path in ("/v1/deskmate/diary", "/v1/deskmate/moments?limit=5"):
            t0 = time.monotonic()
            r = _local_get(f"{server.base}{path}", timeout=15)
            dt = time.monotonic() - t0
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert dt < 2.5, f"{path} 被拖慢到 {dt:.2f}s —— LLM 必须隔离在专用池"

        for t in threads:
            t.join(timeout=30)
    finally:
        server.stop()


# ---- C. 数据端点 12s 超时中间件（防白屏挂死）----


async def test_data_endpoint_timeout_returns_504(tmp_path, monkeypatch):
    """数据端点被拖住时返回 504（前端能重试），而不是 200 或连接挂死。

    拥堵两种形态：① 排队型（线程池/连接池）——wait_for 正常计时，~12s 504；
    ② loop 冻结型（同步调用跑在事件循环上）——计时器也冻，等底层完成才 504
    （根治靠 LLM/同步重活出循环，见 test_event_loop_not_blocked 的 8 处锁）。
    本测试用 monkeypatch 把 diary 读锁住 20s 模拟拥堵，断言**拿到 504**；
    耗时不锁形态（loop 冻结型耗时 ≈ 底层时长）。
    """
    from deskmate.growth import diary as diary_mod

    orig_list = diary_mod.DiaryWriter.list_diaries

    def slow_list(self, *args, **kwargs):
        time.sleep(20)
        return orig_list(self, *args, **kwargs)

    monkeypatch.setattr(diary_mod.DiaryWriter, "list_diaries", slow_list)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    server = _make_server(tmp_path)
    try:
        t0 = time.monotonic()
        r = _local_get(f"{server.base}/v1/deskmate/diary", timeout=40)
        dt = time.monotonic() - t0
        assert r.status_code == 504, f"应 504 快速失败，实得 {r.status_code}"
        assert dt < 25, f"不应比底层 20s 更久，实得 {dt:.1f}s"
    finally:
        server.stop()
