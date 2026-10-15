"""对话层时钟 —— 让她知道「现在几点」。

2026-10-15 workbuddy 提议、萧哥拍板：给对话注入一行纯事实时钟。
不补，她聊天时永远不知道几点——「这么晚了」「早上好」这类意识全靠蒙。

设计红线（与天气感知同范式）：
- **纯事实，不导演**：只给「现在是 2026 年 10 月 15 日 周四 晚上 22:06」，
  不写「夜深了，劝他早点休息」这类指令——时间该让她生出什么反应，是她的事。
- **零网络零 LLM**：本地时间派生，毫秒级，不花 token。
- **fail-open**：任何异常 → None（没有时钟行也不能断主聊天）。

配置：``DESKMATE_CLOCK=0`` 关闭（默认开）。

时段分档（她语言里的词，不是工程词）：
  00-03 深夜 / 04-05 凌晨 / 06-08 早上 / 09-11 上午 / 12-13 中午
  14-16 下午 / 17-18 傍晚 / 19-22 晚上 / 23 深夜
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# 周一~周日（datetime.weekday()：0=周一）
_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 时段词 + 小时上界（左闭右开）；最后一档兜到 24。
# 顺序即分档顺序——23 点单独回到「深夜」，所以深夜档出现两次（首尾）。
_PERIOD_BUCKETS: tuple[tuple[int, str], ...] = (
    (4, "深夜"),    # 00-03
    (6, "凌晨"),    # 04-05
    (9, "早上"),    # 06-08
    (12, "上午"),   # 09-11
    (14, "中午"),   # 12-13
    (17, "下午"),   # 14-16
    (19, "傍晚"),   # 17-18
    (23, "晚上"),   # 19-22
    (24, "深夜"),   # 23
)

# 对外常量：所有可能的时段词（测试守护「每小时都有归属」）
PERIOD_OF_DAY = tuple(dict.fromkeys(p for _, p in _PERIOD_BUCKETS))


def _enabled() -> bool:
    return os.environ.get("DESKMATE_CLOCK", "1") != "0"


def period_of_day(hour: int) -> str:
    """小时 → 时段词。非法值按夹断到 0-23 处理（fail-open 不抛错）。"""
    h = max(0, min(23, int(hour)))
    for upper, word in _PERIOD_BUCKETS:
        if h < upper:
            return word
    return "深夜"  # 理论不可达（23 < 24 已命中），留作兜底


def clock_context(now: Optional[datetime] = None) -> Optional[str]:
    """对话注入块：标题 + 一行纯事实。

    「## 此刻\\n现在是 2026 年 10 月 15 日 周四 晚上 22:06」——
    标题与天气块「## 此刻窗外」同族。开关关/异常 → None。
    """
    if not _enabled():
        return None
    try:
        now = now or datetime.now()
        line = (
            f"现在是 {now.year} 年 {now.month} 月 {now.day} 日 "
            f"{_WEEKDAYS_CN[now.weekday()]} {period_of_day(now.hour)} "
            f"{now:%H:%M}"
        )
        return f"## 此刻\n{line}"
    except Exception:
        logger.debug("clock_context failed", exc_info=True)
        return None
