"""WorksStore：作品从类别落到个体，别名归一。

对应 2026-11-17 判据：情感对象必须是有具体指向的个体；「稿子」这种类别
不构成对象，《夜不收》与「稿子」不是一回事。
"""

from __future__ import annotations

import sqlite3

from deskmate.cognitive.works import (
    WorksStore,
    clean_surface,
    is_generic_work_ref,
    work_id,
)


def _mk_db(tmp_path):
    db = tmp_path / "deskmate.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS learned_history ("
        "id TEXT PRIMARY KEY, entity TEXT, ts TEXT)"
    )
    conn.commit()
    conn.close()
    return db


def _mk_shelf(tmp_path, names):
    root = tmp_path / "books"
    root.mkdir()
    for name in names:
        (root / name).mkdir()
    return root


def test_work_id_only_from_title():
    assert work_id("夜不收") == work_id("《夜不收》")
    assert work_id("夜不收") != work_id("国子监秘闻录")


def test_clean_surface_strips_wrappers_and_prefix():
    assert clean_surface("《夜不收》") == "夜不收"
    assert clean_surface("16.夜不收") == "夜不收"
    assert clean_surface("  「国子监秘闻录」 ") == "国子监秘闻录"


def test_generic_work_refs():
    for ref in ("稿子", "稿件", "章节", "文章", "章稿子", "这稿子", "大纲"):
        assert is_generic_work_ref(ref), ref
    for ref in ("夜不收", "国子监秘闻录", "第五部", "封面", "第七章"):
        assert not is_generic_work_ref(ref), ref


def test_seed_from_shelf_and_alias_resolution(tmp_path):
    db = _mk_db(tmp_path)
    shelf = _mk_shelf(tmp_path, ["16.夜不收", "03.国子监秘闻录"])
    store = WorksStore(db, books_root=shelf)

    assert store.seed_from_shelf() == 2
    # 书名号 / 编号 / 口误都能归到正名
    assert store.resolve("《夜不收》")["title"] == "夜不收"
    assert store.resolve("夜不收那本")["title"] == "夜不收"
    assert store.resolve("国子监")["title"] == "国子监秘闻录"
    assert store.resolve("稿子") is None


def test_register_merges_aliases(tmp_path):
    db = _mk_db(tmp_path)
    store = WorksStore(db, books_root=tmp_path / "nope")
    store.register("夜不收", aliases=["夜不收那本"], source="manual")
    store.register("夜不收", aliases=["那本夜不收"], source="manual")
    assert set(store.aliases_for("夜不收")) == {"夜不收那本", "那本夜不收"}
    assert store.resolve("那本夜不收")["title"] == "夜不收"


def test_seed_aliases_only_from_short_forms(tmp_path):
    """learned_history 里只有正名的短写才当别名；长写可能是另一本书。"""
    db = _mk_db(tmp_path)
    shelf = _mk_shelf(tmp_path, ["夜不收", "国子监秘闻录", "袁崇焕"])
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO learned_history(id, entity, ts) VALUES(?,?,?)",
        [
            ("1", "书:国子监", "2026-10-01T10:00:00"),
            ("2", "书:夜不收：绝命救援", "2026-10-02T10:00:00"),
            ("3", "书:大明手术师：袁崇焕", "2026-10-03T10:00:00"),
            ("4", "书:某条新闻栏目", "2026-10-04T10:00:00"),
        ],
    )
    conn.commit()
    conn.close()

    store = WorksStore(db, books_root=shelf)
    store.seed_from_shelf()

    assert store.aliases_for("国子监秘闻录") == ["国子监"]
    assert store.aliases_for("夜不收") == []
    assert store.aliases_for("袁崇焕") == []
    # 噪声不建作品
    assert store.resolve("某条新闻栏目") is None
    assert len(store.list_active()) == 3
