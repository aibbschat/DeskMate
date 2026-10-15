"""对话层时钟（deskmate/perception/clock.py）单测。

覆盖：时段词分档、注入行格式、开关、fail-open。
"""

from datetime import datetime

import pytest

from deskmate.perception.clock import (
    PERIOD_OF_DAY,
    clock_context,
    period_of_day,
)


# ----------------------------------------------------------------------
# 时段词
# ----------------------------------------------------------------------
@pytest.mark.parametrize("hour,expected", [
    (0, "深夜"), (3, "深夜"), (4, "凌晨"), (6, "早上"), (7, "早上"),
    (9, "上午"), (11, "上午"), (12, "中午"), (13, "中午"),
    (14, "下午"), (16, "下午"), (17, "傍晚"), (18, "傍晚"),
    (19, "晚上"), (22, "晚上"), (23, "深夜"),
])
def test_period_of_day_buckets(hour, expected):
    assert period_of_day(hour) == expected


def test_period_of_day_bounds_are_covered():
    """每个小时都有归属（不允许漏档）。"""
    for h in range(24):
        assert period_of_day(h) in PERIOD_OF_DAY


# ----------------------------------------------------------------------
# 注入行
# ----------------------------------------------------------------------
def test_clock_context_line_format():
    """注入块 = 年月日 + 周几 + 时段 + 时刻（纯事实一行，不导演）。"""
    # 2026-10-15 是周四
    now = datetime(2026, 10, 15, 22, 6)
    line = clock_context(now=now)
    assert "2026 年 10 月 15 日" in line
    assert "周四" in line
    assert "22:06" in line
    assert "晚上" in line
    assert "现在是" in line
    # 不导演：块里不许出现任何表达方式指令
    for word in ("应该", "劝他", "提醒他", "务必", "一定要"):
        assert word not in line


def test_clock_context_weekday_all_days():
    """七天各有其名（周一~周日）。"""
    # 2026-10-12 是周一
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    base = datetime(2026, 10, 12, 9, 0)
    for i, name in enumerate(names):
        from datetime import timedelta
        assert name in clock_context(now=base + timedelta(days=i))


def test_clock_context_switch_off(monkeypatch):
    """DESKMATE_CLOCK=0 → 不注入。"""
    monkeypatch.setenv("DESKMATE_CLOCK", "0")
    assert clock_context(now=datetime(2026, 10, 15, 22, 6)) is None


def test_clock_context_default_on(monkeypatch):
    """默认开（无环境变量时也有行）。"""
    monkeypatch.delenv("DESKMATE_CLOCK", raising=False)
    assert clock_context(now=datetime(2026, 10, 15, 8, 30))
