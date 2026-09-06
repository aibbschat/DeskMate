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
from typing import Awaitable, Callable, Optional

from .tick import ProactiveEngine, TickResult

logger = logging.getLogger("deskmate.proactivity.background")

ExtraTick = Callable[[], Awaitable[None]]


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
        # 1) 主动推理（感知→判断→决策：说话/关怀/日记/花园）
        try:
            # proactive_tick 内含感知 / LLM，放到后台线程避免阻塞 scheduler 事件循环
            result = await asyncio.to_thread(engine.tick)
        except Exception:
            logger.exception("deskmate proactive background tick failed")
            result = None
        # 2) L2 自主层（替你行动）：kill switch 关则整块跳过，绝不相互影响
        try:
            from deskmate.agency.engine import AgencyEngine

            agency = AgencyEngine(base_dir=engine.base_dir)
            await asyncio.to_thread(agency.tick)
        except Exception:
            logger.exception("deskmate L2 agency tick failed")
        return result

    return _tick
