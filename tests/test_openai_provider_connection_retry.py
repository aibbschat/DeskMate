"""Connection-error resilience for OpenAIProvider (2026-10-26).

DeepSeek / compat gateways silently close idle keep-alive sockets, so a reused pooled
connection (chat a few rounds later) or a long stream can die with a bare
"Connection error." The provider must retry those on a freshly built socket instead of
surfacing them to the user.

These tests are SDK-free (inject a fake client) — they assert the app-layer retry in
`complete()` / `stream()` fires for wire errors and stays out of the way for 4xx/5xx.
"""

from __future__ import annotations

import httpx
from types import SimpleNamespace

import pytest

from coworker.providers import AssistantTurn, OpenAIProvider, StreamChunk, ToolCall


def _response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _chunk(content=None, tool_call=None, finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[tool_call] if tool_call else None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _FlakyCompletions:
    """Raises a wire error on the first create(), succeeds afterwards."""

    def __init__(self, response):
        self._response = response
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("Connection error.")
        return self._response


class _FlakyClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FlakyCompletions(response))


class _FlakyStreamClient:
    """Streaming create() raises a wire error on the first call, streams afterwards."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.calls = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("Connection error.")
        return iter(self._chunks)


def _provider(client):
    """Build a provider whose client-rebuild returns the same (stateful) fake."""
    p = OpenAIProvider(client=client)
    p._ensure_client = lambda: client
    return p


def test_complete_retries_connection_error_on_fresh_socket():
    client = _FlakyClient(_response(content="recovered"))
    turn = _provider(client).complete(
        model="deepseek-v4-pro", messages=[{"role": "user", "content": "hi"}]
    )
    assert isinstance(turn, AssistantTurn)
    assert turn.text == "recovered"
    assert client.chat.completions.calls == 2  # exactly one retry, no blind loop


def test_stream_retries_connection_error_at_create():
    chunks = [_chunk(content="Hel"), _chunk(content="lo"), _chunk(finish="stop")]
    client = _FlakyStreamClient(chunks)
    out = list(_provider(client).stream(model="deepseek-v4-pro", messages=[]))
    assert [c.text_delta for c in out if c.text_delta] == ["Hel", "lo"]
    assert out[-1].turn.finish_reason == "stop"
    assert client.calls == 2


def test_stream_retries_when_connection_dies_before_first_token():
    chunks = [_chunk(content="Hel"), _chunk(content="lo"), _chunk(finish="stop")]

    class _DieBeforeToken:
        def __init__(self):
            self.calls = 0

        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:

                def _boom():
                    raise httpx.ConnectError("Connection error.")
                    yield  # pragma: no cover

                return _boom()
            return iter(chunks)

    client = _DieBeforeToken()
    out = list(_provider(client).stream(model="deepseek-v4-pro", messages=[]))
    assert [c.text_delta for c in out if c.text_delta] == ["Hel", "lo"]
    assert client.calls == 2


def test_stream_surfaces_connection_error_after_tokens():
    """A stream that dies AFTER emitting tokens must NOT be silently restarted — the
    engine owns turn-level retry, and restarting would double-deliver output."""

    class _DieAfterToken:
        @property
        def chat(self):
            return self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            def _gen():
                yield _chunk(content="partial")
                raise httpx.ConnectError("Connection error.")

            return _gen()

    client = _DieAfterToken()
    with pytest.raises(httpx.ConnectError):
        list(_provider(client).stream(model="deepseek-v4-pro", messages=[]))


def test_non_connection_error_is_not_retried():
    """A 400 (application error) must surface immediately — only wire errors retry."""

    class _Always400:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            raise RuntimeError("Error code: 400 - context_length_exceeded")

    client = _FlakyClient(_response(content="x"))
    client.chat.completions = _Always400()
    try:
        _provider(client).complete(model="deepseek-v4-pro", messages=[])
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert client.chat.completions.calls == 1  # no blind retry
