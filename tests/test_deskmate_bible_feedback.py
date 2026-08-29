"""Tests for P1-6 圣经回流（deskmate/creation/bible_feedback.py）。

核心：创作定稿后把「该章新出现的关键信息」回流进改编圣经——
可见（回流日志）+ 可回滚（日志即追溯依据），幂等，零 LLM，fail-open。
"""

from __future__ import annotations

import json

import pytest

from deskmate.creation.bible_feedback import (
    _feedback_log_path,
    extract_candidate_entities,
    list_feedback,
    record_bible_feedback,
)


def _make_bible(tmp_path, ref: str, text: str = "# 改编圣经\n\n## 人物表\n李慕白：主角\n"):
    """造一本临时圣经（隔离，不碰真机 ~/.deskmate/novels）。"""
    from deskmate.creation.bible_feedback import _cache_key

    d = tmp_path / _cache_key(ref)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bible.md").write_text(text, encoding="utf-8")
    return d


def _bible_text(tmp_path, ref: str) -> str:
    from deskmate.creation.bible_feedback import _cache_key

    p = tmp_path / _cache_key(ref) / "bible.md"
    return p.read_text(encoding="utf-8")


class TestExtractEntities:
    def test_extracts_cjk_sequences(self):
        got = extract_candidate_entities("李慕白夜探王府，李慕白偶得玄铁令。")
        # 核心专名（3 字）应被提取；top8 排序是启发式，不纠结具体成员
        assert "李慕白" in got
        assert len(got) <= 8

    def test_filters_stopwords(self):
        got = extract_candidate_entities("我们知道这个是那个，所以然后如果但是。")
        assert all(w not in got for w in ("我们", "知道", "这个", "那个", "所以", "然后", "如果", "但是"))

    def test_dedup_against_known_text(self):
        got = extract_candidate_entities("李慕白与苏若雪相遇，李慕白用玄铁令。", known_text="李慕白")
        assert "李慕白" not in got  # 圣经已有的不算新增

    def test_empty_input(self):
        assert extract_candidate_entities("") == []
        assert extract_candidate_entities(None) == []


class TestRecordFeedback:
    def test_normal_flow_appends_bible_and_log(self, tmp_path):
        ref = "/tmp/原著.txt"
        _make_bible(tmp_path, ref)
        r = record_bible_feedback(ref, "3", "李慕白夜探王府，偶得玄铁令。", cache_root=tmp_path)
        assert r["ok"] is True
        assert r["skipped"] is False
        assert r["entities"]
        # bible 追加了创作回流区
        bible = _bible_text(tmp_path, ref)
        assert "## 创作回流" in bible
        assert "第3集" in bible
        # 日志可读（回滚依据）
        log = list_feedback(ref, tmp_path)
        assert len(log) == 1
        assert log[0]["episode"] == "3"
        assert log[0]["entities"]

    def test_idempotent_same_episode(self, tmp_path):
        ref = "/tmp/原著2.txt"
        _make_bible(tmp_path, ref)
        record_bible_feedback(ref, "1", "李慕白登场。", cache_root=tmp_path)
        r2 = record_bible_feedback(ref, "1", "李慕白登场。", cache_root=tmp_path)
        assert r2["skipped"] is True
        assert len(list_feedback(ref, tmp_path)) == 1  # 不重复追加

    def test_missing_bible_skips(self, tmp_path):
        ref = "/tmp/未建索引.txt"  # 无 bible.md
        r = record_bible_feedback(ref, "1", "文本", cache_root=tmp_path)
        assert r["ok"] is False
        assert r["skipped"] is True

    def test_missing_ref_or_content(self, tmp_path):
        r = record_bible_feedback("", "1", "文本", cache_root=tmp_path)
        assert r["skipped"] is True
        r2 = record_bible_feedback("/tmp/a.txt", "1", "", cache_root=tmp_path)
        assert r2["skipped"] is True

    def test_log_path_isolation(self, tmp_path):
        ref = "/tmp/原著3.txt"
        _make_bible(tmp_path, ref)
        record_bible_feedback(ref, "2", "新角色登场，新地点出现。", cache_root=tmp_path)
        p = _feedback_log_path(ref, tmp_path)
        assert p.is_file()
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, list) and data[0]["episode"] == "2"
