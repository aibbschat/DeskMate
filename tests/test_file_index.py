"""三段检索：文件索引 / 融合 / 兜底 / 锚一跳 的单元测试。

不依赖 fastembed：向量通道用直接写入的 BLOB 验证；embedding 调用被 monkeypatch
成 None，走「关键词可用、向量不可用」的 fail-open 路径（生产同样支持）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest

from deskmate.memory import file_search
from deskmate.memory.file_index import FileIndex, chunk_file, tokenize


@pytest.fixture()
def base(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "USER.md").write_text(
        "# 用户画像\n\n"
        "## 硬规矩\n\n"
        "- 正文里的笔误只标不改，禁止同义替换。\n"
        "- 写入文件一律用 write_file，不要用 shell 重定向。\n\n"
        "## 偏好与事实（非常驻·待检索）\n\n"
        "- 萧盛出版有《中国妖怪录》。\n"
        "- 朗读音色用御姐音。\n",
        encoding="utf-8",
    )
    (tmp_path / "MEMORY.md").write_text(
        "§ 创作 #stable\n\n"
        "- 2026-11-09: 南旗旗主为迟正河，1518 年与宁靖交过手。\n"
        "- 2026-11-10: 大和盟要李昖死，倭军要李昖活。\n",
        encoding="utf-8",
    )
    (tmp_path / "diary_memory.md").write_text(
        "§ 日记沉淀 #moment #diary\n\n"
        "2026-10-29：🩷 影视公司昨天跟我要了《夜不收》的版权。\n\n"
        "2026-10-22：🩷 我今天两本书的合同废了。\n",
        encoding="utf-8",
    )
    diary = tmp_path / "diary"
    diary.mkdir()
    (diary / "2026-11-10.md").write_text(
        "## 09:31\n\n- 默契 0.8\n\n太原那场建筑火灾，14 人遇难。\n",
        encoding="utf-8",
    )
    (diary / "2026-11-09.md.bak-20261110").write_text("旧备份不该进索引", encoding="utf-8")
    vault = tmp_path / "vault" / "work"
    vault.mkdir(parents=True)
    (vault / "2026-08-10-试卷调包案.md").write_text(
        "试卷调包案里，被顶替的寒门太学生是韦涵亮。", encoding="utf-8"
    )
    archive = tmp_path / "vault" / "archive"
    archive.mkdir(parents=True)
    (archive / "old.md").write_text("归档内容不该进索引", encoding="utf-8")

    # 向量通道测试不加载模型
    monkeypatch.setattr("deskmate.memory.local_embed.embed", lambda texts, allow_load=True: None)
    return tmp_path


def test_tokenize_filters_function_bigrams():
    toks = set(tokenize("你还记得那个稿子搁哪儿了吗"))
    assert "记得" not in toks          # 功能词 bigram
    assert "稿子" in toks
    assert "搁哪" in toks               # 去「哪儿」，但保留有区分度的「搁哪」
    assert "哪儿" not in toks


def test_chunkers_split_bullets_and_skip_metadata(base: Path):
    user = chunk_file("user", (base / "USER.md").read_text(encoding="utf-8"))
    assert [c for _s, c in user if "只标不改" in c]
    day = chunk_file(
        "diary_day", (base / "diary" / "2026-11-10.md").read_text(encoding="utf-8")
    )
    assert any("14 人遇难" in c for _s, c in day)
    assert all("默契" not in c for _s, c in day)


def test_sync_builds_index_and_skips_unchanged(base: Path):
    idx = FileIndex(base)
    first = idx.sync()
    assert first["changed"] >= 4
    assert first["chunks"] > 0
    # vault/archive 与 .bak 不入索引
    kinds = idx.stats()["by_kind"]
    assert kinds.get("vault", 0) == 1
    assert kinds.get("diary_day", 0) == 1
    second = idx.sync()
    assert second["changed"] == 0
    assert second["skipped"] >= 4
    assert second["ms"] < 500


def test_new_memory_searchable_on_next_query(base: Path):
    FileIndex(base).sync()
    with (base / "USER.md").open("a", encoding="utf-8") as f:
        f.write("\n## 临时加条\n\n- 新规矩：封面只放图不压字。\n")
    t0 = time.perf_counter()
    hits = file_search.search_memory_files("封面能不能压字", base=base, k=5)
    elapsed = time.perf_counter() - t0
    assert any("封面只放图不压字" in h["content"] for h in hits)
    assert elapsed < 3.0  # 下一轮即可检索（含增量同步 + 关键词检索）


def test_keyword_query_and_budget(base: Path):
    FileIndex(base).sync()
    hits = file_search.search_memory_files("试卷调包案被顶替的人", base=base, k=8)
    assert any("韦涵亮" in h["content"] for h in hits)
    assert len("\n\n".join(h["content"] for h in hits)) <= 1200
    assert all(len(h["content"]) <= 400 for h in hits)


def test_section_filter_for_preferences(base: Path):
    FileIndex(base).sync()
    hits = file_search.search_memory_files(
        "妖怪书", base=base, k=5, section_contains="偏好与事实"
    )
    assert hits
    assert all("偏好与事实" in h["section"] for h in hits)


def test_vector_search_with_direct_embedding(base: Path):
    idx = FileIndex(base)
    idx.sync()
    with idx._connect() as conn:
        row = conn.execute(
            "SELECT id FROM memory_chunks WHERE content LIKE '%御姐音%'"
        ).fetchone()
        vec = np.asarray([0.0, 0.0, 1.0, 0.0], dtype="float32")
        conn.execute(
            "UPDATE memory_chunks SET embedding=?, embedding_dim=4, "
            "embedding_model='test' WHERE id=?",
            (vec.tobytes(), row["id"]),
        )
    res = idx.vector_search([0.0, 0.0, 1.0, 0.0], limit=1)
    assert res and "御姐音" in res[0]["content"]


def test_backstop_budget_and_sources(base: Path):
    FileIndex(base).sync()
    bs = file_search.backstop_chunks(base, limit=3, max_chars=400)
    assert bs and len(bs) <= 3
    assert sum(len(h["content"]) for h in bs) <= 400


def test_anchor_one_hop(base: Path):
    idx = FileIndex(base)
    idx.sync()
    with sqlite3.connect(str(base / "deskmate.db")) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_anchors("
            "item_kind TEXT, item_id TEXT, anchor_kind TEXT, anchor_value TEXT)"
        )
        conn.executemany(
            "INSERT INTO memory_anchors VALUES(?,?,?,?)",
            [
                ("profile_item", "p1", "person", "迟正河"),
                ("profile_item", "p1", "topic", "南旗"),
            ],
        )
    assert "南旗" in file_search._anchor_neighbors(base, ["迟正河"])
    hits = file_search._anchor_hits(idx, base, "迟正河是哪一旗的")
    assert any("迟正河" in h["content"] for h in hits)


def test_anchor_neighbor_miss_returns_empty(base: Path):
    FileIndex(base).sync()
    assert file_search._anchor_neighbors(base, ["不存在的锚"]) == []


def test_index_healthy_is_readonly_and_bootstraps(base: Path, monkeypatch):
    """健康检查只读探测：不清表、不建表；无 db 时放行给 sync 首次自举。

    修前：index_healthy 经 ``_idx()`` 构造 FileIndex 触发 _ensure_schema，
    CHUNK_VERSION 一变就 DELETE 全部索引表——健康检查自己就是破坏者，
    随后 chunks=0 判不健康，唯一重建入口（sync）又被它门控，永久锁死。
    """
    monkeypatch.setattr("deskmate.memory.local_embed.embed", lambda texts, allow_load=True: None)
    # 1) 无 db、有源文件 → healthy=True（把首轮交给 search 里的 sync 自举）
    assert (base / "deskmate.db").exists() is False
    assert file_search.index_healthy(base) is True

    # 2) 建索引后连续探测两次，chunks 计数不得被清
    idx = FileIndex(base)
    idx.sync()
    before = idx.stats()["chunks"]
    assert before > 0
    assert file_search.index_healthy(base) is True
    assert file_search.index_healthy(base) is True
    after = FileIndex(base).stats()["chunks"]
    assert after == before, f"健康探测清了表：{before} → {after}"


def test_chunk_memory_keeps_plain_first_line(tmp_path: Path, monkeypatch):
    """§ 标题下首行是普通句子（非 bullet）不得丢弃。

    修前：旧逻辑只认 ``- `` 开头，普通首行直接丢 → sync 报 changed=1
    但 chunks=0，该节内容检索永远搜不到（历史文件/人工手写/压缩漂移）。
    """
    monkeypatch.setattr("deskmate.memory.local_embed.embed", lambda texts, allow_load=True: None)
    (tmp_path / "MEMORY.md").write_text(
        "§ 喜好\n\n用户喜欢Python和深夜写作。\n", encoding="utf-8"
    )
    idx = FileIndex(tmp_path)
    idx.sync()
    assert idx.stats()["chunks"] > 0
    hits = file_search.search_memory_files("Python", base=tmp_path, read_only=True)
    assert hits and any("Python" in (h.get("content") or "") for h in hits)
