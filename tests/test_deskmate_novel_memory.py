"""Tests for deskmate.novel_memory.core — pure-logic functions (no LLM calls)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from deskmate.novel_memory.core import (
    scan_novel,
    _split_batches,
    _parse_bible_docs,
    inject_bible,
    read_bible,
    novel_recall,
    novel_relationships,
    _BIBLE_FILES,
)


# ---------------------------------------------------------------------------
# scan_novel
# ---------------------------------------------------------------------------


class TestScanNovel:
    def test_reads_txt_files(self, tmp_path: Path):
        (tmp_path / "ch1.txt").write_text("第一章\n\n张三和李四见面了。", encoding="utf-8")
        (tmp_path / "ch2.txt").write_text("第二章\n\n两人对峙。", encoding="utf-8")
        text, titles = scan_novel(str(tmp_path))
        assert "张三" in text
        assert "对峙" in text
        assert len(titles) == 2

    def test_reads_md_files(self, tmp_path: Path):
        (tmp_path / "chapter.md").write_text("# 第一章\n\n故事开始了。", encoding="utf-8")
        text, titles = scan_novel(str(tmp_path))
        assert "故事开始了" in text
        assert titles == ["chapter.md"]

    def test_skips_hidden_dirs(self, tmp_path: Path):
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "secret.txt").write_text("不应该看到", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("应该看到", encoding="utf-8")
        text, _ = scan_novel(str(tmp_path))
        assert "应该看到" in text
        assert "不应该看到" not in text

    def test_skips_deskmate_dir(self, tmp_path: Path):
        (tmp_path / ".deskmate").mkdir()
        (tmp_path / ".deskmate" / "CHARACTERS.md").write_text("人物表", encoding="utf-8")
        (tmp_path / "正文.txt").write_text("正文内容", encoding="utf-8")
        text, _ = scan_novel(str(tmp_path))
        assert "正文内容" in text
        assert "人物表" not in text

    def test_empty_folder(self, tmp_path: Path):
        text, titles = scan_novel(str(tmp_path))
        assert text == ""
        assert titles == []

    def test_not_a_directory(self, tmp_path: Path):
        f = tmp_path / "not_a_dir.txt"
        f.write_text("hi", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            scan_novel(str(f))

    def test_max_chars_truncation(self, tmp_path: Path):
        # Create a file that exceeds max_chars
        big_text = "A" * 5000
        (tmp_path / "big.txt").write_text(big_text, encoding="utf-8")
        text, _ = scan_novel(str(tmp_path), max_chars=1000)
        assert len(text) <= 1000


# ---------------------------------------------------------------------------
# _split_batches
# ---------------------------------------------------------------------------


class TestSplitBatches:
    def test_short_text_single_batch(self):
        text = "段落一。\n\n段落二。"
        batches = _split_batches(text, size=1000)
        assert len(batches) == 1
        assert "段落一" in batches[0]

    def test_long_text_splits(self):
        paras = [f"第{i}段。" * 50 for i in range(20)]
        text = "\n\n".join(paras)
        batches = _split_batches(text, size=500, overlap=50)
        assert len(batches) > 1

    def test_overlap_present(self):
        text = "A" * 300 + "\n\n" + "B" * 300
        batches = _split_batches(text, size=400, overlap=100)
        if len(batches) > 1:
            # Second batch should start with some chars from first batch's end
            assert len(batches[1]) > 300


# ---------------------------------------------------------------------------
# _parse_bible_docs
# ---------------------------------------------------------------------------


class TestParseBibleDocs:
    def test_parse_with_separators(self):
        merged = (
            "===== CHARACTERS =====\n## CHARACTERS\n张三：主角\n"
            "===== OUTLINE =====\n## OUTLINE\n主线剧情\n"
            "===== TIMELINE =====\n## TIMELINE\n第一天\n"
            "===== NOTES =====\n## NOTES\n世界观设定"
        )
        docs = _parse_bible_docs(merged)
        assert "CHARACTERS" in docs
        assert "张三" in docs["CHARACTERS"]
        assert "OUTLINE" in docs
        assert "TIMELINE" in docs
        assert "NOTES" in docs

    def test_parse_without_separators(self):
        merged = "## CHARACTERS\n张三：主角\n\n## OUTLINE\n主线"
        docs = _parse_bible_docs(merged)
        assert "CHARACTERS" in docs
        assert "OUTLINE" in docs

    def test_empty_input(self):
        docs = _parse_bible_docs("")
        assert docs == {}


# ---------------------------------------------------------------------------
# inject_bible + read_bible
# ---------------------------------------------------------------------------


class TestInjectAndRead:
    def test_writes_files_and_reads_back(self, tmp_path: Path):
        docs = {
            "CHARACTERS": "## CHARACTERS\n张三：主角",
            "OUTLINE": "## OUTLINE\n主线剧情",
        }
        # Monkey-patch _db_path to use tmp sqlite
        db_path = tmp_path / "test.db"
        import deskmate.novel_memory.core as mod
        original_db_path = mod._db_path
        mod._db_path = lambda: db_path

        try:
            written = inject_bible(str(tmp_path), docs)
            assert written == 2

            # Check files on disk
            bible_dir = tmp_path / ".deskmate"
            assert (bible_dir / "CHARACTERS.md").exists()
            assert (bible_dir / "OUTLINE.md").exists()

            # Check read_bible
            loaded = read_bible(str(tmp_path))
            assert "CHARACTERS" in loaded
            assert "张三" in loaded["CHARACTERS"]

            # Check SQLite was written
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.close()
            assert rows == 2
        finally:
            mod._db_path = original_db_path

    def test_upsert_updates_existing(self, tmp_path: Path):
        docs = {"CHARACTERS": "## CHARACTERS\nv1"}
        db_path = tmp_path / "test.db"
        import deskmate.novel_memory.core as mod
        original_db_path = mod._db_path
        mod._db_path = lambda: db_path

        try:
            inject_bible(str(tmp_path), docs)
            docs_v2 = {"CHARACTERS": "## CHARACTERS\nv2 updated"}
            inject_bible(str(tmp_path), docs_v2)

            import sqlite3
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute("SELECT content FROM memories WHERE key LIKE '%CHARACTERS%'").fetchall()
            conn.close()
            assert len(rows) == 1  # not duplicated
            assert "v2 updated" in rows[0][0]
        finally:
            mod._db_path = original_db_path


# ---------------------------------------------------------------------------
# novel_recall
# ---------------------------------------------------------------------------


class TestNovelRecall:
    def test_returns_bible_summary(self, tmp_path: Path):
        bible_dir = tmp_path / ".deskmate"
        bible_dir.mkdir()
        (bible_dir / "CHARACTERS.md").write_text("## CHARACTERS\n张三：主角", encoding="utf-8")
        result = novel_recall("张三", str(tmp_path))
        assert "人物圣经摘要" in result
        assert "张三" in result

    def test_keyword_search_in_text(self, tmp_path: Path):
        (tmp_path / "ch1.txt").write_text(
            "张三走进了客栈，看到李四正在喝酒。两人对视一笑。", encoding="utf-8"
        )
        result = novel_recall("张三 客栈", str(tmp_path))
        assert "正文命中片段" in result
        assert "客栈" in result

    def test_multi_keyword_ranking(self, tmp_path: Path):
        # Snippet with both keywords should rank higher
        (tmp_path / "ch1.txt").write_text(
            "张三在客栈里找到了李四。\n\n"
            "张三独自走在路上。\n\n"
            "李四在客栈喝酒。",
            encoding="utf-8",
        )
        result = novel_recall("张三 客栈", str(tmp_path), max_snippets=2)
        # The snippet with both "张三" and "客栈" should appear
        assert "张三" in result
        assert "客栈" in result

    def test_no_bible_no_text(self, tmp_path: Path):
        result = novel_recall("不存在", str(tmp_path))
        assert "尚未生成人物圣经" in result

    def test_deduplication(self, tmp_path: Path):
        # Two files with same content: each file should produce at most a few snippets
        (tmp_path / "a.txt").write_text("张三走进了客栈。" * 20, encoding="utf-8")
        (tmp_path / "b.txt").write_text("张三走进了客栈。" * 20, encoding="utf-8")
        result = novel_recall("张三 客栈", str(tmp_path), max_snippets=10)
        # 每个文件最多3个片段（file_hits limit），不会产生40个重叠片段
        snippet_a = result.count("[a.txt]")
        snippet_b = result.count("[b.txt]")
        assert snippet_a <= 3
        assert snippet_b <= 3


# ---------------------------------------------------------------------------
# novel_relationships
# ---------------------------------------------------------------------------


class TestNovelRelationships:
    def test_finds_character(self, tmp_path: Path):
        bible_dir = tmp_path / ".deskmate"
        bible_dir.mkdir()
        (bible_dir / "CHARACTERS.md").write_text(
            "## CHARACTERS\n- 张三：主角，与李四敌对\n- 李四：反派，与张三敌对",
            encoding="utf-8",
        )
        result = novel_relationships("张三", str(tmp_path))
        assert "张三" in result
        assert "敌对" in result

    def test_character_not_found(self, tmp_path: Path):
        bible_dir = tmp_path / ".deskmate"
        bible_dir.mkdir()
        (bible_dir / "CHARACTERS.md").write_text("## CHARACTERS\n- 张三：主角", encoding="utf-8")
        result = novel_relationships("王五", str(tmp_path))
        assert "未直接提及" in result

    def test_no_bible(self, tmp_path: Path):
        result = novel_relationships("张三", str(tmp_path))
        assert "尚未生成人物圣经" in result
