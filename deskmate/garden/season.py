"""四季与昼夜 —— 花园的确定性时间层。

设计要点（呼应「细节要真正落地可见」）：
- **纯本地时间计算，零 LLM、零后端依赖**：季节由日期派生、昼夜由小时派生，
  前端与后端可各自独立计算，结果必然一致（同一时区同一时刻）。
- **确定性 &gt; 检测**：花园的视觉与互动都挂在这层时间刻度上，
  不需要任何场景识别的偶然性，改了系统时间即可复现验证。
- **不打扰**：季节/昼夜只改变视觉主题与轻提示的措辞，不改变信息架构。

对外只暴露两个函数 + 常量表，便于前后端共用同一套语义。
"""

from __future__ import annotations

import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# 四季
# ---------------------------------------------------------------------------
SEASONS: list[dict] = [
    {"key": "spring", "label": "春", "emoji": "🌸", "months": (3, 4, 5)},
    {"key": "summer", "label": "夏", "emoji": "🌿", "months": (6, 7, 8)},
    {"key": "autumn", "label": "秋", "emoji": "🍁", "months": (9, 10, 11)},
    {"key": "winter", "label": "冬", "emoji": "❄️", "months": (12, 1, 2)},
]


def season_of(date: Optional[datetime.date | str] = None) -> dict:
    """日期 → 季节。默认今天（本地）。

    Returns: ``{"key", "label", "emoji", "months"}``
    """
    if isinstance(date, str):
        try:
            date = datetime.date.fromisoformat(date)
        except ValueError:
            date = None
    if date is None:
        date = datetime.date.today()
    m = date.month
    for s in SEASONS:
        if m in s["months"]:
            return dict(s)
    return dict(SEASONS[2])  # 兜底：秋（现有暖橘基座）


# ---------------------------------------------------------------------------
# 昼夜
# ---------------------------------------------------------------------------
DAYPARTS: list[dict] = [
    {"key": "morning", "label": "晨", "emoji": "🌅", "hours": (5, 6, 7, 8, 9, 10)},
    {"key": "day", "label": "昼", "emoji": "☀️", "hours": (11, 12, 13, 14, 15, 16)},
    {"key": "dusk", "label": "黄昏", "emoji": "🌇", "hours": (17, 18, 19)},
    {"key": "night", "label": "深夜", "emoji": "🌙", "hours": (20, 21, 22, 23, 0, 1, 2, 3, 4)},
]


def daypart_of(hour: Optional[int] = None) -> dict:
    """小时 → 昼夜。默认当前本地小时。

    Returns: ``{"key", "label", "emoji", "hours"}``
    """
    if hour is None:
        hour = datetime.datetime.now().hour
    for d in DAYPARTS:
        if hour in d["hours"]:
            return dict(d)
    return dict(DAYPARTS[0])  # 兜底：晨


def now_clock(base_dir: Optional[str] = None) -> dict:
    """当前时刻的花园时间刻度：``{"season", "daypart", "date", "hour"}``。

    供后端 API 与前端 visit 共用，保证同一时刻返回同一组值。
    """
    now = datetime.datetime.now()
    return {
        "season": season_of(now.date()),
        "daypart": daypart_of(now.hour),
        "date": now.date().isoformat(),
        "hour": now.hour,
        "iso": now.isoformat(timespec="seconds"),
    }
