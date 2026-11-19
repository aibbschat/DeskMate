"""首字 / idle 看门狗回归（2026-11-19 PR2）。

没有看门狗时，上游卡住只受 SDK read timeout（实测 600s）约束，异常迟迟不来，
`paid_fallback` 无从换通道。这组用例用极短阈值验证：
1. 首字超时抛 FirstTokenTimeout；
2. 流中途 idle 超时抛 StreamIdleTimeout；
3. COWORKER_WATCHDOG=0 恢复旧行为；
4. 看门狗异常会被付费组回退链接住并在吐字前换通道。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from coworker.providers import paid_fallback
from coworker.providers.errors import FirstTokenTimeout, StreamIdleTimeout
from coworker.providers.openai_provider import OpenAIProvider


def _provider() -> OpenAIProvider:
    # client 是惰性构建的；_stream_impl 被 monkeypatch 掉，不会真的用到它。
    return OpenAIProvider(client=object())


def _messages() -> list[dict]:
    return [{"role": "user", "content": "x"}]


def test_first_token_timeout(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.05")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "1")

    def stalled(_self, **_kw):
        time.sleep(0.4)
        yield SimpleNamespace(text_delta="late")

    monkeypatch.setattr(OpenAIProvider, "_stream_impl", stalled)
    t0 = time.perf_counter()
    with pytest.raises(FirstTokenTimeout) as ei:
        list(_provider().stream(model="m", messages=_messages()))
    assert time.perf_counter() - t0 < 0.3
    assert "timeout" in str(ei.value)


def test_stream_idle_timeout_after_first_chunk(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "1")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "1")
    monkeypatch.setenv("COWORKER_STREAM_IDLE_TIMEOUT", "0.05")

    def stalled(_self, **_kw):
        yield SimpleNamespace(text_delta="a")
        time.sleep(0.4)
        yield SimpleNamespace(text_delta="b")

    monkeypatch.setattr(OpenAIProvider, "_stream_impl", stalled)
    with pytest.raises(StreamIdleTimeout):
        list(_provider().stream(model="m", messages=_messages()))


def test_watchdog_can_be_disabled(monkeypatch):
    monkeypatch.setenv("COWORKER_WATCHDOG", "0")
    monkeypatch.setenv("COWORKER_FIRST_TOKEN_TIMEOUT", "0.01")

    def slow_but_alive(_self, **_kw):
        time.sleep(0.05)
        yield SimpleNamespace(text_delta="ok")

    monkeypatch.setattr(OpenAIProvider, "_stream_impl", slow_but_alive)
    chunks = list(_provider().stream(model="m", messages=_messages()))
    assert chunks and chunks[0].text_delta == "ok"


def test_fallback_switches_channel_on_first_token_timeout(monkeypatch):
    """看门狗异常必须被付费组回退链接住；且只在吐字前换通道。"""
    monkeypatch.setattr(
        paid_fallback, "_filtered_attempts", lambda _requested: ["a:m1", "b:m2"]
    )
    monkeypatch.setattr(paid_fallback, "_note_failure", lambda *a, **kw: None)
    monkeypatch.setattr(paid_fallback, "_note_success", lambda *a, **kw: None)

    attempted = []

    def factory(model):
        attempted.append(model)
        if model == "a:m1":
            raise FirstTokenTimeout(1)
        return iter([SimpleNamespace(text_delta="from backup")])

    chunks = list(paid_fallback.stream_with_fallback(factory, "a:m1"))
    assert attempted == ["a:m1", "b:m2"]
    assert chunks[0].text_delta == "from backup"


def test_fallback_does_not_switch_after_first_chunk(monkeypatch):
    """已有契约：吐过一个 chunk 之后失败（含 idle 超时）原样上抛，不混两条回复。"""
    monkeypatch.setattr(
        paid_fallback, "_filtered_attempts", lambda _requested: ["a:m1", "b:m2"]
    )
    monkeypatch.setattr(paid_fallback, "_note_failure", lambda *a, **kw: None)

    attempted = []

    def factory(model):
        attempted.append(model)

        def gen():
            yield SimpleNamespace(text_delta="first")
            raise StreamIdleTimeout(1)

        return gen()

    with pytest.raises(StreamIdleTimeout):
        list(paid_fallback.stream_with_fallback(factory, "a:m1"))
    assert attempted == ["a:m1"]  # 没有换通道
