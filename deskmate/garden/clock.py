"""花园时钟 —— 把互动挂在「确定发生」的时刻上。

设计要点（用户 2026-09-02 定调：**建起来不难，难的是真正落地可见**）：
- **确定性 &gt; 检测**：所有事件都挂在确定发生的事上——「进入花园 visit」+
  「本地时间 / 距上次来访间隔 / 节日表」。不需要任何场景识别的偶然性，
  改了系统时间即可复现验证（测试就靠这个）。
- **幂等**：每个事件有稳定 key（``morning:2026-09-03`` 等），同一天只触发一次；
  触发即写 ``clock_log``，之后再来不再重复。
- **克制不打扰**：一次 visit 最多只触发**一个**最值得的事件（优先级
  节日 &gt; 归来 &gt; 深夜 &gt; 黄昏 &gt; 晨间），其余候选留给下次进园，避免轰炸。
- **沉淀可回看**：每次触发写入 ``garden_log``，花园「最近痕迹」面板可回看。
- **fail-open**：任何异常返回最小快照，绝不阻断个人中心。

后台兜底：``maybe_settle_garden`` 由 proactivity tick 调用，安静结算花开，
不占用 SUGGEST 通道（与日记灵感同一范式）。
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

from deskmate.garden import season as season_mod
from deskmate.garden.flowers import maybe_bloom
from deskmate.garden.state import load_garden, save_garden

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 节日表（零依赖）
# 固定阳历节日 + 农历节日（春节/中秋）逐年公历映射。农历无法纯计算，
# 用逐年映射表（2026-2031，日期核验自公开历法，多个来源交叉一致），
# 每年初补一年即可，保持「确定性 &gt; 检测」。
# ---------------------------------------------------------------------------
FESTIVALS: dict[str, str] = {
    "01-01": "元旦",
    "02-14": "情人节",
    "10-01": "国庆节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
    "12-31": "跨年夜",
}

# 农历节日逐年公历：春节=正月初一，中秋=八月十五。
# 2026-2029 春节：中华网/中国政府网/whatistheday 交叉确认；2030-2031 春节：腾讯云/佛山Plus/ChineseCalendarOnline。
# 2026-2031 中秋：timeanddate / ChineseCalendarOnline / MoonCal / 族谱网 交叉确认。
LUNAR_FESTIVALS: dict[int, list[tuple[int, int, str]]] = {
    2026: [(2, 17, "春节"), (9, 25, "中秋")],
    2027: [(2, 6, "春节"), (9, 15, "中秋")],
    2028: [(1, 26, "春节"), (10, 3, "中秋")],
    2029: [(2, 13, "春节"), (9, 22, "中秋")],
    2030: [(2, 3, "春节"), (9, 12, "中秋")],
    2031: [(1, 23, "春节"), (10, 1, "中秋")],
}


def _festival_name(now: datetime.datetime) -> Optional[str]:
    """当天是否节日；返回节日名（固定表 + 农历逐年表）。

    双节同庆（如 2031-10-01 国庆中秋同日）时合并显示，如「国庆节·中秋」。
    """
    md = f"{now.month:02d}-{now.day:02d}"
    names: list[str] = []
    if md in FESTIVALS:
        names.append(FESTIVALS[md])
    for m, d, name in LUNAR_FESTIVALS.get(now.year, []):
        if m == now.month and d == now.day:
            names.append(name)
    if not names:
        return None
    return "·".join(names)

# 归来间隔（天）
COMEBACK_DAYS = 3

# 事件优先级：越靠前越优先（一次 visit 只触发一个）
_EVENT_PRIORITY = ["festival", "comeback", "night", "dusk", "morning"]


def _today_str(now: datetime.datetime) -> str:
    return now.date().isoformat()


def _clock_key(event: str, now: datetime.datetime) -> str:
    """事件幂等键。"""
    date = now.date().isoformat()
    if event == "festival":
        return f"festival:{now.year}:{now.month:02d}-{now.day:02d}"
    if event == "comeback":
        return f"comeback:{date}"
    return f"{event}:{date}"


def _candidate_events(garden, now: datetime.datetime) -> list[tuple[str, dict]]:
    """满足触发条件的候选事件（按优先级排序），幂等由调用方检查。"""
    hour = now.hour
    cands: list[tuple[str, dict]] = []

    if 5 <= hour < 11:
        cands.append(("morning", {"kind": "morning", "label": "晨间问候"}))
    elif 17 <= hour < 20:
        cands.append(("dusk", {"kind": "dusk", "label": "黄昏入夜"}))
    elif hour >= 23 or hour < 5:
        cands.append(("night", {"kind": "night", "label": "深夜亮灯"}))

    # 归来：距上次来访 >= 3 天（且不是首次）
    if garden.visit_count > 0 and garden.last_visit_date:
        try:
            last = datetime.date.fromisoformat(garden.last_visit_date)
            if (now.date() - last).days >= COMEBACK_DAYS:
                cands.append(("comeback", {"kind": "comeback", "label": "好久不见"}))
        except ValueError:
            pass

    # 节日（固定 + 农历逐年）
    name = _festival_name(now)
    if name:
        cands.append(("festival", {"kind": "festival", "label": name}))

    # 按优先级排序（高优先在前）
    order = {k: i for i, k in enumerate(_EVENT_PRIORITY)}
    cands.sort(key=lambda x: order.get(x[0], 99))
    return cands


def maybe_visit(base_dir: Path | str | None = None, now: Optional[datetime.datetime] = None) -> dict:
    """前端进入花园时上报：更新来访、判定并触发（最多一个）确定性事件。

    Args:
        base_dir: 花园数据目录（None=默认 ~/.deskmate）
        now: 注入时间（测试用）；默认当前本地时间

    Returns:
        ``{visit_count, last_visit_date, season, daypart, triggered, pending}``
        - triggered: 本次触发的事件 dict（无则 None）
        - pending: 满足条件但本次未触发（留给下次）的事件 key 列表
    """
    try:
        garden = load_garden(base_dir)
        now = now or datetime.datetime.now()

        before_last_visit = garden.last_visit_date
        before_visit_count = garden.visit_count

        # 先判定候选（用「本次来访前」的 last_visit_date，归来间隔以旧值为准），
        # 再更新来访状态——顺序颠倒会让归来判定永远拿到「今天」而失败。
        candidates = _candidate_events(garden, now)

        garden.visit_count += 1
        garden.last_visit_date = _today_str(now)

        # P2 · 每日浇水（幂等 water:date）+ 跨年年花（幂等 year_flowers[year]）
        from deskmate.garden.flowers import (
            year_flower_ensure,
        )

        today_str = _today_str(now)
        watered = today_str not in garden.water_log
        if watered:
            garden.water_log[today_str] = now.isoformat(timespec="seconds")
            _log(garden, {"type": "water", "date": today_str, "at": today_str})
        streak = 0
        cursor = now.date()
        while garden.water_log.get(cursor.isoformat()) is not None:
            streak += 1
            cursor -= datetime.timedelta(days=1)
        yf = year_flower_ensure(garden, now)
        water = {"watered": watered, "streak": streak, "today": today_str}

        # 候选 + 幂等：取第一个未触发过的事件
        triggered = None
        pending: list[str] = []
        for event, payload in candidates:
            key = _clock_key(event, now)
            if key in garden.clock_log:
                continue
            if triggered is None:
                # 触发
                payload = dict(payload)
                payload["key"] = key
                payload["at"] = now.isoformat(timespec="seconds")
                payload["season"] = season_mod.season_of(now.date())
                payload["daypart"] = season_mod.daypart_of(now.hour)
                garden.clock_log[key] = payload["at"]
                _log(garden, {"type": "clock", "event": event, "key": key,
                              "label": payload["label"], "at": payload["at"]})
                # 归来事件需要把间隔天数带给前端
                if event == "comeback":
                    try:
                        last = datetime.date.fromisoformat(before_last_visit)
                        payload["gap_days"] = (now.date() - last).days
                    except ValueError:
                        payload["gap_days"] = None
                triggered = payload
            else:
                pending.append(key)

        save_garden(garden, base_dir)
        return {
            "visit_count": garden.visit_count,
            "last_visit_date": garden.last_visit_date,
            "season": season_mod.season_of(now.date()),
            "daypart": season_mod.daypart_of(now.hour),
            "triggered": triggered,
            "pending": pending,
            "water": water,
            "year_flower": yf,
        }
    except Exception:
        logger.exception("garden maybe_visit failed")
        now = now or datetime.datetime.now()
        return {
            "visit_count": 0,
            "last_visit_date": "",
            "season": season_mod.season_of(now.date()),
            "daypart": season_mod.daypart_of(now.hour),
            "triggered": None,
            "pending": [],
            "water": {"watered": False, "streak": 0, "today": ""},
            "year_flower": {},
        }


def maybe_settle_garden(base_dir: Path | str | None = None) -> Optional[dict]:
    """后台花开结算（proactivity tick 调用）：安静把新事件开成花。

    Returns:
        有新花开返回 ``{"bloomed": n, "flowers": [...]}``；否则 None。
        任何异常返回 None（失败绝不阻断主动推理主流程）。
    """
    try:
        fresh = maybe_bloom(base_dir)
        if not fresh:
            return None
        return {"bloomed": len(fresh), "flowers": fresh}
    except Exception:
        logger.exception("garden maybe_settle failed")
        return None


def _log(garden, entry: dict) -> None:
    garden.garden_log.append(entry)
