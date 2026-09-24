"""红线回归：作者稿路径守卫（单一事实源 deskmate/sacred_paths）。

2026-09-24 萧哥拍板的新政策：自己的稿子（如《国子监秘闻录》）是写作助理的核心工作对象，
**读 / 讨论 / 灌进通用记忆 / 自动刷新人物圣经** 都是红线之内、用户授权的正常行为。
唯一的硬红线是 **原稿正文绝不直接改写/删除**——由 :func:`guarded_write` 守住。

本文件锁死：
1. 作者稿可识别（``is_author_manuscript``）；
2. 读侧（``guarded_file`` / ``guarded_dir_entry`` / ``guarded_assistance_read``）对作者稿放行；
3. 写侧（``guarded_write``）对原稿正文拒绝、对 ``.deskmate`` 圣经子目录放行；
4. symlink 解析后的真实路径判定一致（作者稿经软链也照常可读、不可写原稿）。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from deskmate.ingest import local_intake
from deskmate.sacred_paths import (
    guarded_assistance_read,
    guarded_dir_entry,
    guarded_file,
    guarded_write,
    is_author_manuscript,
)

SACRED_NAME = "国子监秘闻录"


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / "base"
        self.sacred = self.base / SACRED_NAME
        self.sacred.mkdir(parents=True)
        (self.sacred / "ch1.md").write_text(
            "## 第一章\n这是绝密稿件正文 ABC123。", encoding="utf-8"
        )
        # 圣经子目录（小桔自己维护的记忆库，可写）
        (self.sacred / ".deskmate").mkdir()
        (self.sacred / ".deskmate" / "CHARACTERS.md").write_text(
            "圣经内容", encoding="utf-8"
        )
        self.notes = self.base / "notes"
        self.notes.mkdir(parents=True)
        (self.notes / "ok.md").write_text(
            "这是一条普通笔记，可以放心记住。", encoding="utf-8"
        )
        try:
            os.symlink(self.sacred, self.notes / "dirlink")
            os.symlink(self.sacred / "ch1.md", self.notes / "filelink.md")
        except OSError:
            self.skipTest("当前环境不支持符号链接")


class TestAuthorManuscriptIdentify(_Base):
    def test_is_author_manuscript_literal(self):
        self.assertTrue(is_author_manuscript(self.sacred / "ch1.md"))

    def test_is_author_manuscript_through_symlink(self):
        self.assertTrue(is_author_manuscript(self.notes / "dirlink" / "ch1.md"))
        self.assertTrue(is_author_manuscript(self.notes / "filelink.md"))

    def test_non_manuscript_false(self):
        self.assertFalse(is_author_manuscript(self.notes / "ok.md"))


class TestReadSideAllowed(_Base):
    """读侧（摄入 / 发现 / 协助）对作者稿一律放行——读稿协助是红线之内的正常行为。"""

    def test_guarded_file_allows_manuscript(self):
        real = guarded_file(self.sacred / "ch1.md", {".md"})
        self.assertIsNotNone(real, "作者稿应可摄入进通用记忆")
        self.assertIn("ch1.md", real.name)
        # symlink 也放行（解析后真实路径即作者稿，可读）
        self.assertIsNotNone(guarded_file(self.notes / "filelink.md", {".md"}))

    def test_guarded_file_enforces_ext_whitelist(self):
        self.assertIsNone(guarded_file(self.notes / "ok.md", {".pdf"}))

    def test_guarded_dir_entry_allows(self):
        self.assertTrue(guarded_dir_entry(self.sacred, allow_discovery=True))
        self.assertTrue(guarded_dir_entry(self.sacred))
        self.assertTrue(guarded_dir_entry(self.notes / "dirlink", allow_discovery=True))

    def test_assistance_read_allows_manuscript(self):
        text = guarded_assistance_read(self.sacred / "ch1.md")
        self.assertIsNotNone(text)
        self.assertIn("绝密稿件正文 ABC123", text)

    def test_assistance_read_rejects_binary_ext(self):
        (self.sacred / "secret.key").write_text("KEYDATA", encoding="utf-8")
        self.assertIsNone(guarded_assistance_read(self.sacred / "secret.key"))


class TestLocalIntake(_Base):
    def test_scan_includes_manuscript(self):
        paths = [c["path"] for c in local_intake.scan_directories([str(self.notes)])]
        self.assertTrue(any("ok.md" in p for p in paths), "正常笔记应被扫到")
        # 作者稿（含经 symlink）应可被扫到（读侧放行）
        self.assertTrue(
            any(SACRED_NAME in p or "dirlink" in p or "filelink" in p for p in paths),
            f"作者稿应可被扫描到: {paths}",
        )

    def test_ingest_manuscript_works(self):
        r = local_intake.ingest_files(
            [str(self.sacred / "ch1.md")], base_dir=str(self.tmp / "dm")
        )
        self.assertEqual(r["ingested"], 1, f"作者稿应可摄入: {r}")

    def test_ingest_via_symlink_works(self):
        # 经 symlink 抵达的作者稿必须可摄入（不被守卫拦截）。
        # 两个 symlink 解析后都是同一真实文件 ch1.md，若放进同一次调用的同 store
        # 会被内容去重幂等跳过——这里每个 symlink 用独立 store，干净验证「不被拦截」。
        for i, sym in enumerate(
            (self.notes / "dirlink" / "ch1.md", self.notes / "filelink.md")
        ):
            r = local_intake.ingest_files([str(sym)], base_dir=str(self.tmp / f"dm{i}"))
            self.assertEqual(r["ingested"], 1, f"经 symlink 的作者稿应可摄入: {r}")
            self.assertEqual(r["errors"], [], f"不该有错误: {r}")

    def test_build_facts_returns_for_manuscript(self):
        self.assertTrue(local_intake.build_facts(str(self.sacred / "ch1.md")))

    def test_normal_file_still_works(self):
        r = local_intake.ingest_files(
            [str(self.notes / "ok.md")], base_dir=str(self.tmp / "dm")
        )
        self.assertEqual(r["ingested"], 1)


class TestPrivateStore(_Base):
    def setUp(self):
        super().setUp()
        from coworker.history import private_store

        self.ps = private_store
        self.hist = str(self.tmp / "history")

    def test_ingest_allows_manuscript(self):
        r = self.ps.ingest_files(
            [
                str(self.sacred / "ch1.md"),
                str(self.notes / "dirlink" / "ch1.md"),
                str(self.notes / "filelink.md"),
            ],
            category="测试",
            history_dir=self.hist,
        )
        self.assertEqual(r["ingested"], 3, f"作者稿应可摄入私人通道: {r}")

    def test_scan_includes_manuscript(self):
        paths = [
            c["path"]
            for c in self.ps.scan_sources([str(self.notes)], history_dir=self.hist)
        ]
        self.assertTrue(any("ok.md" in p for p in paths))
        self.assertTrue(
            any("dirlink" in p or "filelink" in p or SACRED_NAME in p for p in paths)
        )

    def test_normal_file_still_works(self):
        r = self.ps.ingest_files(
            [str(self.notes / "ok.md")], category="测试", history_dir=self.hist
        )
        self.assertEqual(r["ingested"], 1)


class TestWriteGuardHardLine(_Base):
    """唯一的硬红线：原稿正文绝不直接改写 / 删除；``.deskmate`` 圣经子目录可写。"""

    def test_write_blocks_source_file(self):
        self.assertFalse(guarded_write(self.sacred / "ch1.md"))
        self.assertFalse(guarded_write(self.sacred / "人物小传.md"))
        self.assertFalse(guarded_write(self.sacred / "大纲.docx"))

    def test_write_allows_bible_subdir(self):
        self.assertTrue(guarded_write(self.sacred / ".deskmate"))
        self.assertTrue(guarded_write(self.sacred / ".deskmate" / "CHARACTERS.md"))

    def test_write_allows_non_manuscript(self):
        self.assertTrue(guarded_write(self.notes / "ok.md"))
        self.assertTrue(guarded_write(self.tmp / "output.txt"))

    def test_write_plan_a_detects_arbitrary_book_by_bible(self):
        """方案 A 核心：保护信号 = 目录含 .deskmate 圣经，与书名无关。

        换一本全新书名（如《大明烟尘》）后：
        - 建圣经前：暂不受保护（需先 analyze_novel 建 .deskmate，符合预期）；
        - 建圣经后：原稿自动受保护、圣经子目录仍可写，且**无需改代码/加书名**。
        """
        newbook = self.base / "大明烟尘"  # 与国子监秘闻录 完全不同的书名
        newbook.mkdir(parents=True)
        src = newbook / "ch1.md"
        src.write_text("新书的开头。", encoding="utf-8")

        # ① 建圣经前：默认尚未受保护（接受 bootstrap 窗口）
        self.assertTrue(
            guarded_write(src),
            "新书尚未建 .deskmate 圣经前，默认放行（需先 analyze_novel）",
        )

        # ② 建好 .deskmate 圣经：自动受保护，且与书名无关
        (newbook / ".deskmate").mkdir()
        (newbook / ".deskmate" / "CHARACTERS.md").write_text("新书圣经", encoding="utf-8")
        self.assertFalse(guarded_write(src), "新书建圣经后，原稿应自动受保护")
        self.assertFalse(
            guarded_write(newbook / "大纲.docx"), "新书建圣经后，其它正文也应受保护"
        )
        # 圣经子目录本身始终可写
        self.assertTrue(guarded_write(newbook / ".deskmate"))
        self.assertTrue(guarded_write(newbook / ".deskmate" / "CHARACTERS.md"))

        # ③ is_author_manuscript 也认这本新书（读侧标记与写侧保护同一信号）
        self.assertTrue(is_author_manuscript(src))

    def test_is_author_manuscript_follows_bible_not_title(self):
        """识别/保护信号来自 .deskmate 而非写死书名：无 .deskmate 不算、有则算。"""
        plain = self.base / "随便一个目录"
        plain.mkdir()
        (plain / "x.md").write_text("hi")
        self.assertFalse(is_author_manuscript(plain / "x.md"))
        (plain / ".deskmate").mkdir()
        self.assertTrue(is_author_manuscript(plain / "x.md"))


if __name__ == "__main__":
    unittest.main()
