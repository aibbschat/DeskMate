"""ProviderRouter — one `ProviderClient` that dispatches by the `provider:` prefix of a model
string to a per-provider client, built lazily from its SecretStore profile and cached.

This is the single provider the `SessionManager` hands to every engine, so `complete()/stream()`
(which already receive the full model string per-call) route themselves: `ollama:llama3.3` →
the Ollama client (Ollama's OpenAI-compatible `/v1`), bare `gpt-5.5` → the default (OpenAI). The
prefix is stripped before delegating, since the underlying SDKs want the bare model name.

Config changes (a new key, a new Ollama URL) call `invalidate()` to drop cached clients, so
existing engines pick up the change without a rebuild.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

from .base import ProviderClient
from .capabilities import capabilities_for
from .matrix import normalize_model_id
from .registry import build_provider_client, get_descriptor
from .dynamic_route import (
    AUTO_MODEL,
    GROUP_PAID,
    resolve_auto_model,
)


class ProviderRouter(ProviderClient):
    def __init__(
        self,
        secrets: Any = None,
        *,
        default_provider: str = "openai",
        on_use: Any = None,
        dynamic_routing: bool = False,
        auto_route_fn: Any = None,
    ) -> None:
        self._secrets = secrets
        self._default = default_provider
        self._clients: dict[str, ProviderClient] = {}
        self._lock = threading.Lock()
        # Optional callable(provider_name) fired when a completion is dispatched — drives the
        # Settings pane's "Last used" line. Best-effort: its failures never break a model call.
        self._on_use = on_use
        # 动态路由 judge 层（借鉴 PilotDeck）。默认关：仅当会话模型 == "auto" 哨兵且本开关开时，
        # 才按单条消息难度动态选免费组/付费组；否则完全透传，不越权替用户换模型。
        self._dynamic_routing = bool(dynamic_routing)
        # 测试 / 注入用：覆盖默认 resolve_auto_model（避免测试时真的走免费组通道）。
        self._auto_route_fn = auto_route_fn
        # 上一轮动态选的组（free/paid），供短续接消息继承（对齐 PilotDeck isShortContinuation）。
        self._last_dynamic_group: Any = None

    def set_dynamic_routing(self, enabled: bool) -> None:
        """运行时开关（无需重建 router 即可生效）。"""
        self._dynamic_routing = bool(enabled)

    def _resolve_auto(self, model: str, messages: list) -> str:
        """把 ``AUTO_MODEL`` 哨兵解析成具体通道；不介入时原样返回。

        fail-open：解析异常也原样返回 ``model``，绝不卡主流程。
        """
        if model != AUTO_MODEL or not self._dynamic_routing:
            return model
        fn = self._auto_route_fn or resolve_auto_model
        try:
            resolved = fn(
                messages,
                dynamic_enabled=True,
                prev_group=self._last_dynamic_group,
            )
        except Exception:  # noqa: BLE001
            logger.debug("动态路由解析异常，透传原模型", exc_info=True)
            return model
        if resolved:
            self._last_dynamic_group = self._group_of_key(resolved)
            return resolved
        return model

    @staticmethod
    def _group_of_key(key: str) -> Any:
        """由通道 key 推断组（paid/free），用于续接继承；取不到返回 None。"""
        try:
            from .dynamic_route import GROUP_PAID, GROUP_FREE
            from deskmate.llm_channels import PAID_GROUP

            prov = key.split(":", 1)[0]
            return GROUP_PAID if prov in {p for p, _ in PAID_GROUP} else GROUP_FREE
        except Exception:  # noqa: BLE001
            return None

    def _note_use(self, model: str) -> None:
        if self._on_use is None:
            return
        try:
            self._on_use(self._provider_name(model))
        except Exception:
            pass

    # -- routing ----------------------------------------------------------------
    def _provider_name(self, model: str) -> str:
        """The provider for a model: the `prefix` of `prefix:rest` if it's a known provider,
        else the default. (A colon that isn't a known provider — unlikely — falls through.)
        """
        if ":" in model:
            prefix = model.split(":", 1)[0]
            if get_descriptor(prefix) is not None:
                return prefix
        return self._default

    def _client_for(self, model: str) -> ProviderClient:
        name = self._provider_name(model)
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                profile = {}
                if self._secrets is not None:
                    profile = self._secrets.get(f"provider:{name}") or {}
                client = build_provider_client(name, profile, self._secrets)
                self._clients[name] = client
            return client

    @staticmethod
    def _bare(model: str) -> str:
        """Strip a KNOWN provider prefix; the underlying SDK wants the bare model name. A model
        whose first segment isn't a provider (e.g. `qwen2.5-coder:32b` — a version tag, not a
        prefix) is returned unchanged, so the colon isn't mistaken for a provider separator.
        """
        if ":" in model:
            prefix, rest = model.split(":", 1)
            if get_descriptor(prefix) is not None:
                return rest
        return model

    def invalidate(self, name: Optional[str] = None) -> None:
        """Drop cached client(s) so the next call rebuilds with fresh config."""
        with self._lock:
            if name is None:
                self._clients.clear()
            else:
                self._clients.pop(name, None)

    # -- ProviderClient ---------------------------------------------------------
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        # 历史会话/旧 prefs 里的已下线型号，在发出请求前换成厂商指定的在售型号。
        model = normalize_model_id(self._resolve_auto(model, messages))
        self._note_use(model)

        def _call(m: str):
            return self._client_for(m).complete(
                model=self._bare(m), messages=messages, tools=tools, **settings
            )

        # 付费套餐撞额度时按 PAID_GROUP 换下一条（火山 → 腾讯 hy3；小米线 2026-10-10 起暂停排查）。
        # 非付费套餐与免费组在 plan_attempts 里原样返回单通道，行为不变。
        from .paid_fallback import complete_with_fallback

        return complete_with_fallback(_call, model)

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        model = normalize_model_id(self._resolve_auto(model, messages))
        self._note_use(model)

        def _open(m: str):
            return self._client_for(m).stream(
                model=self._bare(m), messages=messages, tools=tools, **settings
            )

        # 流式同样要能换通道。注意此处**不写 return**：stream 必须是惰性生成器，
        # 写成 return ... 会把生成器对象原样返回、丢掉外层的回退代理。
        # stream_with_fallback 内部保证「首个 chunk 之前」可换通道，之后原样上抛。
        from .paid_fallback import stream_with_fallback

        yield from stream_with_fallback(_open, model)

    def capabilities(self, model: str):
        return capabilities_for(model)
