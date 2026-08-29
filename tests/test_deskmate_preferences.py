"""Tests for P1-2 主动建议的事后反馈闭环（deskmate/proactivity/preferences.py + tick 注入）。

核心：被接受的建议沉淀为「行为偏好」→ judge 决策时注入，让主动本身越用越懂。
独立文件（proactivity/preferences.json），不碰 memory manager。
"""

from __future__ import annotations

import time

import pytest

from deskmate.proactivity.preferences import (
    PreferenceEntry,
    PreferenceStore,
)
from deskmate.proactivity.tick import ProactiveEngine


class TestPreferenceStore:
    def test_record_accepted_accumulates(self, tmp_path):
        s = PreferenceStore(tmp_path)
        s.record_accepted("plot_contradiction", "第3章时间线矛盾")
        s.record_accepted("plot_contradiction", "第5章伏笔没接")
        s.record_accepted("timeline_error", "时间线错乱")
        assert s._prefs["plot_contradiction"].accepted_count == 2
        assert s._prefs["timeline_error"].accepted_count == 1

    def test_active_threshold_and_decay(self, tmp_path):
        s = PreferenceStore(tmp_path)
        s.record_accepted("plot_contradiction", "a")
        s.record_accepted("plot_contradiction", "b")  # 达阈值
        s.record_accepted("missing_foreshadow", "c")  # 仅 1 次，未达阈值
        # 造一条旧数据（>30 天）→ 不活跃
        s._prefs["timeline_error"] = PreferenceEntry(
            type="timeline_error",
            accepted_count=3,
            first_ts=time.time() - 40 * 86400,
            last_ts=time.time() - 40 * 86400,
        )
        act = s.active_preferences()
        types = {p["type"] for p in act}
        assert "plot_contradiction" in types      # 达阈值且活跃
        assert "missing_foreshadow" not in types  # 未达阈值
        assert "timeline_error" not in types      # 已衰减
        # 排序：接受次数多者在前
        assert act[0]["accepted_count"] >= act[-1]["accepted_count"]

    def test_to_context_empty_and_nonempty(self, tmp_path):
        s = PreferenceStore(tmp_path)
        assert s.to_context() == ""  # 无活跃偏好
        s.record_accepted("plot_contradiction", "时间线")
        s.record_accepted("plot_contradiction", "伏笔")
        ctx = s.to_context()
        assert "情节矛盾提醒" in ctx
        assert "2" in ctx

    def test_persist_roundtrip(self, tmp_path):
        s = PreferenceStore(tmp_path)
        s.record_accepted("timeline_error", "内容")
        s.record_accepted("timeline_error", "内容2")
        s2 = PreferenceStore(tmp_path)  # 重读
        assert s2._prefs["timeline_error"].accepted_count == 2
        assert s2._prefs["timeline_error"].last_content == "内容2"


class TestTickPreferenceIntegration:
    def test_record_feedback_accepted_sediments_preference(self, tmp_path):
        """用户接受建议 → 反查类型 → 沉淀偏好。"""
        from deskmate.proactivity.disturb import SuggestionRecord
        from deskmate.proactivity.preferences import PreferenceStore

        engine = ProactiveEngine(base_dir=tmp_path)
        # 造一条未回应的 plot_contradiction 建议记录
        engine.disturb.records.append(
            SuggestionRecord(
                suggestion_type="plot_contradiction",
                suggestion_key="第3章时间线矛盾",
                timestamp=time.time(),
                accepted=None,
            )
        )
        engine.record_feedback("第3章时间线矛盾", True)
        s = PreferenceStore(tmp_path)
        assert s._prefs["plot_contradiction"].accepted_count == 1

    def test_judge_context_contains_preference(self, tmp_path, monkeypatch):
        """judge 收到的上下文含用户行为偏好。"""
        from deskmate.proactivity.preferences import PreferenceStore

        s = PreferenceStore(tmp_path)
        s.record_accepted("missing_foreshadow", "第2章伏笔")
        s.record_accepted("missing_foreshadow", "第4章伏笔")

        from deskmate.perception import window_monitor as wm
        from deskmate.proactivity import tick as tick_mod

        class _Win:
            pass

        _Win.app = "Finder"
        _Win.title = "Desktop"
        _Win.url = None
        _Win.bundle_id = ""
        _Win.pid = 0

        captured: dict[str, str] = {}

        def _spy_judge(context_text, model=None):
            captured["text"] = context_text
            from deskmate.proactivity.judge import JudgmentResult

            return JudgmentResult(
                should_act=False,
                confidence=0.2,
                suggestion_type="none",
                suggestion_content="",
                reasoning="test",
            )

        monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
        monkeypatch.setattr(tick_mod, "judge_proactive_action", _spy_judge)
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.tick(user_message="", skip_activity_check=True)
        assert "用户行为偏好" in captured.get("text", "")
        assert "伏笔遗漏提醒" in captured.get("text", "")
