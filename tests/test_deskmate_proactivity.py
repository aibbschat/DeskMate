"""Tests for deskmate.proactivity module."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from deskmate.proactivity.user_active import (
    UserActivity,
    detect_user_activity,
    get_system_idle_seconds,
)
from deskmate.proactivity.confidence import (
    ActionType,
    THRESHOLD_PREPARE,
    THRESHOLD_SUGGEST,
    evaluate_action,
)
from deskmate.proactivity.disturb import DisturbManager, SuggestionRecord
from deskmate.proactivity.judge import JudgmentResult, _parse_judgment
from deskmate.proactivity.context_builder import (
    PerceptionContext,
    ProactiveContext,
    format_context_for_judge,
)
from deskmate.proactivity.tick import ProactiveEngine, TickResult
from deskmate.proactivity.background import make_extra_tick


# ============================================================
# USER ACTIVE
# ============================================================


class TestUserActive:
    def test_get_system_idle_seconds_returns_int(self):
        # macOS only, may return 0 if not available
        result = get_system_idle_seconds()
        assert isinstance(result, int)
        assert result >= 0

    def test_detect_user_activity_returns_state(self):
        state = detect_user_activity(app_name="Finder", window_title="Desktop")
        assert state.level in (UserActivity.ACTIVE, UserActivity.IDLE, UserActivity.BUSY)

    def test_busy_app_detection(self):
        state = detect_user_activity(app_name="zoom.us", window_title="Meeting Room")
        # Might be BUSY if system idle is low enough
        # We mainly test it doesn't crash
        assert state.level in (UserActivity.ACTIVE, UserActivity.IDLE, UserActivity.BUSY)


# ============================================================
# CONFIDENCE
# ============================================================


class TestConfidence:
    def test_high_confidence_suggest(self):
        result = evaluate_action(0.9)
        assert result.action == ActionType.SUGGEST
        assert result.confidence == 0.9

    def test_medium_confidence_prepare(self):
        result = evaluate_action(0.65)
        assert result.action == ActionType.PREPARE
        assert result.confidence == 0.65

    def test_low_confidence_no_action(self):
        result = evaluate_action(0.3)
        assert result.action == ActionType.NO_ACTION

    def test_boundary_high(self):
        result = evaluate_action(THRESHOLD_SUGGEST)
        assert result.action == ActionType.SUGGEST

    def test_boundary_medium(self):
        result = evaluate_action(THRESHOLD_PREPARE)
        assert result.action == ActionType.PREPARE

    def test_boundary_low(self):
        result = evaluate_action(THRESHOLD_PREPARE - 0.01)
        assert result.action == ActionType.NO_ACTION

    def test_clamp_above_1(self):
        result = evaluate_action(1.5)
        assert result.confidence == 1.0

    def test_clamp_below_0(self):
        result = evaluate_action(-0.5)
        assert result.confidence == 0.0

    def test_custom_reason(self):
        result = evaluate_action(0.9, reason="发现明显矛盾")
        assert result.reason == "发现明显矛盾"

    # --- P2-5 情感关怀专属低门槛（修复「关怀弹不出」软断点） ---

    def test_care_at_low_threshold_suggests(self):
        # 关怀只需 0.6，普通建议 0.65 仍走 PREPARE，这里应直接 SUGGEST
        result = evaluate_action(0.65, suggestion_type="emotional_care")
        assert result.action == ActionType.SUGGEST

    def test_care_at_exact_threshold_suggests(self):
        result = evaluate_action(0.6, suggestion_type="emotional_care")
        assert result.action == ActionType.SUGGEST

    def test_care_below_threshold_no_action(self):
        # 关怀低于 0.6 直接 NO_ACTION，绝不进 PREPARE（不被静默积压）
        result = evaluate_action(0.59, suggestion_type="emotional_care")
        assert result.action == ActionType.NO_ACTION

    def test_care_very_low_no_action(self):
        result = evaluate_action(0.3, suggestion_type="emotional_care")
        assert result.action == ActionType.NO_ACTION

    def test_care_skips_prepare_band(self):
        # 普通建议 0.65 在 PREPARE 带；关怀同置信度必须跳过 PREPARE 直接弹
        normal = evaluate_action(0.65)
        care = evaluate_action(0.65, suggestion_type="emotional_care")
        assert normal.action == ActionType.PREPARE
        assert care.action == ActionType.SUGGEST



# ============================================================
# DISTURB MANAGER
# ============================================================


class TestDisturbManager:
    def test_initial_allows_suggestion(self):
        mgr = DisturbManager()
        assert mgr.should_allow("plot_contradiction", "某建议")

    def test_same_key_cooldown(self):
        mgr = DisturbManager()
        mgr.record_suggestion("type1", "key1")
        # 同一 key 在冷却期内应该被拦截
        assert not mgr.should_allow("type1", "key1")
        # 不同 key 应该允许
        assert mgr.should_allow("type1", "key2")

    def test_different_type_allows(self):
        mgr = DisturbManager()
        mgr.record_suggestion("type1", "key1")
        # 不同类型 + 不同 key 应该允许
        assert mgr.should_allow("type2", "key2")
        # 同一 key 即使类型不同，也在冷却期内（key 去重）
        assert not mgr.should_allow("type2", "key1")

    def test_consecutive_ignores_blocks(self):
        mgr = DisturbManager()
        # 连续忽略 3 次同类型
        mgr.record_suggestion("type1", "key1", accepted=False)
        mgr.record_suggestion("type1", "key2", accepted=False)
        mgr.record_suggestion("type1", "key3", accepted=False)
        # 第 4 次同类型应该被拦截
        assert not mgr.should_allow("type1", "key4")

    def test_mixed_feedback_allows(self):
        mgr = DisturbManager()
        mgr.record_suggestion("type1", "key1", accepted=False)
        mgr.record_suggestion("type1", "key2", accepted=True)  # 接受了，重置计数
        mgr.record_suggestion("type1", "key3", accepted=False)
        # 只有 1 次连续忽略，应该允许
        assert mgr.should_allow("type1", "key4")

    def test_silent_mode_blocks_all(self):
        mgr = DisturbManager()
        mgr.set_silent_mode(True)
        assert not mgr.should_allow("any_type", "any_key")

    def test_record_feedback_updates(self):
        mgr = DisturbManager()
        mgr.record_suggestion("type1", "key1")
        mgr.record_feedback("key1", accepted=True)
        assert mgr.records[-1].accepted is True

    def test_stats(self):
        mgr = DisturbManager()
        mgr.record_suggestion("type1", "key1", accepted=True)
        stats = mgr.get_stats()
        assert stats["total_records"] == 1
        assert stats["recent_accepted"] == 1

    def test_persist_and_load(self, tmp_path: Path):
        path = tmp_path / "disturb.json"
        mgr1 = DisturbManager()
        mgr1.persist(path)
        mgr1.record_suggestion("type1", "key1", accepted=True)
        assert path.is_file()

        mgr2 = DisturbManager()
        mgr2.persist(path)
        assert len(mgr2.records) == 1
        assert mgr2.records[0].suggestion_type == "type1"

    def test_max_records_trim(self):
        mgr = DisturbManager(max_records=5)
        for i in range(10):
            mgr.record_suggestion("type1", f"key{i}")
        assert len(mgr.records) == 5
        # 保留最新的
        assert mgr.records[-1].suggestion_key == "key9"


# ============================================================
# JUDGE
# ============================================================


class TestJudge:
    def test_parse_valid_json(self):
        response = '{"should_act": true, "confidence": 0.85, "suggestion_type": "plot_contradiction", "suggestion_content": "第3章和第7章矛盾", "reasoning": "发现矛盾"}'
        result = _parse_judgment(response)
        assert result.should_act is True
        assert result.confidence == 0.85
        assert result.suggestion_type == "plot_contradiction"

    def test_parse_json_in_codeblock(self):
        response = '这是分析：\n```json\n{"should_act": false, "confidence": 0.3, "suggestion_type": "none", "suggestion_content": "", "reasoning": "信息不足"}\n```'
        result = _parse_judgment(response)
        assert result.should_act is False
        assert result.confidence == 0.3

    def test_parse_malformed_returns_no_action(self):
        result = _parse_judgment("这不是 JSON")
        assert result.should_act is False
        assert result.confidence == 0.0

    def test_parse_partial_json(self):
        response = '{"should_act": true, "confidence": 0.9}'
        result = _parse_judgment(response)
        assert result.should_act is True
        assert result.confidence == 0.9


# ============================================================
# CONTEXT BUILDER
# ============================================================


class TestContextBuilder:
    def test_format_context_minimal(self):
        ctx = ProactiveContext(
            perception=PerceptionContext("", "", "", "", ""),
            memory_context="",
            user_activity="",
            project_context="",
        )
        result = format_context_for_judge(ctx)
        assert "桌面状态" in result

    def test_format_context_full(self):
        ctx = ProactiveContext(
            perception=PerceptionContext(
                active_window="VS Code | main.py",
                active_file_content="",
                recent_files="main.py modified",
                clipboard="hello world",
                calendar="10:00 会议",
            ),
            memory_context="用户喜欢Python",
            user_activity="状态: active",
            project_context="OpenWorker 开发",
        )
        result = format_context_for_judge(ctx)
        assert "VS Code" in result
        assert "Python" in result
        assert "OpenWorker" in result


# ============================================================
# TICK RESULT
# ============================================================


class TestTickResult:
    def test_no_action_to_dict(self):
        result = TickResult(action=ActionType.NO_ACTION, skipped_reason="用户空闲")
        d = result.to_dict()
        assert d["action"] == "no_action"
        assert d["reason"] == "用户空闲"

    def test_suggest_to_dict(self):
        judgment = JudgmentResult(
            should_act=True,
            confidence=0.9,
            suggestion_type="plot_contradiction",
            suggestion_content="发现矛盾",
            reasoning="测试",
        )
        decision = evaluate_action(0.9)
        result = TickResult(
            action=ActionType.SUGGEST,
            judgment=judgment,
            decision=decision,
        )
        d = result.to_dict()
        assert d["action"] == "suggest"
        assert d["confidence"] == 0.9
        assert d["suggestion_content"] == "发现矛盾"

    def test_has_suggestion_property(self):
        result = TickResult(action=ActionType.NO_ACTION)
        assert not result.has_suggestion

    def test_heartbeat_interval_serialized(self):
        result = TickResult(
            action=ActionType.NO_ACTION,
            skipped_reason="测试",
            heartbeat_interval_ms=180_000,
        )
        d = result.to_dict()
        assert d["heartbeat_interval_ms"] == 180_000

    def test_no_action_still_serializes_user_activity(self):
        from deskmate.proactivity.user_active import ActivityState, UserActivity

        result = TickResult(
            action=ActionType.NO_ACTION,
            skipped_reason="测试",
            activity=ActivityState(
                level=UserActivity.ACTIVE,
                idle_seconds=0,
                window_title="",
                app_name="",
                reason="在桌前",
            ),
            heartbeat_interval_ms=180_000,
        )
        d = result.to_dict()
        assert d["user_activity"] == "active"
        assert d["heartbeat_interval_ms"] == 180_000


# ============================================================
# PROACTIVE ENGINE
# ============================================================


class TestProactiveEngine:
    def test_engine_creation(self):
        engine = ProactiveEngine()
        assert engine is not None

    def test_set_memory_context(self):
        engine = ProactiveEngine()
        engine.set_memory_context("test memory")
        assert engine._memory_context == "test memory"

    def test_set_silent_mode(self, tmp_path: Path):
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.set_silent(True)
        assert engine.disturb.silent_mode is True

    def test_record_feedback(self, tmp_path: Path):
        engine = ProactiveEngine(base_dir=tmp_path)
        engine.disturb.record_suggestion("type1", "key1")
        engine.record_feedback("key1", True)
        assert engine.disturb.records[-1].accepted is True


# ============================================================
# BACKGROUND TICK（scheduler extra_tick 接入）
# ============================================================


class _FakeEngine:
    def __init__(self):
        self.tick_calls = 0
        self.boom = False

    def tick(self):
        self.tick_calls += 1
        if self.boom:
            raise RuntimeError("perception exploded")
        return "tick-ok"


class TestBackgroundTick:
    def test_make_extra_tick_runs_engine(self):
        engine = _FakeEngine()
        tick = make_extra_tick(engine)
        import asyncio

        asyncio.run(tick())
        assert engine.tick_calls == 1

    def test_make_extra_tick_runs_wrapped_first(self):
        engine = _FakeEngine()
        called = []

        async def wrapped():
            called.append("wrapped")

        tick = make_extra_tick(engine, wrapped=wrapped)
        import asyncio

        asyncio.run(tick())
        assert called == ["wrapped"]
        assert engine.tick_calls == 1

    def test_make_extra_tick_isolates_engine_error(self):
        """引擎报错不应向外传播，且 wrapped 仍被执行。"""
        engine = _FakeEngine()
        engine.boom = True
        called = []

        async def wrapped():
            called.append("wrapped")

        tick = make_extra_tick(engine, wrapped=wrapped)
        import asyncio

        # 不抛异常
        asyncio.run(tick())
        assert called == ["wrapped"]
        assert engine.tick_calls == 1

    def test_make_extra_tick_isolates_wrapped_error(self):
        engine = _FakeEngine()

        async def wrapped():
            raise RuntimeError("wrapped boom")

        tick = make_extra_tick(engine, wrapped=wrapped)
        import asyncio

        asyncio.run(tick())  # 不传播
        assert engine.tick_calls == 1
