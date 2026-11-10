"""P3 正文证据锚点：把「已写正文唯一基准」变成可校验、可回查的事实。

对应 docs/memory-discipline-design.md §5 / §9 P3（方案 B，零 schema）：
* 只有带**本地正文逐字引文且命中**的条目才允许 ``source=manuscript``；
  未命中 / 云端无正文 → 降级 ``author_ruling``，不臆造事实基准。
* 证据指针落在本地 ``canon_evidence.json``（原子写、原子替换），
  键用 fp；**不改 learned_history schema、不进 SYNC_TABLES**。
* 召回时正文条附「出处：书名 第N段」；云端无索引时明说仅本地可回查。

全部用 tmp 目录造 FTS5 正文索引，不碰作者原稿与真实库。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from deskmate.memory.learned_store import (
    LearnedStore,
    canon_evidence_path,
    canon_provenance,
    load_canon_evidence,
    source_prefix,
    verify_canon_quote,
)
from deskmate.memory.user_fact_context import build_user_fact_context
from deskmate.novel_memory.canon import (
    get_verbatim_paragraph,
    paragraphs,
    resolve_quote,
    verify_quote,
)

MANUSCRIPT = "\n".join([
    "夜不收：绝命救援",
    "第一章：碧蹄馆",
    "万历二十一年（1593年）二月二十日，天空阴恻恻的，大风从海上袭来。",
    "队列前站着名大汉，名唤孔大贵，乃东征提督李如松帐下辽东军侦察营夜不收百户。",
    "后头跟着的十四人，个个都是精挑细选出来的好手。",
])

QUOTE = "队列前站着名大汉，名唤孔大贵"


def _make_book(root: Path) -> Path:
    folder = root / "16.夜不收"
    db_dir = folder / ".deskmate"
    db_dir.mkdir(parents=True)
    con = sqlite3.connect(str(db_dir / "fts5.db"))
    try:
        con.execute(
            "CREATE VIRTUAL TABLE novels USING fts5("
            "file_path, title, content, tokenize='unicode61')"
        )
        con.execute(
            "INSERT INTO novels(file_path, title, content) VALUES (?, ?, ?)",
            ("夜不收：绝命救援.docx", "夜不收：绝命救援", MANUSCRIPT),
        )
        con.commit()
    finally:
        con.close()
    return folder


class CanonIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = _make_book(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_paragraphs_use_one_based_line_numbers(self):
        paras = paragraphs(self.folder)
        self.assertEqual(len(paras), 5)
        self.assertEqual(paras[3]["para_no"], 4)
        self.assertIn("孔大贵", paras[3]["text"])

    def test_resolve_quote_hits_verbatim_paragraph(self):
        ev = resolve_quote(self.folder, QUOTE, book_id="夜不收")
        self.assertTrue(ev["ok"], ev)
        self.assertEqual(ev["para_no"], 4)
        self.assertEqual(ev["book"], "夜不收")
        self.assertIn("孔大贵", ev["verbatim"])
        self.assertTrue(ev["quote_hash"])

    def test_fabricated_quote_fails(self):
        ev = resolve_quote(self.folder, "韩效武乃大和盟东旗总旗主", book_id="夜不收")
        self.assertFalse(ev["ok"])
        self.assertIn("未", ev["reason"])

    def test_short_quote_rejected(self):
        self.assertFalse(resolve_quote(self.folder, "孔", book_id="夜不收")["ok"])

    def test_get_verbatim_paragraph_with_context(self):
        ev = get_verbatim_paragraph(
            self.folder, "夜不收：绝命救援.docx", 4, context=1
        )
        self.assertTrue(ev["ok"])
        self.assertIn("孔大贵", ev["verbatim"])
        self.assertEqual(len(ev["context"]), 3)

    def test_verify_quote_explicit_folder(self):
        self.assertTrue(verify_quote(QUOTE, "夜不收", folder=self.folder)["ok"])
        self.assertFalse(verify_quote(None, "夜不收", folder=self.folder)["ok"])

    def test_verify_quote_missing_book_is_not_ok(self):
        ev = verify_quote(QUOTE, "一本不存在的书")
        self.assertFalse(ev["ok"])
        self.assertIn("云端", ev["reason"])


class CanonWriteGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = _make_book(self.root)
        self.base = self.root / "memory"
        self.base.mkdir()
        self.store = LearnedStore(self.base)

    def tearDown(self):
        self._tmp.cleanup()

    def _source(self) -> str:
        with sqlite3.connect(str(self.base / "deskmate.db")) as con:
            return con.execute("SELECT source FROM learned_history LIMIT 1").fetchone()[0]

    def test_manuscript_without_quote_is_demoted(self):
        self.store.append(["孔大贵是夜不收百户"], source="manuscript:夜不收")
        self.assertEqual(source_prefix(self._source()), "author_ruling")
        self.assertEqual(load_canon_evidence(self.base), {})

    def test_manuscript_with_fabricated_quote_is_demoted(self):
        self.store.append(
            ["孔大贵是夜不收百户"], source="manuscript:夜不收",
            canon_quote="韩效武乃大和盟东旗总旗主", canon_folder=str(self.folder),
        )
        self.assertEqual(source_prefix(self._source()), "author_ruling")

    def test_verified_manuscript_keeps_authority_and_records_evidence(self):
        fact = "队列前站着名大汉，名唤孔大贵，乃夜不收百户"
        self.store.append(
            [fact], source="manuscript:夜不收",
            canon_quote=QUOTE, canon_folder=str(self.folder),
        )
        src = self._source()
        self.assertEqual(source_prefix(src), "manuscript")
        ev = load_canon_evidence(self.base)
        self.assertEqual(len(ev), 1)
        rec = next(iter(ev.values()))
        self.assertEqual(rec["para_no"], 4)
        self.assertEqual(rec["book"], "夜不收")
        self.assertTrue(canon_evidence_path(self.base).is_file())
        # 原子写产物可读、无残留临时文件
        self.assertFalse(list(self.base.glob("canon_evidence.json.*.tmp")))

    def test_non_manuscript_sources_untouched(self):
        self.store.append(["《夜不收》设定：孔大贵是百户"], source="setting")
        self.assertNotEqual(source_prefix(self._source()), "author_ruling")
        self.assertEqual(load_canon_evidence(self.base), {})

    def test_injected_primary_row_has_provenance(self):
        fact = "队列前站着名大汉，名唤孔大贵，乃夜不收百户"
        self.store.append(
            [fact], source="manuscript:夜不收",
            canon_quote=QUOTE, canon_folder=str(self.folder),
        )
        ctx = build_user_fact_context(self.base, query="孔大贵")
        self.assertIn("出处：夜不收 第4段", ctx)
        self.assertIn("以正文为准", ctx)

    def test_missing_local_index_says_local_only(self):
        base2 = self.root / "cloud_like"
        base2.mkdir()
        with sqlite3.connect(str(base2 / "deskmate.db")) as con:
            con.execute(
                "CREATE TABLE learned_history (id INTEGER PRIMARY KEY, ts TEXT, fact TEXT, "
                "category TEXT, fp TEXT, source TEXT, entity TEXT, polarity TEXT, "
                "status TEXT, valid_until TEXT, valid_from TEXT, superseded_by INTEGER)"
            )
            con.execute(
                "INSERT INTO learned_history(ts, fact, category, fp, source, status) "
                "VALUES ('2026-11-10T12:00:00', ?, 'other', 'fp-x', 'manuscript:夜不收', 'active')",
                ("孔大贵是夜不收百户",),
            )
        ctx = build_user_fact_context(base2, query="孔大贵")
        self.assertIn("仅本地可回查", ctx)


if __name__ == "__main__":
    unittest.main()
