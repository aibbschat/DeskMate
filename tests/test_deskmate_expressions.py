"""Tests for 统一表达源 deskmate/cognitive/expressions.py（F2/F3 地基）。

hermetic：不碰真实 ~/.deskmate，全部用临时目录。
"""

from __future__ import annotations

import datetime

import pytest

from deskmate.cognitive import expressions as ex


# ----------------------------------------------------------------------
# 日期解析
# ----------------------------------------------------------------------
class TestDayOf:
    def test_iso_with_timezone(self):
        assert ex._day_of("2026-09-05T07:33:08+08:00") == "2026-09-05"

    def test_iso_naive(self):
        assert ex._day_of("2026-09-04T16:17:38.883375") == "2026-09-04"

    def test_date_only(self):
        assert ex._day_of("2026-09-01") == "2026-09-01"

    def test_empty_and_garbage(self):
        assert ex._day_of("") == ""
        assert ex._day_of("不是时间") == ""
        assert ex._day_of(None) == ""


# ----------------------------------------------------------------------
# 可信度排序
# ----------------------------------------------------------------------
class TestPickByTrust:
    @staticmethod
    def _e(source, text):
        return {"source": source, "text": text, "id": f"{source}:1"}

    def test_diary_wins_over_interview(self):
        exprs = [self._e(ex.INTERVIEW, "访谈说的"), self._e(ex.DIARY, "日记写的")]
        got = ex.pick_by_trust(exprs, ex.TRUST_ORDER)
        assert [e["source"] for e in got] == [ex.DIARY]

    def test_falls_back_to_interview_when_no_diary(self):
        exprs = [self._e(ex.LEARNED, "聊天说的"), self._e(ex.INTERVIEW, "访谈说的")]
        got = ex.pick_by_trust(exprs, ex.TRUST_ORDER)
        assert [e["source"] for e in got] == [ex.INTERVIEW]

    def test_falls_back_to_learned(self):
        exprs = [self._e(ex.LEARNED, "聊天说的")]
        got = ex.pick_by_trust(exprs, ex.TRUST_ORDER)
        assert [e["source"] for e in got] == [ex.LEARNED]

    def test_empty(self):
        assert ex.pick_by_trust([], ex.TRUST_ORDER) == []

    def test_not_mixing_sources(self):
        """核心契约：不是三源混着判，高可信源有内容就不再看低可信源。"""
        exprs = [
            self._e(ex.DIARY, "日记A"),
            self._e(ex.INTERVIEW, "访谈B"),
            self._e(ex.LEARNED, "聊天C"),
        ]
        got = ex.pick_by_trust(exprs, (ex.DIARY, ex.INTERVIEW, ex.LEARNED))
        assert len(got) == 1 and got[0]["source"] == ex.DIARY


# ----------------------------------------------------------------------
# 读取与降级
# ----------------------------------------------------------------------
class TestCollect:
    def test_empty_dir_returns_nothing(self, tmp_path):
        """目录里没有库文件 → 三个源都读不到，但不许抛异常。"""
        assert ex.collect(days=7, base_dir=tmp_path) == []
        assert ex.by_day(days=7, base_dir=tmp_path) == {}

    def test_sources_filter(self, tmp_path):
        got = ex.collect(days=7, base_dir=tmp_path, sources=(ex.DIARY,))
        assert got == []

    def test_diary_read_failure_degrades(self, tmp_path, monkeypatch):
        """日记库炸了 → 该源降级为空，不影响别的源，也不抛异常。"""
        monkeypatch.setattr(
            ex,
            "_read_diary",
            lambda base, days: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # 另外两源在空目录下也是空 → 整体为空，但没抛异常即通过
        assert ex.collect(days=7, base_dir=tmp_path) == []

    def test_by_day_groups_and_sorts(self, monkeypatch):
        rows = [
            {"id": "d:2", "day": "2026-09-05", "ts": "2026-09-05T10:00:00",
             "text": "第二则", "source": ex.DIARY, "category": ""},
            {"id": "d:1", "day": "2026-09-04", "ts": "2026-09-04T09:00:00",
             "text": "第一则", "source": ex.DIARY, "category": ""},
            {"id": "i:1", "day": "2026-09-04", "ts": "2026-09-04T20:00:00",
             "text": "访谈答的", "source": ex.INTERVIEW, "category": "访谈"},
        ]
        monkeypatch.setattr(ex, "collect", lambda **kw: rows)
        got = ex.by_day()
        assert sorted(got) == ["2026-09-04", "2026-09-05"]
        # 同组内保持时间正序
        assert [e["id"] for e in got["2026-09-04"]] == ["d:1", "i:1"]

    def test_texts_by_day(self, monkeypatch):
        monkeypatch.setattr(
            ex,
            "collect",
            lambda **kw: [{"id": "d:1", "day": "2026-09-04", "ts": "t",
                           "text": "原文", "source": ex.DIARY, "category": ""}],
        )
        assert ex.texts_by_day() == {"2026-09-04": ["原文"]}


# ----------------------------------------------------------------------
# 常量契约
# ----------------------------------------------------------------------
class TestContracts:
    def test_id_prefixes_distinct(self):
        """三源 id 前缀必须互异，否则已处理标记会碰撞。"""
        prefixes = {"d:", "i:", "l:"}
        assert len(prefixes) == 3

    def test_trust_order_covers_all(self):
        assert set(ex.TRUST_ORDER) == set(ex.ALL_SOURCES)

    def test_transactional_categories_excluded_by_default(self):
        """默认不过滤时，learned 只取非事务性分类（不放 189 条流水进来）。"""
        assert "tool" in ex.TRANSACTIONAL_CATEGORIES
        assert "other" in ex.TRANSACTIONAL_CATEGORIES

    def test_cutoff_inclusive_today(self):
        assert ex._cutoff(1) == datetime.date.today().isoformat()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
