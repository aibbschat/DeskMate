"""Tests for P2-4 桌面常驻「看见你在做什么」主动陪伴（deskmate/proactivity/companion.py）。"""

from __future__ import annotations

import time

import pytest

from deskmate.perception.window_monitor import ActiveWindow
from deskmate.proactivity.companion import (
    CompanionTracker,
    FocusSession,
    describe_activity,
)
from deskmate.proactivity.user_active import ActivityState, UserActivity


def _win(app="Visual Studio Code", title="openworker — tick.py", bundle_id="com.microsoft.VSCode"):
    return ActiveWindow(app=app, title=title, bundle_id=bundle_id, pid=1, url=None)


def _activity(level=UserActivity.ACTIVE, idle=0, app="Visual Studio Code", title="openworker"):
    return ActivityState(level=level, idle_seconds=idle, window_title=title, app_name=app, reason="")


def _session(start_min=0, app="Visual Studio Code", bundle_id="com.microsoft.VSCode", notified=None):
    """构造一个真实 FocusSession 用于测试。"""
    return FocusSession(
        start_ts=time.time() - start_min * 60,
        last_ts=time.time(),
        app=app,
        bundle_id=bundle_id,
        notified_minutes=set(notified or []),
    )


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
        tracker._session = _session(start_min=31, notified=[])
        msg = tracker.update(_activity(), _win())
        assert msg is not None
        assert "半小时" in msg or "30" in msg

    def test_trigger_at_60_min(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=61, notified=[30])
        msg = tracker.update(_activity(), _win())
        assert msg is not None
        assert "1 小时" in msg or "60" in msg

    def test_dedup_same_threshold(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=35, notified=[30])
        msg = tracker.update(_activity(), _win())
        assert msg is None  # 30 分钟已通知过，不重复

    def test_switch_to_distraction_resets(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=60, notified=[30])
        # 切到微信（聊天 = 分心类）→ 离开工作流，重置
        msg = tracker.update(
            _activity(app="WeChat"),
            _win(app="WeChat", bundle_id="com.tencent.xinWeChat"),
        )
        assert msg is None
        assert tracker._session is None  # 切到分心类，专注计时清零

    def test_switch_between_work_apps_keeps_session(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=35, notified=[30])
        # 切到 Safari（浏览网页 = 非分心类）→ 不重置，继续累计
        msg = tracker.update(
            _activity(app="Safari"),
            _win(app="Safari", bundle_id="com.apple.Safari"),
        )
        assert msg is None
        assert tracker._session is not None  # 会话保留
        assert tracker._session.app == "Safari"  # 更新为当前窗口
        assert tracker._session.notified_minutes == {30}  # 计时未清零

    def test_idle_resets(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=60, notified=[30, 60])
        msg = tracker.update(_activity(level=UserActivity.IDLE), _win())
        assert msg is None
        assert tracker._session is None  # idle 重置

    def test_busy_resets(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=60, notified=[30])
        msg = tracker.update(_activity(level=UserActivity.BUSY), _win())
        assert msg is None
        assert tracker._session is None

    def test_reset_method(self):
        tracker = CompanionTracker()
        tracker._session = _session(start_min=0, notified=[])
        tracker.reset()
        assert tracker._session is None

    def test_her_line_preferred(self, monkeypatch, tmp_path):
        """活感 v2 §三.3：有她的近作时，陪伴语优先用她的声音，而非固定模板。"""
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        diary = tmp_path / "diary"
        diary.mkdir()
        (diary / "2026-10-13.md").write_text(
            "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
            encoding="utf-8",
        )
        tracker = CompanionTracker()
        tracker._session = _session(start_min=31, notified=[])
        msg = tracker.update(_activity(), _win())
        assert msg == "今天写完一章，心里亮亮的"

    def test_no_her_line_falls_to_template(self, monkeypatch, tmp_path):
        """无她的历史 → 仍落固定模板（最后防线，陪伴语永远有）。"""
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        tracker = CompanionTracker()
        tracker._session = _session(start_min=31, notified=[])
        msg = tracker.update(_activity(), _win())
        assert msg is not None
        assert "半小时" in msg or "30" in msg

    def test_her_line_cooldown_falls_to_template(self, monkeypatch, tmp_path):
        """用过的近作进冷却：唯一近作在冷却中 → 落固定模板，不重复复读。"""
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        diary = tmp_path / "diary"
        diary.mkdir()
        (diary / "2026-10-13.md").write_text(
            "# 我们的回忆\n\n## 09:00 <!--xj:e1-->\n今天写完一章，心里亮亮的\n",
            encoding="utf-8",
        )
        tracker = CompanionTracker()
        tracker._session = _session(start_min=31, notified=[])
        tracker._session.used_lines = {"今天写完一章，心里亮亮的"}
        msg = tracker.update(_activity(), _win())
        assert msg != "今天写完一章，心里亮亮的"
