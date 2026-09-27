"""scripts/bench_memory_recall.py 的 harness 行为测试。

锁三个关键行为（都是真实跑基准时踩出来的）：
1. match 归一化匹配——query 带标点/原文分行也能对上（否则 FTS 通道假 0%）；
2. channels 适用性——semantic 不把 learned_history 的 case 混进统计（否则 R 无意义）；
3. 只读保证——跑基准前后库文件逐字节不变。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_memory_recall as B  # noqa: E402

_EXT_SCHEMA = """
CREATE TABLE external_items (
    id TEXT, title TEXT DEFAULT '', summary TEXT DEFAULT '', content TEXT DEFAULT '',
    ingestion_status TEXT,
    embedding BLOB, embedding_model TEXT, embedding_dim INTEGER
);
"""
_LEARNED_SCHEMA = "CREATE TABLE learned_history (id INTEGER, ts TEXT, fact TEXT);"


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    base = tmp_path / "dm"
    base.mkdir()
    con = sqlite3.connect(str(base / "deskmate.db"))
    con.executescript(_LEARNED_SCHEMA + _EXT_SCHEMA)
    con.execute("INSERT INTO learned_history VALUES (1,'2026-09-01','萧哥在写《国子监秘闻录》，背景是明朝国子监')")
    con.execute("INSERT INTO external_items VALUES "
                "('item_a','美军航母动态','近期动向汇总','美军航母在西太活动','digested',X'00000000','bge',384)")
    con.execute("INSERT INTO external_items VALUES "
                "('item_b','','','','archived',NULL,'',0)")
    con.commit()
    con.close()
    return base


def test_keyword_self_retrieval_full_recall(tmp_base: Path):
    """自检索种子的 keyword 通道应 R=1.0——链路最基本的健全性。"""
    pool = B.load_deskmate_pool(tmp_base / "deskmate.db")
    cases = [{
        "id": "t1", "query": "国子监秘闻录", "corpus": "deskmate",
        "match": "明朝国子监", "channels": ["keyword2gram"],
    }]
    report = B.run_bench(cases, tmp_base)
    row = next(r for r in report["table"] if r["channel"] == "keyword2gram")
    assert row["R@5"] == 1.0 and row["n@5"] == 1


def test_match_normalized_against_punctuated_text(tmp_base: Path):
    """原文带标点/换行、query 不带——归一化后必须能对上（FTS 假 0% 的教训）。"""
    pool = B.load_deskmate_pool(tmp_base / "deskmate.db")
    # match 故意写成「明朝，国子监」——原文是「明朝国子监」无标点
    cases = [{
        "id": "t2", "query": "国子监秘闻录", "corpus": "deskmate",
        "match": "明朝，国子监", "channels": ["keyword2gram"],
    }]
    report = B.run_bench(cases, tmp_base)
    row = next(r for r in report["table"] if r["channel"] == "keyword2gram")
    assert row["R@5"] == 1.0


def test_channel_applicability_excludes_inapplicable_cases(tmp_base: Path):
    """learned_history 的 case 不许混进 semantic 统计（无向量，结构性搜不到）。"""
    cases = [{
        "id": "t3", "query": "国子监", "corpus": "deskmate",
        "match": "国子监", "channels": ["keyword2gram"],  # 未声明 semantic
    }]
    report = B.run_bench(cases, tmp_base)
    sem = next(r for r in report["table"] if r["channel"] == "semantic")
    assert sem["R@5"] is None and sem["n@5"] == 0


def test_bench_is_read_only(tmp_base: Path):
    db = tmp_base / "deskmate.db"
    before = db.read_bytes()
    cases = [{"id": "t4", "query": "国子监", "corpus": "deskmate",
              "match": "国子监", "channels": ["keyword2gram"]}]
    B.run_bench(cases, tmp_base)
    assert db.read_bytes() == before


def test_seed_writes_cases_and_is_idempotent(tmp_base: Path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_base))
    cases = B.make_seed(tmp_base)
    assert cases, "合成库应至少抽出 learned 种子"
    msg = B.save_seed(cases, B.cases_path(), force=False)
    assert "已生成" in msg or "已存在" in msg
    loaded = B.load_cases(B.cases_path())
    assert loaded and all(c["corpus"] in ("deskmate", "history") for c in loaded)


def test_report_json_serializable(tmp_base: Path):
    cases = [{"id": "t6", "query": "国子监", "corpus": "deskmate",
              "match": "国子监", "channels": ["keyword2gram"]}]
    report = B.run_bench(cases, tmp_base)
    json.dumps(report, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 文章级命中（2026-09-26）：同文章相邻 chunk 常同为合格答案
# ---------------------------------------------------------------------------

def test_hit_article_level():
    rows = [{"articleId": 42}]
    assert B._hit("不存在的锚子串", ["别的文本"], article="42", rows=rows)
    assert not B._hit("不存在的锚子串", ["别的文本"], article="7", rows=rows)
    # 无 article 字段时退回纯子串判法
    assert not B._hit("不存在的锚子串", ["别的文本"])
    # article 与子串任一命中即可
    assert B._hit("锚子串", ["前缀锚子串后缀"], article="999", rows=rows)
