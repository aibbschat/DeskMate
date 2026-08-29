"""Tests for P2-2 查询扩展（别名桥接，封堵隐式查询缺口）。

审计结论：仓库无向量索引，记忆检索纯关键词；文档 P08 暴露「用户问年号但记忆写朝代」
的隐式召回缺口。本测试验证查询扩展能桥接 年号↔朝代，且不破坏显式命中、不臆造关联。
"""

from __future__ import annotations

from pathlib import Path

from deskmate.memory.manager import DeskMateMemoryManager
from deskmate.memory.query_expand import EXPANSION_MAP, expand_keywords
from deskmate.memory.vault_store import VaultStore


class TestExpandKeywords:
    def test_reign_to_dynasty(self):
        out = expand_keywords(["成化"])
        assert "明朝" in out
        assert "宪宗" in out

    def test_dynasty_to_reign(self):
        out = expand_keywords(["明朝"])
        assert "成化" in out and "万历" in out

    def test_no_fabrication(self):
        # 不在表中的词不扩展、不臆造
        assert expand_keywords(["Python"]) == ["Python"]

    def test_no_duplicate(self):
        out = expand_keywords(["成化", "明朝"])
        assert out.count("明朝") == 1

    def test_cap(self):
        out = expand_keywords(["明朝"] * 30)
        assert len(out) <= 20


class TestExtractKeywordsExpansion:
    def test_extract_includes_expansion(self):
        kws = DeskMateMemoryManager._extract_keywords("成化年间的制度")
        assert "明朝" in kws  # 扩展桥接生效


class TestVaultSearchExpansion:
    def test_reign_recalls_dynasty_note(self, tmp_path):
        """P08 缺口封堵：vault 笔记写「明朝」，用年号「成化」应召回。"""
        vault = VaultStore(vault_dir=tmp_path)
        vault.write_entry(content="明朝开国皇帝是朱元璋，定都南京", category="note")
        # 仅含年号、无朝代字样的查询，依赖扩展桥接
        res = vault.search("成化")
        assert res and "朱元璋" in res

    def test_explicit_still_works(self, tmp_path):
        vault = VaultStore(vault_dir=tmp_path)
        vault.write_entry(content="用户偏好简洁回复", category="preference")
        assert "简洁" in vault.search("简洁回复")


class TestGlobalMemoryExpansion:
    def test_prefetch_bridges_reign_to_dynasty(self, tmp_path):
        """P08 缺口封堵：MEMORY.md 写「明朝官制」，用「成化」应召回。"""
        mgr = DeskMateMemoryManager(tmp_path)
        (tmp_path / "MEMORY.md").write_text(
            "§ 工作\n用户研究明朝官制黑幕\n\n§ 偏好\n用户喜欢历史悬疑",
            encoding="utf-8",
        )
        res = mgr.prefetch("成化年间的制度")
        assert "明朝官制" in res

    def test_explicit_recall_unaffected(self, tmp_path):
        mgr = DeskMateMemoryManager(tmp_path)
        (tmp_path / "MEMORY.md").write_text(
            "§ 喜好\n用户喜欢Python编程", encoding="utf-8"
        )
        assert "Python" in mgr.prefetch("Python")
