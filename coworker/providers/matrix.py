"""The curated model matrix — the only models we actively suggest, label, and vouch for.

Keyed by the FULL routed id, exactly as the ProviderRouter receives it — including reseller
"ugly names" like ``together:zai-org/GLM-5.2`` (bare ids route to the OpenAI default). Each
entry carries the UI display label and the model's capabilities, making this the single
source of truth the capability probe and the GUI's pickers read from.

Deliberately SMALL (owner call, 2026-07-04): current-generation, agent-capable (tool-calling)
models only. It is not user-editable — users can still add any custom model string, which
falls back to the conservative heuristics in ``capabilities.py`` at their own risk of
degraded results. Ids verified against vendor/reseller catalogs on 2026-07-04; refresh the
reseller rows when catalogs rotate (they rename on every model generation).

Context windows (``context_window``, tokens) feed the GUI's context-fill meter. Entries
where the vendor spec wasn't re-checked stay ``None`` — the meter simply hides rather than
showing a made-up denominator. Values entered 2026-07-28 from vendor docs; verify alongside
the id refresh.

Resellers: Together + Fireworks + OpenRouter. TODO: add Groq entries here AND its
descriptor in ``registry.py`` once the current provider surface is tested — deliberately
deferred to bound how much needs verifying at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .base import ModelCapabilities

_AGENTIC = ModelCapabilities(
    tools=True, vision=False, parallel_tool_calls=True, streaming=True
)
# The native three (OpenAI, Anthropic, Gemini) all take PDFs directly; every
# OpenAI-compatible vendor and reseller in the matrix does not (their chat APIs have
# no inline file part — checked 2026-07-17), so those fall back via pdf_support.py.
_AGENTIC_VISION = ModelCapabilities(
    tools=True, vision=True, pdf=True, parallel_tool_calls=True, streaming=True
)


@dataclass(frozen=True)
class ModelEntry:
    label: str  # UI display name, e.g. "GLM-5.2 · via Together"
    caps: ModelCapabilities = _AGENTIC
    # Max context length in tokens (prompt side), for the GUI's context-fill meter.
    # None = not verified against the vendor spec yet; the meter hides.
    context_window: Optional[int] = None


MATRIX: dict[str, ModelEntry] = {
    # -- first-party ------------------------------------------------------------
    # GPT-5.6 (2026-07-09): number = generation, Sol/Terra/Luna = capability tiers.
    # Bare "gpt-5.6" aliases to Sol server-side; we list the explicit tier ids only.
    # Rolling out — accounts without access get a friendly error (providers/errors.py).
    "gpt-5.6-sol": ModelEntry("GPT-5.6 Sol · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.6-terra": ModelEntry("GPT-5.6 Terra · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.6-luna": ModelEntry("GPT-5.6 Luna · OpenAI", _AGENTIC_VISION, 400_000),
    "gpt-5.5": ModelEntry("GPT-5.5 · OpenAI", _AGENTIC_VISION, 400_000),
    # ChatGPT-subscription catalog (the `openai-codex` OAuth provider). Curated to the
    # ids the subscription backend actually serves; vision per the vendor's model docs,
    # PDF unverified over this backend → local fallback via pdf_support.py.
    # 5.6 tiers (Sol flagship / Terra balanced / Luna fast) serve over the subscription
    # backend by plan — Sol is rate-limited on Plus, full on Pro.
    "openai-codex:gpt-5.6-sol": ModelEntry(
        "GPT-5.6 Sol · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.6-terra": ModelEntry(
        "GPT-5.6 Terra · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.6-luna": ModelEntry(
        "GPT-5.6 Luna · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.2-codex": ModelEntry(
        "GPT-5.2 Codex · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.2": ModelEntry(
        "GPT-5.2 · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.1-codex": ModelEntry(
        "GPT-5.1 Codex · ChatGPT plan",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        400_000,
    ),
    "openai-codex:gpt-5.1-codex-mini": ModelEntry(
        "GPT-5.1 Codex Mini · ChatGPT plan", _AGENTIC, 400_000
    ),
    # Fable 5 (2026-06-09) is GA; its Mythos 5 sibling is approved-orgs-only, so it
    # stays out of a picker meant for the public.
    "anthropic:claude-fable-5": ModelEntry(
        "Claude Fable 5 · Anthropic", _AGENTIC_VISION, 1_000_000
    ),
    "anthropic:claude-opus-4-8": ModelEntry(
        "Claude Opus 4.8 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    "anthropic:claude-sonnet-4-6": ModelEntry(
        "Claude Sonnet 4.6 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    "anthropic:claude-haiku-4-5": ModelEntry(
        "Claude Haiku 4.5 · Anthropic", _AGENTIC_VISION, 200_000
    ),
    # Gemini 3 (thought signatures required in tool loops — carried via the `_gemini`
    # message sidecar, see gemini_provider.py; ids from the vendor catalog 2026-07-22).
    "gemini:gemini-3.1-pro-preview": ModelEntry(
        "Gemini 3.1 Pro · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-3.6-flash": ModelEntry(
        "Gemini 3.6 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-2.5-pro": ModelEntry(
        "Gemini 2.5 Pro · Google", _AGENTIC_VISION, 1_048_576
    ),
    "gemini:gemini-2.5-flash": ModelEntry(
        "Gemini 2.5 Flash · Google", _AGENTIC_VISION, 1_048_576
    ),
    # Ark Responses API providers (verified 2026-08-14). BytePlus pay-as-you-go and
    # Volcengine Agent Plan intentionally use separate provider prefixes because their
    # endpoints, credentials, regions, and model catalogs are not interchangeable.
    "ark:dola-seed-evolving-latest-version": ModelEntry(
        "Dola Seed Evolving · BytePlus Ark", context_window=256_000
    ),
    "ark:dola-seed-2-1-turbo-260628": ModelEntry(
        "Dola Seed 2.1 Turbo · BytePlus Ark", context_window=256_000
    ),
    "ark-agent-plan-cn:doubao-seed-evolving": ModelEntry(
        "Doubao Seed Evolving · Volcengine Agent Plan", context_window=1_048_576
    ),
    "ark-agent-plan-cn:doubao-seed-2.1-turbo": ModelEntry(
        "Doubao Seed 2.1 Turbo · Volcengine Agent Plan", context_window=256_000
    ),
    # Volcengine Agent Plan 全量文本模型（verified 2026-09-01，实测全部可用）：
    # doubao-seed-2.0-mini/lite / deepseek-v4-flash/pro / minimax-m3 / kimi-k2.7-code。
    # glm-5.2 已下线、glm-5.3 默认开思考不可关，均不列入。
    "ark-agent-plan-cn:doubao-seed-2.0-mini": ModelEntry(
        "Doubao Seed 2.0 Mini · Volcengine Agent Plan", context_window=256_000
    ),
    "ark-agent-plan-cn:doubao-seed-2.0-lite": ModelEntry(
        "Doubao Seed 2.0 Lite · Volcengine Agent Plan", context_window=256_000
    ),
    "ark-agent-plan-cn:deepseek-v4-flash": ModelEntry(
        "DeepSeek V4 Flash · Volcengine Agent Plan", context_window=1_048_576
    ),
    "ark-agent-plan-cn:deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · Volcengine Agent Plan", context_window=1_048_576
    ),
    "ark-agent-plan-cn:minimax-m3": ModelEntry(
        "MiniMax M3 · Volcengine Agent Plan", context_window=1_048_576
    ),
    "ark-agent-plan-cn:kimi-k2.7-code": ModelEntry(
        "Kimi K2.7 Code · Volcengine Agent Plan", context_window=256_000
    ),
    # Xiaomi 官方 token-plan 端点（provider:xiaomi，2026-09-01 实测模型 ID 服务端认可，
    # 当前 429 额度用尽属暂时；OpenRouter 中转不稳定故注册官方端点备用）。
    "xiaomi:mimo-v2.5-pro": ModelEntry(
        "MiMo V2.5 Pro · Xiaomi token-plan", context_window=256_000
    ),
    "xiaomi:mimo-v2.5": ModelEntry(
        "MiMo V2.5 · Xiaomi token-plan", context_window=256_000
    ),
    # -- direct OpenAI-compatible vendors ----------------------------------------
    # Muse Spark (Meta Model API, public preview 2026-07-09): multimodal + tools via
    # their OpenAI-compat surface. Vision yes; PDFs unverified over compat — falls
    # back via pdf_support.py like the other compat vendors.
    "meta:muse-spark-1.1": ModelEntry(
        "Muse Spark 1.1 · Meta",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
    ),
    "zai:glm-5.2": ModelEntry("GLM-5.2 · Z AI", _AGENTIC, 128_000),
    "deepseek:deepseek-v4-flash": ModelEntry(
        "DeepSeek V4 Flash · DeepSeek", _AGENTIC, 128_000
    ),
    "deepseek:deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · DeepSeek", _AGENTIC, 128_000
    ),
    "kimi:kimi-k2.6": ModelEntry("Kimi K2.6 · Moonshot", _AGENTIC, 256_000),
    "minimax:MiniMax-M2.5": ModelEntry("MiniMax M2.5 · MiniMax"),
    "qwen:qwen3-max": ModelEntry("Qwen3 Max · Alibaba", _AGENTIC, 256_000),
    "xai:grok-4.3": ModelEntry("Grok 4.3 · xAI", _AGENTIC, 256_000),
    "mistral:mistral-large-latest": ModelEntry(
        "Mistral Large · Mistral", _AGENTIC, 128_000
    ),
    # -- resellers (their model namespaces, verbatim) -----------------------------
    "together:thinkingmachines/Inkling": ModelEntry("Inkling · via Together"),
    "together:zai-org/GLM-5.2": ModelEntry("GLM-5.2 · via Together", _AGENTIC, 128_000),
    # Kimi K3 on Together (landed late July 2026): 1M window, native vision; PDFs
    # unverified over the compat surface (falls back via pdf_support.py, like Muse Spark).
    "together:moonshotai/Kimi-K3": ModelEntry(
        "Kimi K3 · via Together",
        ModelCapabilities(
            tools=True, vision=True, parallel_tool_calls=True, streaming=True
        ),
        1_000_000,
    ),
    "together:moonshotai/Kimi-K2.7-Code": ModelEntry(
        "Kimi K2.7 Code · via Together", _AGENTIC, 256_000
    ),
    "together:moonshotai/Kimi-K2.6": ModelEntry(
        "Kimi K2.6 · via Together", _AGENTIC, 256_000
    ),
    "together:deepseek-ai/DeepSeek-V4-Pro": ModelEntry(
        "DeepSeek V4 Pro · via Together", _AGENTIC, 128_000
    ),
    "together:meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8": ModelEntry(
        "Llama 4 Maverick · via Together", _AGENTIC, 1_000_000
    ),
    "fireworks:accounts/fireworks/models/glm-5p2": ModelEntry(
        "GLM-5.2 · via Fireworks", _AGENTIC, 128_000
    ),
    "fireworks:accounts/fireworks/models/kimi-k2p6": ModelEntry(
        "Kimi K2.6 · via Fireworks", _AGENTIC, 256_000
    ),
    "fireworks:accounts/fireworks/models/deepseek-v4-pro": ModelEntry(
        "DeepSeek V4 Pro · via Fireworks", _AGENTIC, 128_000
    ),
    "fireworks:accounts/fireworks/models/llama4-maverick-instruct-basic": ModelEntry(
        "Llama 4 Maverick · via Fireworks", _AGENTIC, 1_000_000
    ),
    # -- OpenRouter -------------------------------------------------------------
    # No longer hand-curated here: the OpenRouter portion is mirrored live from the
    # vendor catalog (coworker/providers/openrouter_catalog.py) at startup + every 6h,
    # so free/paid status stays aligned with upstream instead of going stale. See that
    # module for the rationale and the accepted "declared-free may still bill" tradeoff.

    # -- local AI gateway (OmniRoute) --------------------------------------------
    # Keyless free combo. Marked streaming-only + no tools: free upstreams vary, and we use
    # this for the roundtable's external peer / occasional drafting, never the main flow.
    "omniroute:auto": ModelEntry(
        "Auto · OmniRoute 免费",
        ModelCapabilities(tools=False, vision=False, streaming=True),
    ),
    # -- OpenCode 公共免费网关 (zen/v1) ----------------------------------------
    # 2026-08-26 owner call: the six curated `opencode:*-free` entries were removed —
    # they were mostly unusable in practice (free-tier throttling/failures) and crowded
    # the picker's 免费 section. The provider descriptor in registry.py stays, so the
    # gateway remains routable: manually added `opencode:<model>` ids still work
    # (tier detection via needs_key=False keeps them in the 免费 group), they just no
    # longer ship by default and set_provider's recommended-model auto-add is inert.
    # -- cloud accounts (models running in the user's own AWS/GCP) ----------------
    # Bedrock ids carry a family segment (claude/ → native Anthropic path, other/ →
    # Converse) plus AWS's own `-v<n>:<m>` version suffix. Some regions require the
    # `us.`/`eu.` cross-region inference-profile prefix — custom add-model accepts those.
    "bedrock:claude/anthropic.claude-sonnet-4-6-v1:0": ModelEntry(
        "Claude Sonnet 4.6 · AWS Bedrock", _AGENTIC_VISION, 200_000
    ),
    "bedrock:claude/anthropic.claude-haiku-4-5-v1:0": ModelEntry(
        "Claude Haiku 4.5 · AWS Bedrock", _AGENTIC_VISION, 200_000
    ),
    "bedrock:other/amazon.nova-2-pro-v1:0": ModelEntry(
        "Nova 2 Pro · AWS Bedrock", _AGENTIC, 300_000
    ),
    "bedrock:other/meta.llama4-maverick-17b-instruct-v1:0": ModelEntry(
        "Llama 4 Maverick · AWS Bedrock", _AGENTIC, 1_000_000
    ),
    "bedrock:other/mistral.mistral-large-3-v1:0": ModelEntry(
        "Mistral Large 3 · AWS Bedrock", _AGENTIC, 128_000
    ),
    # Live-verified on Converse 2026-07-26 (complete/stream/tool round trip); asked for
    # two tool calls it emits them one at a time, so parallel stays off.
    "bedrock:other/nvidia.nemotron-super-3-120b": ModelEntry(
        "Nemotron Super 3 120B · AWS Bedrock",
        ModelCapabilities(
            tools=True, vision=False, parallel_tool_calls=False, streaming=True
        ),
    ),
    # Vertex ids carry a family segment too (gemini/ and claude/ → native paths,
    # openweight/ → the MaaS OpenAI-compat endpoint, keeping the publisher segment).
    "vertex:gemini/gemini-3.1-pro-preview": ModelEntry(
        "Gemini 3.1 Pro · Vertex AI", _AGENTIC_VISION, 1_048_576
    ),
    "vertex:gemini/gemini-3.6-flash": ModelEntry(
        "Gemini 3.6 Flash · Vertex AI", _AGENTIC_VISION, 1_048_576
    ),
    "vertex:claude/claude-sonnet-4-6": ModelEntry(
        "Claude Sonnet 4.6 · Vertex AI", _AGENTIC_VISION, 200_000
    ),
    "vertex:claude/claude-haiku-4-5": ModelEntry(
        "Claude Haiku 4.5 · Vertex AI", _AGENTIC_VISION, 200_000
    ),
    "vertex:openweight/meta/llama-4-maverick-17b-128e-instruct-maas": ModelEntry(
        "Llama 4 Maverick · Vertex AI", _AGENTIC, 1_000_000
    ),
    "vertex:openweight/qwen/qwen3-coder-480b-a35b-instruct-maas": ModelEntry(
        "Qwen3 Coder · Vertex AI", _AGENTIC, 256_000
    ),
}


def _live_openrouter() -> Dict[str, ModelEntry]:
    """Live OpenRouter models (``openrouter:<id>`` → ModelEntry), mirrored from the vendor
    catalog. Returns {} on any failure so the static matrix stays the sole source if the
    catalog module errors."""
    from .openrouter_catalog import get_openrouter_entries

    try:
        return get_openrouter_entries()
    except Exception:
        return {}


def entry_for(model: str) -> ModelEntry | None:
    return MATRIX.get(model) or _live_openrouter().get(model)


def model_labels() -> dict[str, str]:
    """Full-id → display-label map, shipped to the GUI so every picker shows human names."""
    live = _live_openrouter()
    labels = {mid: e.label for mid, e in MATRIX.items()}
    labels.update({mid: e.label for mid, e in live.items()})
    return labels


def model_context_windows() -> dict[str, int]:
    """Full-id → context-window map (verified entries only), for the GUI's fill meter."""
    live = _live_openrouter()
    windows = {mid: e.context_window for mid, e in MATRIX.items() if e.context_window}
    windows.update(
        {mid: e.context_window for mid, e in live.items() if e.context_window}
    )
    return windows


def all_model_ids() -> list[str]:
    """Every curated id (static matrix + live OpenRouter). Drives the composer picker."""
    return [*MATRIX, *_live_openrouter().keys()]


def is_derived_model(model: str) -> bool:
    """True if ``model`` comes from curation (static matrix OR the live OpenRouter catalog),
    as opposed to a user-added custom string. Used to decide hide-vs-drop on removal."""
    return model in MATRIX or model in _live_openrouter()


def models_for_provider(provider: str) -> list[str]:
    """BARE model ids (prefix stripped) the matrix curates for a provider — feeds the
    Settings pane's suggestions and the composer picker so both stay in lockstep with the
    matrix. OpenAI entries are stored without a prefix (bare ids route to the OpenAI
    default), so its list is every un-prefixed id."""
    if provider == "openrouter":
        return [mid[len("openrouter:"):] for mid in _live_openrouter().keys()]
    if provider == "openai":
        return [mid for mid in MATRIX if ":" not in mid]
    prefix = provider + ":"
    return [mid[len(prefix):] for mid in MATRIX if mid.startswith(prefix)]
