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
    # Xiaomi 官方 token-plan 端点（provider:xiaomi）。模型清单以端点
    # GET /v1/models 实测为准（2026-10-17 复核）：mimo-v2.5 / mimo-v2.5-pro /
    # mimo-v2.6-flash / mimo-v2.6-pro（外加 asr/tts 专用号，非对话模型）。
    # ⚠️ 曾经铺过的 ``xiaomi:mimo-v2.6`` 是幻影：端点没有这个 model id，
    # chat 调用必返 400 "Unsupported model"（default_model 误设成它时每轮
    # 撞墙再降级，小爱语音延迟 5-10s）。清出去，别再铺回来。
    "xiaomi:mimo-v2.6-pro": ModelEntry(
        "MiMo V2.6 Pro · Xiaomi token-plan", context_window=256_000
    ),
    # Tencent TokenHub Token Plan（provider:tencent-tokenplan）。Hy 套餐为纯文本线——官方
    # 「可用模型」明确写「暂不支持图片、视频等多模态能力」，故 caps 用 _AGENTIC
    # （tools=True/vision=False），不要指望它承担看图；看图仍归 vision_overrides 声明的视觉模型。
    # 2026-10-02 按官方「可用模型」逐个实测（curl /plan/v3/chat/completions，正是官方文档给的
    # 验证方式）：
    #   hy3          ✅ 200
    #   hy3-preview  ✅ 200（官方 2384 号公告：请求会自动路由到 hy3，功能等同）
    #   hy4-preview  ✅ 200
    #   hy3-202608   ❌ 502 / 502003 "no available service" 网关没有匹配的后端服务，重试仍失败
    #                —— 文档列了但它当前不可用，故**故意不收录**，免得选择器里多一个点了必报错的项。
    # 官方要求 Model ID 必须全小写；通用与 Hy 两套餐共用同一 key/端点，由 model id 决定扣哪个包。
    "tencent-tokenplan:hy3": ModelEntry(
        "Hy3 · Tencent Token Plan", context_window=256_000
    ),
    "tencent-tokenplan:hy3-preview": ModelEntry(
        "Hy3 Preview · Tencent Token Plan", context_window=256_000
    ),
    # Hy4 preview：官方未公布上下文长度，故 context_window 留空（GUI 的上下文计自动隐藏），
    # 不拿 Hy3 的 256k 去猜。⚠ 它是**推理模型**：实测同一句「收到」背后 reasoning_tokens 就占
    # 93~170（单次响应 13~20s），max_tokens 给太小会被推理吃光、content 返回空串（本机实测到过一次
    # 空回复）。选它时别把输出上限压太低，也别拿它当低延迟模型用。
    "tencent-tokenplan:hy4-preview": ModelEntry(
        "Hy4 Preview · Tencent Token Plan"
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
    # DeepSeek V4.1 Flash (2026-09 发布, 10-17 官方将 API 名统一为 deepseek-flash;
    # 旧名 deepseek-v4-flash 仍可用并自动路由到 V4.1 Flash, 列为别名保留向后兼容)。
    # 上下文 1M, 原生多模态(图)+ 工具调用; thinking 默认开, 可用参数切非思考。
    "deepseek:deepseek-flash": ModelEntry(
        "DeepSeek V4.1 Flash · DeepSeek", _AGENTIC, 1_048_576
    ),
    "deepseek:deepseek-v4-flash": ModelEntry(
        "DeepSeek V4.1 Flash · DeepSeek (legacy alias)", _AGENTIC, 1_048_576
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

    # -- OpenCode Zen 免费通道 (zen/v1) ----------------------------------------
    # 🔴 2026-10-18 删除（萧哥拍板）：opencode 官方封禁免费使用（免费通道一条也
    # 用不了，连官方客户端都不可用），且持续的**动态探测**可能让官方一直不放行
    # ——「今天才知道探测多了会被封号，留着没用，下次也不敢再踩这个坑」。
    # 本曾铺 8 个 *-free 手铺项 + zen/kilo 两个动态 catalog + 两个 provider
    # 全部删除（git rm：opencode_provider / kilo_provider / zen_catalog /
    # kilo_catalog）。删除态契约见 tests/test_free_lane_removed.py。
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


# 🔴 2026-10-18 删除（萧哥拍板）：opencode 官方封禁免费使用（免费通道一条也
# 用不了，连官方客户端都不可用），持续的**动态探测**可能让官方一直不放行
# ——「今天才知道探测多了会被封号，留着没用，下次也不敢再踩这个坑」。
# 原 _live_zen（opencode 免费清单镜像）/ _live_kilo（Kilo 免费清单镜像）
# 随 zen_catalog / kilo_catalog 两个模块一并删除（git rm），删除态契约见
# tests/test_free_lane_removed.py。


def _live_catalogs() -> Dict[str, ModelEntry]:
    """所有动态清单的并集（仅剩 OpenRouter 付费/免费；zen/kilo 车道 2026-10-18 删除）。"""
    merged: Dict[str, ModelEntry] = {}
    for source in (_live_openrouter(),):
        merged.update(source)
    return merged


def entry_for(model: str) -> ModelEntry | None:
    """Look up one model's entry.

    静态 MATRIX 优先，其次动态清单（OpenRouter）。opencode/kilo 车道
    2026-10-18 已删除（外网免费车道全撤），不再有前缀特判。
    """
    return MATRIX.get(model) or _live_catalogs().get(model)


def model_labels() -> dict[str, str]:
    """Full-id → display-label map, shipped to the GUI so every picker shows human names."""
    live = _live_catalogs()
    labels = {mid: e.label for mid, e in MATRIX.items()}
    labels.update({mid: e.label for mid, e in live.items()})
    return labels


def model_context_windows() -> dict[str, int]:
    """Full-id → context-window map (verified entries only), for the GUI's fill meter."""
    live = _live_catalogs()
    windows = {mid: e.context_window for mid, e in MATRIX.items() if e.context_window}
    windows.update(
        {mid: e.context_window for mid, e in live.items() if e.context_window}
    )
    return windows


def all_model_ids() -> list[str]:
    """Every curated id (static matrix + live catalogs). Drives the composer picker.

    静态 MATRIX + 动态清单（OpenRouter）的并集。opencode/kilo 免费车道
    2026-10-18 已删除（外网免费车道全撤，萧哥拍板）——历史上 opencode:
    前缀曾以动态清单为唯一权威源、静态 8 项仅冷启动兜底（2026-10-09 修
    「聊天框免费模型不更新」），随车道删除一并作废。openrouter 为纯动态
    （静态表本就无 openrouter 条目），无需特判。
    """
    live = _live_catalogs()
    return [*MATRIX.keys(), *live.keys()]


def is_derived_model(model: str) -> bool:
    """True if ``model`` comes from curation (static matrix OR a live catalog),
    i.e. NOT a derived/synthetic id, as opposed to a user-added custom string.
    Used to decide hide-vs-drop on removal."""
    return model in MATRIX or model in _live_catalogs()


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
