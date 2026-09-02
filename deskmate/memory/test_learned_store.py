"""LearnedStore 测试：长期记忆从 learned_history.jsonl 收进统一库。

锁定几条不能退化的行为：
1. **items 时间正序（最老在前）**——前端 UnderstandingCard 会 reverse 后
   展示，顺序错了就是倒着显示（这是此前踩过的同类坑）。
2. 同一事实重复学到只入库一条（fp UNIQUE 幂等）。
3. 统计走 SQL 聚合，**不受 limit 影响**（此前 by_category 只数返回的 30 条）。
4. 迁移幂等：跑两次不会翻倍；校验不过**绝不归档**原文件。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.memory.learned_store import LearnedStore, classify_fact, _fingerprint


def _legacy(base: Path, rows: list[dict]) -> None:
    """写一份 legacy jsonl 到隔离目录。"""
    p = base / "memory" / "learned_history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


class AppendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_and_read_back(self):
        self.store.append(["用户偏好深色模式写代码"])
        items = self.store.recent(10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["fact"], "用户偏好深色模式写代码")
        self.assertEqual(items[0]["category"], "preference")

    def test_duplicate_is_idempotent(self):
        """同一件事重复学到 → 只记一条（长期记忆不该重复计数）。"""
        self.store.append(["用户偏好深色模式写代码"])
        n = self.store.append(["用户偏好深色模式写代码"])
        self.assertEqual(n, 0)
        self.assertEqual(self.store.stats()["total"], 1)

    def test_whitespace_variant_deduped(self):
        """空白差异不算新事实。"""
        self.store.append(["用户偏好深色模式"])
        self.store.append(["用户偏好 深色模式"])
        self.assertEqual(self.store.stats()["total"], 1)

    def test_empty_facts_ignored(self):
        self.assertEqual(self.store.append(["", "   ", None]), 0)
        self.assertEqual(self.store.stats()["total"], 0)


class OrderTest(unittest.TestCase):
    """顺序契约：正序返回，同秒靠 id 保持学到的先后。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_recent_is_chronological(self):
        """items 必须最老在前——前端 reverse 后才是最新在前。"""
        for i in range(5):
            self.store.append([f"事实-{i}"], ts=f"2026-09-0{i + 1}T10:00:00")
        items = self.store.recent(10)
        self.assertEqual([it["fact"] for it in items], [f"事实-{i}" for i in range(5)])

    def test_recent_takes_newest_n(self):
        """limit 取的是「最近 N 条」，不是最老 N 条（原实现的反向陷阱）。"""
        for i in range(10):
            self.store.append([f"事实-{i}"], ts=f"2026-09-01T10:{i:02d}:00")
        items = self.store.recent(3)
        self.assertEqual([it["fact"] for it in items], ["事实-7", "事实-8", "事实-9"])

    def test_same_second_keeps_insertion_order(self):
        """ts 是秒精度，同秒多条靠 id tie-break 保序。"""
        self.store.append(["甲", "乙", "丙"], ts="2026-09-01T10:00:00")
        items = self.store.recent(10)
        self.assertEqual([it["fact"] for it in items], ["甲", "乙", "丙"])


class StatsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stats_cover_all_not_just_limited(self):
        """统计基于全量，不被 limit 截断——此前分类只数了 30 条。"""
        self.store.append(["我喜欢用深色主题"] + [f"用工具 {i}" for i in range(50)])
        snap = self.store.snapshot(limit=5)
        self.assertEqual(len(snap["items"]), 5)
        self.assertEqual(snap["stats"]["total"], 51)
        self.assertEqual(sum(snap["stats"]["by_category"].values()), 51)


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(classify_fact("用户偏好深色模式"), "preference")
        self.assertEqual(classify_fact("他习惯每天先写大纲"), "habit")
        self.assertEqual(classify_fact("用 git rebase 整理提交"), "tool")
        self.assertEqual(classify_fact("明朝于1368年建立"), "other")

    def test_priority_locked(self):
        """锁定匹配顺序 preference > habit > tool > other。

        这是沿用既有启发式的固有顺序（不是最优，但**不能改**——改了就会
        重写存量 211 条的分类口径，「了解度」统计会整体漂移）。
        """
        # 同时命中「常用」(preference) 与「命令」(tool) → 归 preference
        self.assertEqual(classify_fact("常用命令是 git rebase"), "preference")
        # 同时命中「习惯」(habit) 与「使用」(tool) → 归 habit
        self.assertEqual(classify_fact("习惯使用 vim"), "habit")

    def test_fingerprint_ignores_whitespace(self):
        self.assertEqual(_fingerprint("a b"), _fingerprint("ab"))


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_migrate_imports_all(self):
        rows = [
            {"ts": "2026-08-0%dT10:00:00" % i, "fact": f"事实-{i}", "category": "other"}
            for i in range(1, 6)
        ]
        _legacy(self.base, rows)
        store = LearnedStore(self.base)
        res = store.migrate_from_legacy()
        self.assertTrue(res["ok"])
        self.assertEqual(res["in_file"], 5)
        self.assertEqual(res["in_db"], 5)
        self.assertTrue(res["archived"])
        self.assertFalse(store.legacy_path.is_file())  # 已改名归档

    def test_migrate_is_idempotent(self):
        """跑两次不会翻倍——重启多少次都安全。"""
        _legacy(self.base, [{"ts": "2026-09-01T10:00:00", "fact": "甲"}])
        store = LearnedStore(self.base)
        store.migrate_from_legacy()
        again = store.migrate_from_legacy()  # 文件已归档，这次应无数据可导
        self.assertTrue(again["ok"])
        self.assertEqual(again["in_file"], 0)
        self.assertEqual(store.stats()["total"], 1)  # 没翻倍

    def test_migrate_keeps_legacy_category(self):
        """文件里已有分类就尊重它，不重算——避免迁移改变既有统计口径。"""
        _legacy(self.base, [{"ts": "2026-09-01T10:00:00", "fact": "随便一句话", "category": "preference"}])
        store = LearnedStore(self.base)
        store.migrate_from_legacy()
        self.assertEqual(store.recent(1)[0]["category"], "preference")

    def test_no_archive_when_import_fails(self):
        """导入失败必须保留原文件——这是数据安全的底线。"""
        _legacy(self.base, [{"ts": "2026-09-01T10:00:00", "fact": "甲"}])
        store = LearnedStore(self.base)
        # 把 DB 路径指向一个目录：sqlite3 连不上 → 走 except，绝不归档
        store.db_path = self.base / "not-a-file" / "deskmate.db"
        (self.base / "not-a-file").mkdir()
        res = store.migrate_from_legacy()
        self.assertFalse(res["ok"])
        self.assertFalse(res["archived"])
        self.assertTrue(store.legacy_path.is_file())  # 原文件还在

    def test_archive_renames_not_deletes(self):
        _legacy(self.base, [{"ts": "2026-09-01T10:00:00", "fact": "甲"}])
        store = LearnedStore(self.base)
        self.assertTrue(store.archive_legacy())
        backups = list((self.base / "memory").glob("learned_history.jsonl.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertGreater(backups[0].stat().st_size, 0)


class FailOpenTest(unittest.TestCase):
    """DB 出事时不能拖垮主链路。"""

    def test_read_returns_empty_on_db_error(self):
        base = Path(TemporaryDirectory().name)
        store = LearnedStore(base)
        store.db_path = base / "gone" / "x.db"
        self.assertEqual(store.recent(10), [])
        self.assertEqual(store.stats()["total"], 0)


if __name__ == "__main__":
    unittest.main()
