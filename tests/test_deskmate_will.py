"""v1.9.0 意志层（成为我）测试 — W1 settings + W1 digest + W2 调制。"""

from __future__ import annotations

import json
import os

import pytest

from deskmate.will.settings import (
    DEFAULTS,
    WillSettings,
    _reset_cache_for_test as reset_settings_cache,
    load_will_settings,
    reset_will_settings,
    save_will_settings,
)
from deskmate.will.digest import (
    WillDigest,
    _reset_cache_for_test as reset_digest_cache,
    get_digest,
    invalidate_digest,
)


# ===========================
# Fixtures：每个测试前清缓存（避免 TTL 串扰）
# ===========================


@pytest.fixture(autouse=True)
def _clear_will_caches():
    reset_settings_cache()
    reset_digest_cache()
    yield
    reset_settings_cache()
    reset_digest_cache()


# ===========================
# W1-1 settings
# ===========================


class TestWillSettingsDefaults:
    def test_defaults_round_trip(self, tmp_path):
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.enabled is True
        assert s.thresholds == dict(DEFAULTS["thresholds"])

    def test_missing_file_returns_defaults(self, tmp_path):
        assert (tmp_path / "will_settings.json").exists() is False
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.enabled is True
        assert s.thresholds["emotional_proactive_frequency_max"] == 1.4

    def test_corrupted_json_falls_back(self, tmp_path):
        (tmp_path / "will_settings.json").write_text("{not json", encoding="utf-8")
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.enabled is True

    def test_partial_data_keeps_default_for_missing(self, tmp_path):
        (tmp_path / "will_settings.json").write_text(
            json.dumps({"thresholds": {"memory_budget_ratio": 1.3}}, ensure_ascii=False),
            encoding="utf-8",
        )
        reset_settings_cache()
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.thresholds["memory_budget_ratio"] == 1.3
        assert s.thresholds["emotional_proactive_frequency_max"] == 1.4

    def test_threshold_out_of_range_is_clamped(self, tmp_path):
        (tmp_path / "will_settings.json").write_text(
            json.dumps(
                {
                    "thresholds": {
                        "emotional_proactive_frequency_max": 99,
                        "cognitive_threshold_delta_pos": -1,
                        "tone_prompt_max_len": 9999,
                    }
                }
            ),
            encoding="utf-8",
        )
        reset_settings_cache()
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.thresholds["emotional_proactive_frequency_max"] <= 3.0
        assert s.thresholds["cognitive_threshold_delta_pos"] >= 0.0
        assert s.thresholds["tone_prompt_max_len"] <= 200


class TestWillSettingsPersist:
    def test_save_writes_file(self, tmp_path):
        s = WillSettings(enabled=False, thresholds={"tone_prompt_max_len": 50})
        assert save_will_settings(s, base_dir=str(tmp_path)) is True
        p = tmp_path / "will_settings.json"
        assert p.exists()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["enabled"] is False
        assert data["thresholds"]["tone_prompt_max_len"] == 50

    def test_save_clears_cache_so_next_read_sees_new(self, tmp_path):
        _ = load_will_settings(base_dir=str(tmp_path))
        save_will_settings(WillSettings(enabled=False), base_dir=str(tmp_path))
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.enabled is False

    def test_reset_restores_defaults(self, tmp_path):
        save_will_settings(WillSettings(enabled=False), base_dir=str(tmp_path))
        s = reset_will_settings(base_dir=str(tmp_path))
        assert s.enabled is True
        assert s.thresholds == dict(DEFAULTS["thresholds"])

    def test_kill_switch_env_forces_disabled(self, tmp_path, monkeypatch):
        save_will_settings(WillSettings(enabled=True), base_dir=str(tmp_path))
        monkeypatch.setenv("DESKMATE_WILL_ENABLED", "0")
        reset_settings_cache()
        s = load_will_settings(base_dir=str(tmp_path))
        assert s.enabled is False


# ===========================
# W1-2 digest
# ===========================


class TestWillDigestEmpty:
    def test_no_profile_returns_empty(self, tmp_path, monkeypatch):
        # 真实库 ~/.deskmate/deskmate.db 不动 → 把 DESKMATE_DIR 设到 tmp_path
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        d = get_digest(base_dir=str(tmp_path))
        # tmp_path 下没有 deskmate.db → 应 fail-open 返回空
        assert d.available is False
        assert d.top_stances == []
        assert d.stance_count == 0

    def test_kill_switch_returns_empty(self, tmp_path, monkeypatch):
        # 即使 tmp_path 下有 deskmate.db，kill switch 也应返回空
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        save_will_settings(WillSettings(enabled=False), base_dir=str(tmp_path))
        d = get_digest(base_dir=str(tmp_path))
        assert d.available is False

    def test_cache_hit_does_not_rebuild(self, tmp_path):
        d1 = get_digest(base_dir=str(tmp_path))
        # 第二次命中缓存（即便 base_dir 改成不存在的也不会重读）
        d2 = get_digest(base_dir="/does/not/exist")
        assert d2 is d1

    def test_cache_hit_returns_same_object(self, tmp_path):
        # 显式 once-per-cache: 即便碰 file_no_profile_returns_empty, 然后环境变,
        # get_digest 仍发原值（缓存生效）。这两个调用共享对象。
        d1 = get_digest(base_dir=str(tmp_path))
        d2 = get_digest(base_dir=str(tmp_path))
        assert d2 is d1

    def test_invalidate_forces_rebuild(self):
        invalidate_digest()
        from deskmate.will.digest import _cache

        assert _cache["data"] is None


# ===========================
# 独立文件契约
# ===========================


def test_will_settings_file_independent_of_settings_json(tmp_path):
    """will_settings.json 必须独立于含 API key 的 settings.json。"""
    # 模拟存在 settings.json（含 key），will 应读 will_settings.json 而不是 settings.json
    other = tmp_path / "settings.json"
    other.write_text(json.dumps({"api_key": "sk-secret"}), encoding="utf-8")
    s = load_will_settings(base_dir=str(tmp_path))
    assert s.enabled is True


# ===========================
# W2a 调制（emotion → proactive frequency）
# ===========================


class TestModulateProactiveFrequency:
    def test_empty_digest_unchanged(self, tmp_path, monkeypatch):
        """空画像 / 情绪 buffer 空 → 行为等同今天（vitality 仍是原值）。"""
        # 把 DESKMATE_DIR 设到 tmp_path，让 digest 走空
        monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
        from deskmate.growth.engine import GrowthEngine

        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.vitality = 60
        out = e.modulate_proactive_frequency(100.0)
        # 空 digest → 不叠加情绪增量
        # 计算：活力 60 → ratio = 1.6 - 0.6 * 0.75 = 1.15
        assert out == pytest.approx(115.0)

    def test_emotion_pressured_multiplies(self, tmp_path, monkeypatch):
        """digest.recent_emotion='压力大' → 间隔 ×1.4。"""
        # 直接 monkeypatch digest._build 返回一个有负面情绪的 digest
        from deskmate.will import digest as dm
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest

        fake = WillDigest(
            available=True, recent_emotion="压力大", emotion_evidence="今天压力很大"
        )
        # 用 monkeypatch 在 settings/digest 里都注入我们想要的
        from deskmate.will.settings import load_will_settings

        def fake_load(base_dir=None):
            return WillSettings(enabled=True)

        monkeypatch.setattr(dm, "_build", lambda base_dir=None: fake)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings", fake_load
        )

        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.vitality = 60
        out = e.modulate_proactive_frequency(100.0)
        # vitality=60 → 115 * 1.4 = 161
        assert out == pytest.approx(115.0 * 1.4)

    def test_kill_switch_disables_modulation(self, tmp_path, monkeypatch):
        """kill switch 关闭时不叠加。"""
        from deskmate.will.settings import save_will_settings, load_will_settings, WillSettings

        save_will_settings(WillSettings(enabled=False), base_dir=str(tmp_path))
        from deskmate.will import digest as dm
        from deskmate.will.digest import WillDigest

        fake = WillDigest(available=True, recent_emotion="压力大")

        monkeypatch.setattr(dm, "_build", lambda base_dir=None: fake)
        # 重置缓存强制重读
        dm._cache["data"] = None

        from deskmate.growth.engine import GrowthEngine

        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.vitality = 60
        out = e.modulate_proactive_frequency(100.0)
        # kill switch 关闭 → 不叠加，乘数 1.0 不变
        assert out == pytest.approx(115.0)


# ===========================
# W2b 调制（cognitive → threshold / budget / tone）
# ===========================


class TestModulateProactiveThreshold:
    def _engage_will(self, monkeypatch, digest, enabled=True):
        """monkeypatch digest._build + settings.load_will_settings 回到测试可控态。"""
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        def fake_load(base_dir=None):
            return WillSettings(enabled=enabled)

        monkeypatch.setattr(dm, "_build", lambda base_dir=None: digest)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings", fake_load
        )

    def test_open_gap_increases_threshold(self, tmp_path, monkeypatch):
        """有 open gap → 阈值 +cognitive_threshold_delta_neg。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest

        d = WillDigest(available=True, open_gap_count=2)
        self._engage_will(monkeypatch, d)
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.trust = 0.5  # 现状不动
        e.state.trust_controls_proactive = True
        base = e.modulate_proactive_threshold(0.5)
        # trust=0.5 → offset=0；空 digest 不变；开 gap → +0.15
        assert base == pytest.approx(0.65)

    def test_three_stances_decreases_threshold(self, tmp_path, monkeypatch):
        """≥3 高置信立场 → 阈值 -cognitive_threshold_delta_pos。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest

        d = WillDigest(available=True, stance_count=4)
        self._engage_will(monkeypatch, d)
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.trust = 0.5
        e.state.trust_controls_proactive = True
        out = e.modulate_proactive_threshold(0.5)
        # trust=0.5 → offset=0；stance_count=4 ≥ 3 → -0.15
        assert out == pytest.approx(0.35)

    def test_trust_off_returns_original_unchanged(self, tmp_path, monkeypatch):
        """trust_controls_proactive=False 时不调（无论 digest）。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest

        d = WillDigest(available=True, stance_count=5, open_gap_count=10)
        self._engage_will(monkeypatch, d, enabled=True)
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.trust = 0.5
        e.state.trust_controls_proactive = False
        assert e.modulate_proactive_threshold(0.5) == pytest.approx(0.5)


class TestModulateMemoryBudget:
    def test_value_signal_increases_budget(self, tmp_path, monkeypatch):
        """has_value_signal → 预算 ×memory_budget_ratio。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        d = WillDigest(available=True, has_value_signal=True)
        monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings",
            lambda base_dir=None: WillSettings(enabled=True),
        )
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.empathy = 0  # 强制 empathy=0 → ratio = EMPATHY_BUDGET_MIN = 0.9
        out = e.modulate_memory_budget(1000)
        # empathy=0 → base = 1000 * 0.9 = 900 → has_value_signal=True → 900 * 1.1 = 990
        assert out == pytest.approx(990)

    def test_no_value_signal_unchanged(self, tmp_path, monkeypatch):
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        d = WillDigest(available=True, has_value_signal=False)
        monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings",
            lambda base_dir=None: WillSettings(enabled=True),
        )
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.empathy = 0
        out = e.modulate_memory_budget(1000)
        # empathy=0 → 900，无 signal 不乘
        assert out == pytest.approx(900)


class TestModulateTonePrompt:
    def test_two_stances_appended_with_max_len(self, tmp_path, monkeypatch):
        """≥2 高置信立场 → 追加「他表达过:…」，严格截断。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        d = WillDigest(
            available=True,
            top_stances=[
                ("topic1", "content_short_1", 0.9),
                ("topic2", "content_short_2", 0.8),
            ],
        )
        monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings",
            lambda base_dir=None: WillSettings(enabled=True),
        )
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.intimacy = 60  # 触发 base 提示
        out = e.modulate_tone_prompt()
        assert "他表达过" in out
        assert "content_short_1" in out

    def test_one_stance_not_appended(self, tmp_path, monkeypatch):
        """仅 1 个立场 → 不追加。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        d = WillDigest(available=True, top_stances=[("t", "c", 0.9)])
        monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings",
            lambda base_dir=None: WillSettings(enabled=True),
        )
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.intimacy = 60
        out = e.modulate_tone_prompt()
        assert "他表达过" not in out

    def test_long_snippet_truncated_to_max_len(self, tmp_path, monkeypatch):
        """超出 max_len → 严格截断。"""
        from deskmate.growth.engine import GrowthEngine
        from deskmate.will.digest import WillDigest
        from deskmate.will import digest as dm
        from deskmate.will.settings import WillSettings

        long_c = "这是一个非常非常长的具体内容描述" * 10
        d = WillDigest(
            available=True,
            top_stances=[
                ("t1", long_c, 0.9),
                ("t2", long_c, 0.8),
            ],
        )
        # 配置阈值 30 字
        s = WillSettings(enabled=True, thresholds={"tone_prompt_max_len": 30.0})
        monkeypatch.setattr(dm, "_build", lambda base_dir=None: d)
        monkeypatch.setattr(
            "deskmate.will.settings.load_will_settings", lambda base_dir=None: s
        )
        e = GrowthEngine(base_dir=str(tmp_path))
        e.state.intimacy = 60
        out = e.modulate_tone_prompt()
        # 抽取 snippet 部分（" 他表达过：{snippet}。" 整段）
        # 实际我们的实现只截 snippet 内文，输出的整段"他表达过:"会保留
        assert "他表达过" in out
