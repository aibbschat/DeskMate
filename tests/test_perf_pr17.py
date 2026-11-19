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
