"""Tests for 表达源回灌 deskmate/cognitive/expression_ingest.py（F2）。

hermetic：认知画像用临时库，表达源用 monkeypatch 注入，不碰真实 ~/.deskmate。

重点测两道闸门——它们是真实数据打出来的：
- 人称闸门：长期记忆是第三人称观察，"用户希望AI变聪明"不是他的心情；
- 主语闸门：主语是 AI 的记录（"AI承诺…"）不是他的主张。
"""

from __future__ import annotations

import shutil
import tempfile

import pytest

from deskmate.cognitive import expression_ingest as ei
from deskmate.cognitive import expressions as ex


# ----------------------------------------------------------------------
# 人称闸门（情感侧）
# ----------------------------------------------------------------------
class TestLooksLikeFeeling:
    @pytest.mark.parametrize("text", [
        "2026-08-26: 用户希望AI变得更聪明并更好地理解自己。",
        "用户期望AI通过持续学习变得更聪明。",
        "用户希望 AI 越来越聪明，更懂用户。",
        "用户纠正AI，指出ID=301肯定不是今天发布的新记录。",
        "用户提出进度不用补充，只维护圣经即可。",
        "用户为AI接入了AI圈的古籍库，希望AI以后查资料时优先使用该库。",
    ])
    def test_outward_statements_rejected(self, text):
        """指向外部（AI/系统）的期望与动作 = 不是他的心情。

        实测教训：08-26 就因「用户希望AI…」被判成「顺利」，证据是"希望"两个字。
        """
        assert ei.looks_like_feeling(text) is False, text

    @pytest.mark.parametrize("text", [
        "2026-09-04: 用户表示享受孩子的依赖，不想阻止这种依赖。",
        "用户提到最近写得很顺，心里踏实。",
        "用户表示创作遇阻时很烦躁。",
    ])
    def test_inner_feelings_accepted(self, text):
        assert ei.looks_like_feeling(text) is True, text

    def test_empty(self):
        assert ei.looks_like_feeling("") is False
        assert ei.looks_like_feeling(None) is False


# ----------------------------------------------------------------------
# 主语闸门 + 议题提取（认知侧）
# ----------------------------------------------------------------------
class TestIsHisOwn:
    @pytest.mark.parametrize("text", [
        "2026-07-31，AI承诺后续写入前先查询，避免重复覆盖已有内容。",
        "AI审阅用户小说第二章时指出：庆历四年太学尚无三舍法。",
        "小桔建议将此处改为省试。",
    ])
    def test_ai_subject_rejected(self, text):
        """主语是 AI 的记录是**我们**的行为，存进他的画像等于替他编立场。"""
        assert ei._is_his_own(text) is False, text

    @pytest.mark.parametrize("text", [
        "2026-07-31，用户偏好查资料时只用权威正史记载。",
        "用户于2026-07-29明确表示不希望再写古代记者题材的故事。",
    ])
    def test_user_subject_accepted(self, text):
        assert ei._is_his_own(text) is True, text


class TestTopicOf:
    def test_strips_date_and_boilerplate_colon(self):
        got = ei._topic_of("2026-09-04: 用户明确表示不希望再写古代记者题材的故事")
        assert not got.startswith("2026")
        assert "不希望再写古代记者题材" in got

    def test_strips_date_and_boilerplate_comma(self):
        """实测分隔符有中文逗号，正则必须认。"""
        got = ei._topic_of("2026-07-31，用户偏好查资料时只用权威正史记载")
        assert not got.startswith("2026")
        assert "查资料" in got

    def test_strips_mid_sentence_date(self):
        """日期夹在句中时也要抹掉，否则同义条目因日期不同无法合并。"""
        got = ei._topic_of("用户于2026-07-29明确表示不希望再写古代记者题材")
        assert "2026" not in got

    def test_never_empty_for_nonempty_input(self):
        assert ei._topic_of("用户") != ""
        assert ei._topic_of("") == ""


# ----------------------------------------------------------------------
# 认知侧回灌
# ----------------------------------------------------------------------
def _pm(tmp):
    from deskmate.cognitive.profile import CognitiveProfileManager

    return CognitiveProfileManager(profile_path=f"{tmp}/p.db", layer="cognitive")


class TestIngestPreferences:
    @staticmethod
    def _seed(monkeypatch, rows):
        monkeypatch.setattr(
            ex, "collect", lambda **kw: rows if kw.get("sources") == (ex.LEARNED,) else []
        )

    def test_ingests_his_own_preferences(self, tmp_path, monkeypatch):
        pm = _pm(str(tmp_path))
        self._seed(monkeypatch, [
            {"id": "l:1", "day": "2026-07-31", "text": "2026-07-31，用户偏好查资料时只用权威正史记载",
             "source": ex.LEARNED, "category": "preference"},
            {"id": "l:2", "day": "2026-07-31", "text": "AI承诺后续写入前先查询",
             "source": ex.LEARNED, "category": "preference"},
        ])
        res = ei.ingest_preferences(limit=10, pm=pm)
        assert res["added"] == 1, f"只应并入他自己的偏好，实际 {res}"
        assert any("权威正史" in s.content for s in pm.profile.stances)
        assert not any("AI承诺" in s.content for s in pm.profile.stances)

    def test_idempotent(self, tmp_path, monkeypatch):
        pm = _pm(str(tmp_path))
        rows = [{"id": "l:1", "day": "2026-07-31", "text": "用户偏好查资料只用正史",
                 "source": ex.LEARNED, "category": "preference"}]
        self._seed(monkeypatch, rows)
        r1 = ei.ingest_preferences(limit=10, pm=pm)
        r2 = ei.ingest_preferences(limit=10, pm=pm)
        assert r1["added"] == 1
        assert r2["scanned"] == 0, "已处理过的 id 不应重复扫描"
        assert r2["added"] == 0

    def test_limit_respected(self, tmp_path, monkeypatch):
        pm = _pm(str(tmp_path))
        rows = [
            {"id": f"l:{i}", "day": "2026-07-31",
             "text": f"用户偏好第{i}条", "source": ex.LEARNED, "category": "preference"}
            for i in range(8)
        ]
        self._seed(monkeypatch, rows)
        res = ei.ingest_preferences(limit=3, pm=pm)
        assert res["added"] == 3, "限速：一次只补 3 条，不一次灌爆画像"

    def test_same_topic_merges_not_duplicates(self, tmp_path, monkeypatch):
        pm = _pm(str(tmp_path))
        self._seed(monkeypatch, [
            {"id": "l:1", "day": "2026-07-31", "text": "用户偏好查资料只用正史",
             "source": ex.LEARNED, "category": "preference"},
        ])
        ei.ingest_preferences(limit=10, pm=pm)
        n_before = len(pm.profile.stances)
        # 同 topic 再来一条 → 补来源，不新建条目
        self._seed(monkeypatch, [
            {"id": "l:2", "day": "2026-08-01", "text": "用户偏好查资料只用正史",
             "source": ex.LEARNED, "category": "preference"},
        ])
        res = ei.ingest_preferences(limit=10, pm=pm)
        assert res["merged"] == 1
        assert len(pm.profile.stances) == n_before, "同 topic 不应重复建条目"


# ----------------------------------------------------------------------
# 情感侧回灌
# ----------------------------------------------------------------------
class TestIngestEmotions:
    def test_skips_diary_source(self, tmp_path, monkeypatch):
        """日记由 record_today / backfill 负责，这里不重复处理。"""
        import deskmate.cognitive.expression_ingest as mod

        calls = []
        monkeypatch.setattr(
            ex, "by_day",
            lambda **kw: {"2026-09-04": [
                {"id": "d:1", "text": "日记内容", "source": ex.DIARY},
                {"id": "i:1", "text": "访谈回答", "source": ex.INTERVIEW},
            ]},
        )
        tl_stub = type("TL", (), {
            "_statements_for": lambda self, day: [],
            "record_statements": lambda self, eng, texts, day=None: calls.append(texts) or {},
        })()
        monkeypatch.setattr(
            "deskmate.growth.emotion_timeline.EmotionTimeline",
            lambda base_dir=None: tl_stub,
        )
        res = mod.ingest_emotions(days=7, base_dir=str(tmp_path))
        assert res["scanned"] == 1, "只处理非日记源"
        assert calls == [["访谈回答"]]

    def test_learned_filtered_by_feeling_gate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ex, "by_day",
            lambda **kw: {"2026-08-26": [
                {"id": "l:1", "text": "用户希望AI变得更聪明", "source": ex.LEARNED},
                {"id": "l:2", "text": "用户表示享受孩子的依赖", "source": ex.LEARNED},
            ]},
        )
        got = {}
        tl_stub = type("TL", (), {
            "_statements_for": lambda self, day: [],
            "record_statements": lambda self, eng, texts, day=None: got.setdefault(day, texts),
        })()
        monkeypatch.setattr(
            "deskmate.growth.emotion_timeline.EmotionTimeline",
            lambda base_dir=None: tl_stub,
        )
        ei.ingest_emotions(days=7, base_dir=str(tmp_path))
        assert got == {"2026-08-26": ["用户表示享受孩子的依赖"]}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
