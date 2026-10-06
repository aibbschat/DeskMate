"""P0 gate tests — provider layer. SDK-free (inject a fake OpenAI client)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    OpenAIProvider,
    ToolCall,
    capabilities_for,
)


class _FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response))


def _response(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def test_complete_returns_text():
    client = _FakeClient(_response(content="hello there"))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(
        model="gpt-5.5", messages=[{"role": "user", "content": "hi"}]
    )

    assert isinstance(turn, AssistantTurn)
    assert turn.text == "hello there"
    assert turn.tool_calls == []
    assert turn.has_tool_calls is False
    assert turn.finish_reason == "stop"


def test_complete_parses_tool_calls():
    tc = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(
            name="read_file", arguments=json.dumps({"path": "a.py"})
        ),
    )
    client = _FakeClient(_response(tool_calls=[tc], finish_reason="tool_calls"))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(
        model="gpt-5.5",
        messages=[],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )

    assert turn.has_tool_calls
    assert turn.tool_calls[0] == ToolCall(
        id="call_1", name="read_file", arguments={"path": "a.py"}
    )
    # tools forwarded to the API
    assert "tools" in client.chat.completions.calls[0]


def test_complete_tolerates_bad_tool_args():
    tc = SimpleNamespace(
        id="call_2", function=SimpleNamespace(name="x", arguments="{not json")
    )
    client = _FakeClient(_response(tool_calls=[tc]))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(model="gpt-5.5", messages=[])

    assert turn.tool_calls[0].arguments == {"_raw": "{not json"}


def test_tools_omitted_when_none():
    client = _FakeClient(_response(content="x"))
    provider = OpenAIProvider(client=client)

    provider.complete(model="gpt-5.5", messages=[])

    assert "tools" not in client.chat.completions.calls[0]


def test_settings_forwarded():
    client = _FakeClient(_response(content="x"))
    provider = OpenAIProvider(client=client)

    provider.complete(model="gpt-5.5", messages=[], temperature=0.2)

    assert client.chat.completions.calls[0]["temperature"] == 0.2


def test_capabilities_known_models():
    assert capabilities_for("gpt-5.5").tools is True
    assert capabilities_for("openai:gpt-5.5").vision is True  # provider prefix stripped
    assert capabilities_for("o3-mini").parallel_tool_calls is False
    assert capabilities_for("deepseek-chat").tools is True


def test_capabilities_via_provider():
    provider = OpenAIProvider(client=_FakeClient(_response()))
    caps = provider.capabilities("gpt-5.5")
    assert isinstance(caps, ModelCapabilities)
    assert caps.tools is True


# -- GPT-5.6 tools + reasoning_effort on chat/completions (owner repro 2026-07-14) ----
# The API defaults these models to effort "medium" and then rejects function tools:
# "Function tools with reasoning_effort are not supported for gpt-5.6-sol in
# /v1/chat/completions. To use function tools, use /v1/responses or set
# reasoning_effort to 'none'." Until we speak the Responses API, we pin effort none.

_TOOLS = [{"type": "function", "function": {"name": "read_file"}}]
_EFFORT_400 = (
    "Error code: 400 - {'error': {'message': \"Function tools with reasoning_effort "
    "are not supported for %s in /v1/chat/completions. To use function tools, use "
    "/v1/responses or set reasoning_effort to 'none'.\", 'type': "
    "'invalid_request_error', 'param': 'reasoning_effort', 'code': None}}"
)


def test_gpt56_tools_pin_reasoning_effort_none():
    client = _FakeClient(_response(content="x"))
    provider = OpenAIProvider(client=client)
    calls = client.chat.completions.calls

    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        provider.complete(model=model, messages=[], tools=_TOOLS)
    assert [c["reasoning_effort"] for c in calls] == ["none"] * 3

    # an explicit caller choice is respected on the first attempt
    provider.complete(
        model="gpt-5.6-sol", messages=[], tools=_TOOLS, reasoning_effort="low"
    )
    assert calls[3]["reasoning_effort"] == "low"

    # no tools, or another model → the request is untouched
    provider.complete(model="gpt-5.6-sol", messages=[])
    provider.complete(model="gpt-5.5", messages=[], tools=_TOOLS)
    assert "reasoning_effort" not in calls[4] and "reasoning_effort" not in calls[5]


class _EffortRejectingCompletions:
    """Behaves like the live API: tools + any effort other than 'none' → the 400."""

    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools") and kwargs.get("reasoning_effort") != "none":
            raise RuntimeError(_EFFORT_400 % kwargs["model"])
        if kwargs.get("stream"):
            return iter([_chunk(content="ok"), _chunk(finish="stop")])
        return self._response


def test_effort_400_from_an_unpinned_model_retries_once_at_none():
    # a hypothetical next generation we haven't listed yet — proactive pin misses it
    client = _FakeClient(_response(content="x"))
    client.chat.completions = _EffortRejectingCompletions(_response(content="x"))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(model="gpt-5.7-sol", messages=[], tools=_TOOLS)
    calls = client.chat.completions.calls
    assert turn.text == "x" and len(calls) == 2
    assert "reasoning_effort" not in calls[0] and calls[1]["reasoning_effort"] == "none"

    # streaming path retries the same way
    out = list(provider.stream(model="gpt-5.7-sol", messages=[], tools=_TOOLS))
    assert out[-1].turn.text == "ok" and len(client.chat.completions.calls) == 4


def test_max_tokens_rejection_retries_as_max_completion_tokens():
    """Reasoning-routed models 400 on max_tokens (want max_completion_tokens); compat
    servers know only max_tokens — so the swap happens on rejection, never up front.
    (Owner-hit 2026-07-20: the auto-title call silently no-oped on gpt-5.6-sol.)"""

    class _MaxTokensRejecting:
        def __init__(self, response):
            self._response = response
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "max_tokens" in kwargs:
                raise RuntimeError(
                    "Error code: 400 - Unsupported parameter: 'max_tokens' is not "
                    "supported with this model. Use 'max_completion_tokens' instead."
                )
            return self._response

    client = _FakeClient(_response(content="Jira vs Linear"))
    client.chat.completions = _MaxTokensRejecting(_response(content="Jira vs Linear"))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(model="gpt-5.6-sol", messages=[], max_tokens=64)
    calls = client.chat.completions.calls
    assert turn.text == "Jira vs Linear" and len(calls) == 2
    assert calls[0]["max_tokens"] == 64
    assert "max_tokens" not in calls[1] and calls[1]["max_completion_tokens"] == 64


def test_unrelated_400s_are_not_retried():
    class _AlwaysRejects:
        calls: list = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("Error code: 400 - context_length_exceeded")

    client = _FakeClient(_response(content="x"))
    client.chat.completions = _AlwaysRejects()
    provider = OpenAIProvider(client=client)
    try:
        provider.complete(model="gpt-5.5", messages=[], tools=_TOOLS)
        raise AssertionError("should have raised")
    except RuntimeError:
        pass
    assert len(client.chat.completions.calls) == 1  # no blind second attempt


# -- streaming ------------------------------------------------------------------


def _chunk(content=None, tool_call=None, finish=None):
    delta = SimpleNamespace(
        content=content, tool_calls=[tool_call] if tool_call else None
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _StreamClient:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: iter(chunks))
        )


def test_stream_text_deltas():
    chunks = [_chunk(content="Hel"), _chunk(content="lo"), _chunk(finish="stop")]
    provider = OpenAIProvider(client=_StreamClient(chunks))
    out = list(provider.stream(model="gpt-5.5", messages=[]))
    assert [c.text_delta for c in out if c.text_delta] == ["Hel", "lo"]
    assert out[-1].turn.text == "Hello"
    assert out[-1].turn.finish_reason == "stop"


def test_stream_accumulates_tool_calls():
    tc1 = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments='{"pa'),
    )
    tc2 = SimpleNamespace(
        index=0, id=None, function=SimpleNamespace(name=None, arguments='th": "a.py"}')
    )
    chunks = [_chunk(tool_call=tc1), _chunk(tool_call=tc2), _chunk(finish="tool_calls")]
    provider = OpenAIProvider(client=_StreamClient(chunks))
    turn = list(provider.stream(model="gpt-5.5", messages=[]))[-1].turn
    assert turn.tool_calls[0] == ToolCall(
        id="call_1", name="read_file", arguments={"path": "a.py"}
    )


# -- OpenAI-compatible vendor providers (Z AI, DeepSeek, Kimi, MiniMax, Qwen, xAI, Mistral) ------

COMPAT_VENDORS = {
    "zai": "https://api.z.ai/api/paas/v4",
    "deepseek": "https://api.deepseek.com",
    "kimi": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimax.io/v1",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
}


def test_compat_vendor_descriptors_ship_prefilled_endpoints():
    from coworker.providers.registry import get_descriptor

    for name, endpoint in COMPAT_VENDORS.items():
        d = get_descriptor(name)
        assert d is not None and d.needs_key, name
        base = next(f for f in d.fields if f.key == "base_url")
        assert base.default == endpoint  # prefilled, editable
        assert not base.required  # blank falls back to the default in the builder
        assert "OpenAI-compatible" in d.blurb
        assert d.env_key and d.recommended_model


def test_compat_builder_defaults_and_profile_override(monkeypatch):
    from coworker.providers.registry import build_provider_client

    p = build_provider_client("zai", {"api_key": "zk"}, None)
    assert p._base_url == COMPAT_VENDORS["zai"]
    assert p._api_key == "zk"

    override = "https://open.bigmodel.cn/api/paas/v4"
    p2 = build_provider_client("zai", {"api_key": "zk", "base_url": override}, None)
    assert p2._base_url == override


def test_compat_builder_env_key_fallback(monkeypatch):
    from coworker.providers.registry import build_provider_client

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    p = build_provider_client("deepseek", {}, None)
    assert p._api_key == "ds-key"
    assert p._base_url == COMPAT_VENDORS["deepseek"]


def test_compat_builder_never_leaks_the_openai_key(monkeypatch):
    """A configured OPENAI_API_KEY must never be sent to a different vendor's endpoint —
    a missing vendor key fails fast with a vendor-named error instead."""
    import pytest

    from coworker.providers.registry import build_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-real")
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Kimi"):
        build_provider_client("kimi", {}, None)


ARK_RESPONSES_VENDORS = {
    "ark": {
        "base_url": "https://ark.ap-southeast.bytepluses.com/api/v3",
        "env_key": "ARK_API_KEY",
        "recommended_model": "dola-seed-evolving-latest-version",
        "reasoning_summary": False,
    },
    "ark-agent-plan-cn": {
        "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "env_key": "ARK_AGENT_PLAN_CN_API_KEY",
        "recommended_model": "doubao-seed-evolving",
        "reasoning_summary": True,
    },
}


def test_ark_responses_descriptors_are_separate():
    from coworker.providers.registry import get_descriptor

    for name, expected in ARK_RESPONSES_VENDORS.items():
        d = get_descriptor(name)
        assert d is not None and d.needs_key, name
        assert d.env_key == expected["env_key"]
        assert d.recommended_model == expected["recommended_model"]
        assert "Responses API" in d.blurb
        base = next(f for f in d.fields if f.key == "base_url")
        assert base.default == expected["base_url"]
        assert not base.required


def test_ark_responses_builder_capabilities_PathsUnchanged(monkeypatch):
    from coworker.providers.openai_responses import OpenAIResponsesProvider
    from coworker.providers.registry import build_provider_client

    monkeypatch.setenv("ARK_AGENT_PLAN_CN_API_KEY", "plan-key")
    bp = build_provider_client("ark", {"api_key": "bp-key"}, None)
    plan = build_provider_client("ark-agent-plan-cn", {}, None)

    assert isinstance(bp, OpenAIResponsesProvider)
    assert (bp._api_key, bp._base_url) == (
        "bp-key",
        ARK_RESPONSES_VENDORS["ark"]["base_url"],
    )
    assert isinstance(plan, OpenAIResponsesProvider)
    assert (plan._api_key, plan._base_url) == (
        "plan-key",
        ARK_RESPONSES_VENDORS["ark-agent-plan-cn"]["base_url"],
    )
    assert bp._reasoning_summary is ARK_RESPONSES_VENDORS["ark"]["reasoning_summary"]
    assert plan._reasoning_summary is ARK_RESPONSES_VENDORS["ark-agent-plan-cn"][
        "reasoning_summary"
    ]


def test_ark_responses_never_leak_the_openai_key(monkeypatch):
    import pytest

    from coworker.providers.registry import build_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-real")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BytePlus Ark"):
        build_provider_client("ark", {}, None)


def test_existing_chat_compat_paths_unchanged():
    """Lockdown: adding Responses vendors must not migrate existing compat providers."""
    from coworker.providers.registry import build_provider_client

    provider = build_provider_client("deepseek", {"api_key": "ds-key"}, None)
    assert isinstance(provider, OpenAIProvider)
    assert provider._base_url == COMPAT_VENDORS["deepseek"]


def test_ark_curated_models_are_strict_allowlists():
    from coworker.providers.matrix import models_for_provider

    assert models_for_provider("ark") == [
        "dola-seed-evolving-latest-version",
        "dola-seed-2-1-turbo-260628",
    ]
    # 火山 Agent Plan（主力套餐）的可用清单。这条的价值就在"精确" —— 矩阵里增删任何一个
    # 都必须同步改这里，防止清单被静默改动。2026-10-02 按控制台实际可用模型补全（原断言
    # 停在 2 个 doubao，漏掉了随后接入的 6 个：doubao-2.0-mini/lite、deepseek-v4 两档、
    # minimax-m3、kimi-k2.7-code）。
    assert models_for_provider("ark-agent-plan-cn") == [
        "doubao-seed-evolving",
        "doubao-seed-2.1-turbo",
        "doubao-seed-2.0-mini",
        "doubao-seed-2.0-lite",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "minimax-m3",
        "kimi-k2.7-code",
    ]


def test_ark_models_route_and_get_verified_agent_capabilities(monkeypatch):
    from coworker.providers import vision_overrides
    from coworker.providers.router import ProviderRouter

    # 这条锁的是**矩阵内置声明**（文本/agent 模型一律 vision=False），不是用户的视觉声明。
    # 显式清空「用户声明的视觉模型」层：本机 ~/.deskmate/settings.json 恰好把
    # ark-agent-plan-cn:doubao-seed-2.1-turbo 声明成了视觉模型，不钉住的话这条断言会随
    # 开发者的桌面配置飘红（2026-10-02 前正是如此恒红）。路径解析的根因已在
    # vision_overrides._settings_path() 修掉，这里再钉一层，保证断言不依赖外部状态。
    monkeypatch.setattr(vision_overrides, "vision_override_ids", lambda: frozenset())

    models = (
        "ark:dola-seed-evolving-latest-version",
        "ark:dola-seed-2-1-turbo-260628",
        "ark-agent-plan-cn:doubao-seed-evolving",
        "ark-agent-plan-cn:doubao-seed-2.1-turbo",
    )
    router = ProviderRouter.__new__(ProviderRouter)
    for model in models:
        prefix, bare = model.split(":", 1)
        assert router._provider_name(model) == prefix
        assert ProviderRouter._bare(model) == bare
        caps = capabilities_for(model)
        assert caps.tools and caps.parallel_tool_calls and caps.streaming
        assert not caps.vision


def test_ark_recommended_models_are_curated():
    from coworker.providers.matrix import models_for_provider
    from coworker.providers.registry import get_descriptor

    for name in ARK_RESPONSES_VENDORS:
        d = get_descriptor(name)
        assert d.recommended_model in models_for_provider(name)


def test_compat_models_route_and_get_tool_capabilities():
    from coworker.providers.router import ProviderRouter

    router = ProviderRouter.__new__(
        ProviderRouter
    )  # only using _provider_name (stateless)
    for model in (
        "zai:glm-5.2",
        "deepseek:deepseek-v4-flash",
        "kimi:kimi-k2.6",
        "minimax:MiniMax-M2.5",
        "qwen:qwen3-max",
        "xai:grok-4.3",
        "mistral:mistral-large-latest",
    ):
        prefix = model.split(":", 1)[0]
        assert router._provider_name(model) == prefix
        assert ProviderRouter._bare(model) == model.split(":", 1)[1]
        caps = capabilities_for(model)
        assert caps.tools and caps.streaming


def test_compat_recommended_models_are_in_the_suggested_lists():
    """set_provider only auto-adds the recommended model if it's in _suggested_models —
    keep the registry and the manager's COMPAT_MODELS table in lockstep."""
    from coworker.providers.registry import get_descriptor
    from coworker.server.manager import SessionManager

    for name in COMPAT_VENDORS:
        d = get_descriptor(name)
        assert d.recommended_model in SessionManager.COMPAT_MODELS[name], name


# -- curated model matrix (labels + capabilities by full routed id) -----------------


def test_matrix_answers_capabilities_for_reseller_ids():
    """Reseller ids ('together:zai-org/GLM-5.2') defeat the name-prefix heuristics — the
    matrix must answer them exactly, with tool calling on.

    OpenRouter rows now come from the live catalog; ``fetch_catalog`` falls back to the
    offline seed when the network is unreachable, and the seed ships ``minimax-m3:free``,
    so the assertion stays deterministic without network. The catalog honestly reports
    ``parallel_tool_calls=False`` for most `:free` models (only ``tool_choice`` is
    advertised), so the OpenRouter assertion checks tools+streaming, not parallelism."""
    from coworker.providers.openrouter_catalog import fetch_catalog

    fetch_catalog()
    for mid in (
        "together:zai-org/GLM-5.2",
        "together:meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
        "fireworks:accounts/fireworks/models/kimi-k2p6",
        "openrouter:minimax/minimax-m3:free",
    ):
        caps = capabilities_for(mid)
        if mid.startswith("openrouter:"):
            assert caps.tools and caps.streaming
        else:
            assert caps.tools and caps.parallel_tool_calls and caps.streaming


def test_openrouter_free_tier_is_allowlisted():
    """免费档只放白名单内实测可用的模型，杜绝一堆声明免费却用不了的占满 picker。"""
    from coworker.providers.openrouter_catalog import (
        get_openrouter_entries,
        _free_tier_allowed,
    )

    entries = get_openrouter_entries()
    free_ids = [k for k in entries if k.endswith(":free")]
    # 免费档每一个都必须落在白名单内
    for k in free_ids:
        assert _free_tier_allowed(k[len("openrouter:"):]), f"免费档漏网非白名单: {k}"
    # 实测不可用的必须消失（含 nemotron-3.5-content-safety 这个非对话分类器）
    for bad in (
        "openrouter:z-ai/glm-5.2:free",
        "openrouter:google/gemma-4-31b-it:free",
        "openrouter:thinkingmachines/inkling:free",
        "openrouter:thinkingmachines/inkling-small:free",
        "openrouter:nvidia/nemotron-3.5-content-safety:free",
    ):
        assert bad not in entries, f"应已排除却仍在: {bad}"
    # 实测可用的必须在
    for good in (
        "openrouter:minimax/minimax-m3:free",
        "openrouter:minimax/minimax-m2.7:free",
        "openrouter:cohere/north-mini-code:free",
        "openrouter:nvidia/nemotron-3.5-lightning:free",
    ):
        assert good in entries, f"实测可用却丢失: {good}"


def test_matrix_labels_and_custom_model_fallback():
    from coworker.providers.matrix import MATRIX, model_labels

    labels = model_labels()
    assert labels["together:zai-org/GLM-5.2"] == "GLM-5.2 · via Together"
    assert labels["zai:glm-5.2"] == "GLM-5.2 · Z AI"
    # Curated agent-capable models only (owner call, 2026-07-04). The keyless free
    # gateways (OmniRoute / OpenCode) are intentionally tool-less drafting/peer helpers
    # and are excluded from the "every entry supports tools" contract.
    # ⚠ 数量断言是**防无节制膨胀**的护栏，不是"矩阵就只有这么多"的承诺：2026-07-04 定的是
    # <60，此后陆续接入火山 Agent Plan、腾讯 Token Plan、Bedrock、Vertex 等条目 → 66
    # （2026-10-02）。顶到阈值时先自问"这个 provider 该不该进矩阵"，再上调阈值并写明日期；
    # 不要默默删掉这条 —— 逐条锁住的其实是下面那行 all(caps.tools)。
    _free = lambda k: k.split(":", 1)[0] in ("omniroute", "opencode")
    _agentic = {k: e for k, e in MATRIX.items() if not _free(k)}
    assert len(_agentic) < 80
    assert all(e.caps.tools for e in _agentic.values())
    # A custom (unlisted) reseller model falls back to the conservative default — usable,
    # but at the user's own risk (no parallel tool calls assumed).
    caps = capabilities_for("together:some-org/Brand-New-Model")
    assert caps.tools and not caps.parallel_tool_calls


def test_opencode_free_models_curated_after_protocol_port():
    """2026-10-06：按 opencode2dsh/dsh-plugin 移植线协议后重新铺货（2026-08-26 曾因裸
    OpenAI 请求被网关拒绝而撤货，根因是缺 CLI 伪装 + bash/read 门面）。契约：curated
    集合非空、全部 *-free、descriptor 的 recommended_model 在其中（set_provider 自动
    添加依赖它）、descriptor 免密钥可路由。"""
    from coworker.providers.matrix import MATRIX, models_for_provider
    from coworker.providers.registry import get_descriptor

    entries = models_for_provider("opencode")
    assert entries, "opencode 重新铺货后 curated 集合不应为空"
    assert all(e.endswith("-free") for e in entries)
    assert set(entries) == {k[len("opencode:"):] for k in MATRIX if k.startswith("opencode:")}
    d = get_descriptor("opencode")
    assert d is not None and not d.needs_key
    assert d.recommended_model in entries
    # 铺的每个条目都声明支持工具调用（免费通道过门面后与普通 agent 流量同形）。
    from coworker.providers.matrix import entry_for

    assert all(entry_for(f"opencode:{e}").caps.tools for e in entries)


def test_opencode_session_and_project_ids_match_plugin():
    """与 opencode2dsh 插件 lib/catalog.js 逐字节平价（2026-10-06 用 node 复算核对过）。
    网关按 session id 形状做匿名记账，形状错 → 403 FreeTierError。"""
    from coworker.providers.opencode_provider import (
        _canonical_session_id,
        _PROJECT_ID,
        _stable_id,
    )

    assert _canonical_session_id('"hi"') == "ses_48486505dd980W9sJsFf6G4ZmD"
    assert _stable_id("prj", "opencode2dsh:default-project") == _PROJECT_ID
    assert _PROJECT_ID == "prj_275aad355251f5cec22671d1"


def test_opencode_gate_tools_and_tool_choice():
    """免费通道的请求体门：tools 必须含 bash/read 门面桩；仅当调用方没有真实工具时才
    置 tool_choice=none（有真实工具时保持网关默认，绝不覆盖引擎的选择）。"""
    from coworker.providers.opencode_provider import _apply_free_lane_gate

    tools, choice = _apply_free_lane_gate(None)
    assert [t["function"]["name"] for t in tools] == ["bash", "read"]
    assert choice == "none"

    real = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    tools, choice = _apply_free_lane_gate(real)
    assert [t["function"]["name"] for t in tools] == ["read_file", "bash", "read"]
    assert choice is None

    # 已带 bash 的场景只补 read，不重复。
    covered = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    tools, choice = _apply_free_lane_gate(covered)
    assert [t["function"]["name"] for t in tools] == ["bash", "read"]
    assert choice is None


def test_opencode_stream_injects_disguise_and_gate(monkeypatch):
    """stream() 注入 CLI 伪装头 + 门面工具 + 空工具时的 tool_choice，全部透传给底层
    OpenAIProvider（extra_headers/tool_choice 是 SDK 的合法 create 参数）。"""
    from coworker.providers import openai_provider
    from coworker.providers.opencode_provider import OpencodeProvider

    captured: dict = {}

    def fake_stream(self, *, model, messages, tools=None, **settings):
        captured["model"] = model
        captured["tool_names"] = [t["function"]["name"] for t in tools or []]
        captured["settings"] = settings
        return iter(())

    monkeypatch.setattr(openai_provider.OpenAIProvider, "stream", fake_stream)
    p = OpencodeProvider()
    list(
        p.stream(
            model="opencode:space-bunny-free",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert captured["tool_names"] == ["bash", "read"]
    assert captured["settings"]["tool_choice"] == "none"
    headers = captured["settings"]["extra_headers"]
    assert headers["x-opencode-client"] == "cli"
    assert headers["x-opencode-session"] == "ses_48486505dd980W9sJsFf6G4ZmD"
    assert headers["x-opencode-request"].startswith("req_") and len(headers["x-opencode-request"]) == 36
    assert headers["user-agent"].startswith("opencode/")
    # 有真实工具时不设 tool_choice，门面桩照补。
    captured.clear()
    real = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]
    list(
        p.stream(
            model="opencode:space-bunny-free",
            messages=[{"role": "user", "content": "hi"}],
            tools=real,
        )
    )
    assert captured["tool_names"] == ["write_file", "bash", "read"]
    assert "tool_choice" not in captured["settings"]
    # 默认匿名 key 与网关地址就位；显式 api_key 获得更高优先（付费模型）。
    assert p._api_key == "public"
    assert p._base_url == "https://opencode.ai/zen/v1"
    assert OpencodeProvider(api_key=" paid-key ")._api_key == "paid-key"


def test_opencode_complete_aggregates_stream(monkeypatch):
    """免费通道只收流式：complete() 必须聚合 stream() 的最终 turn（对引擎透明）。"""
    from coworker.providers import openai_provider
    from coworker.providers.base import AssistantTurn, StreamChunk
    from coworker.providers.opencode_provider import OpencodeProvider

    def fake_stream(self, *, model, messages, tools=None, **settings):
        yield StreamChunk(text_delta="你")
        yield StreamChunk(text_delta="好")
        yield StreamChunk(turn=AssistantTurn(text="你好", finish_reason="stop"))

    monkeypatch.setattr(openai_provider.OpenAIProvider, "stream", fake_stream)
    p = OpencodeProvider()
    turn = p.complete(
        model="opencode:space-bunny-free",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert turn.text == "你好"
    assert turn.finish_reason == "stop"


def test_reseller_descriptors_and_matrix_stay_in_lockstep():
    """Reseller suggested models derive from the matrix, and each descriptor's
    recommended model must be one of them (set_provider's auto-add depends on it)."""
    from coworker.providers.matrix import models_for_provider
    from coworker.providers.registry import get_descriptor

    for name in ("together", "fireworks", "openrouter"):
        d = get_descriptor(name)
        assert d is not None and d.needs_key
        curated = models_for_provider(name)
        assert curated and d.recommended_model in curated
        # full ids in the matrix must round-trip: prefix + bare == matrix key
        base = next(f for f in d.fields if f.key == "base_url")
        assert base.default.startswith("https://")


def test_foreign_sidecars_stripped_from_outbound_messages():
    """Provider-private sidecars (`_gemini` thought signatures et al) must never reach the
    OpenAI wire — it and its compat servers reject unknown message fields."""
    client = _FakeClient(_response(content="ok"))
    provider = OpenAIProvider(client=client)
    provider.complete(
        model="gpt-5.5",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prev", "_gemini": {"call_sigs": ["x"]}},
        ],
    )
    sent = client.chat.completions.calls[0]["messages"]
    assert sent[1] == {"role": "assistant", "content": "prev"}


def test_stream_reasoning_content_deltas():
    """DeepSeek-style thinking: reasoning_content deltas surface as reasoning chunks and
    land on the final turn — never mixed into the answer text."""
    def rchunk(text):
        delta = SimpleNamespace(content=None, tool_calls=None, reasoning_content=text)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])

    chunks = [rchunk("hmm "), rchunk("okay."), _chunk(content="Answer"), _chunk(finish="stop")]
    provider = OpenAIProvider(client=_StreamClient(chunks))
    out = list(provider.stream(model="deepseek-v4-pro", messages=[]))
    assert [c.reasoning_delta for c in out if c.reasoning_delta] == ["hmm ", "okay."]
    final = out[-1].turn
    assert final.text == "Answer" and final.reasoning == "hmm okay."


def test_complete_picks_up_reasoning_content():
    message = SimpleNamespace(content="Answer", tool_calls=None, reasoning_content="deep thought")
    choice = SimpleNamespace(message=message, finish_reason="stop")
    provider = OpenAIProvider(client=_FakeClient(SimpleNamespace(choices=[choice])))
    turn = provider.complete(model="deepseek-v4-pro", messages=[{"role": "user", "content": "x"}])
    assert turn.text == "Answer" and turn.reasoning == "deep thought"


def test_default_max_tokens_injected_and_caller_setting_wins():
    """Compat servers left to their OWN defaults cap completions absurdly low
    (owner-hit 2026-08-15: Together defaulted Kimi K3 to ~2k tokens, so every report
    write truncated mid-arguments). The request always names a ceiling now."""
    from coworker.providers.openai_provider import DEFAULT_MAX_TOKENS

    client = _FakeClient(_response(content="ok"))
    provider = OpenAIProvider(client=client)
    provider.complete(model="kimi-k3", messages=[])
    assert client.chat.completions.calls[0]["max_tokens"] == DEFAULT_MAX_TOKENS

    client2 = _FakeClient(_response(content="ok"))
    provider2 = OpenAIProvider(client=client2)
    provider2.complete(model="kimi-k3", messages=[], max_tokens=512)
    assert client2.chat.completions.calls[0]["max_tokens"] == 512


def test_over_limit_max_tokens_is_dropped_and_retried():
    """A model whose completion limit sits below our default must not surface the 400:
    drop the param, retry on the server's own default (yesterday's behavior, at worst)."""

    class _LimitRejecting:
        def __init__(self, response):
            self._response = response
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "max_tokens" in kwargs:
                raise RuntimeError(
                    "Error code: 400 - max_tokens must be at most 8193 for this model"
                )
            return self._response

    client = _FakeClient(_response(content="ok"))
    client.chat.completions = _LimitRejecting(_response(content="ok"))
    provider = OpenAIProvider(client=client)

    turn = provider.complete(model="tiny-model", messages=[])
    calls = client.chat.completions.calls
    assert turn.text == "ok" and len(calls) == 2
    assert "max_tokens" not in calls[1]
