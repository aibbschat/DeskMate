"""Tests for deskmate.style_metrics / style_profile_store（P1-5 写作风格画像量化）。"""

from __future__ import annotations

from pathlib import Path

from deskmate.style_metrics import compute_style_metrics, merge_metrics
from deskmate.style_profile_store import (
    DEFAULT_PROFILE_PATH,
    load_profile,
    update_style_profile,
)


# ============================================================
# style_metrics：纯统计指标正确性
# ============================================================


class TestComputeMetrics:
    def test_empty_text_returns_zeros(self):
        m = compute_style_metrics("   \n  ")
        assert m["chars"] == 0
        assert m["dialogue_pct"] == 0.0

    def test_sentence_stats(self):
        # 两句：一句 12 字（短句），一句 50+ 字（长句）
        text = "此去经年，应是良辰好景虚设。" + "而今回首往事如烟云散尽，唯有故人旧事仍在心头萦绕不去，令人唏嘘不已，难以自持良久矣，此情可待成追忆只是当时已惘然。"
        m = compute_style_metrics(text)
        s = m["sentences"]
        assert s["count"] == 2
        assert s["short_pct"] == 50.0   # 一句 ≤12 字
        assert s["long_pct"] == 50.0    # 一句 ≥40 字

    def test_four_char_detection(self):
        text = "风华绝代，倾国倾城，一笑倾人城再笑倾人国。"
        m = compute_style_metrics(text)
        assert m["four_char"]["count"] >= 1  # 风华绝代/倾国倾城 均为 4 字词块
        assert m["four_char"]["pct_of_chunks"] > 0

    def test_classical_particles_density(self):
        # 「之乎者也」浓度高的文段
        text = "夫君子之德，其犹龙乎？见龙在田，利见大人。之乎者也，何其多也！"
        m = compute_style_metrics(text)
        assert m["classical_per_1k"] > 0
        # 现代语气词应相对低
        assert m["modern_per_1k"] < m["classical_per_1k"] * 2

    def test_dialogue_pct(self):
        text = "他说：“此去一行，生死未卜。”她又道：“无妨，我等你回来。”说罢转身而去。"
        m = compute_style_metrics(text)
        # 引号内文字占比应 >0 且 <100
        assert 0 < m["dialogue_pct"] < 100

    def test_paragraph_stats(self):
        text = "短段。\n\n又是一段较长的话，用来填充第二段的长度让它成为一个普通段落。\n\n尾声。"
        m = compute_style_metrics(text)
        assert m["paragraphs"]["count"] == 3
        assert m["paragraphs"]["short_pct"] >= 33.0  # 「短段。」和「尾声。」都是短段

    def test_punct_density(self):
        text = "一、二、三。四，五，六；七。"
        m = compute_style_metrics(text)
        assert m["punct"]["dunhao_per_1k"] > 0
        assert m["punct"]["period_per_1k"] > 0


# ============================================================
# merge_metrics：加权平均合并
# ============================================================


class TestMergeMetrics:
    def test_first_merge_returns_new(self):
        new = compute_style_metrics("短句。长句，长句，长句，长句，长句，长句，长句，长句，长句。")
        merged = merge_metrics(None, new, 0)
        assert merged["sentences"]["count"] == new["sentences"]["count"]

    def test_second_merge_is_weighted_average(self):
        a = compute_style_metrics("此去经年，应是良辰好景虚设。")
        b = compute_style_metrics("短。短。短。短。短。短。短。短。")
        merged = merge_metrics(a, b, old_n=1)
        # a 的 avg_len 与 b 的 avg_len 取 1:1 平均
        expect = round(
            (a["sentences"]["avg_len"] * 1 + b["sentences"]["avg_len"] * 1) / 2, 2
        )
        assert merged["sentences"]["avg_len"] == expect

    def test_merge_keeps_missing_keys(self):
        a = {"sentences": {"count": 5, "avg_len": 10.0}}
        b = {"sentences": {"count": 3, "avg_len": 20.0}, "chars": 30}
        merged = merge_metrics(a, b, old_n=1)
        # chars 只在 b 中 → 保留
        assert merged["chars"] == 30
        # count 累加（5+3），avg_len 加权平均 (10*1+20*1)/2
        assert merged["sentences"]["count"] == 8
        assert merged["sentences"]["avg_len"] == 15.0


# ============================================================
# style_profile_store：增量更新 + 向后兼容
# ============================================================


class TestProfileStore:
    def test_update_creates_profile_and_preserves_qualitative(self, tmp_path: Path):
        path = tmp_path / "style_profile.json"
        # 先写一个带定性画像的旧文件（模拟 writing.extract_style_profile 产出）
        old = {
            "profile": {
                "summary": "历史悬疑小说，半文半白说书人腔调。",
                "features": {"sentence_style": "短句为主，对仗偶现", "taboos": "忌欧化长句"},
            },
            "source": "旧样本",
            "chars": 100,
            "updated_at": "2026-08-01T00:00:00",
        }
        path.write_text(
            __import__("json").dumps(old, ensure_ascii=False), encoding="utf-8"
        )

        text = "他说：“此去一行，生死未卜。”此乃旧事，历历在目，然时过境迁，唯有叹息而已。"
        data = update_style_profile(text, source="《国子监秘闻录》第3章", profile_path=path)

        # 定性层保留
        assert data["profile"]["summary"] == old["profile"]["summary"]
        assert data["profile"]["features"]["taboos"] == "忌欧化长句"
        # 量化层新增
        assert data["metrics"]["n"] == 1
        assert data["metrics"]["sentences"]["count"] >= 1
        # 样本记录
        assert data["samples"][0]["source"] == "《国子监秘闻录》第3章"
        # 计数
        assert data["updates"] == 1
        assert data["chars"] == 100 + data["samples"][0]["chars"]

    def test_second_update_increments_and_averages(self, tmp_path: Path):
        path = tmp_path / "style_profile.json"
        update_style_profile("短句。短句。短句。", source="样本一", profile_path=path)
        update_style_profile("此去经年，应是良辰好景虚设。长句萦绕不去，令人唏嘘不已。", source="样本二", profile_path=path)

        data = load_profile(path)
        assert data["updates"] == 2
        assert data["metrics"]["n"] == 2
        assert len(data["samples"]) == 2

    def test_backward_compat_with_writing_prompt(self, tmp_path: Path):
        """带 metrics 的画像仍能被 deskmate.writing.format_profile_for_prompt 消费。"""
        from deskmate.writing import format_profile_for_prompt

        path = tmp_path / "style_profile.json"
        # ① 空 profile 场景：update 后 format 不抛异常、回退兜底
        update_style_profile("短句。对话：“无妨。”此乃旧事。", source="样本", profile_path=path)
        data = load_profile(path)
        prompt = format_profile_for_prompt(data)
        assert "metrics" not in prompt  # 量化层不进 prompt（避免污染上下文）

        # ② 带定性画像场景：update 保留 summary/features，format 正常出画像
        path2 = tmp_path / "style_profile2.json"
        old = {
            "profile": {
                "summary": "历史悬疑小说，半文半白说书人腔调。",
                "features": {"sentence_style": "短句为主"},
            },
            "source": "旧样本",
            "chars": 100,
            "updated_at": "2026-08-01T00:00:00",
        }
        path2.write_text(
            __import__("json").dumps(old, ensure_ascii=False), encoding="utf-8"
        )
        update_style_profile("此去经年，应是良辰好景虚设。", source="样本二", profile_path=path2)
        data2 = load_profile(path2)
        prompt2 = format_profile_for_prompt(data2)
        assert "整体画像" in prompt2
        assert data2["metrics"]["n"] == 1

    def test_default_path_points_to_home_deskmate(self):
        assert DEFAULT_PROFILE_PATH.name == "style_profile.json"
        assert ".deskmate" in str(DEFAULT_PROFILE_PATH)
