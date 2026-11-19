"""OpenAI Chat Completions provider — the compat workhorse.

Uses the OpenAI Python SDK `chat.completions` API only, which is what the entire
OpenAI-compatible world implements: the compat vendors (DeepSeek, Z AI, Kimi, …),
resellers, Ollama, custom endpoints (Azure OpenAI, vLLM), and the Bedrock/Vertex MaaS
paths. Native OpenAI models (the `openai` provider with no custom endpoint) route to
`openai_responses.OpenAIResponsesProvider` instead — Chat Completions rejects function
tools combined with reasoning on GPT-5.6+, so reasoning + tools needs `/v1/responses`.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    StreamChunk,
    TokenUsage,
    ToolCall,
)
from .capabilities import capabilities_for

# --------------------------------------------------------------------------- #
# 风控载荷取证（2026-10-24）：小米 MiMo / 火山 ark 会以 "…considered high risk"
# 瞬时拒绝请求（实测为分钟级自愈的网关级风控窗口，非内容定罪）。被拒时把完整
# 请求载荷落一份本地 JSON，下次窗口出现即可取证「到底是不是内容触发」。
# 只在 high-risk 拒绝时落盘（罕见事件），DESKMATE_RISK_CAPTURE=0 关闭。
# --------------------------------------------------------------------------- #
_RISK_MARKERS = ("high risk", "rejected because", "content_filter", "content filter")
_CAPTURE_KEEP = 20


def _capture_risk_payload(kwargs: dict[str, Any], exc: BaseException) -> None:
    """high-risk 拒绝 → 完整请求载荷（messages+tools）落盘。绝不上抛（取证不能拖垮主链路）。"""
    try:
        if os.environ.get("DESKMATE_RISK_CAPTURE", "") == "0":
            return
        text = str(exc)
        if not any(m in text for m in _RISK_MARKERS):
            return
        base = os.environ.get("DESKMATE_LLM_PAYLOAD_CAPTURE_DIR", "").strip()
        cap_dir = Path(base) if base else Path.home() / ".deskmate" / "llm_payload_capture"
        cap_dir.mkdir(parents=True, exist_ok=True)
        import time as _time

        stamp = _time.strftime("%Y%m%d-%H%M%S")
        out = {
            "captured_at": _time.time(),
            "model": kwargs.get("model"),
            "error": text[:500],
            "n_messages": len(kwargs.get("messages") or []),
            "n_tools": len(kwargs.get("tools") or []),
            "messages": kwargs.get("messages"),
            "tools": kwargs.get("tools"),
        }
        (cap_dir / f"risk-{stamp}.json").write_text(
            json.dumps(out, ensure_ascii=False), encoding="utf-8"
        )
        # 只留最近 _CAPTURE_KEEP 份（载荷含完整上下文，单份可能几百 KB）。
        olds = sorted(cap_dir.glob("risk-*.json"))
        for p in olds[: max(0, len(olds) - _CAPTURE_KEEP)]:
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def resolve_api_key(secrets: Any = None) -> Optional[str]:
    """Resolve the OpenAI API key: env `OPENAI_API_KEY` first, else the SecretStore
    `provider:openai` profile (`{api_key}`). Lets a Tauri-launched sidecar — which does NOT
    inherit the shell env — still find a key the user entered in Settings. The value never
    enters the model context; it only configures the SDK client.
    """
    import os

    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    if secrets is not None:
        profile = secrets.get("provider:openai") or {}
        return profile.get("api_key") or None
    return None


# GPT-5.6 (2026-07) defaults reasoning_effort to "medium" server-side, and
# /v1/chat/completions rejects function tools combined with any effort other than
# "none" ("use /v1/responses"). Native OpenAI now routes to the Responses provider,
# but GPT-5.6 can still land here through a custom endpoint (Azure OpenAI serves the
# same wire), so keep pinning effort to none whenever tools ride along on these
# models — and when the API rejects a call with that exact complaint anyway (a future
# generation, an alias we didn't list), retry once at effort none so the user gets a
# working turn instead of a 400.
_EFFORT_ERROR = "function tools with reasoning_effort are not supported"


def _pin_reasoning_effort(kwargs: dict[str, Any]) -> None:
    if kwargs.get("tools") and str(kwargs.get("model", "")).startswith("gpt-5.6"):
        kwargs.setdefault("reasoning_effort", "none")


def _delta_reasoning(obj: Any) -> Optional[str]:
    """Thinking text off a delta/message: `reasoning_content` (DeepSeek, GLM, Kimi, and
    most compat vendors) or `reasoning` (xAI, OpenRouter). Extra response fields survive
    the OpenAI SDK's models (extra="allow"), so plain getattr sees them."""
    value = getattr(obj, "reasoning_content", None) or getattr(obj, "reasoning", None)
    return value if isinstance(value, str) and value else None


def _without_reasoning_hint(settings: dict[str, Any]) -> dict[str, Any]:
    """Drop the provider-agnostic `reasoning="none"` hint (see compaction.summarize_span).
    Chat completions has no such request param — thinking arrives via `reasoning_content`
    and can't be disabled request-side — and strict compat servers 400 on unknown keys."""
    if "reasoning" in settings:
        return {k: v for k, v in settings.items() if k != "reasoning"}
    return settings


def _strip_foreign_sidecars(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop provider-private message sidecars (underscore-prefixed keys, e.g. `_gemini`
    thought signatures — see providers/base.py): they belong to other providers, and the
    OpenAI wire (and its compat servers) rejects unknown message fields."""
    return [
        (
            {k: v for k, v in m.items() if not k.startswith("_")}
            if any(k.startswith("_") for k in m)
            else m
        )
        for m in messages
    ]


_MAX_TOKENS_ERROR = "'max_tokens' is not supported"

# Ceiling, not a spend target — same rationale as the Anthropic provider's default: a
# coworker writing a report ships the whole file inside one tool call's arguments, and
# compat servers left to their OWN defaults cap completions absurdly low (observed
# 2026-08-15: Together defaulted Kimi K3 to ~2k tokens — every ~5KB write truncated).
DEFAULT_MAX_TOKENS = 32000

# Connection-level resilience (2026-10-26). DeepSeek and other gateways close idle
# keep-alive sockets, so a reused pooled connection (chat a few rounds later) or a long
# stream can die with a bare "Connection error." We (a) disable keep-alive on the SDK
# client and (b) retry transient wire failures on a freshly built socket at the app layer.
_CONNECTION_RETRIES = 3


def _env_timeout(name: str, default: float) -> float:
    """2026-11-19 PR2：看门狗阈值（秒）。空串/垃圾值一律回退默认，<=0 表示关闭该项。"""
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _is_retryable_connection_error(exc: BaseException) -> bool:
    """True for transient transport failures worth retrying on a fresh socket.

    These mean "the wire broke" — keep-alive socket closed by the gateway, idle timeout,
    mid-stream reset — NOT a 4xx/5xx application error. The SDK's own ``max_retries`` can
    be exhausted or doesn't cover a stream that dies after it started, so we add an
    app-layer retry that rebuilds the client (discarding any dead pooled connection).
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001
        httpx = None
    if httpx is not None and isinstance(exc, httpx.TransportError):
        return True
    try:
        import openai
    except Exception:  # noqa: BLE001
        openai = None
    if openai is not None and isinstance(exc, openai.APIConnectionError):
        return True
    # Bare "Connection error." is the SDK's message when it can't subclass the failure.
    return "Connection error" in str(exc)


def _param_fix_retry(kwargs: dict[str, Any], exc: Exception) -> dict[str, Any]:
    """Kwargs for the one retry an unsupported-parameter error earns, or re-raise.

    Reasoning-routed OpenAI models reject `max_tokens` outright (they want
    `max_completion_tokens`) — but compat servers (Ollama's /v1) know ONLY
    `max_tokens`, so the swap must happen on rejection, never up front. Same
    contract as the reasoning_effort retry: fix exactly what the server named.
    """
    msg = str(exc).lower()
    if _EFFORT_ERROR in msg and kwargs.get("reasoning_effort") != "none":
        return {**kwargs, "reasoning_effort": "none"}
    if _MAX_TOKENS_ERROR in msg and "max_tokens" in kwargs:
        fixed = dict(kwargs)
        fixed["max_completion_tokens"] = fixed.pop("max_tokens")
        return fixed
    if "stream_options" in msg and "stream_options" in kwargs:
        # Older compat servers don't know the usage opt-in; drop it, lose only metering.
        fixed = dict(kwargs)
        fixed.pop("stream_options")
        return fixed
    if ("max_tokens" in msg or "max_new_tokens" in msg) and "max_tokens" in kwargs:
        # Our 32k default exceeded this model's completion limit (each server words the
        # 400 differently, so no number parsing) — drop the param and retry on the
        # server's own default rather than surfacing the 400. Worst case is exactly
        # yesterday's behavior; best case the server allows far more once asked.
        fixed = dict(kwargs)
        fixed.pop("max_tokens")
        return fixed
    raise exc


def _usage_from(usage: Any) -> Optional[TokenUsage]:
    """chat.completions usage → normalized counts. `prompt_tokens` INCLUDES cached
    tokens, so the cached share is subtracted into `cache_read`; no write-side split
    exists on this API shape."""
    if usage is None:
        return None
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0)
    return TokenUsage(
        input=max(prompt - cached, 0),
        output=int(getattr(usage, "completion_tokens", 0) or 0),
        cache_read=cached,
    )


class OpenAIProvider(ProviderClient):
    def __init__(
        self,
        client: Any = None,
        *,
        default_model: str = "gpt-5.6-sol",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        secrets: Any = None,
        default_headers: Optional[dict[str, str]] = None,
    ):
        # The SDK client is built lazily on first use, NOT at construction. This lets an engine
        # be assembled before any key exists — the desktop app lets you enter the key in Settings
        # *after* launch — and the super-agent engine to be built at startup with no key. The key
        # is resolved at call time: explicit `api_key` → env `OPENAI_API_KEY` → SecretStore. Tests
        # inject a `client` directly, bypassing all of this.
        #
        # `base_url` points the same OpenAI SDK at any OpenAI-compatible endpoint — used by the
        # provider router for Ollama (`http://localhost:11434/v1`, with a placeholder key) and,
        # later, other OpenAI-shaped backends. When None, behavior is identical to stock OpenAI.
        # `default_headers` rides every request (e.g. OpenCode Go 要求第三方 agent 自带
        # 诚实 UA + `x-opencode-session` 会话头，见 registry 的 opencode-go descriptor）。
        self._client = client
        self._api_key = api_key
        self._base_url = base_url
        self._secrets = secrets
        self._default_headers = dict(default_headers or {})
        self.default_model = default_model

    def _ensure_client(self) -> Any:
        if self._client is None:
            # Lazy import so the SDK is only required when actually talking to OpenAI.
            from openai import OpenAI
            import httpx

            key = self._api_key or resolve_api_key(self._secrets)
            if not key:
                raise RuntimeError(
                    "No model API key configured. Set OPENAI_API_KEY in the environment, "
                    "or add your key in Manage → Settings."
                )
            # `Connection: close` → every request opens a fresh TCP/TLS connection and the
            # gateway closes it after the response. This eliminates the whole class of
            # "reused a keep-alive socket the gateway silently closed" failures (the
            # "chat a few rounds then Connection error" symptom). The extra handshake is
            # ~tens of ms — negligible next to LLM latency.
            http_client = httpx.Client(headers={"Connection": "close"})
            kwargs: dict[str, Any] = {
                "api_key": key,
                "max_retries": 2,
                "timeout": httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=30.0),
                "http_client": http_client,
            }
            if self._base_url:
                kwargs["base_url"] = self._base_url
            if self._default_headers:
                kwargs["default_headers"] = self._default_headers
            self._client = OpenAI(**kwargs)
        return self._client

    def _create_with_param_fix(self, client: Any, kwargs: dict[str, Any]) -> Any:
        """chat.completions.create wrapped in the 3x parameter-fix retry (unchanged).

        Returns the SDK response, or raises a non-parameter error untouched so the
        caller's connection-retry loop can decide what to do with it.
        """
        for _ in range(3):
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                kwargs = _param_fix_retry(kwargs, exc)
        else:
            return client.chat.completions.create(**kwargs)

    def _messages_char_len(self, messages: list[dict[str, Any]]) -> int:
        """请求侧字符数（用量账本的代理指标；usage 拿不到时靠它）。"""
        total = 0
        for m in messages or []:
            if isinstance(m, dict):
                c = m.get("content")
                if isinstance(c, str):
                    total += len(c)
        return total

    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        """complete 出口记账（2026-11-16 B1 用量账本）后委托原实现。

        fail-open：账本任何异常在 coworker.llm_usage 内部吞掉，不影响对话。
        """
        from .. import llm_usage

        prompt_chars = self._messages_char_len(messages)
        try:
            turn = self._complete_impl(model=model, messages=messages, tools=tools, **settings)
        except BaseException as exc:
            llm_usage.record(
                model=model,
                provider=self._base_url,
                prompt_chars=prompt_chars,
                status="error",
                error="%s: %s" % (type(exc).__name__, exc),
            )
            raise
        llm_usage.record(
            model=model,
            provider=self._base_url,
            usage=getattr(turn, "usage", None),
            prompt_chars=prompt_chars,
            completion_chars=len(getattr(turn, "text", None) or ""),
        )
        return turn

    def _complete_impl(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _strip_foreign_sidecars(messages),
            **_without_reasoning_hint(settings),
        }
        if tools:
            kwargs["tools"] = tools
        kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        _pin_reasoning_effort(kwargs)

        client = self._ensure_client()
        # Connection-level retry: a fresh socket per attempt (see
        # _is_retryable_connection_error). This is what makes DeepSeek's intermittent
        # "Connection error." self-heal instead of reaching the user.
        last_exc: Optional[BaseException] = None
        for _attempt in range(_CONNECTION_RETRIES):
            try:
                response = self._create_with_param_fix(client, kwargs)
                break
            except Exception as exc:
                if _is_retryable_connection_error(exc):
                    last_exc = exc
                    self._client = None
                    client = self._ensure_client()
                    continue
                _capture_risk_payload(kwargs, exc)
                raise
        else:
            _capture_risk_payload(
                kwargs, last_exc or RuntimeError("connection retries exhausted")
            )
            raise last_exc or RuntimeError("connection retries exhausted")
        choice = response.choices[0]
        message = choice.message
        text = getattr(message, "content", None)
        tool_calls = _parse_tool_calls(getattr(message, "tool_calls", None))
        text, tool_calls = _maybe_salvage_tool_calls(text, tool_calls, tools=tools)
        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
            raw=response,
            reasoning=_delta_reasoning(message),
            usage=_usage_from(getattr(response, "usage", None)),
        )

    def capabilities(self, model: str) -> ModelCapabilities:
        return capabilities_for(model)

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        """stream 出口记账（2026-11-16 B1 用量账本）后委托原生成器。

        成功：以末 chunk 的 AssistantTurn.usage 记账（含增量中途被弃的
        GeneratorExit/中断——那些也是真实花掉的钱，记 status=error）。
        fail-open：账本任何异常在 coworker.llm_usage 内部吞掉，不影响对话。
        """
        from .. import llm_usage

        prompt_chars = self._messages_char_len(messages)
        streamed_chars = 0
        final_usage: Optional[TokenUsage] = None
        final_text_len = 0
        # 2026-11-19 PR0 首字延迟基线：只记时长数字，不记内容；记账失败由 llm_usage fail-open 吞掉。
        _t0 = time.perf_counter()
        _first_ms: Optional[float] = None

        # 2026-11-19 PR2：首字 / idle 看门狗。SDK 的 read timeout 默认 600s，
        # 上游卡住时不会产生异常，回退链无从触发。把底层阻塞迭代放进 pump 线程，
        # 本线程按"距上次 chunk 的剩余预算"取数；超时抛 ProviderStall（消息含
        # timeout ⇒ 熔断器归 transient；paid_fallback 只在吐字前换通道）。
        from .errors import FirstTokenTimeout, StreamIdleTimeout

        _first_timeout = _env_timeout("COWORKER_FIRST_TOKEN_TIMEOUT", 75.0)
        _idle_timeout = _env_timeout("COWORKER_STREAM_IDLE_TIMEOUT", 120.0)
        _watchdog_on = (
            os.environ.get("COWORKER_WATCHDOG", "1") != "0"
            and (_first_timeout > 0 or _idle_timeout > 0)
        )
        _stop: Any = None

        try:
            if not _watchdog_on:
                for chunk in self._stream_impl(
                    model=model, messages=messages, tools=tools, **settings
                ):
                    if _first_ms is None:
                        _first_ms = round((time.perf_counter() - _t0) * 1000, 2)
                    turn = getattr(chunk, "turn", None)
                    if turn is not None:
                        final_usage = getattr(turn, "usage", None)
                        final_text_len = len(getattr(turn, "text", None) or "")
                    delta_text = getattr(chunk, "text_delta", None)
                    if delta_text:
                        streamed_chars += len(delta_text)
                    yield chunk
            else:
                import queue
                import threading

                q: "queue.Queue" = queue.Queue()
                _stop = threading.Event()

                def _pump() -> None:
                    try:
                        for c in self._stream_impl(
                            model=model, messages=messages, tools=tools, **settings
                        ):
                            if _stop.is_set():
                                break
                            q.put(("chunk", c))
                    except BaseException as exc:  # noqa: BLE001 - 原样交给主循环
                        q.put(("error", exc))
                    finally:
                        q.put(("done", None))

                threading.Thread(
                    target=_pump, name="provider-stream-pump", daemon=True
                ).start()
                _last = _t0
                while True:
                    limit = _first_timeout if _first_ms is None else _idle_timeout
                    remaining = limit - (time.perf_counter() - _last)
                    if limit > 0 and remaining <= 0:
                        raise (
                            FirstTokenTimeout(_first_timeout)
                            if _first_ms is None
                            else StreamIdleTimeout(_idle_timeout)
                        )
                    try:
                        kind, payload = q.get(timeout=remaining if limit > 0 else None)
                    except queue.Empty:
                        raise (
                            FirstTokenTimeout(_first_timeout)
                            if _first_ms is None
                            else StreamIdleTimeout(_idle_timeout)
                        )
                    if kind == "chunk":
                        if _first_ms is None:
                            _first_ms = round((time.perf_counter() - _t0) * 1000, 2)
                        _last = time.perf_counter()
                        turn = getattr(payload, "turn", None)
                        if turn is not None:
                            final_usage = getattr(turn, "usage", None)
                            final_text_len = len(getattr(turn, "text", None) or "")
                        delta_text = getattr(payload, "text_delta", None)
                        if delta_text:
                            streamed_chars += len(delta_text)
                        yield payload
                    elif kind == "error":
                        raise payload
                    else:
                        break
        except BaseException as exc:
            if _stop is not None:
                _stop.set()
            llm_usage.record(
                model=model,
                provider=self._base_url,
                prompt_chars=prompt_chars,
                completion_chars=streamed_chars,
                status="error",
                error="%s: %s" % (type(exc).__name__, exc),
                first_chunk_ms=_first_ms,
                stream_ms=round((time.perf_counter() - _t0) * 1000, 2),
            )
            raise
        llm_usage.record(
            model=model,
            provider=self._base_url,
            usage=final_usage,
            prompt_chars=prompt_chars,
            completion_chars=final_text_len or streamed_chars,
            first_chunk_ms=_first_ms,
            stream_ms=round((time.perf_counter() - _t0) * 1000, 2),
        )

    def _stream_impl(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": _strip_foreign_sidecars(messages),
            "stream": True,
            # Usage on the final chunk (empty `choices`). Compat servers that reject
            # the option get a one-shot retry without it (_param_fix_retry).
            "stream_options": {"include_usage": True},
            **_without_reasoning_hint(settings),
        }
        if tools:
            kwargs["tools"] = tools
        kwargs.setdefault("max_tokens", DEFAULT_MAX_TOKENS)
        _pin_reasoning_effort(kwargs)
        client = self._ensure_client()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_accum: dict[int, dict[str, str]] = {}
        finish_reason = None
        usage: Optional[TokenUsage] = None

        # Connection-level retry: a fresh socket per attempt (see
        # _is_retryable_connection_error). A stream that dies BEFORE emitting any token is
        # restarted from scratch; one that dies after tokens is surfaced (never silently
        # doubled) — the engine owns turn-level retry.
        last_exc: Optional[BaseException] = None
        for _attempt in range(_CONNECTION_RETRIES):
            try:
                chunks = self._create_with_param_fix(client, kwargs)
            except Exception as exc:
                if _is_retryable_connection_error(exc):
                    last_exc = exc
                    self._client = None
                    client = self._ensure_client()
                    continue
                _capture_risk_payload(kwargs, exc)
                raise

            emitted = False
            try:
                for chunk in chunks:
                    chunk_usage = _usage_from(getattr(chunk, "usage", None))
                    if chunk_usage is not None:
                        usage = chunk_usage
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        reasoning = _delta_reasoning(delta)
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            emitted = True
                            yield StreamChunk(reasoning_delta=reasoning)
                        content = getattr(delta, "content", None)
                        if content:
                            text_parts.append(content)
                            emitted = True
                            yield StreamChunk(text_delta=content)
                        for tc in getattr(delta, "tool_calls", None) or []:
                            acc = tool_accum.setdefault(
                                getattr(tc, "index", 0), {"id": "", "name": "", "args": ""}
                            )
                            if getattr(tc, "id", None):
                                acc["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    acc["name"] = fn.name
                                if getattr(fn, "arguments", None):
                                    acc["args"] += fn.arguments
                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
            except Exception as exc:
                if emitted or not _is_retryable_connection_error(exc):
                    _capture_risk_payload(kwargs, exc)
                    raise
                # Died before producing any token → restart the whole stream fresh.
                last_exc = exc
                self._client = None
                client = self._ensure_client()
                text_parts = []
                reasoning_parts = []
                tool_accum = {}
                finish_reason = None
                usage = None
                continue
            break  # stream completed cleanly
        else:
            _capture_risk_payload(
                kwargs, last_exc or RuntimeError("connection retries exhausted")
            )
            raise last_exc or RuntimeError("connection retries exhausted")

        tool_calls = []
        for index in sorted(tool_accum):
            acc = tool_accum[index]
            try:
                arguments = json.loads(acc["args"]) if acc["args"] else {}
            except (TypeError, json.JSONDecodeError):
                arguments = {"_raw": acc["args"]}
            tool_calls.append(
                ToolCall(id=acc["id"], name=acc["name"], arguments=arguments)
            )

        text, tool_calls = _maybe_salvage_tool_calls(
            "".join(text_parts) or None, tool_calls, tools=tools
        )
        yield StreamChunk(
            turn=AssistantTurn(
                text=text,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                reasoning="".join(reasoning_parts) or None,
                usage=usage,
            )
        )


def _parse_tool_calls(raw_tool_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for tc in raw_tool_calls or []:
        function = tc.function
        raw_args = getattr(function, "arguments", None)
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except (TypeError, json.JSONDecodeError):
            # Surface unparseable arguments rather than dropping the call; the engine
            # can return a tool-error so the model corrects itself.
            arguments = {"_raw": raw_args}
        calls.append(
            ToolCall(id=getattr(tc, "id", ""), name=function.name, arguments=arguments)
        )
    return calls


# Some OpenAI-compatible backends — notably Ollama for several local models (qwen, etc.) —
# fail to populate the structured `tool_calls` field and instead emit the call as TEXT, in
# wildly varied shapes: a `<tool_call>{…}</tool_call>` block, a bare `{"name","arguments"}` object
# (often mixed in with prose), or a `toolname {args}` / `toolname [args]` shorthand. Our agent
# loop needs structured calls, so we recover them — using the requested tool SCHEMAS to recognize
# tool-name forms and to filter out anything whose name isn't a real tool (no false positives).
# Gated on: tools were requested AND no structured calls came back. Never fires for OpenAI.
_TOOLCALL_OPEN = re.compile(r"<tool_call>\s*", re.IGNORECASE)

# Qwen/Hermes native tool-call template — NOT JSON. The model writes the call as nested XML:
#   <function=write_file><parameter=path>hello.txt</parameter><parameter=content>hi</parameter></function>
# (usually wrapped in <tool_call>…</tool_call>). qwen3-coder emits exactly this, so we parse the
# function/parameter tags directly. Values are taken verbatim (stripped); only no-whitespace JSON
# tokens (numbers, bools, objects/arrays) are coerced, so free-text content stays a string.
_FUNCTION_BLOCK = re.compile(
    r"<function\s*=\s*(?P<name>[^>\s]+)\s*>(?P<body>.*?)</function\s*>",
    re.IGNORECASE | re.DOTALL,
)
_PARAM_BLOCK = re.compile(
    r"<parameter\s*=\s*(?P<key>[^>\s]+)\s*>(?P<val>.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _coerce_param(raw: str) -> Any:
    """Keep free-text verbatim (the common case: file content), but recover real JSON values when
    the whole token is unambiguous JSON (no embedded whitespace) — e.g. `3`, `true`, `{"a":1}`.
    """
    s = raw.strip()
    if s and not any(c.isspace() for c in s):
        v = _loads(s)
        if isinstance(v, (dict, list, int, float, bool)):
            return v
    return s


def _maybe_salvage_tool_calls(
    text: Optional[str],
    tool_calls: list[ToolCall],
    *,
    tools: Optional[list[dict[str, Any]]],
) -> tuple[Optional[str], list[ToolCall]]:
    """If the model returned tool calls as text, convert them. Returns (text, tool_calls):
    on success the salvaged calls replace `tool_calls` and `text` is cleared."""
    if tool_calls or not tools or not text:
        return text, tool_calls
    salvaged = _salvage_tool_calls_from_text(text, tools)
    if salvaged:
        return None, salvaged
    return text, tool_calls


def _tool_index(
    tools: Optional[list[dict[str, Any]]],
) -> tuple[Optional[set[str]], dict[str, Optional[str]]]:
    """(known tool names, {name: sole-parameter-name}) from OpenAI tool schemas. The sole-param
    map lets us map a bare `toolname [args]` to `{param: args}` when a tool has one parameter.
    """
    if not tools:
        return None, {}
    names: set[str] = set()
    single: dict[str, Optional[str]] = {}
    for t in tools:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        names.add(name)
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        if len(props) == 1:
            single[name] = next(iter(props))
        else:
            required = params.get("required") or []
            single[name] = required[0] if len(required) == 1 else None
    return names, single


def _loads(s: str) -> Any:
    try:
        return json.loads(s)
    except (TypeError, json.JSONDecodeError):
        return None


def _extract_balanced(text: str, start: int) -> Optional[str]:
    """Return the balanced `{…}`/`[…]` substring beginning at `text[start]` (string-aware), or
    None if it doesn't close — so nested braces/brackets are handled correctly."""
    open_ch = text[start]
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _iter_top_objects(text: str):
    """Yield balanced `{…}` substrings at brace-depth 0 (array brackets ignored), so embedded
    JSON objects are found even amid surrounding prose."""
    i = 0
    while i < len(text):
        if text[i] == "{":
            sub = _extract_balanced(text, i)
            if sub:
                yield sub
                i += len(sub)
                continue
        i += 1


def _call_from_dict(d: Any, names: Optional[set[str]]) -> Optional[ToolCall]:
    """Build a ToolCall from a `{"name","arguments"}` dict, or None if it isn't one / the name
    isn't a known tool."""
    if not isinstance(d, dict):
        return None
    name = d.get("name")
    if not isinstance(name, str) or not name:
        return None
    if names is not None and name not in names:
        return None
    args = d.get("arguments", d.get("parameters"))
    if args is None:
        args = {}
    if isinstance(args, str):
        args = _loads(args)
        if not isinstance(args, dict):
            args = {"_raw": d.get("arguments")}
    if not isinstance(args, dict):
        args = {"_raw": args}
    return ToolCall(id="", name=name, arguments=args)


def _renumber(calls: list[ToolCall]) -> list[ToolCall]:
    return [
        ToolCall(id=f"call_salvaged_{i}", name=c.name, arguments=c.arguments)
        for i, c in enumerate(calls)
    ]


def _salvage_tool_calls_from_text(
    content: str, tools: Optional[list[dict[str, Any]]] = None
) -> list[ToolCall]:
    """Best-effort recovery of tool calls embedded in assistant text. Tries, in order:
    1. `<tool_call>…</tool_call>` blocks (anywhere, balanced); 2. embedded `{"name","arguments"}`
    objects (even mixed with prose); 3. `toolname {args}` / `toolname [args]` for known tools.
    Returns [] (treat as plain text) when nothing tool-shaped is found."""
    text = (content or "").strip()
    if not text:
        return []
    names, single = _tool_index(tools)

    # 1) <tool_call> … </tool_call> blocks.
    calls: list[ToolCall] = []
    for m in _TOOLCALL_OPEN.finditer(text):
        j = m.end()
        if j < len(text) and text[j] in "{[":
            sub = _extract_balanced(text, j)
            parsed = _loads(sub) if sub else None
            for d in parsed if isinstance(parsed, list) else [parsed]:
                c = _call_from_dict(d, names)
                if c:
                    calls.append(c)
    if calls:
        return _renumber(calls)

    # 1b) Qwen/Hermes XML calls: <function=NAME><parameter=KEY>VAL</parameter>…</function>.
    for fm in _FUNCTION_BLOCK.finditer(text):
        name = fm.group("name").strip()
        if names is not None and name not in names:
            continue
        args = {
            pm.group("key").strip(): _coerce_param(pm.group("val"))
            for pm in _PARAM_BLOCK.finditer(fm.group("body"))
        }
        calls.append(ToolCall(id="", name=name, arguments=args))
    if calls:
        return _renumber(calls)

    # 2) Embedded {"name": …, "arguments": …} objects, even surrounded by prose.
    for sub in _iter_top_objects(text):
        d = _loads(sub)
        if isinstance(d, dict) and "name" in d:
            c = _call_from_dict(d, names)
            if c:
                calls.append(c)
    if calls:
        return _renumber(calls)

    # 3) `toolname {args}` / `toolname [args]` shorthand — only for tools we actually offered.
    if names:
        for name in names:
            for m in re.finditer(re.escape(name) + r"\s*[:=]?\s*", text):
                j = m.end()
                if j >= len(text) or text[j] not in "{[":
                    continue
                sub = _extract_balanced(text, j)
                parsed = _loads(sub) if sub else None
                if parsed is None:
                    continue
                if isinstance(parsed, dict):
                    args = parsed
                else:
                    param = single.get(name)
                    if not param:
                        continue
                    args = {param: parsed}
                calls.append(ToolCall(id="", name=name, arguments=args))
                break  # one salvaged call per tool name
    return _renumber(calls)
