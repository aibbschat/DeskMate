"""B-1 实体链接：词典归一、chunk 打标、检索接线、旧通道回落。

不依赖 fastembed：embedding 被 monkeypatch 成 None，索引照常建。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deskmate.memory import entity_link, file_search
from deskmate.memory.file_index import FileIndex


@pytest.fixture()
def base(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "USER.md").write_text(
        "# 用户画像\n\n## 偏好与事实（非常驻·待检索）\n\n"
        "- 萧盛出版有《中国妖怪录》。\n"
        "- 萧哥给客人留了中秋节礼物。\n",
        encoding="utf-8",
    )
    (tmp_path / "MEMORY.md").write_text(
        "§ 创作 #stable\n\n- 2026-11-09: 南旗旗主为迟正河。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "deskmate.memory.local_embed.embed", lambda texts, allow_load=True: None
    )
    return tmp_path


def test_alias_file_merges_surfaces(base: Path):
    (base / "entity_aliases.json").write_text(
        json.dumps({"萧哥": "萧盛"}, ensure_ascii=False), encoding="utf-8"
    )
    lexicon = entity_link.load_lexicon(base, force=True)
    assert lexicon["萧哥"] == ("person", "萧盛")
    assert lexicon["萧盛"] == ("person", "萧盛")
    assert [c for _, c, _ in entity_link.match_entities("萧盛最近在忙什么", lexicon)] == ["萧盛"]


def test_chunk_entities_and_query_hit(base: Path):
    (base / "entity_aliases.json").write_text(
        json.dumps({"萧哥": "萧盛"}, ensure_ascii=False), encoding="utf-8"
    )
    idx = FileIndex(base)
    idx.sync()
    stats = idx.entity_stats()
    assert stats["rows"] > 0 and stats["chunks"] > 0
    # 正名 query → 命中只写了别名的 chunk
    ids = {r["id"] for r in idx.entity_chunks(["萧盛"], limit=10)}
    assert ids
    hits = file_search._anchor_hits(idx, base, "萧盛给客人留了什么", limit=6)
    assert any("中秋节礼物" in h["content"] for h in hits)


def test_ambiguous_surface_is_dropped(base: Path):
    conn = sqlite3.connect(str(base / "deskmate.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_persons("
        "name TEXT, aliases TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO known_persons VALUES(?,?,?)",
        [("萧盛", "老萧", "active"), ("萧仪", "老萧", "active")],
    )
    conn.commit()
    conn.close()
    lexicon = entity_link.load_lexicon(base, force=True)
    assert "老萧" not in lexicon     # 同优先级冲突 → 宁缺不猜
    assert "萧盛" in lexicon


def test_no_lexicon_falls_back_to_anchor_channel(base: Path):
    """无已知实体（无人名/别名文件）→ 实体通道为空，旧锚通道仍可用。"""
    idx = FileIndex(base)
    idx.sync()
    assert file_search._entity_hits(idx, base, "迟正河是哪一旗的") == []
    conn = sqlite3.connect(str(base / "deskmate.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_anchors("
        "item_kind TEXT, item_id TEXT, anchor_kind TEXT, anchor_value TEXT)"
    )
    conn.execute(
        "INSERT INTO memory_anchors VALUES('profile_item','p1','person','迟正河')"
    )
    conn.commit()
    conn.close()
    hits = file_search._anchor_hits(idx, base, "迟正河是哪一旗的")
    assert any("迟正河" in h["content"] for h in hits)
