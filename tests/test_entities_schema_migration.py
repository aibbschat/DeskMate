"""entities 存量库「schema 列补齐」回归测试。

事故背景（2026-09-07）：``connect()`` 的迁移只补了 role_category / author_confirmed，
漏了 tier 与 needs_review。老库上 ``CREATE TABLE IF NOT EXISTS`` 是空操作，两列永远补不进，
新版工具一写就报 ``entities has no column named tier``。

根治方案：改成按 ``_SCHEMA`` 全量列通用补齐（``_ensure_columns``）。本测试钉死：
1. 老库（缺列）打开后自动补齐，且默认值正确；
2. 补齐后可正常 INSERT/SELECT tier（原故障点）；
3. 已齐的库重复 open 不报错、不重复加列；
4. 未来新加列也能自动补（模拟：往 _SCHEMA 加一列 → 老库应补上）。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmate.novel_memory import entities as ent  # noqa: E402


# 出事前的老版建表语句：没有 tier / needs_review
_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    object       TEXT NOT NULL DEFAULT '',
    rel_type     TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '',
    source_file  TEXT NOT NULL DEFAULT '',
    source_seq   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    confidence   REAL NOT NULL DEFAULT 0,
    aliases      TEXT NOT NULL DEFAULT '[]',
    role_category TEXT NOT NULL DEFAULT 'narrative',
    history      TEXT NOT NULL DEFAULT '[]',
    author_confirmed INTEGER NOT NULL DEFAULT 0,
    first_seen   REAL NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL DEFAULT 0
);
"""


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO entities(id, kind, key, name, detail, status) "
        "VALUES('e1','character','张三','张三','老库里的旧条目','confirmed')"
    )
    conn.commit()
    conn.close()


class TestSchemaColumnBackfill(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="ent_mig_"))
        self.bible_dir = self.tmp / "bible"
        self.bible_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.bible_dir / "entities.db"
        _make_old_db(self.db)

    def _cols(self) -> set[str]:
        conn = sqlite3.connect(str(self.db))
        try:
            return {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
        finally:
            conn.close()

    def test_old_db_missing_tier_before_fix(self):
        """前置断言：老库确实缺 tier / needs_review（还原事故现场）。"""
        cols = self._cols()
        self.assertNotIn("tier", cols)
        self.assertNotIn("needs_review", cols)

    def test_connect_backfills_missing_columns(self):
        conn = ent.connect(self.bible_dir)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
            self.assertIn("tier", cols, "tier 必须被自动补齐")
            self.assertIn("needs_review", cols, "needs_review 必须被自动补齐")
            # 默认值正确（manuscript / 0），老行也要带上
            row = conn.execute("SELECT tier, needs_review FROM entities WHERE id='e1'").fetchone()
            self.assertEqual(row["tier"], "manuscript")
            self.assertEqual(row["needs_review"], 0)
        finally:
            conn.close()

    def test_can_write_tier_after_backfill(self):
        """原故障点：往补齐后的库写 tier 不再报 no column named tier。"""
        conn = ent.connect(self.bible_dir)
        try:
            conn.execute(
                "INSERT INTO entities(id, kind, key, name, tier, needs_review, status) "
                "VALUES('e2','character','姜阿大','姜阿大','canon',1,'pending')"
            )
            conn.commit()
            row = conn.execute("SELECT tier, needs_review FROM entities WHERE id='e2'").fetchone()
            self.assertEqual(row["tier"], "canon")
            self.assertEqual(row["needs_review"], 1)
        finally:
            conn.close()

    def test_reopen_is_idempotent(self):
        conn = ent.connect(self.bible_dir)
        conn.close()
        first = self._cols()
        conn = ent.connect(self.bible_dir)
        conn.close()
        self.assertEqual(first, self._cols(), "重复打开不应再加列")

    def test_future_new_column_is_backfilled(self):
        """根治验证：往 _SCHEMA 加一列后，老库应自动补上（以后加列不会再漏）。"""
        original = ent._SCHEMA
        patched = original.replace(
            "    last_updated REAL NOT NULL DEFAULT 0\n",
            "    last_updated REAL NOT NULL DEFAULT 0,\n    brand_new_col TEXT NOT NULL DEFAULT 'x'\n",
            1,
        )
        self.assertNotEqual(original, patched)
        ent._SCHEMA = patched
        try:
            conn = ent.connect(self.bible_dir)
            conn.close()
            self.assertIn("brand_new_col", self._cols(), "新增列应被通用补齐逻辑覆盖")
        finally:
            ent._SCHEMA = original


class TestSchemaColumnParsing(unittest.TestCase):
    def test_parses_all_declared_columns(self):
        cols = ent._schema_columns(ent._SCHEMA, "entities")
        self.assertIn("tier", cols)
        self.assertIn("needs_review", cols)
        self.assertIn("id", cols)
        # 表级约束不应被当成列
        self.assertNotIn("PRIMARY", cols)
        self.assertNotIn("CONSTRAINT", cols)

    def test_ensure_columns_returns_added_names(self):
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="ent_added_"))
        bible = tmp / "bible"
        bible.mkdir(parents=True)
        _make_old_db(bible / "entities.db")
        conn = sqlite3.connect(str(bible / "entities.db"))
        conn.row_factory = sqlite3.Row
        conn.executescript(ent._SCHEMA)
        try:
            added = ent._ensure_columns(conn, "entities")
            self.assertIn("tier", added)
            self.assertIn("needs_review", added)
            # 第二次调用：无新增
            self.assertEqual(ent._ensure_columns(conn, "entities"), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
