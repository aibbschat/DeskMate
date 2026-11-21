"""Tests for deskmate.growth module."""

from __future__ import annotations

import datetime
from pathlib import Path

from deskmate.growth.engine import (
    GrowthEngine,
    MATCH_PROFICIENCY_WEIGHT,
    compute_proficiency,
    level_from_value,
)
from deskmate.growth.state import GrowthState, load_state, save_state


# ============================================================
# STATE
# ============================================================


class TestState:
    def test_save_load_roundtrip(self, tmp_path: Path):
        st = GrowthState(intimacy=42.0, trust=0.7, growth_value=30, level=3)
        st.skill_proficiency["novel-review"] = st.skill_proficiency.get(
            "novel-review"
        )
        from deskmate.growth.state import SkillProficiency

        st.skill_proficiency["novel-review"] = SkillProficiency(
            uses=5, success_rate=0.8, last_used="2026-08-01", proficiency=0.6
        )
        save_state(st, tmp_path)
        loaded = load_state(tmp_path)
        assert loaded.intimacy == 42.0
        assert loaded.trust == 0.7
        assert loaded.level == 3
        assert loaded.skill_proficiency["novel-review"].uses == 5

    def test_load_missing_returns_default(self, tmp_path: Path):
        st = load_state(tmp_path / "nonexistent")
        assert st.level == 1
        assert st.trust == 0.5

    def test_load_corrupt_returns_default(self, tmp_path: Path):
        (tmp_path / "growth.json").write_text("{not json", encoding="utf-8")
        st = load_state(tmp_path)
        assert st.level == 1


# ============================================================
# PROFICIENCY
# ============================================================


class TestProficiency:
    def test_zero_uses_zero_proficiency(self):
        assert compute_proficiency(0, 0.0, "") < 0.21

    def test_high_usage_high_rate_high_proficiency(self):
        p = compute_proficiency(20, 1.0, "2026-08-01")
        assert p >= 0.9

    def test_uses_capped(self):
        p1 = compute_proficiency(20, 0.5, "2026-08-01")
        p2 = compute_proficiency(100, 0.5, "2026-08-01")
        assert abs(p1 - p2) < 0.01  # 超过 target 不再增长

    def test_recency_decay(self):
        # 日期相对今天取，不写死 —— 硬编码 "2026-08-01" 会随着时间推移
        # 滑出 30 天衰减窗口，导致 fresh 与 stale 同值，测试凭空失败。
        today = datetime.date.today()
        fresh = compute_proficiency(10, 0.8, today.isoformat())
        stale = compute_proficiency(10, 0.8, (today - datetime.timedelta(days=60)).isoformat())
        assert stale < fresh


# ============================================================
# LEVEL
# ============================================================


class TestLevel:
    def test_level_boundaries(self):
        assert level_from_value(0) == 1
        assert level_from_value(4) == 1
        assert level_from_value(5) == 2
        assert level_from_value(14) == 2
        assert level_from_value(15) == 3

    def test_maxed(self):
        assert level_from_value(10_000) == len([0, 5, 15, 30, 50, 80, 120, 170, 230, 300, 400, 520])


# ============================================================
# ENGINE EVENTS
# ============================================================


class TestEngine:
    def test_skill_use_updates_proficiency_and_xp(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        eng.record_skill_created("novel-review")
        eng.record_skill_use("novel-review", success=True, skill_store_uses=3, skill_store_rate=0.9)
        snap = eng.snapshot()
        assert snap["growth_value"] >= 10 + 2
        assert snap["skill_proficiency"]["novel-review"]["proficiency"] > 0
        assert snap["skill_proficiency"]["novel-review"]["uses"] == 3

    def test_suggestion_feedback_trust(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        eng.record_suggestion_feedback(True)
        assert eng.state.trust > 0.5
        eng.record_suggestion_feedback(True)
        eng.record_suggestion_feedback(True)
        eng.record_suggestion_feedback(False)
        assert eng.state.trust < 0.5 + 0.03 * 3  # 4 次净 +3 次接受 -1 忽略
        assert eng.state.empathy > 0
        assert eng.state.mood < 100

    def test_memory_learned_increases_empathy(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        eng.record_memory_learned(3)
        assert eng.state.empathy > 0
        assert eng.state.intimacy > 0

    def test_vitality_decays_on_use_recovers_on_tick(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        v0 = eng.state.vitality
        eng.record_skill_use("x", success=True, skill_store_uses=1, skill_store_rate=0.9)
        assert eng.state.vitality < v0
        eng.tick_decay()
        assert eng.state.vitality > eng.state.vitality - 0.1  # 恢复方向

    def test_persist_across_instances(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        eng.record_skill_created("a")
        eng2 = GrowthEngine(tmp_path)
        assert "a" in eng2.state.skill_proficiency


class TestDailyEventLedger:
    """E2：按天的持久事件账。

    recent_events 落盘时被截到最后 50 条，是个**会滚走的窗口**。跨天回看时
    前几天的事件早被挤没了，拿它统计「那天发生了什么」必然漏。情感时间线
    要按天推断情绪，只能靠这份不会滚走的账。
    """

    def test_add_event_writes_day_ledger(self, tmp_path: Path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.record_creation_feedback(True)
        eng.record_creation_feedback(False)
        day = datetime.date.today().isoformat()
        assert eng.state.daily_events[day]["creation_approved"] == 1
        assert eng.state.daily_events[day]["creation_rework"] == 1

    def test_ledger_survives_reload(self, tmp_path: Path):
        eng = GrowthEngine(base_dir=tmp_path)
        eng.record_creation_feedback(True)
        day = datetime.date.today().isoformat()
        assert GrowthEngine(base_dir=tmp_path).state.daily_events[day]["creation_approved"] == 1

    def test_ledger_keeps_days_that_fell_out_of_recent_events(self, tmp_path: Path):
        """把 recent_events 撑爆之后，当天发生的事仍然查得到 —— 这正是它存在的理由。"""
        eng = GrowthEngine(base_dir=tmp_path)
        eng.record_creation_feedback(True)
        day = datetime.date.today().isoformat()
        for _ in range(80):
            eng.state.recent_events.append(
                {"ts": "2020-01-01T00:00:00+00:00", "kind": "noise"}
            )
        eng.save()
        reloaded = GrowthEngine(base_dir=tmp_path)
        assert len(reloaded.state.recent_events) <= 50       # 窗口滚走了
        assert reloaded.state.daily_events[day]["creation_approved"] == 1

    def test_events_for_prefers_ledger_over_window(self, tmp_path: Path):
        from deskmate.growth.emotion_timeline import EmotionTimeline

        eng = GrowthEngine(base_dir=tmp_path)
        day = datetime.date.today().isoformat()
        eng.state.daily_events[day] = {"creation_approved": 3}
        eng.state.recent_events = []     # 窗口里一条都没有
        assert EmotionTimeline._events_for(day, eng.state)["creation_approved"] == 3

    def test_events_for_falls_back_to_window_on_legacy_state(self, tmp_path: Path):
        """老状态没有 daily_events → 回落到 recent_events（结果可能不全，但总比没有强）。"""
        from deskmate.growth.emotion_timeline import EmotionTimeline

        eng = GrowthEngine(base_dir=tmp_path)
        eng.state.daily_events = {}
        eng.state.recent_events = [
            {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "kind": "creation_approved"}
        ]
        assert EmotionTimeline._events_for(
            datetime.date.today().isoformat(), eng.state
        )["creation_approved"] == 1

    def test_ledger_trimmed_to_recent_days(self):
        st = GrowthState()
        for i in range(40):
            day = (datetime.date.today() - datetime.timedelta(days=i)).isoformat()
            st.daily_events[day] = {"chat": 1}
        st.daily_events["2020-01-01"] = {"chat": 1}
        out = st.to_dict()["daily_events"]
        assert len(out) <= 30
        assert "2020-01-01" not in out

    def test_dirty_ledger_entries_are_dropped(self):
        st = GrowthState()
        st.daily_events["2026-09-05"] = {"chat": 1, "bad": "x", "worse": None}
        assert st.to_dict()["daily_events"]["2026-09-05"] == {"chat": 1}


# ============================================================
# MODULATION（行为调制 = 真实后果）
# ============================================================


class TestModulation:
    def test_skill_ranking_bounded(self):
        eng = GrowthEngine()
        # 熟练度 0 → 权重下降；熟练度 1 → 权重上升
        low = eng.modulate_skill_ranking(10, "nonexistent-skill")
        assert low < 10
        eng.state.skill_proficiency["x"] = __import__(
            "deskmate.growth.state", fromlist=["SkillProficiency"]
        ).SkillProficiency(uses=20, success_rate=1.0, last_used="2026-08-01", proficiency=1.0)
        high = eng.modulate_skill_ranking(10, "x")
        assert high > 10
        # ±20% 内
        assert low >= 8.0 and high <= 12.0

    def test_proactive_threshold_modulated(self):
        eng = GrowthEngine()
        base = 0.8
        eng.state.trust = 0.5
        # 默认 trust 0.5 → 阈值不变
        assert abs(eng.modulate_proactive_threshold(base) - base) < 1e-9
        eng.state.trust = 0.8  # 高信任 → 阈值下调（更主动）
        assert eng.modulate_proactive_threshold(base) < base
        # 可关闭
        eng.state.trust_controls_proactive = False
        assert eng.modulate_proactive_threshold(base) == base

    def test_memory_budget_scaled(self):
        eng = GrowthEngine()
        base = 6000
        eng.state.empathy = 0
        low = eng.modulate_memory_budget(base)
        eng.state.empathy = 100
        high = eng.modulate_memory_budget(base)
        assert low < high
        assert low >= 6000 * 0.9 and high <= 6000 * 1.3

    def test_tone_prompt_by_intimacy(self):
        eng = GrowthEngine()
        eng.state.intimacy = 0
        assert eng.modulate_tone_prompt() == ""
        eng.state.intimacy = 80
        out = eng.modulate_tone_prompt()
        assert "亲密度 80" in out  # 只给事实，不给语气演出指令
        assert "老朋友" not in out

    def test_proactive_frequency_by_vitality(self):
        eng = GrowthEngine()
        eng.state.vitality = 100
        fast = eng.modulate_proactive_frequency(60.0)
        eng.state.vitality = 0
        slow = eng.modulate_proactive_frequency(60.0)
        assert slow > fast  # 活力低 → 间隔更长（不打扰）

    def test_skill_use_recomputes_level(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        # 升到 3 级需要 15 点成长值；技能创建 10 + 每次使用 2 → 3 次使用够 16 点
        eng.record_skill_created("x")
        eng.record_skill_use("x", success=True, skill_store_uses=1, skill_store_rate=0.9)
        eng.record_skill_use("x", success=True, skill_store_uses=2, skill_store_rate=0.9)
        eng.record_skill_use("x", success=True, skill_store_uses=3, skill_store_rate=0.9)
        assert eng.state.growth_value >= 15
        assert eng.state.level == 3  # record_skill_use 必须即时重算等级

    def test_vitality_decays_on_suggestion_and_memory(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        v0 = eng.state.vitality
        eng.record_suggestion_feedback(True)
        assert eng.state.vitality < v0
        v1 = eng.state.vitality
        eng.record_memory_learned(1)
        assert eng.state.vitality < v1

    def test_tick_decay_recovers_vitality(self, tmp_path: Path):
        eng = GrowthEngine(tmp_path)
        eng.state.vitality = 50.0
        eng.tick_decay()
        assert eng.state.vitality > 50.0
        # 心情不再被 tick 改动：它现在是当日情绪的派生值（E1），
        # 由 emotion_timeline 结算写入，不靠「向 80 收敛」这种无来源的微调。
        eng.state.mood = 90.0
        eng.tick_decay()
        assert eng.state.mood == 90.0
