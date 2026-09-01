"""Tests for 史料库检索（search_history）— FTS5 三级降级、排序、去重、跨库合并、降级容错。

No network: 用临时 SQLite 建带数据的史料库（复刻灌库侧的 fts_content 字间插空格预处理）。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from coworker.history import (
    build_fts5_query,
    list_sources,
    make_history_search_tool,
    search_across_dbs,
    search_one_db,
)


# ---------------------------------------------------------------- 建库 helper


def _tokenize_cjk(text: str) -> str:
    """复刻灌库侧的 fts_content 预处理：CJK 字间插空格，让 unicode61 切成单字。"""
    return re.sub(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])", r" \1 ", text).strip()


def _build_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE articles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_file TEXT NOT NULL,
          title TEXT NOT NULL,
          category TEXT DEFAULT '',
          dynasty TEXT DEFAULT '',
          author TEXT DEFAULT '',
          content TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          file_path TEXT,
          created_at TEXT DEFAULT (datetime('now')),
          UNIQUE(source, source_file)
        );
        CREATE TABLE chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          article_id INTEGER NOT NULL,
          chunk_index INTEGER NOT NULL,
          heading TEXT DEFAULT '',
          content TEXT NOT NULL,
          fts_content TEXT DEFAULT '',
          UNIQUE(article_id, chunk_index)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
          content, heading, content='chunks', content_rowid='id', tokenize='unicode61'
        );
        CREATE TRIGGER chunks_fts_insert AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts(rowid, content, heading) VALUES (new.id, new.fts_content, new.heading);
        END;
        """
    )
    con.commit()
    return con


def _add_article(
    con: sqlite3.Connection,
    *,
    source: str,
    title: str,
    dynasty: str = "",
    author: str = "",
    content: str = "",
    heading: str = "",
) -> int:
    cur = con.execute(
        "INSERT INTO articles (source, source_file, title, dynasty, author, content, content_hash) "
        "VALUES (?,?,?,?,?,?,?)",
        (source, title, title, dynasty, author, content, f"hash-{title}"),
    )
    aid = cur.lastrowid
    con.execute(
        "INSERT INTO chunks (article_id, chunk_index, heading, content, fts_content) "
        "VALUES (?,?,?,?,?)",
        (aid, 0, heading, content, _tokenize_cjk(content)),
    )
    con.commit()
    return aid


# ---------------------------------------------------------------- build_fts5_query


def test_build_fts5_query_empty():
    assert build_fts5_query("")["empty"] is True
    assert build_fts5_query("   ")["empty"] is True
    assert build_fts5_query('"*^()')["empty"] is True


def test_build_fts5_query_non_cjk():
    q = build_fts5_query("hello world")
    assert q["trigram_query"] == '"hello" OR "world"'
    assert q["chars"] == []


def test_build_fts5_query_single_char():
    q = build_fts5_query("史")
    assert q["trigram_query"] == '"史"'
    assert q["bigram_query"] is None


def test_build_fts5_query_two_char_bigram_only():
    q = build_fts5_query("项羽")
    assert q["trigram_query"] is None  # 2 字不足以构 trigram
    assert q["bigram_query"] == '"项 羽"'


def test_build_fts5_query_three_char_trigram():
    q = build_fts5_query("鸿门宴")
    assert q["trigram_query"] == '"鸿 门 宴"'
    assert q["bigram_query"] == '"鸿 门" OR "门 宴"'


def test_build_fts5_query_synonym_expansion():
    q = build_fts5_query("变法")
    # 同义词扩展进 trigram（扩展后字符集 ≥5 时只生成 trigram），char_and 用原始查询字符
    assert '"变 法 改"' in q["trigram_query"]
    assert q["char_and_query"] == "变 法"


def test_build_fts5_query_strips_fts_metachars():
    q = build_fts5_query('项羽"OR AND')
    # OR/AND 是 FTS5 布尔关键字，应被清洗掉；2 字查询走 bigram
    assert q["trigram_query"] is None
    assert q["bigram_query"] == '"项 羽"'


# ---------------------------------------------------------------- search_one_db


def test_search_one_db_hits_and_orders_by_source(tmp_path):
    con = _build_db(tmp_path / "d.db")
    # daizhigev20 先插入，chtxt 后插入 —— 结果应 chtxt 排前（sourceOrder）
    _add_article(con, source="daizhigev20", title="项羽本纪（全量）", dynasty="西汉",
                 author="司马迁", content="项籍者，下相人也，字羽。")
    _add_article(con, source="chtxt", title="史记·项羽本纪", dynasty="西汉",
                 author="司马迁", content="项籍者，下相人也，字羽。")

    rows = search_one_db(con, "项羽")
    assert rows, "应命中"
    assert rows[0]["source"] == "chtxt"
    assert rows[0]["title"] == "史记·项羽本纪"
    assert rows[0]["dynasty"] == "西汉" and rows[0]["author"] == "司马迁"
    # 四要素字段齐备
    for k in ("articleId", "chunkId", "title", "dynasty", "author", "source", "snippet"):
        assert k in rows[0]
    con.close()


def test_search_one_db_snippet_truncated(tmp_path):
    con = _build_db(tmp_path / "d.db")
    long_text = "史" * 700
    _add_article(con, source="chtxt", title="史记", content=long_text)
    rows = search_one_db(con, "史" * 3)
    assert rows and len(rows[0]["snippet"]) <= 600
    con.close()


def test_search_one_db_no_hit_returns_empty(tmp_path):
    con = _build_db(tmp_path / "d.db")
    _add_article(con, source="chtxt", title="史记", content="项籍者下相人也")
    assert search_one_db(con, "朱元璋") == []
    con.close()


# ---------------------------------------------------------------- search_across_dbs


def test_search_across_dbs_merges_with_expert_id(tmp_path):
    p1 = tmp_path / "duan-dai-shi.db"
    p2 = tmp_path / "dong-shi-gong.db"
    c1 = _build_db(p1)
    _add_article(c1, source="chtxt", title="史记·项羽本纪", content="项籍者下相人也字羽")
    c1.close()
    c2 = _build_db(p2)
    _add_article(c2, source="daizhigev20", title="论语·学而", content="学而时习之")
    c2.close()

    rows = search_across_dbs([str(p1), str(p2)], "项羽")
    assert rows and rows[0]["expertId"] == "duan-dai-shi"

    # 缺失的库跳过不影响其它库
    rows = search_across_dbs([str(tmp_path / "nope.db"), str(p1)], "项羽")
    assert rows and rows[0]["expertId"] == "duan-dai-shi"


# ---------------------------------------------------------------- list_sources


def test_list_sources_reports_missing_and_available(tmp_path):
    _build_db(tmp_path / "duan-dai-shi.db").close()
    out = list_sources(tmp_path, ["duan-dai-shi", "dong-shi-gong"])
    by_id = {s["expertId"]: s for s in out}
    assert by_id["duan-dai-shi"]["available"] is True
    assert by_id["dong-shi-gong"]["available"] is False


# ---------------------------------------------------------------- tool


def test_tool_returns_results(tmp_path):
    con = _build_db(tmp_path / "duan-dai-shi.db")
    _add_article(con, source="chtxt", title="史记·项羽本纪", dynasty="西汉",
                 author="司马迁", content="项籍者下相人也字羽")
    con.close()

    tool = make_history_search_tool(data_dir=tmp_path, experts=["duan-dai-shi"])
    out = tool(query="项羽")
    assert out["count"] >= 1
    assert out["results"][0]["title"] == "史记·项羽本纪"
    # metadata + schema for the registry
    assert tool.__aisuite_tool_metadata__.category == "web"
    assert tool.__coworker_schema__["function"]["name"] == "search_history"


def test_tool_missing_db_reports_error(tmp_path):
    tool = make_history_search_tool(data_dir=tmp_path, experts=["duan-dai-shi"])
    out = tool(query="项羽")
    assert "error" in out and "没有可用的史料库" in out["error"]
    assert out["sources"][0]["available"] is False


def test_tool_empty_query_reports_error(tmp_path):
    tool = make_history_search_tool(data_dir=tmp_path)
    assert "error" in tool(query="")


def test_tool_clamps_limit(tmp_path):
    con = _build_db(tmp_path / "duan-dai-shi.db")
    _add_article(con, source="chtxt", title="史记", content="项籍者下相人也")
    con.close()
    tool = make_history_search_tool(data_dir=tmp_path, experts=["duan-dai-shi"])
    # limit 超 20 应被钳到 20，不报错
    out = tool(query="项羽", limit=99)
    assert "count" in out


def test_engine_registers_search_history(tmp_path):
    from coworker.agent import build_engine
    from coworker.agents import chat_agent
    from coworker.secrets import SecretStore

    eng = build_engine(
        agent=chat_agent(),
        provider=_StubProvider(),
        secrets=SecretStore(tmp_path / "s.json"),
    )
    assert "search_history" in eng.registry.names()
    assert "web_search" in eng.registry.names()  # 二者并列


class _StubProvider:
    def complete(self, **_kw):
        from coworker.providers import AssistantTurn

        return AssistantTurn()

    def capabilities(self, _model):
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def stream(self, **_kw):
        from coworker.providers.base import StreamChunk

        yield StreamChunk(turn=self.complete())
