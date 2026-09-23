"""红线回归：神圣稿件守卫（单一事实源 deskmate/sacred_paths）。

背景（2026-09-23 巡检实测攻破的两条洞）：
1. **ingest 侧零守卫** —— local_intake 的 ``_is_sacred`` 只在 scan 的 ``_walk``
   里被调用，``build_facts`` / ``ingest_files`` 直接读外部传入的 path，而端点还
   收任意 ``paths`` → ``POST /ingest {"paths":["~/国子监秘闻录/ch1.md"]}`` 直接
   把正文灌进记忆库。
2. **symlink 绕过** —— 判定只看字面路径分段；配置目录里放 ``dirlink -> 神圣目录``，
   遍历时 entry.name 是 "dirlink"（无敏感词）就被 follow 进去了。private_store
   的守卫也只查字面路径，同病。

以下用例把「守卫必须在**解析后的真实路径**上生效」锁死在两侧（local_intake /
private_store）和两条通道（scan / ingest）上。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from deskmate.ingest import local_intake
from deskmate.sacred_paths import guarded_file, is_sacred

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
        self.notes = self.base / "notes"
        self.notes.mkdir(parents=True)
        (self.notes / "ok.md").write_text("这是一条普通笔记，可以放心记住。", encoding="utf-8")
        # symlink 姿势一：目录软链
        try:
            os.symlink(self.sacred, self.notes / "dirlink")
            os.symlink(self.sacred / "ch1.md", self.notes / "filelink.md")
        except OSError:  # 极端环境不支持软链则跳过软链用例
            self.skipTest("当前环境不支持符号链接")


class TestSacredGuardUnit(_Base):
    def test_is_sacred_literal_path(self):
        self.assertTrue(is_sacred(self.sacred / "ch1.md"))

    def test_is_sacred_through_symlink(self):
        self.assertTrue(is_sacred(self.notes / "dirlink" / "ch1.md"))
        self.assertTrue(is_sacred(self.notes / "filelink.md"))

    def test_guarded_file_rejects_and_allows(self):
        self.assertIsNone(guarded_file(self.sacred / "ch1.md", {".md"}))
        self.assertIsNone(guarded_file(self.notes / "filelink.md", {".md"}))
        real = guarded_file(self.notes / "ok.md", {".md"})
        self.assertIsNotNone(real)
        self.assertEqual(real.name, "ok.md")

    def test_guarded_file_enforces_ext_whitelist(self):
        self.assertIsNone(guarded_file(self.notes / "ok.md", {".pdf"}))


class TestLocalIntakeRedLine(_Base):
    def test_scan_excludes_sacred_and_symlinks(self):
        paths = [c["path"] for c in local_intake.scan_directories([str(self.notes)])]
        self.assertTrue(any("ok.md" in p for p in paths), "正常笔记应被扫到")
        self.assertFalse(
            any(SACRED_NAME in p or "dirlink" in p or "filelink" in p for p in paths),
            f"神圣/软链泄漏: {paths}",
        )

    def test_ingest_direct_sacred_path_rejected(self):
        r = local_intake.ingest_files([str(self.sacred / "ch1.md")],
                                      base_dir=str(self.tmp / "dm"))
        self.assertEqual(r["ingested"], 0)
        self.assertEqual(r["skipped"], 1)

    def test_ingest_via_symlink_rejected(self):
        r = local_intake.ingest_files(
            [str(self.notes / "dirlink" / "ch1.md"), str(self.notes / "filelink.md")],
            base_dir=str(self.tmp / "dm"),
        )
        self.assertEqual(r["ingested"], 0)
        self.assertEqual(r["skipped"], 2)

    def test_build_facts_returns_nothing_for_sacred(self):
        self.assertEqual(local_intake.build_facts(str(self.sacred / "ch1.md")), [])
        self.assertEqual(local_intake.build_facts(str(self.notes / "filelink.md")), [])

    def test_normal_file_still_works(self):
        r = local_intake.ingest_files([str(self.notes / "ok.md")],
                                      base_dir=str(self.tmp / "dm"))
        self.assertEqual(r["ingested"], 1)


class TestPrivateStoreRedLine(_Base):
    def setUp(self):
        super().setUp()
        from coworker.history import private_store

        self.ps = private_store
        self.hist = str(self.tmp / "history")

    def test_ingest_rejects_sacred_and_symlink(self):
        r = self.ps.ingest_files(
            [str(self.sacred / "ch1.md"), str(self.notes / "dirlink" / "ch1.md"),
             str(self.notes / "filelink.md")],
            category="测试",
            history_dir=self.hist,
        )
        self.assertEqual(r["ingested"], 0)

    def test_scan_excludes_symlinks(self):
        paths = [c["path"] for c in self.ps.scan_sources([str(self.notes)],
                                                         history_dir=self.hist)]
        self.assertTrue(any("ok.md" in p for p in paths))
        self.assertFalse(any("dirlink" in p or "filelink" in p for p in paths))

    def test_normal_file_still_works(self):
        r = self.ps.ingest_files([str(self.notes / "ok.md")], category="测试",
                                 history_dir=self.hist)
        self.assertEqual(r["ingested"], 1)


if __name__ == "__main__":
    unittest.main()
