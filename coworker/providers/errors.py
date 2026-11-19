"""Friendly translation of model access + quota failures.

The picker now defaults to brand-new flagships (GPT-5.6 Sol, Claude Fable 5), and not every
account can use them: OpenAI is still rolling GPT-5.6 out per-organization, and both vendors
reject calls once quota/credits run out. Those failures arrive as terse SDK exceptions
wrapping JSON error bodies; this maps the well-known shapes to one actionable sentence.
Anything unrecognized returns None and the caller surfaces the raw error unchanged.

Matching is on the error BODY text (error codes/types), not just HTTP status — a 404 also
means "wrong base_url" and a 429 also means "slow down", and neither of those should be
dressed up as an access problem.
"""

from __future__ import annotations

from typing import Optional


class ProviderStall(Exception):
    """模型调用停滞（首字超时 / 流中途 idle 超时）。

    2026-11-19 PR2：SDK 的 read timeout 默认 600s，连接卡住时最长 10 分钟没有
    任何异常，`paid_fallback` 因而无从降级。本异常把"卡住"变成普通可重试错误：
    消息含 ``timeout``，会被 `deskmate.llm_circuit_breaker.classify_error` 归为
    transient/retryable；且只在尚未吐出第一个 chunk 时由回退链换通道。
    """

    def __init__(self, kind: str, timeout_s: float) -> None:
        self.kind = kind
        self.timeout_s = float(timeout_s)
        super().__init__(f"provider {kind} timeout after {timeout_s:g}s (stalled upstream)")


class FirstTokenTimeout(ProviderStall):
    """连接建立后首字迟迟不来。"""

    def __init__(self, timeout_s: float) -> None:
        super().__init__("first-token", timeout_s)


class StreamIdleTimeout(ProviderStall):
    """流已开始但两次 chunk 间隔超过阈值。"""

    def __init__(self, timeout_s: float) -> None:
        super().__init__("stream-idle", timeout_s)


# Error-body markers, verbatim from the vendors' error codes/messages:
# OpenAI: {"error": {"code": "model_not_found", "message": "The model `X` does not exist or
#   you do not have access to it."}} (404/403) and {"code": "insufficient_quota"} (429).
# Anthropic: {"type": "not_found_error", "message": "model: X"} (404),
#   {"type": "permission_error"} (403), and "credit balance is too low" (400).
_NO_ACCESS = (
    "model_not_found",
    "does not exist or you do not have access",
    "does not have access to model",
    "permission_error",
    "permission denied",
)
_NO_QUOTA = (
    "insufficient_quota",
    "exceeded your current quota",
    "credit balance is too low",
    "billing hard limit",
)
# 网关内容风控拒绝（小米 MiMo / 火山 ark 实测同文案，2026-10-24 定性：分钟级
# 瞬时风控窗口，非内容定罪——同一窗口内聊天 + 后台任务齐拒，同载荷稍后重放全过）。
_RISK_CONTROL = (
    "high risk",
    "rejected because",
    "content_filter",
    "content filter",
)


def friendly_model_error(model: str, exc: Exception) -> Optional[str]:
    """One actionable sentence for "your account can't use this model" failures, or None."""
    text = str(exc).lower()
    no_access = (
        f"Your account doesn't have access to {model} — new models can roll out "
        "gradually or require a plan upgrade. Pick a different model, or check "
        "the provider's console for availability."
    )
    if any(marker in text for marker in _RISK_CONTROL):
        # 2026-10-24 实测定性：这是服务商网关的瞬时风控窗口（1-2 分钟自愈），
        # 不是内容违规判定。说清楚这三点，别让用户以为自己在"犯事"。
        return (
            "（服务商风控网关临时拦截了这次请求，通常 1-2 分钟自动恢复。"
            "这不是内容违规，也不扣额度——稍等重发一次即可，后台任务会自动切到下一家通道。）"
        )
    if any(marker in text for marker in _NO_QUOTA):
        return (
            f"Your account is out of quota for {model} — add credits or raise the limit "
            "in the provider's billing console, or pick a different model."
        )
    if any(marker in text for marker in _NO_ACCESS):
        return no_access
    # Anthropic's 404 body is just "model: <id>" under type not_found_error; require both
    # halves so unrelated 404s (bad base_url, deleted resource) keep their raw message.
    if "not_found_error" in text and f"model: {model.split(':')[-1].lower()}" in text:
        return no_access
    return None
