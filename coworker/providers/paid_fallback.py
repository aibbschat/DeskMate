"""付费组主链路回退：让「撞了自动切小米/切 hy」在**主对话**上也真正生效。

## 为什么需要（2026-10-09 实测）

火山套餐（``ark-agent-plan-cn``）周额度撞顶，日志实测::

    openai.RateLimitError 429 AccountQuotaExceeded
    "weekly usage quota ... reset at 2026-10-12 00:00:00 +0800"

熔断器**已经正确记账**（``kind=quota_exhausted``，冷却期对齐上游重置点，审计日志
持续打 ``skip | ark-agent-plan-cn:doubao | 熔断中``），但主聊天**仍然在撞**：

- 主链路是 ``manager.provider_complete`` → ``ProviderRouter.complete`` → 客户端直连；
- 已有的组内回退只写在 ``deskmate/memory/llm.py`` 的 ``_attempt_group``（2026-10-06 修过
  同一个 bug），覆盖后台/记忆类调用；
- ``ProviderRouter.complete`` / ``.stream`` **完全裸奔**：解析到 provider 就 ``return``，
  没有任何 try/except，既不查熔断也不换通道。

⇒ 熔断器在记账、主链路不看账，两套配置脱节。

## 为什么默认模型没救回来

``default_model`` 常被设成付费组里**不在候选表**的模型（实测：
``ark-agent-plan-cn:doubao-seed-2.1-turbo`` 不在 ``PAID_GROUP`` 内，而候选表首位是
同套餐的 ``deepseek-v4-flash``）。但**同套餐共用同一份周额度**，所以「换成同套餐另一个
模型」并不解决问题 —— 额度是套餐级的，不分模型。

本模块因此**不做「换个同套餐模型」的无效尝试**，而是把决策权交给跨套餐的
``PAID_GROUP``：火山熔断 → 小米 → 腾讯 hy3。

## 触发条件（刻意保守，只覆盖已验证的真故障）

仅当**请求的模型本身属于付费组候选之一**时才启用回退。否则回退会偷换用户明确指定的
模型（例如手动选了 openrouter 或某个本地模型），那是越权。

## stream 的实现约束

``stream`` 是生成器，**必须惰性**：一旦吐出第一个 chunk 就不能再换通道（否则会把两个
模型的输出混成一条回复）。故实现为「先取到第一个 chunk，成功后才 commit」的代理：
在拿到首个 chunk 之前发生的失败可以安全换通道重试；之后一律原样上抛。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: 回退最多试几条（付费组 3 条；留一点余量防候选表扩容时死循环）。
MAX_ATTEMPTS = 8


def _breaker_base_dir():
    """熔断器落库目录（与 deskmate/memory/llm.py 保持一致）。

    刻意复用对方私有函数：两者必须看同一份账，账分两份就等于没回退。
    """
    from deskmate.memory.llm import _breaker_base_dir as _f

    return _f()


def paid_group_channels() -> List[tuple]:
    """付费组候选 ``[(provider, model), ...]``，顺序即优先级。取不到时返回空表。

    导入失败（deskmate 未部署 / 测试环境）**不抛**：本模块是增强，不该拖垮主链路。
    """
    try:
        from deskmate.llm_channels import GROUP_PAID, channels_for_group

        return [(c.provider, c.model) for c in channels_for_group(GROUP_PAID)]
    except Exception:  # noqa: BLE001
        logger.debug("付费组候选表不可用，主链路回退降级为单通道", exc_info=True)
        return []


def _status_of(exc: BaseException) -> Optional[int]:
    """尽最大努力从异常里挖出 HTTP 状态码。

    熔断分类依赖状态码（429=配额/限流，5xx=瞬时）。openai SDK 把 status_code 挂在
    异常上；拿不到就交给 ``classify_error`` 从消息文本兜底判。
    """
    for attr in ("status_code", "http_status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _retry_after_of(exc: BaseException) -> Optional[float]:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _is_open(provider: str, model: str) -> bool:
    try:
        from deskmate.llm_circuit_breaker import is_open

        return bool(is_open(f"{provider}:{model}", base_dir=_breaker_base_dir()))
    except Exception:  # noqa: BLE001
        return False


def _note_failure(provider: str, model: str, exc: BaseException) -> None:
    """把失败记进熔断器（让下一次调用直接跳过，不再重复撞墙）。

    fail-open：记账失败绝不能反过来打断这次降级尝试。
    """
    try:
        from deskmate.llm_circuit_breaker import classify_error, record_failure

        verdict = classify_error(
            status_code=_status_of(exc),
            body=f"{type(exc).__name__}: {exc}",
            retry_after=_retry_after_of(exc),
        )
        record_failure(f"{provider}:{model}", verdict, base_dir=_breaker_base_dir())
    except Exception:  # noqa: BLE001
        logger.debug("熔断记账失败（不阻断降级）", exc_info=True)


def _note_success(provider: str, model: str) -> None:
    try:
        from deskmate.llm_circuit_breaker import record_success

        record_success(f"{provider}:{model}", base_dir=_breaker_base_dir())
    except Exception:  # noqa: BLE001
        logger.debug("熔断成功记账失败（不阻断）", exc_info=True)


def plan_attempts(requested: str) -> List[str]:
    """给定请求的模型，返回**按优先级排好的尝试序列**（含原模型，最多 ``MAX_ATTEMPTS`` 条）。

    规则：
    1. 原模型的 **provider 不属于付费套餐** → 只试它自己（不越权替用户换模型）。
       注意按 provider 判、**不按模型**判：default_model 常是付费套餐里不在候选表的
       模型（实测 doubao-seed-2.1-turbo），按模型判会让回退对它完全不启用。
    2. 属于付费套餐 → 原模型在首位，其余候选按 ``PAID_GROUP`` 原顺序追加，
       但**跳过同套餐的其它模型**（额度是套餐级，换模型等于白撞一次）。
    3. 整条序列里**滤掉当前熔断中的通道**，但**原模型永远保留在首位**：
       它可能是用户显式指定的，不该被我们悄悄换掉；真撑不住时让它按原样失败，
       错误信息才是用户该看到的。
    """
    channels = paid_group_channels()
    if not channels:
        return [requested]

    if ":" in requested:
        req_provider, req_model = requested.split(":", 1)
    else:
        req_provider, req_model = None, requested

    # 判定基准是**套餐（provider）**，不是具体模型。
    #
    # 关键（实测踩坑）：default_model 常被设成付费组里**不在候选表**的模型，如
    # `ark-agent-plan-cn:doubao-seed-2.1-turbo` 就不在 PAID_GROUP 内。若按模型判定，
    # 回退对它完全不启用 —— 而它恰恰是最需要回退的那个。故只看 provider 是否属于
    # 付费套餐：同套餐换模型无用（额度是套餐级的），跨套餐才是真回退。
    #
    # 非付费套餐（openrouter / 本地 ollama 等）→ 只试它自己，不越权替用户换模型。
    if req_provider is None or req_provider not in {c[0] for c in channels}:
        return [requested]

    ordered: List[str] = [requested]
    for provider, model in channels:
        cand = f"{provider}:{model}"
        if cand == requested or cand in ordered:
            continue
        if req_provider is not None and provider == req_provider:
            # 同 provider 的其它模型跳过：套餐级额度，换模型等于白撞一次。
            continue
        ordered.append(cand)
        if len(ordered) >= MAX_ATTEMPTS:
            break

    return ordered


def _filtered_attempts(requested: str) -> List[str]:
    """``plan_attempts`` 再滤掉（除首位外的）熔断中的通道。"""
    seq = plan_attempts(requested)
    head, rest = seq[0], seq[1:]
    return [head] + [c for c in rest if not _is_open(*c.split(":", 1))]


def complete_with_fallback(call, requested: str, **kwargs) -> Any:
    """按尝试序列调用 ``call(model)``，失败换下一条；全失败则抛最后一个异常。

    ``call`` 接收 model 字符串并返回结果。**熔断记账在这里**：失败记一笔、成功记一笔，
    于是「第一次撞墙 → 之后冷却期内直接跳过」自动成立。
    """
    attempts = _filtered_attempts(requested)
    if len(attempts) == 1:
        return call(attempts[0])

    last_exc: Optional[BaseException] = None
    for idx, model in enumerate(attempts):
        provider, _, bare = model.partition(":")
        try:
            out = call(model)
        except Exception as exc:  # noqa: BLE001
            _note_failure(provider, bare, exc)
            last_exc = exc
            logger.warning(
                "主链路通道 %s 失败（%s），换下一条", model, type(exc).__name__
            )
            continue
        if idx > 0:
            logger.warning("主链路已降级到 %s（原 %s 不可用）", model, requested)
            _note_success(provider, bare)
        else:
            _note_success(provider, bare)
        return out
    assert last_exc is not None
    raise last_exc


def stream_with_fallback(factory, requested: str, **kwargs) -> Iterator[Any]:
    """流式回退代理。

    ``factory(model)`` 返回一个**可迭代的 chunk 流**。逐条尝试：

    - 在拿到**第一个 chunk 之前**失败 → 换下一条通道重来（安全，输出还没开始）；
    - 拿到第一个 chunk **之后**失败 → 原样上抛（此时换通道会把两条回复混在一起）。
    """
    attempts = _filtered_attempts(requested)
    if len(attempts) == 1:
        yield from factory(attempts[0])
        return

    last_exc: Optional[BaseException] = None
    for idx, model in enumerate(attempts):
        provider, _, bare = model.partition(":")
        emitted = False
        try:
            for chunk in factory(model):
                if not emitted:
                    emitted = True
                    if idx > 0:
                        logger.warning(
                            "主链路已降级到 %s（原 %s 不可用）", model, requested
                        )
                    _note_success(provider, bare)
                yield chunk
            return
        except Exception as exc:  # noqa: BLE001
            _note_failure(provider, bare, exc)
            last_exc = exc
            if emitted:
                # 已经吐过内容，不能换通道重来。
                raise
            logger.warning(
                "主链路通道 %s 流式失败（%s），换下一条",
                model, type(exc).__name__,
            )
            continue
    assert last_exc is not None
    raise last_exc
