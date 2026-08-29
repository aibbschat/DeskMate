"""Tests for P2-5 连续情感关怀（deskmate/growth/emotion_timeline.py）。

核心：情感时间线 + 回顾注入 proactive judge，让 AI 记得用户上周的状态并主动关怀。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from deskmate.growth.emotion_timeline import (
    EmotionTimeline,
    _infer_emotion,
)
from deskmate.growth.engine import GrowthEngine


def _make_engine(tmp_path: Path, mood: float = 80.0, events: list[dict] | None = None) -> GrowthEngine:
    eng = GrowthEngine(base_dir=tmp_path)
    eng.state.mood = mood
    eng.state.trust = 0.7
    eng.state.intimacy = 50.0
    if events:
        eng.state.recent_events = events
    eng.save()
    return eng


def _evt(kind: str, days_ago: int = 0) -> dict:
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    ts = datetime.datetime(d.year, d.month, d.day, 12, 0, tzinfo=datetime.timezone.utc).isoformat()
    return {"ts": ts, "kind": kind}


class TestInferEmotion:
    def test_stressed_by_negative(self):
        state, note = _infer_emotion(70.0, {"creation_rework": 1})
        assert state == "压力大"
        assert "不顺" in note

    def test_stressed_by_creative_low_mood(self):
        state, _ = _infer_emotion(70.0, {"writing": 2, "roundtable": 1})
        assert state == "压力大"

    def test_low_mood_with_negative(self):
        state, note = _infer_emotion(55.0, {"user_corrected": 1, "suggestion_ignored": 2})
        assert state == "低落"
        assert "55" in note

    def test_smooth_positive(self):
        state, note = _infer_emotion(85.0, {"creation_approved": 2, "suggestion_accepted": 1})
        assert state == "顺利"
        assert "正反馈" in note

    def test_busy_many_events(self):
        state, _ = _infer_emotion(80.0, {"chat": 5})
        assert state == "忙碌"

    def test_calm_default(self):
        state, note = _infer_emotion(80.0, {})
        assert state == "平静"


class TestEmotionTimeline:
    def test_record_today_creates_snapshot(self, tmp_path):
        eng = _make_engine(tmp_path, mood=72.0, events=[_evt("creation_rework"), _evt("writing")])
        tl = EmotionTimeline(base_dir=tmp_path)
        snap = tl.record_today(eng)
        assert snap["state"] == "压力大"
        assert snap["mood"] == 72.0
        assert snap["events"]["creation_rework"] == 1
        # 持久化
        tl2 = EmotionTimeline(base_dir=tmp_path)
        assert datetime.date.today().isoformat() in tl2._data

    def test_record_today_idempotent(self, tmp_path):
        eng = _make_engine(tmp_path, mood=80.0)
        tl = EmotionTimeline(base_dir=tmp_path)
        tl.record_today(eng)
        eng.state.mood = 90.0
        eng.save()
        tl.record_today(eng)  # 同一天覆盖
        snaps = tl.snapshots(1)
        assert len(snaps) == 1
        assert snaps[0]["mood"] == 90.0

    def test_lookback_with_snapshots(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        # 手动造 3 天快照
        for i, (mood, state, note) in enumerate([
            (70.0, "压力大", "1次打回"),
            (85.0, "顺利", "2次定稿"),
            (80.0, "平静", "平稳"),
        ]):
            d = (datetime.date.today() - datetime.timedelta(days=2 - i)).isoformat()
            tl._data[d] = {"date": d, "mood": mood, "trust": 0.7, "intimacy": 50,
                            "events": {}, "event_count": 0, "state": state, "note": note}
        tl._save()
        recap = tl.lookback(7)
        assert "情感回顾" in recap
        assert "压力大" in recap
        assert "顺利" in recap
        assert "关怀提示" in recap

    def test_lookback_empty_returns_empty(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.lookback(7) == ""

    def test_snapshots_filters_days(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        for i in range(10):
            d = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            tl._data[d] = {"date": d, "mood": 80.0, "state": "平静", "note": "", "events": {}}
        snaps = tl.snapshots(3)
        assert len(snaps) == 3

    def test_care_cooldown(self, tmp_path):
        tl = EmotionTimeline(base_dir=tmp_path)
        assert tl.should_care("stress_check")
        tl.mark_cared("stress_check")
        assert not tl.should_care("stress_check", cooldown_days=7)
        # 不同主题可以
        assert tl.should_care("progress_check")


class TestTickIntegration:
    def test_emotion_injected_into_judge_context(self, tmp_path, monkeypatch):
        """proactive tick 的 judge 上下文应含情感回顾。"""
        from deskmate.perception import window_monitor as wm
        from deskmate.proactivity import tick as tick_mod
        from deskmate.proactivity.tick import ProactiveEngine

        # 造一个有压力信号的 growth state
        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.mood = 68.0
        eng.state.recent_events = [_evt("creation_rework"), _evt("writing")]
        eng.save()

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
                should_act=False, confidence=0.2, suggestion_type="none",
                suggestion_content="", reasoning="test",
            )

        monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
        monkeypatch.setattr(tick_mod, "judge_proactive_action", _spy_judge)
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.tick(user_message="", skip_activity_check=True)
        text = captured.get("text", "")
        assert "情感回顾" in text
        assert "压力大" in text
