"""Tests for P1-1 打扰分寸接入专注度。

deskmate/perception/focus.py（专注度合成）+ tick 深专注拦截（SUGGEST 静默）。
纯规则零 LLM；全屏检测 mock 掉避免真调 osascript。
"""

from __future__ import annotations

import pytest

from deskmate.perception import focus as focus_mod
from deskmate.perception.focus import get_focus_score
from deskmate.proactivity.judge import JudgmentResult
from deskmate.proactivity.tick import ActionType, ProactiveEngine


@pytest.fixture(autouse=True)
def _no_real_fullscreen(monkeypatch):
    """默认 mock 全屏检测为不可用（None），避免测试真调 osascript。"""
    monkeypatch.setattr(focus_mod, "_fullscreen", lambda: None)


class TestFocusScore:
    def test_deep_code_editor(self):
        f = get_focus_score(app_name="Code", window_title="chat_flow.py")
        assert f.level == "deep"
        assert f.score >= 0.7

    def test_deep_writing(self):
        f = get_focus_score(app_name="Typora", window_title="第3章 手稿")
        assert f.level == "deep"

    def test_light_browser_lowered(self):
        f = get_focus_score(app_name="Safari", window_title="研究资料")
        # 内容消费/阅读降分，不会误判为 deep
        assert f.level != "deep"
        assert f.score < 0.5

    def test_casual_other(self):
        f = get_focus_score(app_name="Finder", window_title="Desktop")
        assert f.level == "casual"

    def test_score_clamped_range(self):
        for i in range(30):
            f = get_focus_score(app_name="Code", window_title="x" * i)
            assert 0.0 <= f.score <= 1.0
            assert f.level in ("deep", "light", "casual")

    def test_fail_open_empty(self):
        f = get_focus_score()
        assert f.level == "casual"
        assert f.score == pytest.approx(0.5)

    def test_fullscreen_boosts(self, monkeypatch):
        monkeypatch.setattr(focus_mod, "_fullscreen", lambda: True)
        f = get_focus_score(app_name="Keynote")
        assert f.score >= 0.7
        assert f.level == "deep"


class TestTickDeepFocus:
    """tick 深专注拦截：深专注时 SUGGEST 静默，不弹建议。"""

    @staticmethod
    def _judge_suggest(*_a, **_k):
        return JudgmentResult(
            should_act=True,
            confidence=0.9,
            suggestion_type="plot_contradiction",
            suggestion_content="第3章与第1章时间线矛盾",
            reasoning="test",
        )

    def _engine_with_win(self, tmp_path, monkeypatch, app, title):
        from deskmate.perception import window_monitor as wm
        from deskmate.proactivity import tick as tick_mod

        class _Win:
            pass

        _Win.app = app
        _Win.title = title
        _Win.url = None
        _Win.bundle_id = ""
        _Win.pid = 0

        monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
        monkeypatch.setattr(tick_mod, "judge_proactive_action", self._judge_suggest)
        return ProactiveEngine(base_dir=tmp_path)

    def test_deep_focus_suppresses_suggest(self, tmp_path, monkeypatch):
        engine = self._engine_with_win(tmp_path, monkeypatch, "Code", "main.py")
        res = engine.tick(user_message="", skip_activity_check=True)
        # 深专注（编辑器写码）→ SUGGEST 被拦为 NO_ACTION
        assert res.action == ActionType.NO_ACTION
        assert "深专注" in (res.skipped_reason or "")

    def test_casual_focus_not_suppressed_by_focus(self, tmp_path, monkeypatch):
        engine = self._engine_with_win(tmp_path, monkeypatch, "Finder", "Desktop")
        res = engine.tick(user_message="", skip_activity_check=True)
        # casual 不因专注度拦截（可能是 SUGGEST 或防打扰/其它原因，但 reason 不含「深专注」）
        assert "深专注" not in (res.skipped_reason or "")

    def test_focus_signal_in_judge_context(self, tmp_path, monkeypatch):
        """judge 收到的上下文应含专注度标注（prompt 第 8 条依赖它）。"""
        from deskmate.perception import window_monitor as wm
        from deskmate.proactivity import tick as tick_mod
        from deskmate.proactivity.context_builder import format_context_for_judge

        class _Win:
            app = "Typora"
            title = "第3章"
            url = None
            bundle_id = ""
            pid = 0

        captured: dict[str, str] = {}

        def _spy_judge(context_text, model=None):
            captured["text"] = context_text
            return self._judge_suggest(context_text, model=model)

        monkeypatch.setattr(wm, "get_active_window", lambda: _Win())
        monkeypatch.setattr(tick_mod, "judge_proactive_action", _spy_judge)
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.tick(user_message="", skip_activity_check=True)
        assert "专注度" in captured.get("text", "")
        assert "deep" in captured.get("text", "")
