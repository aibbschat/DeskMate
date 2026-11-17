"""后台主动推理 ticker — 接入 OpenWorker Scheduler 的 extra_tick。

make_extra_tick(engine, wrapped=None) 返回一个 async 协程：每次 scheduler tick（默认 30s）
先执行 wrapped（OpenWorker 原生 resume_due_wakes），再在后台线程执行一次 proactive_tick。
尊重静默模式与异常隔离：任何错误都不影响原生 scheduler 行为。

注：deskmate-proactivity 引擎按进程隔离（感知 / 防打扰状态在各自进程内），后台 tick 使用的是
主进程内新建的 ProactiveEngine 实例——这是「扩展而非 fork」原则下的最小接入。**默认开启**（P0-2）：
未显式设置或 =1 即启用；仅当环境变量 DESKMATE_PROACTIVE_BACKGROUND=0 时关闭（见 manager.py）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from .tick import ProactiveEngine, TickResult

logger = logging.getLogger("deskmate.proactivity.background")

ExtraTick = Callable[[], Awaitable[None]]

# 2026-11-10「反应慢」：后台主动 tick（感知 + LLM + 行为挖掘）自带几十个 DB 读和
# 线程池任务，与前台 turn 抢同一进程的 GIL / SQLite 连接 / 内存。用户刚说完话的这段
# 时间里，主动打扰本来也不该抢在前面——记录最近一次「人在聊天」的时间，静默窗口内
# 整轮 tick 直接跳过（只是延后，不是关掉；下一次 tick 到点自然补上）。
# DESKMATE_PROACTIVE_QUIET_SECONDS 可调，设 0 = 关闭该静默窗口（恢复旧行为）。
_last_user_activity: float = 0.0
_DEFAULT_QUIET_SECONDS = 90.0


def _record_tick(
    engine,
    *,
    skipped_reason: str = "",
    acted: bool = False,
) -> None:
    """心跳写盘：异常一律吞掉——可见性设施绝不许影响 tick 本身。"""
    try:
        from .heartbeat import record_tick

        record_tick(
            getattr(engine, "base_dir", None),
            skipped_reason=skipped_reason,
            acted=acted,
            model=getattr(engine, "model", "") or "",
        )
    except Exception:  # noqa: BLE001
        logger.debug("proactive heartbeat write failed", exc_info=True)


def note_user_activity() -> None:
    """标记「用户正在前台聊天」。由接入层（server 的 user_message 处理）调用。"""
    global _last_user_activity
    _last_user_activity = time.time()


def seconds_since_user_activity() -> float:
    """距上次前台活动多少秒；从未活动过返回 inf。"""
    if _last_user_activity <= 0.0:
        return float("inf")
    return max(0.0, time.time() - _last_user_activity)


def _quiet_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("DESKMATE_PROACTIVE_QUIET_SECONDS", str(_DEFAULT_QUIET_SECONDS))))
    except (TypeError, ValueError):
        return _DEFAULT_QUIET_SECONDS


def should_skip_background_tick() -> bool:
    """用户刚聊过（静默窗口内）→ 本轮后台 tick 让路。"""
    quiet = _quiet_seconds()
    return quiet > 0.0 and seconds_since_user_activity() < quiet


def make_extra_tick(
    engine: ProactiveEngine,
    wrapped: Optional[ExtraTick] = None,
) -> Callable[[], Awaitable[Optional[TickResult]]]:
    """构造一个可传给 Scheduler(extra_tick=...) 的协程。

    Args:
        engine: ProactiveEngine 实例（在调用方进程内创建）
        wrapped: 原生 extra_tick（如 resume_due_wakes），先执行

    Returns:
        协程；执行一次后台 tick，返回 TickResult（出错时返回 None）。
        调用方可用返回值把「建议」广播给 GUI 的 DeskMate 浮层。
    """

    async def _tick() -> Optional[TickResult]:
        if wrapped is not None:
            try:
                await wrapped()
            except Exception:
                logger.exception("wrapped extra_tick failed")
        # 0) 用户刚聊过 → 整轮让路（不感知、不 LLM、不挖行为模式），只延后不取消。
        if should_skip_background_tick():
            logger.debug(
                "proactive tick skipped: 用户 %.0fs 前还在前台聊天",
                seconds_since_user_activity(),
            )
            _record_tick(engine, skipped_reason="用户刚聊过，本轮让路")
            return None
        # 1) 主动推理（感知→判断→决策：说话/关怀/日记/花园）
        try:
            # proactive_tick 内含感知 / LLM，放到后台线程避免阻塞 scheduler 事件循环。
            # allow_interactive=False：本通道的产物只有下面 caller 的 WS 广播
            # （_broadcast_deskmate_suggestion 只认 judgment），深度访谈 / 认知验证
            # 这两种「要用户在前端浮层点/答」的负载在这里没有出口。若照建 session，
            # 只会消耗 3 天冷却并把邀请吞掉——用户永远看不到，前端 tick 也永远撞冷却
            # （2026-09-22 定位）。发起权归前端 proactive/tick。
            result = await asyncio.to_thread(engine.tick, allow_interactive=False)
        except Exception:
            logger.exception("deskmate proactive background tick failed")
            result = None
        # 1.5) 心跳（2026-11-17）：把「这一跳发生了什么」落到文件，设置页直读。
        # 它只回答「她还活着吗」，judge 是否真调了 LLM 由 tick 里的 record_judge 记。
        # 用 getattr 取：tick() 的假实现/旧实现可能返回别的类型（测试用 str），
        # 心跳可见性不该因为一个字段的类型假设把整轮 tick 打挂。
        _record_tick(
            engine,
            skipped_reason=str(getattr(result, "skipped_reason", "") or "")
            if result is not None
            else "tick 异常",
            acted=bool(getattr(result, "has_suggestion", False)),
        )
        # 2) L2 自主层（替你行动）：kill switch 关则整块跳过，绝不相互影响
        try:
            from deskmate.agency.engine import AgencyEngine

            agency = AgencyEngine(base_dir=engine.base_dir)
            await asyncio.to_thread(agency.tick)
        except Exception:
            logger.exception("deskmate L2 agency tick failed")
        return result

    return _tick
