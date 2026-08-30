"""Tests for P2-6 个人 AI 分身（deskmate/alter_ego/）。

决策偏好提取 + 分身画像聚合（写作风格 + 决策偏好 → 「另一个我」）。
"""

from __future__ import annotations

import json
import time

import pytest

from deskmate.alter_ego.decision_profile import (
    DecisionProfile,
    DecisionSignal,
    extract_and_update,
    extract_signals,
    format_decision_profile,
    load_profile,
    save_profile,
    update_profile,
)
from deskmate.alter_ego.persona import (
    build_alter_ego_prompt,
    format_style_qualitative,
    format_style_quantified,
    has_alter_ego_data,
    load_style_profile,
)


class TestDecisionSignalExtraction:
    def test_risk_conservative(self):
        signals = extract_signals("保守一点，别太激进")
        assert any(s.dimension == "risk" and "保守" in s.value for s in signals)

    def test_risk_aggressive(self):
        signals = extract_signals("直接做吧，别犹豫")
        assert any(s.dimension == "risk" and "激进" in s.value for s in signals)

    def test_sequence(self):
        signals = extract_signals("先调研再决定")
        assert any(s.dimension == "sequence" and "先调研" in s.value for s in signals)

    def test_tradeoff(self):
        signals = extract_signals("比起速度我更看重质量")
        assert any(s.dimension == "tradeoff" for s in signals)
        assert any("质量" in s.value for s in signals if s.dimension == "tradeoff")

    def test_change_minimal(self):
        signals = extract_signals("尽量不改，小改就行")
        assert any(s.dimension == "change" and "最小" in s.value for s in signals)

    def test_change_radical(self):
        signals = extract_signals("彻底重构，推翻重来")
        assert any(s.dimension == "change" and "重构" in s.value for s in signals)

    def test_info_compare(self):
        signals = extract_signals("对比一下利弊，给我几个选项")
        assert any(s.dimension == "info_style" and "对比" in s.value for s in signals)

    def test_info_direct(self):
        signals = extract_signals("直接告诉我结论，别废话")
        assert any(s.dimension == "info_style" and "直接" in s.value for s in signals)

    def test_empty_text(self):
        assert extract_signals("") == []
        assert extract_signals(None) == []  # type: ignore

    def test_no_signal_plain_text(self):
        signals = extract_signals("今天天气不错，出去走走")
        assert signals == []

    def test_multiple_dimensions(self):
        signals = extract_signals("保守一点，先调研再决定，对比一下利弊")
        dims = {s.dimension for s in signals}
        assert "risk" in dims
        assert "sequence" in dims
        assert "info_style" in dims


class TestDecisionProfileStorage:
    def test_save_load_roundtrip(self, tmp_path):
        profile = DecisionProfile()
        signals = extract_signals("保守一点，先调研再决定")
        update_profile(profile, signals)
        path = tmp_path / "decision_profile.json"
        save_profile(profile, path)
        loaded = load_profile(path)
        assert loaded.total_signals == profile.total_signals
        assert loaded.risk == profile.risk
        assert len(loaded.sequence) == len(profile.sequence)

    def test_incremental_update(self, tmp_path):
        path = tmp_path / "decision_profile.json"
        # 第一次
        profile1, sigs1 = extract_and_update("保守一点", path)
        assert profile1.total_signals >= 1
        # 第二次（不同信号）
        profile2, sigs2 = extract_and_update("直接做吧", path)
        assert profile2.total_signals > profile1.total_signals
        # 风险偏好应该有两个值
        assert len(profile2.risk) >= 2

    def test_load_missing_file(self, tmp_path):
        profile = load_profile(tmp_path / "nonexistent.json")
        assert profile.total_signals == 0

    def test_format_profile(self):
        profile = DecisionProfile()
        signals = extract_signals("保守一点，先调研再决定，比起速度我更看重质量")
        update_profile(profile, signals)
        text = format_decision_profile(profile)
        assert "决策偏好" in text
        assert "保守" in text
        assert "先调研" in text

    def test_format_empty_profile(self):
        assert format_decision_profile(DecisionProfile()) == ""


class TestAlterEgoPersona:
    def test_format_style_quantified_with_data(self):
        style = {
            "metrics": {
                "n": 3, "chars": 5000,
                "sentences": {"avg_len": 45, "median_len": 30, "short_pct": 20, "long_pct": 30},
                "classical_per_1k": 5.0,
                "modern_per_1k": 15.0,
                "four_char": {"pct_of_chunk": 8.0},
                "dialogue": {"pct": 25},
                "paragraphs": {"avg_len": 100},
            }
        }
        text = format_style_quantified(style)
        assert "写作风格" in text
        assert "句长" in text
        assert "文言" in text

    def test_format_style_quantified_insufficient_data(self):
        style = {"metrics": {"n": 1, "chars": 100}}
        assert format_style_quantified(style) == ""

    def test_format_style_qualitative(self):
        style = {"summary": "半文半白，句式简练", "features": ["善用四字格", "对话占比高"]}
        text = format_style_qualitative(style)
        assert "半文半白" in text
        assert "四字格" in text

    def test_build_alter_ego_full(self):
        style = {
            "metrics": {"n": 3, "chars": 5000, "sentences": {"avg_len": 40}},
            "summary": "简练有力",
        }
        decision = DecisionProfile()
        update_profile(decision, extract_signals("保守一点"))
        prompt = build_alter_ego_prompt(style=style, decision=decision, mode="full")
        assert "分身模式" in prompt
        assert "写作风格" in prompt
        assert "决策偏好" in prompt

    def test_build_alter_ego_writing_only(self):
        style = {"metrics": {"n": 3, "chars": 5000, "sentences": {"avg_len": 40}}}
        prompt = build_alter_ego_prompt(style=style, decision=DecisionProfile(), mode="writing")
        assert "写作风格" in prompt
        assert "决策偏好" not in prompt

    def test_build_alter_ego_decision_only(self):
        decision = DecisionProfile()
        update_profile(decision, extract_signals("保守一点"))
        prompt = build_alter_ego_prompt(style={}, decision=decision, mode="decision")
        assert "决策偏好" in prompt
        assert "写作风格" not in prompt

    def test_build_alter_ego_empty(self):
        assert build_alter_ego_prompt(style={}, decision=DecisionProfile()) == ""

    def test_has_alter_ego_data_with_style(self, tmp_path, monkeypatch):
        # 模拟有风格数据
        import deskmate.alter_ego.persona as p
        monkeypatch.setattr(p, "load_style_profile", lambda: {
            "metrics": {"n": 3, "chars": 5000}
        })
        monkeypatch.setattr(p, "load_decision_profile", lambda: DecisionProfile())
        assert has_alter_ego_data() is True

    def test_has_alter_ego_data_with_decision(self, tmp_path, monkeypatch):
        import deskmate.alter_ego.persona as p
        monkeypatch.setattr(p, "load_style_profile", lambda: {})
        d = DecisionProfile()
        d.total_signals = 1
        monkeypatch.setattr(p, "load_decision_profile", lambda: d)
        assert has_alter_ego_data() is True

    def test_has_alter_ego_data_empty(self, tmp_path, monkeypatch):
        import deskmate.alter_ego.persona as p
        monkeypatch.setattr(p, "load_style_profile", lambda: {})
        monkeypatch.setattr(p, "load_decision_profile", lambda: DecisionProfile())
        assert has_alter_ego_data() is False


class TestAlterEgoWiring:
    """P2-6 补完验收：学习闭环 + 注入闭环 + 开关，真接进主链路（非死代码）。

    注意：deskmate.alter_ego.decision_profile / persona 的 DEFAULT_PROFILE_PATH /
    DEFAULT_STYLE_PATH 是模块级常量，import 时即绑定 DESKMATE_DIR。测试里必须在
    调用前 monkeypatch.setattr 到 tmp，否则无参 load/save 会落到真实 ~/.deskmate，
    既污染用户数据又导致断言读错文件。
    """

    def _patch_paths(self, tmp_path, monkeypatch):
        import deskmate.alter_ego.decision_profile as dp
        import deskmate.alter_ego.persona as persona_mod

        monkeypatch.setattr(dp, "DEFAULT_PROFILE_PATH", tmp_path / "decision_profile.json")
        monkeypatch.setattr(persona_mod, "DEFAULT_STYLE_PATH", tmp_path / "style_profile.json")
        return tmp_path / "decision_profile.json"

    def test_inject_context_includes_alter_ego(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DESKMATE_ALTER_EGO", "1")
        prof_path = self._patch_paths(tmp_path, monkeypatch)
        prof = DecisionProfile()
        prof.risk = {"保守/稳健": {"count": 3, "last_ts": time.time(), "evidence": "求稳"}}
        prof.total_signals = 1
        save_profile(prof, prof_path)

        from deskmate.memory.manager import DeskMateMemoryManager

        mgr = DeskMateMemoryManager(tmp_path)
        ctx = mgr.build_full_context_for_turn("你好，帮我看看这段")
        assert "另一个我" in ctx
        assert "决策偏好" in ctx

    def test_inject_context_skipped_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DESKMATE_ALTER_EGO", "0")
        self._patch_paths(tmp_path, monkeypatch)
        from deskmate.memory.manager import DeskMateMemoryManager

        mgr = DeskMateMemoryManager(tmp_path)
        ctx = mgr.build_full_context_for_turn("你好")
        assert "另一个我" not in ctx

    def test_post_session_learns_alter_ego(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DESKMATE_ALTER_EGO", "1")
        prof_path = self._patch_paths(tmp_path, monkeypatch)
        from deskmate.memory.manager import DeskMateMemoryManager

        mgr = DeskMateMemoryManager(tmp_path)
        mgr.post_session_summary("我比较保守，先调研再决定，别太激进", "好的，我来帮你")
        prof = load_profile(prof_path)
        assert prof.total_signals >= 1

    def test_post_session_no_learn_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DESKMATE_ALTER_EGO", "0")
        prof_path = self._patch_paths(tmp_path, monkeypatch)
        from deskmate.memory.manager import DeskMateMemoryManager

        mgr = DeskMateMemoryManager(tmp_path)
        mgr.post_session_summary("先调研再决定，别太激进", "好的")
        prof = load_profile(prof_path)
        assert prof.total_signals == 0
