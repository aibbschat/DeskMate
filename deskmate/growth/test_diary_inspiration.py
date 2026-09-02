"""小桔日记「灵感机制」测试。

锁定几条不能退化的行为（对应 2026-09-02 定调的设计原则）：
1. 素材不够就**不写**——日记不是定时任务，不能产出流水账。
2. 批量事件被单类封顶——一次导入 15 条记忆 ≠ 15 件值得写的事。
3. 素材账本持久——recent_events 只有 50 条窗口，会丢素材。
4. 已写过后，新增素材够多才重写（不是死幂等，也不是每次都重写）。
"""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.growth.diary_inspiration import (
    _INSPIRE_THRESHOLD,
    _KIND_CAP,
    _REFRESH_DELTA,
    DiaryInspiration,
    MaterialLedger,
    score_items,
)


def _evt(kind: str, day: str, n: int = 1, **extra) -> list[dict]:
    """造 n 条同类型事件（ts 按序区分，避免指纹撞车）。"""
    out = []
    for i in range(n):
        out.append({
            "ts": f"{day}T{10 + i // 60:02d}:{i % 60:02d}:00+00:00",
            "kind": kind,
            **extra,
        })
    return out


class ScoreItemsTest(unittest.TestCase):
    """灵感评分：加权 + 单类封顶。"""

    def test_single_memory_not_enough(self):
        """只学到一件小事 → 远不到阈值，不该动笔。"""
        score, detail = score_items(_evt("memory_learned", "2026-08-24"))
        self.assertLess(score, _INSPIRE_THRESHOLD)
        self.assertEqual(detail, {"memory_learned": 0.5})

    def test_batch_memory_capped(self):
        """批量导入 15 条记忆：封顶，不许刷分。"""
        score, detail = score_items(_evt("memory_learned", "2026-09-01", 15))
        self.assertAlmostEqual(detail["memory_learned"], _KIND_CAP)
        self.assertLessEqual(score, _KIND_CAP)

    def test_batch_skill_evolved_capped(self):
        """11 个技能同时进化：一次批处理的副产品，同样封顶。"""
        _, detail = score_items(_evt("skill_evolved", "2026-09-01", 11))
        self.assertAlmostEqual(detail["skill_evolved"], _KIND_CAP)

    def test_corrections_reach_threshold(self):
        """3 次纠正 → 达标（对标真实 08-30：确实值得写一篇）。"""
        score, _ = score_items(_evt("user_corrected", "2026-08-30", 3))
        self.assertGreaterEqual(score, _INSPIRE_THRESHOLD)

    def test_backfill_is_not_material(self):
        """系统补录不算当天素材。"""
        score, detail = score_items(_evt("depth_backfilled", "2026-08-30", 5))
        self.assertEqual(score, 0.0)
        self.assertEqual(detail, {})

    def test_milestone_weighs_more_than_trace(self):
        """里程碑（新建技能）比痕迹（聊天）重。"""
        a, _ = score_items(_evt("skill_created", "2026-09-02"))
        b, _ = score_items(_evt("chat", "2026-09-02"))
        self.assertGreater(a, b)


class LedgerTest(unittest.TestCase):
    """素材账本：持久、去重、抗 recent_events 截断。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sync_is_idempotent(self):
        """同一批事件同步两次，账本不重复。"""
        led = MaterialLedger(self.base)
        evts = _evt("user_corrected", "2026-09-02", 3)
        self.assertEqual(led.sync(evts), 3)
        self.assertEqual(led.sync(evts), 0)
        self.assertEqual(len(led.load("2026-09-02")), 3)

    def test_distinct_subjects_all_kept(self):
        """不同主体（不同技能）各自保留，不被指纹去重误杀。"""
        led = MaterialLedger(self.base)
        evts = _evt("skill_used", "2026-09-02", 1, skill_name="A") + \
               _evt("skill_used", "2026-09-02", 1, skill_name="B")
        led.sync(evts)
        self.assertEqual(len(led.load("2026-09-02")), 2)

    def test_outlives_event_window(self):
        """账本比 recent_events 活得久——窗口滚过去后素材仍在。

        这是账本存在的唯一理由：recent_events 落盘截断到 50 条。
        """
        led = MaterialLedger(self.base)
        led.sync(_evt("user_corrected", "2026-08-30", 3))
        # 模拟窗口滚动：后续事件涌入，旧事件被挤出 recent_events
        led.sync(_evt("memory_learned", "2026-09-01", 30))
        self.assertEqual(len(led.load("2026-08-30")), 3)


class ShouldWriteTest(unittest.TestCase):
    """动笔判断：阈值 + 重写增量。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        # 造一份 growth.json，让 GrowthEngine 在这个隔离目录里工作
        today = date.today().isoformat()
        (self.base / "growth.json").write_text(
            json.dumps({
                "version": 2, "intimacy": 50.0, "level": 5, "total_chats": 100,
                "recent_events": _evt("user_corrected", today, 3),
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_when_enough(self):
        insp = DiaryInspiration(self.base)
        verdict = insp.should_write()
        self.assertTrue(verdict["should_write"])
        self.assertFalse(verdict["is_refresh"])
        self.assertGreaterEqual(verdict["score"], _INSPIRE_THRESHOLD)

    def test_no_rewrite_without_new_material(self):
        """刚写完、没有新素材 → 不重写。"""
        insp = DiaryInspiration(self.base)
        first = insp.should_write()
        self.assertTrue(first["should_write"])
        insp.ledger.mark_written(first["date"], first["score"])

        again = insp.should_write()
        self.assertFalse(again["should_write"])
        self.assertTrue(again["is_refresh"])

    def test_rewrite_when_new_material_arrives(self):
        """新素材够多 → 重写（避免早上写完后后续事件永远进不去）。"""
        insp = DiaryInspiration(self.base)
        first = insp.should_write()
        insp.ledger.mark_written(first["date"], first["score"])

        # 新来一个里程碑（4.0 分），达到重写增量
        insp.ledger.sync(_evt("skill_created", first["date"], 1, skill_name="新技能"))
        again = insp.should_write()
        self.assertTrue(again["is_refresh"])
        self.assertGreaterEqual(
            again["score"] - first["score"], _REFRESH_DELTA - 1e-9
        )

    def test_silent_when_nothing_happened(self):
        """一整天没什么事 → 安静等着，绝不凑一篇。"""
        (self.base / "growth.json").write_text(
            json.dumps({"version": 2, "recent_events": []}), encoding="utf-8"
        )
        insp = DiaryInspiration(self.base)
        verdict = insp.should_write()
        self.assertFalse(verdict["should_write"])
        self.assertIn("素材还不够", verdict["reason"])


if __name__ == "__main__":
    unittest.main()
