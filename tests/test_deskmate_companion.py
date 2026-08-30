"""Tests for P2-4 桌面常驻「看见你在做什么」主动陪伴（deskmate/proactivity/companion.py）。"""

from __future__ import annotations

import time

import pytest

from deskmate.perception.window_monitor import ActiveWindow
from deskmate.proactivity.companion import (
    CompanionTracker,
    describe_activity,
)
from deskmate.proactivity.user_active import ActivityState, UserActivity


def _win(app="Visual Studio Code", title="openworker — tick.py", bundle_id="com.microsoft.VSCode"):
    return ActiveWindow(app=app, title=title, bundle_id=bundle_id, pid=1, url=None)


def _activity(level=UserActivity.ACTIVE, idle=0, app="Visual Studio Code", title="openworker"):
    return ActivityState(level=level, idle_seconds=idle, window_title=title, app_name=app, reason="")


class TestDescribeActivity:
    def test_vscode(self):
        desc = describe_activity(_win(), _activity())
        assert "Visual Studio Code" in desc
        assert "写代码" in desc

    def test_browser(self):
        desc = describe_activity(_win(app="Safari", title="Google", bundle_id="com.apple.Safari"), _activity())
        assert "Safari" in desc
        assert "浏览网页" in desc

    def test_idle(self):
        desc = describe_activity(_win(), _activity(level=UserActivity.IDLE, idle=600))
        assert "离开" in desc

    def test_busy(self):
        desc = describe_activity(_win(), _activity(level=UserActivity.BUSY))
        assert "忙碌" in desc or "会议" in desc

    def test_unknown_app(self):
        desc = describe_activity(_win(app="UnknownApp", title="", bundle_id="com.unknown"), _activity())
        assert "UnknownApp" in desc
        assert "用电脑" in desc

    def test_no_window(self):
        desc = describe_activity(None, _activity())
        assert "用电脑" in desc

    def test_title_hint(self):
        desc = describe_activity(_win(title="my_novel_chapter3.txt"), _activity())
        assert "my_novel_chapter3" in desc


class TestCompanionTracker:
    def test_no_message_at_start(self):
        tracker = CompanionTracker()
        msg = tracker.update(_activity(), _win())
        assert msg is None  # 刚开始，没达到阈值

    def test_trigger_at_30_min(self):
        tracker = CompanionTracker()
        # 模拟 31 分钟前开始
        tracker._session = type("S", (), {
            "start_ts": time.time() - 31 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": set(),
        })()
        msg = tracker.update(_activity(), _win())
        assert msg is not None
        assert "半小时" in msg or "30" in msg

    def test_trigger_at_60_min(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time() - 61 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": {30},
        })()
        msg = tracker.update(_activity(), _win())
        assert msg is not None
        assert "1 小时" in msg or "60" in msg

    def test_dedup_same_threshold(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time() - 35 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": {30},
        })()
        msg = tracker.update(_activity(), _win())
        assert msg is None  # 30 分钟已通知过，不重复

    def test_switch_app_resets(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time() - 60 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": {30},
        })()
        # 切换到 Safari
        msg = tracker.update(_activity(app="Safari"), _win(app="Safari", bundle_id="com.apple.Safari"))
        assert msg is None  # 切换应用重置，新会话刚开始
        assert tracker._session.app == "Safari"

    def test_idle_resets(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time() - 60 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": {30, 60},
        })()
        msg = tracker.update(_activity(level=UserActivity.IDLE), _win())
        assert msg is None
        assert tracker._session is None  # idle 重置

    def test_busy_resets(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time() - 60 * 60,
            "last_ts": time.time(),
            "app": "Visual Studio Code",
            "notified_minutes": {30},
        })()
        msg = tracker.update(_activity(level=UserActivity.BUSY), _win())
        assert msg is None
        assert tracker._session is None

    def test_reset_method(self):
        tracker = CompanionTracker()
        tracker._session = type("S", (), {
            "start_ts": time.time(), "last_ts": time.time(),
            "app": "x", "notified_minutes": set(),
        })()
        tracker.reset()
        assert tracker._session is None
