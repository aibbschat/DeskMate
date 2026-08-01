"""Tests for VaultStore - Obsidian vault 长期记忆存储层."""

import tempfile
from pathlib import Path

import pytest

from deskmate.memory.vault_store import VaultStore, _safe_filename, _compute_hash, _parse_frontmatter


@pytest.fixture
def vault(tmp_path):
    """临时 vault 目录。"""
    return VaultStore(vault_dir=tmp_path)


class TestSafeFilename:
    def test_basic(self):
        assert _safe_filename("用户喜欢简洁回复") == "用户喜欢简洁回复"

    def test_strips_special_chars(self):
        name = _safe_filename("用户说：\"不要模板化！\" #important")
        assert ":" not in name
        assert "\"" not in name
        assert "#" not in name

    def test_truncates(self):
        long = "这是一个非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常非常长的记忆条目"
        name = _safe_filename(long, max_len=10)
        assert len(name) <= 10


class TestComputeHash:
    def test_same_content_same_hash(self):
        assert _compute_hash("hello") == _compute_hash("hello")

    def test_different_content_different_hash(self):
        assert _compute_hash("hello") != _compute_hash("world")

    def test_hash_length(self):
        assert len(_compute_hash("test")) == 12


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        meta, body = _parse_frontmatter("just text")
        assert meta == {}
        assert body == "just text"

    def test_with_frontmatter(self):
        text = "---\ntype: preference\ntags: [写作, 反馈]\ncreated: 2026-07-29\n---\n用户喜欢简洁回复"
        meta, body = _parse_frontmatter(text)
        assert meta["type"] == "preference"
        assert meta["tags"] == ["写作", "反馈"]
        assert meta["created"] == "2026-07-29"
        assert body == "用户喜欢简洁回复"

    def test_empty_tags(self):
        text = "---\ntype: note\ntags: []\n---\n内容"
        meta, body = _parse_frontmatter(text)
        assert meta["tags"] == []


class TestWriteEntry:
    def test_basic_write(self, vault, tmp_path):
        result = vault.write_entry(
            content="用户喜欢简洁回复",
            category="preference",
            tags=["写作"],
        )
        assert "preferences/" in result
        assert (tmp_path / "preferences" / "用户喜欢简洁回复.md").exists()

    def test_duplicate_skip(self, vault):
        """相同内容写入两次应跳过。"""
        vault.write_entry(content="用户喜欢简洁", category="preference")
        result = vault.write_entry(content="用户喜欢简洁", category="preference")
        assert "跳过" in result

    def test_user_edit_protection(self, vault, tmp_path):
        """用户编辑过的文件不被覆盖。"""
        vault.write_entry(content="原始记忆", category="preference")
        filepath = tmp_path / "preferences" / "原始记忆.md"

        # 模拟用户编辑：修改文件内容但不改 hash
        original = filepath.read_text(encoding="utf-8")
        edited = original.replace("原始记忆", "用户编辑后的记忆") + "\n用户补充"
        filepath.write_text(edited, encoding="utf-8")

        # 再次写入相同内容
        result = vault.write_entry(content="原始记忆", category="preference")

        # 应该创建 -v2 文件，不覆盖用户编辑过的
        assert "v2" in result or "跳过" not in result
        assert "用户编辑后的记忆" in filepath.read_text(encoding="utf-8")


class TestSearch:
    def test_keyword_match(self, vault):
        vault.write_entry(content="用户偏好简洁的回复风格", category="preference", tags=["写作"])
        vault.write_entry(content="正在开发桌搭应用", category="work", tags=["项目"])

        result = vault.search("简洁回复")
        # CJK 2-gram: "简洁回复" -> ["简洁", "回复"] -> matches "简洁...的回复风格"
        assert "简洁" in result
        assert "桌搭" not in result

    def test_multiple_results(self, vault):
        vault.write_entry(content="用户喜欢Python", category="preference")
        vault.write_entry(content="项目用Python开发", category="work")
        vault.write_entry(content="今天天气不错", category="note")

        result = vault.search("Python")
        assert "Python" in result
        # 应该返回两条匹配的
        assert result.count("**[") == 2

    def test_no_match(self, vault):
        vault.write_entry(content="用户喜欢Python", category="preference")
        result = vault.search("量子物理")
        assert result == ""

    def test_tag_search(self, vault):
        vault.write_entry(content="用户偏好简洁", category="preference", tags=["写作"])
        result = vault.search("写作")
        assert "简洁" in result


class TestArchive:
    def test_archive_entry(self, vault, tmp_path):
        vault.write_entry(content="测试条目", category="work")
        src = tmp_path / "work" / "测试条目.md"
        assert src.exists()

        result = vault.archive_entry("work", "测试条目.md", "compressed")
        assert "已归档" in result
        assert not src.exists()
        assert (tmp_path / "archive" / "测试条目.md").exists()

    def test_archive_nonexistent(self, vault):
        result = vault.archive_entry("work", "不存在.md")
        assert "不存在" in result


class TestListEntries:
    def test_list_all(self, vault):
        vault.write_entry(content="偏好1", category="preference")
        vault.write_entry(content="工作1", category="work")
        vault.write_entry(content="笔记1", category="note")

        entries = vault.list_entries()
        assert len(entries) == 3

    def test_list_by_subdir(self, vault):
        vault.write_entry(content="偏好1", category="preference")
        vault.write_entry(content="工作1", category="work")

        entries = vault.list_entries(subdir="preferences")
        assert len(entries) == 1
        assert entries[0]["category"] == "preference"


class TestStats:
    def test_stats(self, vault):
        vault.write_entry(content="偏好1", category="preference")
        vault.write_entry(content="偏好2", category="preference")
        vault.write_entry(content="工作1", category="work")

        stats = vault.stats()
        assert stats["total"] == 3
        assert stats["by_category"]["preferences"] == 2
        assert stats["by_category"]["work"] == 1


class TestImportFromMemoryMd:
    def test_import(self, vault):
        memory = """§ 偏好 #preference
用户喜欢简洁回复

#preference 用户反感模板化内容

§ 工作 #work
#work 正在开发桌搭应用
"""
        imported = vault.import_from_memory_md(memory)
        assert len(imported) == 3

        entries = vault.list_entries()
        assert len(entries) == 3

    def test_import_skips_duplicates(self, vault):
        memory = """§ 偏好 #preference
用户喜欢简洁回复
"""
        vault.import_from_memory_md(memory)
        imported = vault.import_from_memory_md(memory)
        assert len(imported) == 0  # 第二次导入应全部跳过


class TestManagerIntegration:
    """测试 DeskMateMemoryManager 与 vault 的集成。"""

    def test_prefetch_reads_vault(self, tmp_path):
        from deskmate.memory.manager import DeskMateMemoryManager

        mm = DeskMateMemoryManager(base_dir=tmp_path)
        # 写一条 vault 记忆
        mm.vault.write_entry(
            content="用户喜欢简洁回复风格",
            category="preference",
            tags=["写作"],
        )
        # prefetch 应该能搜到
        result = mm.prefetch("简洁回复")
        assert "简洁回复" in result

    def test_vault_initialized_on_manager_creation(self, tmp_path):
        from deskmate.memory.manager import DeskMateMemoryManager

        mm = DeskMateMemoryManager(base_dir=tmp_path)
        assert mm.vault is not None
        assert (tmp_path / "vault").exists()
        assert (tmp_path / "vault" / "preferences").exists()
        assert (tmp_path / "vault" / "work").exists()
        assert (tmp_path / "vault" / "knowledge").exists()
        assert (tmp_path / "vault" / "archive").exists()

    def test_import_memory_to_vault(self, tmp_path):
        from deskmate.memory.manager import DeskMateMemoryManager

        mm = DeskMateMemoryManager(base_dir=tmp_path)
        # 写一些 MEMORY.md 内容
        mm._save_global_memory("""§ 偏好 #preference
用户喜欢简洁回复

§ 工作 #work
正在开发桌搭应用
""")
        result = mm.import_memory_to_vault()
        assert result["imported"] == 2

        # 验证 vault 中有对应文件
        entries = mm.list_vault_entries()
        assert len(entries) == 2
