"""P2 生命周期：写侧有效期、分级衰减、dry-run 计划脚本。

对应 docs/memory-discipline-design.md §3.5 / §6 / §8：

- progress 写入侧默认 30 天 ``valid_until``（不只是召回侧按 ts 判陈旧）；
  显式给定的有效期不被覆盖；正文/裁定/偏好永久。
- reasoning / roundtable 90 天后不再进主块/参考层（**不删行**）；
  setting 不自动过期，只靠效力权重降权（§3.5）。
- ``scripts/memory_lifecycle.py`` 只出计划、绝不写库；评测脚本只读快照。

全部用 tmp 库，不碰真实 ``~/.deskmate/deskmate.db``。
"""

from __future__ import annotations

import datetime
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.memory.learned_store import (
    PROGRESS_TTL_DAYS,
    LearnedStore,
    SOURCE_PROGRESS,
)
from deskmate.memory.user_fact_context import _is_stale


class WriteSideTTLTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self):
        with sqlite3.connect(str(self.base / "deskmate.db")) as conn:
            return conn.execute(
                "SELECT fact, source, valid_until FROM learned_history ORDER BY id"
            ).fetchall()

    def test_progress_gets_default_valid_until(self):
        self.store.append(["《夜不收》第十一章改完了"], source=SOURCE_PROGRESS)
        fact, src, until = self._rows()[0]
        # P1-A 写入纪律会自动补书后缀，这里断言来源效力 + 书维度都对
        self.assertEqual(src.split(":", 1)[0], SOURCE_PROGRESS)
        self.assertEqual(src.split(":", 1)[1], "夜不收")
        self.assertTrue(until, "progress 写入侧必须带 valid_until")
        days = (
            datetime.datetime.fromisoformat(until) - datetime.datetime.now()
        ).total_seconds() / 86400.0
        self.assertAlmostEqual(days, PROGRESS_TTL_DAYS, delta=0.02)

    def test_explicit_valid_until_not_overridden(self):
        explicit = "2026-12-01T00:00:00"
        self.store.append(
            ["《夜不收》第十一章改完了"],
            source=SOURCE_PROGRESS,
            valid_until=explicit,
        )
        self.assertEqual(self._rows()[0][2], explicit)

    def test_non_progress_has_no_default_expiry(self):
        self.store.append(["《夜不收》设定：大和盟总旗主是韩孝民"], source="setting")
        self.store.append(["萧哥偏好雨天写作"], source="live")
        self.store.append(["孔大贵率夜不收十五人东征"], source="manuscript")
        self.assertTrue(all(r[2] is None for r in self._rows()))

    def test_inferred_progress_also_expire(self):
        """P1-A 数据层推断出的 progress 同样要有写侧有效期。"""
        self.store.append(["《夜不收》第十一章改完了"])
        rows = self._rows()
        self.assertEqual(rows[0][1].split(":", 1)[0], SOURCE_PROGRESS)
        self.assertTrue(rows[0][2])


class DecayTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 11, 10, 12, 0, 0)

    def _row(self, source: str, days_ago: int) -> dict:
        ts = (self.now - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")
        return {"ts": ts, "source": source}

    def test_reasoning_stale_after_90_days(self):
        self.assertTrue(_is_stale(self._row("reasoning:夜不收", 91), self.now))
        self.assertFalse(_is_stale(self._row("reasoning:夜不收", 30), self.now))

    def test_roundtable_stale_after_90_days(self):
        self.assertTrue(_is_stale(self._row("roundtable", 100), self.now))

    def test_setting_never_auto_expires(self):
        self.assertFalse(_is_stale(self._row("setting:夜不收", 400), self.now))

    def test_manuscript_and_preference_permanent(self):
        for src in ("manuscript:夜不收", "live", "author_ruling:夜不收", "correction"):
            self.assertFalse(_is_stale(self._row(src, 400), self.now), src)


class StoreLifecycleTest(unittest.TestCase):
    """Explore 指出的两处 P2 缺口：recent 过期过滤、retire 补链。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _ids(self):
        with sqlite3.connect(str(self.base / "deskmate.db")) as conn:
            return dict(conn.execute("SELECT fact, id FROM learned_history").fetchall())

    def test_recent_excludes_expired_active_rows(self):
        past = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat(timespec="seconds")
        self.store.append(["还没过期的偏好"], source="live")
        self.store.append(["已经过期的进度"], source="live", valid_until=past)
        facts = [r["fact"] for r in self.store.recent(10)]
        self.assertIn("还没过期的偏好", facts)
        self.assertNotIn("已经过期的进度", facts)

    def test_retire_by_source_writes_superseded_by_chain(self):
        self.store.append(["旧版资料甲"], source="local_intake:甲.txt")
        self.store.append(["新版资料"], source="live")
        ids = self._ids()
        n = self.store.retire_by_source("local_intake:甲.txt", superseded_by=ids["新版资料"])
        self.assertEqual(n, 1)
        with sqlite3.connect(str(self.base / "deskmate.db")) as conn:
            row = conn.execute(
                "SELECT status, superseded_by FROM learned_history WHERE fact='旧版资料甲'"
            ).fetchone()
        self.assertEqual(row[0], "superseded")
        self.assertEqual(row[1], ids["新版资料"])


class LifecyclePlanTest(unittest.TestCase):
    """生命周期脚本必须只读：跑完库内容逐字节不变。"""

    def test_plan_does_not_write(self):
        from scripts.memory_lifecycle import build_plan

        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = LearnedStore(base)
            store.append(["《夜不收》设定：总旗主是韩孝民"], source="setting")
            store.append(["《夜不收》第十一章改完了"], source=SOURCE_PROGRESS)
            db = store.db_path
            before = db.read_bytes()
            plan = build_plan(base)
            self.assertEqual(db.read_bytes(), before, "dry-run 绝不改库")
            self.assertIn("decay", plan)
            self.assertIn("consolidation", plan)
            self.assertIn("scanned", plan)


if __name__ == "__main__":
    unittest.main()
