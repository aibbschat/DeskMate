"""统一的 provider 超时阈值（2026-11-19 PR17 / #34）。

PR2 只给 OpenAI 出口的 pump 线程装了首字/idle 看门狗；anthropic / gemini /
vertex / bedrock / codex 的 SDK 没有显式 read timeout（SDK 默认最长 600s），
上游 stall 时既不抛异常，paid_fallback 也就无从降级。本模块集中读取阈值，
供两处消费：

1. ``engine._astream`` 消费端的统一 wait 超时（一处覆盖全部 provider，兜底
   所有 SDK）；
2. 各 provider 建 SDK 客户端时显式传 timeout —— 让阻塞调用本身在有限时间内
   返回，释放 ``_PROVIDER_POOL`` 线程。

总开关沿用 PR2 的 ``COWORKER_WATCHDOG=0``：关闭后消费端不设超时，SDK 仍会用
本模块的 300s/15s 默认（避免又回到 600s 的无限等待）；如需完全恢复旧行为，
把 ``COWORKER_SDK_READ_TIMEOUT`` 设为 0 即不传 timeout。
"""

from __future__ import annotations

import os


def env_seconds(name: str, default: float) -> float:
    """秒级阈值：空串/垃圾值一律回退默认；<=0 表示关闭该项。"""
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def watchdog_enabled() -> bool:
    return os.environ.get("COWORKER_WATCHDOG", "1") != "0"


def first_token_timeout() -> float:
    """首字超时（秒）；<=0 关闭。默认 75s，与 PR2 对齐。"""
    return env_seconds("COWORKER_FIRST_TOKEN_TIMEOUT", 75.0)


def stream_idle_timeout() -> float:
    """流中途两次 chunk 的 idle 超时（秒）；<=0 关闭。默认 120s。"""
    return env_seconds("COWORKER_STREAM_IDLE_TIMEOUT", 120.0)


def sdk_read_timeout() -> float | None:
    """SDK 级 read timeout（秒）。默认 300s；返回 None 表示不传（保持 SDK 默认）。

    ``COWORKER_SDK_READ_TIMEOUT<=0`` → None（完全恢复 SDK 默认，供回滚排查）。
    """
    value = env_seconds("COWORKER_SDK_READ_TIMEOUT", 300.0)
    return value if value > 0 else None


def provider_pool_size() -> int:
    """engine._PROVIDER_POOL 的工人数量（2026-11-20 线上事故定案）。

    8 工人在「用户会话 + 机器人 + 后台任务」并发流式 + 免费通道慢流场景下会被
    占满：produce 线程在线程池排队，请求根本没发出去 → 消费端 75s 首字超时 →
    降级链每通道都"stalled upstream"且 llm_usage 零记录。默认提到 24；
    ``COWORKER_PROVIDER_POOL_SIZE`` 可覆盖（最低 8，避免误配成 1 个工人）。
    """
    try:
        raw = (os.environ.get("COWORKER_PROVIDER_POOL_SIZE") or "").strip()
        size = int(raw) if raw else 24
    except (TypeError, ValueError):
        size = 24
    return max(8, size)


def sdk_connect_timeout() -> float | None:
    """SDK 级 connect timeout（秒）。默认 15s；<=0 → None。"""
    value = env_seconds("COWORKER_SDK_CONNECT_TIMEOUT", 15.0)
    return value if value > 0 else None


def genai_http_options():
    """google-genai SDK 的 HttpOptions（timeout 单位毫秒）；无法导入/无阈值时 None。"""
    read = sdk_read_timeout()
    if not read:
        return None
    try:
        from google.genai import types

        return types.HttpOptions(timeout=int(read * 1000))
    except Exception:  # SDK 未安装或 API 变动 —— 退回 SDK 默认
        return None
