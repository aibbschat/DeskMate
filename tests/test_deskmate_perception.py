"""Tests for deskmate.perception — pure-logic modules (no AppleScript calls)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate.perception.filesystem_watcher import FilesystemWatcher, FileChange
from deskmate.perception.file_reader import read_file_safe, list_dir, _human_size
from deskmate.perception.diff_changes import diff_files, diff_text, summarize_changes


# ---------------------------------------------------------------------------
# FilesystemWatcher
# ---------------------------------------------------------------------------


class TestFilesystemWatcher:
    def test_initial_snapshot(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        assert "a.txt" in w._snapshot

    def test_detect_new_file(self, tmp_path: Path):
        w = FilesystemWatcher(root=str(tmp_path))
        (tmp_path / "new.txt").write_text("new", encoding="utf-8")
        changes = w.poll()
        assert len(changes) == 1
        assert changes[0].kind == "created"
        assert changes[0].path == "new.txt"

    def test_detect_modified_file(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        # Modify
        import time
        time.sleep(0.05)
        (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
        changes = w.poll()
        assert any(c.kind == "modified" and c.path == "a.txt" for c in changes)

    def test_detect_deleted_file(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        f.unlink()
        changes = w.poll()
        assert any(c.kind == "deleted" and c.path == "a.txt" for c in changes)

    def test_ignores_hidden_dirs(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        assert ".git/config" not in w._snapshot

    def test_ignores_node_modules(self, tmp_path: Path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").mkdir()
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        assert not any("node_modules" in k for k in w._snapshot)

    def test_ignores_pyc_files(self, tmp_path: Path):
        (tmp_path / "mod.pyc").write_text("x", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        assert "mod.pyc" not in w._snapshot

    def test_nonexistent_dir(self, tmp_path: Path):
        w = FilesystemWatcher(root=str(tmp_path / "nope"))
        assert w._snapshot == {}
        assert w.poll() == []

    def test_get_recent(self, tmp_path: Path):
        w = FilesystemWatcher(root=str(tmp_path))
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        w.poll()
        recent = w.get_recent(minutes=1)
        assert len(recent) >= 1

    def test_no_changes_on_second_poll(self, tmp_path: Path):
        (tmp_path / "a.txt").write_text("a", encoding="utf-8")
        w = FilesystemWatcher(root=str(tmp_path))
        w.poll()  # first poll establishes baseline
        changes = w.poll()  # second poll, no changes
        assert changes == []


# ---------------------------------------------------------------------------
# read_file_safe
# ---------------------------------------------------------------------------


class TestReadFileSafe:
    def test_read_txt(self, tmp_path: Path):
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        assert read_file_safe(str(f)) == "hello world"

    def test_read_python(self, tmp_path: Path):
        f = tmp_path / "script.py"
        f.write_text("print('hi')", encoding="utf-8")
        assert "print" in read_file_safe(str(f))

    def test_read_json(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        assert '"key"' in read_file_safe(str(f))

    def test_rejects_binary_ext(self, tmp_path: Path):
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n")
        result = read_file_safe(str(f))
        assert "不支持" in result

    def test_nonexistent_file(self, tmp_path: Path):
        result = read_file_safe(str(tmp_path / "nope.txt"))
        assert "不存在" in result

    def test_directory(self, tmp_path: Path):
        result = read_file_safe(str(tmp_path))
        assert "不是文件" in result

    def test_truncation(self, tmp_path: Path):
        f = tmp_path / "big.txt"
        f.write_text("A" * 1000, encoding="utf-8")
        # max_chars=600 < 全文 1000 → 不再静默截断，而是明确「未读完」并给出续读 offset
        result = read_file_safe(str(f), max_chars=600)
        assert "未读完" in result
        assert "offset=" in result

    def test_oversize_guided(self, tmp_path: Path):
        f = tmp_path / "huge.txt"
        f.write_text("A" * 1000, encoding="utf-8")
        # max_chars=100 << 全文 1000 → 不再硬拒，而是返回「未读完」提示并给出续读 offset
        result = read_file_safe(str(f), max_chars=100)
        assert "未读完" in result
        assert "offset=" in result

    def test_allows_makefile(self, tmp_path: Path):
        f = tmp_path / "Makefile"
        f.write_text("all:\n\techo hi", encoding="utf-8")
        assert "echo" in read_file_safe(str(f))

    def test_allows_novel_ext(self, tmp_path: Path):
        f = tmp_path / "chapter.novel"
        f.write_text("故事开始了。", encoding="utf-8")
        assert "故事开始了" in read_file_safe(str(f))


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class TestListDir:
    def test_list_files_and_dirs(self, tmp_path: Path):
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        result = list_dir(str(tmp_path))
        assert "file.txt" in result
        assert "subdir/" in result

    def test_nonexistent_dir(self, tmp_path: Path):
        result = list_dir(str(tmp_path / "nope"))
        assert "不是目录" in result

    def test_max_items(self, tmp_path: Path):
        for i in range(20):
            (tmp_path / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        result = list_dir(str(tmp_path), max_items=5)
        assert "…（共" in result


# ---------------------------------------------------------------------------
# diff_changes
# ---------------------------------------------------------------------------


class TestDiffChanges:
    def test_diff_files(self, tmp_path: Path):
        old = tmp_path / "old.txt"
        new = tmp_path / "new.txt"
        old.write_text("line1\nline2\nline3\n", encoding="utf-8")
        new.write_text("line1\nline2 modified\nline3\nline4\n", encoding="utf-8")
        result = diff_files(str(old), str(new))
        assert "line2 modified" in result
        assert "+line4" in result

    def test_diff_files_same(self, tmp_path: Path):
        old = tmp_path / "a.txt"
        new = tmp_path / "b.txt"
        old.write_text("same", encoding="utf-8")
        new.write_text("same", encoding="utf-8")
        result = diff_files(str(old), str(new))
        assert "相同" in result

    def test_diff_text(self):
        result = diff_text("hello\nworld", "hello\nuniverse")
        assert "universe" in result
        assert "world" in result

    def test_diff_text_same(self):
        result = diff_text("same", "same")
        assert "无变更" in result

    def test_summarize_changes(self):
        stats = summarize_changes("a\nb\nc", "a\nb\nd\ne")
        assert stats["added"] == 2  # d, e
        assert stats["removed"] == 1  # c
        assert stats["total_old"] == 3
        assert stats["total_new"] == 4


# ---------------------------------------------------------------------------
# _human_size
# ---------------------------------------------------------------------------


class TestHumanSize:
    def test_bytes(self):
        assert _human_size(100) == "100B"

    def test_kb(self):
        assert "KB" in _human_size(2048)

    def test_mb(self):
        assert "MB" in _human_size(2 * 1024 * 1024)

    def test_gb(self):
        assert "GB" in _human_size(3 * 1024 * 1024 * 1024)
