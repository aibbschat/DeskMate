"""全 provider 停摆看门狗 + SDK 超时（2026-11-19 PR17 / #34）。

PR2 只覆盖了 OpenAI 出口的 pump 线程；其余通道的 SDK 默认 read timeout 长达
600s，停摆不抛异常、paid_fallback 无从降级。这组用例验证：
1. engine 消费端统一 wait 超时 —— 一处覆盖所有 provider（首字 / idle 两阶段）；
2. 各 provider 建 SDK 客户端时确实传了 timeout（否则停摆线程不会释放）；
3. 阈值与总开关可回滚。
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from coworker.engine import TurnEngine
from coworker.providers import timeouts
from coworker.providers.errors import FirstTokenTimeout, StreamIdleTimeout


# --- ① engine 消费端统一看门狗 ------------------------------------------------


class _FakeEngine:
    """只喂 `_astream` 需要的字段，避免构造完整 Engine。"""

    messages = [{"role": "user", "content": "x"}]
    model = "m"
    model_settings: dict = {}
    registry = SimpleNamespace(schemas=lambda: [])

    def __init__(self, stream_fn):
        self._cancel = asyncio.Event()
        self._produce_abort = asyncio.Event()
        self.provider = SimpleNamespace(stream=stream_fn)

    def _outbound_messages(self, snapshot):
        return snapshot


async def _drain(engine):
    async for _ in TurnEngine._astream(engine):
        pass


def test_consumer_first_token_timeout(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.05")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "1")

    def stalled(**_kw):
        time.sleep(0.4)
        yield SimpleNamespace(text_delta="late")

    engine = _FakeEngine(stalled)
    t0 = time.perf_counter()
    with pytest.raises(FirstTokenTimeout) as ei:
        asyncio.run(_drain(engine))
    assert time.perf_counter() - t0 < 0.3
    assert "timeout" in str(ei.value)


def test_consumer_idle_timeout_after_first_chunk(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "1")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "0.05")

    def stalled(**_kw):
        yield SimpleNamespace(text_delta="a")
        time.sleep(0.4)
        yield SimpleNamespace(text_delta="b")

    with pytest.raises(StreamIdleTimeout):
        asyncio.run(_drain(_FakeEngine(stalled)))


def test_consumer_watchdog_off_waits(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "0")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.05")

    def slow(**_kw):
        time.sleep(0.2)
        yield SimpleNamespace(text_delta="ok")

    # 关闭后不设超时：0.05s 的阈值不该打断 0.2s 的慢流。
    asyncio.run(_drain(_FakeEngine(slow)))


def test_consumer_normal_stream_passes_through(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "1")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "1")

    def ok(**_kw):
        yield SimpleNamespace(text_delta="a")
        yield SimpleNamespace(text_delta="b")

    async def collect():
        out = []
        async for x in TurnEngine._astream(_FakeEngine(ok)):
            out.append(x)
        return out

    assert [c.text_delta for c in asyncio.run(collect())] == ["a", "b"]


# --- ② SDK 客户端显式 timeout -------------------------------------------------


def test_anthropic_client_gets_timeout(monkeypatch):
    captured: dict = {}

    class FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    from coworker.providers.anthropic_provider import AnthropicProvider

    AnthropicProvider(api_key="k")._ensure_client()
    assert captured.get("timeout") == timeouts.sdk_read_timeout() == 300.0


def test_openai_responses_client_gets_timeout(monkeypatch):
    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    from coworker.providers.openai_responses import OpenAIResponsesProvider

    OpenAIResponsesProvider(api_key="k")._ensure_client()
    assert captured.get("timeout") == 300.0


def test_gemini_client_gets_http_timeout(monkeypatch):
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    from google import genai

    monkeypatch.setattr(genai, "Client", FakeClient)
    from coworker.providers.gemini_provider import GeminiProvider

    GeminiProvider(api_key="k")._ensure_client()
    opts = captured.get("http_options")
    assert opts is not None and opts.timeout == 300_000


# --- ③ 阈值与开关 -------------------------------------------------------------


def test_sdk_timeout_rollback_to_sdk_default(monkeypatch):
    monkeypatch.setenv("COWORKER_SDK_READ_TIMEOUT", "0")
    assert timeouts.sdk_read_timeout() is None


def test_thresholds_env_and_defaults(monkeypatch):
    monkeypatch.delenv("COWORKER_FIRST_TOKEN_TIMEOUT", raising=False)
    monkeypatch.delenv("COWORKER_STREAM_IDLE_TIMEOUT", raising=False)
    assert timeouts.first_token_timeout() == 75.0
    assert timeouts.stream_idle_timeout() == 120.0
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "12.5")
    assert timeouts.first_token_timeout() == 12.5
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "junk")
    assert timeouts.first_token_timeout() == 75.0


# --- ④ 2026-11-20 事故修复：provider 池饱和冤杀 -------------------------------


def test_provider_pool_size_default_env_and_floor(monkeypatch):
    monkeypatch.delenv("COWORKER_PROVIDER_POOL_SIZE", raising=False)
    assert timeouts.provider_pool_size() == 24
    monkeypatch.setenv("COWORKER_PROVIDER_POOL_SIZE", "40")
    assert timeouts.provider_pool_size() == 40
    monkeypatch.setenv("COWORKER_PROVIDER_POOL_SIZE", "2")  # 误配不低于 8
    assert timeouts.provider_pool_size() == 8
    monkeypatch.setenv("COWORKER_PROVIDER_POOL_SIZE", "junk")
    assert timeouts.provider_pool_size() == 24


def test_engine_pool_uses_configured_size():
    from coworker import engine as engine_mod

    assert engine_mod._PROVIDER_POOL._max_workers == timeouts.provider_pool_size() >= 8


def test_watchdog_aborts_produce_of_abandoned_attempt(monkeypatch):
    """首字超时后，produce 必须在下个 chunk 边界退出，而不是把幽灵流流完。

    2026-11-20 事故：超时只丢消费端，被放弃的 produce 占着 8 工人池里的线程
    流完数分钟的长响应；池满后后续请求排队、被看门狗逐个冤杀（全通道
    stalled upstream 且 llm_usage 零记录）。
    """
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.05")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "1")

    exited = asyncio.Event()
    chunks_seen: list[str] = []

    def ghost(**_kw):
        try:
            time.sleep(0.3)  # 首字迟迟不来 → 消费端 0.05s 看门狗先触发
            for i in range(200):
                chunks_seen.append(str(i))
                yield SimpleNamespace(text_delta=str(i))
                time.sleep(0.02)
        finally:
            # produce 的 break 会关掉生成器（GeneratorExit 走 finally）
            exited.set()

    engine = _FakeEngine(ghost)
    with pytest.raises(FirstTokenTimeout):
        asyncio.run(_drain(engine))
    # 消费端立刻报超时退出；produce 在下个 chunk 边界检测到 abort 并退出。
    # engine 上的 abort 事件已置位（供 produce 线程读取）
    assert engine._produce_abort.is_set()
    # produce 不会把 200 个 chunk 流完
    assert len(chunks_seen) < 200
    # 等待 produce 线程真正退出（生成器 finally 触发）
    async def wait_exit():
        await asyncio.wait_for(exited.wait(), timeout=2)
    asyncio.run(wait_exit())


def test_watchdog_abort_does_not_poison_next_attempt(monkeypatch):
    """同轮降级重试（下一次 _astream 调用）不受上次 abort 影响。"""
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.05")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "1")

    def stalled(**_kw):
        time.sleep(0.3)
        yield SimpleNamespace(text_delta="late")

    engine = _FakeEngine(stalled)
    with pytest.raises(FirstTokenTimeout):
        asyncio.run(_drain(engine))
    assert engine._produce_abort.is_set()

    # 第二次尝试（降级重试）：流正常通过 —— abort 已被清零。
    def ok(**_kw):
        yield SimpleNamespace(text_delta="good")

    engine.provider = SimpleNamespace(stream=ok)

    async def collect():
        out = []
        async for x in TurnEngine._astream(engine):
            out.append(x)
        return out

    assert [c.text_delta for c in asyncio.run(collect())] == ["good"]
