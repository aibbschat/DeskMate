"""Tests for 8.2 成长数值「长跑设计」（deskmate/growth/engine.py + state.py）。

核心：关系数值（intimacy/empathy/mood/trust）封顶后，溢出转入 bond_depth（关系深度），
深度推进 bond_stage（初识→熟络→默契→知己→灵魂伴侣），阶段升级记录事件并调制语气。
"""

from __future__ import annotations

import json

import pytest

from deskmate.growth.engine import BOND_STAGES, GrowthEngine
from deskmate.growth.state import load_state, save_state


@pytest.fixture()
def tmp_growth(tmp_path):
    """临时 growth 目录（隔离，不碰真机 ~/.deskmate）。"""
    return tmp_path


# ============================================================
# 阶段派生
# ============================================================


class TestBondStage:
    def test_stage_thresholds_ordered(self):
        assert BOND_STAGES[0][0] == 0
        for i in range(1, len(BOND_STAGES)):
            assert BOND_STAGES[i][0] > BOND_STAGES[i - 1][0]

    def test_stage_from_depth(self):
        eng = GrowthEngine
        assert eng.bond_stage_from_depth(0)[1] == "初识"
        assert eng.bond_stage_from_depth(30)[1] == "熟络"
        assert eng.bond_stage_from_depth(90)[1] == "默契"
        assert eng.bond_stage_from_depth(200)[1] == "知己"
        assert eng.bond_stage_from_depth(400)[1] == "灵魂伴侣"
        assert eng.bond_stage_from_depth(999)[0] == len(BOND_STAGES) - 1

    def test_negative_depth_treated_as_zero(self):
        assert GrowthEngine.bond_stage_from_depth(-5)[1] == "初识"


# ============================================================
# 封顶不浪费：溢出转深度
# ============================================================


class TestGainRelation:
    def test_normal_gain_no_overflow(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 50.0
        got = eng._gain_relation("intimacy", 3.0)
        assert got == pytest.approx(3.0)
        assert eng.state.intimacy == pytest.approx(53.0)
        assert eng.state.bond_depth == 0.0

    def test_capped_overflow_goes_to_depth(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 99.5
        got = eng._gain_relation("intimacy", 3.0)
        assert got == pytest.approx(0.5)          # 只生效 0.5
        assert eng.state.intimacy == pytest.approx(100.0)
        assert eng.state.bond_depth == pytest.approx(2.5)  # 溢出 2.5 转深度

    def test_repeated_capped_gain_all_to_depth(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 100.0
        for _ in range(10):
            eng._gain_relation("intimacy", 3.0)
        assert eng.state.intimacy == 100.0
        assert eng.state.bond_depth == pytest.approx(30.0)  # 10×3 全转深度

    def test_negative_gain_does_not_touch_depth(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 5.0
        eng._gain_relation("intimacy", -1.0)
        assert eng.state.intimacy == 4.0
        assert eng.state.bond_depth == 0.0

    def test_trust_caps_at_one(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.trust = 0.99
        eng._gain_relation("trust", 0.03, 0.0, 1.0)
        assert eng.state.trust == pytest.approx(1.0)
        assert eng.state.bond_depth == pytest.approx(0.02)


# ============================================================
# 阶段升级事件
# ============================================================


class TestBondStageUp:
    def test_stage_up_records_event(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 100.0
        # 手动把深度推到熟络附近（29.5），再 +3 → 32.5 跨过 30 阈值
        eng.state.bond_depth = 29.5
        eng._gain_relation("intimacy", 3.0)
        events = [e for e in eng.state.recent_events if e["kind"] == "bond_stage_up"]
        assert len(events) == 1
        assert events[0]["stage"] == "熟络"

    def test_stage_up_only_on_crossing(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 100.0
        eng.state.bond_depth = 5.0  # 已越过 0（初识），但未到 30
        eng._gain_relation("intimacy", 3.0)  # 8.0，仍在初识→熟络之间（<30）
        events = [e for e in eng.state.recent_events if e["kind"] == "bond_stage_up"]
        assert len(events) == 0

    def test_stage_up_through_engine_flow(self, tmp_growth):
        """真实链路：满亲密度后多次接受建议，深度累积并升级。

        预设 mood=50（10 次 +4 到 90 不溢出）、empathy 默认 0（10 次 +2 到 20 不溢出）、
        trust 默认 0.5（10 次 +0.03 到 0.8 不溢出）——排除干扰，
        让深度只来自 intimacy 溢出（+3/次 × 10 = 30）。
        """
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 100.0
        eng.state.mood = 50.0
        eng.state.bond_depth = 0.0
        for _ in range(10):  # 10 次 accepted → intimacy 溢出 depth +30 → 熟络
            eng.record_suggestion_feedback(accepted=True)
        assert eng.state.bond_depth == pytest.approx(30.0)
        events = [e for e in eng.state.recent_events if e["kind"] == "bond_stage_up"]
        assert events and events[-1]["stage"] == "熟络"
        assert eng.current_bond()["stage"] == "熟络"


# ============================================================
# current_bond / snapshot
# ============================================================


class TestCurrentBond:
    def test_current_bond_shape(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.bond_depth = 30.0
        bond = eng.current_bond()
        assert bond["stage"] == "熟络"
        assert bond["stage_index"] == 1
        assert bond["next_threshold"] == 90
        assert 0 <= bond["progress"] <= 1
        assert bond["maxed"] is False

    def test_snapshot_includes_bond(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        snap = eng.snapshot()
        assert "bond" in snap
        assert "stage" in snap["bond"]
        assert "depth" in snap["bond"]

    def test_tone_prompt_high_stage(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.intimacy = 80.0
        eng.state.bond_depth = 200.0  # 知己
        prompt = eng.modulate_tone_prompt()
        assert "知己" in prompt
        # 高亲密度但阶段低 → 无阶段追加
        eng2 = GrowthEngine(tmp_growth)
        eng2.state.intimacy = 80.0
        eng2.state.bond_depth = 0.0
        assert "知己" not in eng2.modulate_tone_prompt()


# ============================================================
# 向后兼容
# ============================================================


class TestBackwardCompat:
    def test_old_state_without_depth_loads_fine(self, tmp_path):
        """旧 growth.json 无 bond_depth 字段 → 加载为默认 0，不抛异常。"""
        gpath = tmp_path / "growth.json"
        gpath.write_text(
            json.dumps(
                {"intimacy": 97.5, "trust": 0.5, "empathy": 91.0, "level": 7},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        eng = GrowthEngine(tmp_path)
        assert eng.state.bond_depth == 0.0
        assert eng.state.intimacy == 97.5
        assert eng.current_bond()["stage"] == "初识"

    def test_roundtrip_preserves_depth(self, tmp_path):
        eng = GrowthEngine(tmp_path)
        eng.state.intimacy = 100.0
        eng._gain_relation("intimacy", 10.0)
        eng.save()
        eng2 = GrowthEngine(tmp_path)
        assert eng2.state.bond_depth == pytest.approx(10.0)


# ============================================================
# 默契细分（8.2 深化：per-domain empathy）
# ============================================================


class TestEmpathyDomains:
    def test_domain_growth_by_event_type(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.record_memory_learned(2)      # memory +1.0*2
        eng.record_writing(chapters=1, words=100)  # creation +1
        eng.record_suggestion_feedback(accepted=True)  # proactive +2
        eng.record_chat()                  # companion +0.3
        d = eng.state.empathy_domains
        assert d["memory"] == pytest.approx(2.0)
        assert d["creation"] == pytest.approx(1.0)
        assert d["proactive"] == pytest.approx(2.0)
        assert d["companion"] == pytest.approx(0.3)

    def test_domain_ignored_decreases(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.empathy_domains["proactive"] = 5.0
        eng.record_suggestion_feedback(accepted=False)
        assert eng.state.empathy_domains["proactive"] == pytest.approx(4.0)  # -1
        assert eng.state.bond_depth == 0.0  # 负数不转深度

    def test_domain_cap_overflows_to_depth(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.empathy_domains["memory"] = 99.0
        eng._gain_domain("memory", 2.0)
        assert eng.state.empathy_domains["memory"] == pytest.approx(100.0)
        assert eng.state.bond_depth == pytest.approx(1.0)  # 溢出 1 转深度

    def test_current_empathy_domains_top(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.state.empathy_domains = {"creation": 60.0, "memory": 20.0}
        out = eng.current_empathy_domains()
        assert out["top"] == "创作"
        assert out["top_value"] == pytest.approx(60.0)
        assert out["domains"]["memory"] == pytest.approx(20.0)

    def test_snapshot_includes_empathy_domains(self, tmp_growth):
        eng = GrowthEngine(tmp_growth)
        eng.record_writing(chapters=1, words=100)
        snap = eng.snapshot()
        assert "empathy_domains" in snap
        assert snap["empathy_domains"]["domains"]["creation"] >= 1.0

    def test_old_state_without_domains_loads_fine(self, tmp_path):
        """旧 growth.json 无 empathy_domains → 加载为空 dict，不抛异常。"""
        gpath = tmp_path / "growth.json"
        gpath.write_text(
            json.dumps({"intimacy": 80.0, "trust": 0.5}, ensure_ascii=False),
            encoding="utf-8",
        )
        eng = GrowthEngine(tmp_path)
        assert eng.state.empathy_domains == {}
        assert eng.current_empathy_domains()["top"] == ""

    def test_roundtrip_preserves_domains(self, tmp_path):
        eng = GrowthEngine(tmp_path)
        eng.record_memory_learned(1)
        eng.save()
        eng2 = GrowthEngine(tmp_path)
        assert eng2.state.empathy_domains["memory"] == pytest.approx(1.0)
