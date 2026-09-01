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
from coworker.history.s2t import to_traditional, variants


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
    # 简繁两形各自成短语再 OR：语料简繁混排，只查原形会漏掉繁体原文
    assert q["bigram_query"] == '"项 羽" OR "項 羽"'


def test_build_fts5_query_three_char_trigram():
    q = build_fts5_query("鸿门宴")
    assert q["trigram_query"] == '"鸿 门 宴" OR "鴻 門 宴"'
    assert q["bigram_query"] == '"鸿 门" OR "门 宴" OR "鴻 門" OR "門 宴"'


def test_build_fts5_query_synonym_expansion():
    q = build_fts5_query("变法")
    # 同义词各自独立成短语，OR 进精确档（语义召回）
    assert '"变 法"' in q["exact_query"]
    assert '"改 革"' in q["exact_query"]
    # trigram 只用原始查询字符，不再产出跨词边界的虚假组合
    assert "变 法 改" not in (q["trigram_query"] or "")
    # char_and 是「变体内 AND、变体间 OR」——
    # 若把所有变体的字符混在一起 AND，等于要求同一段里简繁并存，几乎不可能命中
    assert q["char_and_query"] == "(变 AND 法) OR (變 AND 法)"


def test_build_fts5_query_strips_fts_metachars():
    q = build_fts5_query('项羽"OR AND')
    # OR/AND 是 FTS5 布尔关键字，应被清洗掉；2 字查询走 bigram
    assert q["trigram_query"] is None
    assert q["bigram_query"] == '"项 羽" OR "項 羽"'


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


# ---------------------------------------------------------------- Exact 精确档


def test_build_fts5_query_exact_is_full_phrase():
    # Exact 档：整个查询词作为一个连续短语，不被拆成 3-gram
    q = build_fts5_query("王安石变法")
    assert q["exact_query"].startswith('"王 安 石 变 法"')
    # trigram：变体内部滑动窗口，变体之间 OR，并去重
    # （"王 安 石" 简繁同形，只保留一份）
    assert q["trigram_query"] == (
        '"王 安 石" OR "安 石 变" OR "石 变 法" OR "安 石 變" OR "石 變 法"'
    )


def test_build_fts5_query_synonyms_stay_separate_phrases():
    # 同义词独立成短语进精确档，但绝不与原词串接成虚假长词
    # （旧行为把同义词拼在尾部，会生成"变 法 改"/"法 改 革"这类不存在的词）
    q = build_fts5_query("变法")
    assert q["exact_query"].startswith('"变 法"')  # 原词本身仍是第一个短语
    assert '"改 革"' in q["exact_query"]
    assert "变 法 改" not in q["exact_query"]
    assert "法 改 革" not in q["exact_query"]


def test_search_one_db_exact_ranks_above_partial():
    """完整命中必须排在部分命中之前，即使部分命中来自更权威的来源。"""
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    # A：daizhigev20（来源优先级低）但完整含"王安石变法"
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (1,'daizhigev20','a','宋史-精确','','h1')"
    )
    text_a = "熙宁二年，王安石变法始行，青苗免役诸法次第颁于天下。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (1,1,0,?,?)",
        (text_a, _tokenize_cjk(text_a)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (1,?,'')",
        (_tokenize_cjk(text_a),),
    )
    # B：chtxt（来源优先级高）但只有"王安石"，无"变法"连写
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (2,'chtxt','b','元史-部分','','h2')"
    )
    text_b = "元初议政者尝举王安石之法，然未及施行而罢。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (2,2,0,?,?)",
        (text_b, _tokenize_cjk(text_b)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (2,?,'')",
        (_tokenize_cjk(text_b),),
    )
    con.commit()

    rows = search_one_db(con, "王安石变法", limit=5)
    assert len(rows) >= 2
    # 精确命中排第一，且带 exact 标记；部分命中不得抢前
    assert rows[0]["exact"] is True
    assert rows[0]["title"] == "宋史-精确"
    assert rows[1]["exact"] is False
    con.close()


def test_search_one_db_dedups_identical_content():
    """同一段文字被重复灌入不同 chunk 时只返回一条（实测《元史》有此情况）。"""
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    dup = "至正二年正月，中書參議孛羅帖木兒建言開河，議者多以為不可。"
    for i in (1, 2, 3):
        con.execute(
            "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
            f"VALUES ({i},'chtxt','f{i}','元史-{i}','','h{i}')"
        )
        con.execute(
            "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) "
            f"VALUES ({i},{i},0,?,?)",
            (dup, _tokenize_cjk(dup)),
        )
        con.execute(
            "INSERT INTO chunks_fts(rowid, content, heading) VALUES (?,?,'')",
            (i, _tokenize_cjk(dup)),
        )
    con.commit()
    rows = search_one_db(con, "孛羅帖木兒", limit=10)
    assert len(rows) == 1, f"应去重为 1 条，实际 {len(rows)} 条"
    con.close()


def test_search_across_dbs_exact_beats_source_priority(tmp_path):
    """跨库合并时，精确命中优先于来源优先级（chtxt 不再无条件压过 daizhigev20）。"""
    # 库 1：chtxt（优先级高）但只有部分命中
    p1 = tmp_path / "lib-a.db"
    con = sqlite3.connect(str(p1))
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (1,'chtxt','a','元史','','h1')"
    )
    t1 = "至正二年正月議開河事，孛羅帖木兒與焉。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (1,1,0,?,?)",
        (t1, _tokenize_cjk(t1)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (1,?,'')", (_tokenize_cjk(t1),)
    )
    con.commit()
    con.close()

    # 库 2：daizhigev20（优先级低）但完整命中
    p2 = tmp_path / "lib-b.db"
    con = sqlite3.connect(str(p2))
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (1,'daizhigev20','b','宋史','','h2')"
    )
    t2 = "熙宁二年，王安石变法始行。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (1,1,0,?,?)",
        (t2, _tokenize_cjk(t2)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (1,?,'')", (_tokenize_cjk(t2),)
    )
    con.commit()
    con.close()

    rows = search_across_dbs([str(p1), str(p2)], "王安石变法", limit=5)
    # 库 1 只有"王安"级别的部分匹配；精确命中的库 2 必须排最前
    assert rows[0]["exact"] is True
    assert rows[0]["expertId"] == "lib-b"


def test_search_across_dbs_coverage_beats_source_priority(tmp_path):
    """同档位内，匹配查询词更全的条目应排在只匹配一半的之前。

    实测问题：查"嘉靖倭寇"时，只含"嘉靖"的《舊唐書》(chtxt) 会压过同时含
    "嘉靖""倭寇"的《明史》(daizhigev20)，因为来源优先级排在了相关性前面。
    """
    p = tmp_path / "lib.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    # A：来源优先级高(chtxt)，但只命中"嘉靖"一半
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (1,'chtxt','a','舊唐書','','h1')"
    )
    t1 = "嘉靖年間校勘二十一史，詮謬司文學，徧歷輔畿。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (1,1,0,?,?)",
        (t1, _tokenize_cjk(t1)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (1,?,'')", (_tokenize_cjk(t1),)
    )
    # B：来源优先级低(daizhigev20)，但"嘉靖""倭寇"都命中
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, content, content_hash) "
        "VALUES (2,'daizhigev20','b','明史','','h2')"
    )
    t2 = "嘉靖三十四年，倭寇犯浙直，官兵屡败。"
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (2,2,0,?,?)",
        (t2, _tokenize_cjk(t2)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (2,?,'')", (_tokenize_cjk(t2),)
    )
    con.commit()
    con.close()

    rows = search_across_dbs([str(p)], "嘉靖倭寇", limit=5)
    # 都不是连续四字词，走降级档；匹配更全的《明史》必须排最前
    assert rows[0]["title"] == "明史"
    assert rows[0]["sourceOrder"] > rows[-1]["sourceOrder"]  # 来源优先级确实更低


# ---------------------------------------------------------------- 简繁变体
#
# 语料是简繁混排的：正史原文（《史記》《舊唐書》）多为繁体，现代整理本多为简体。
# FTS5 按单字建索引，「项羽」与「項羽」是两组完全不同的 token，只查原形会漏掉
# 另一种字形的全部史料（实测史部子集漏召近四成）。


def _mem_db() -> sqlite3.Connection:
    """正常模式（非 external-content）的内存史料库，与生产库结构一致。"""
    con = sqlite3.connect(":memory:")
    con.executescript(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT, source_file TEXT, "
        "title TEXT, category TEXT, dynasty TEXT, author TEXT, content TEXT, "
        "content_hash TEXT, file_path TEXT);"
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, article_id INTEGER, chunk_index INTEGER, "
        "heading TEXT, content TEXT, fts_content TEXT);"
        "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, heading, tokenize='unicode61');"
    )
    return con


def _add_mem_chunk(
    con: sqlite3.Connection,
    cid: int,
    *,
    source: str,
    title: str,
    text: str,
    dynasty: str = "",
    author: str = "",
) -> None:
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, dynasty, author, content, "
        "content_hash) VALUES (?,?,?,?,?,?,?,?)",
        (cid, source, f"f{cid}", title, dynasty, author, text, f"h{cid}"),
    )
    con.execute(
        "INSERT INTO chunks (id, article_id, chunk_index, content, fts_content) VALUES (?,?,0,?,?)",
        (cid, cid, text, _tokenize_cjk(text)),
    )
    con.execute(
        "INSERT INTO chunks_fts(rowid, content, heading) VALUES (?,?,'')",
        (cid, _tokenize_cjk(text)),
    )
    con.commit()


def test_variants_covers_both_scripts():
    # 简体查询要能覆盖繁体原文，繁体查询同样要能覆盖简体整理本
    assert variants("项羽") == ["项羽", "項羽"]
    assert variants("項羽") == ["項羽", "项羽"]


def test_variants_uses_phrase_dict_for_ambiguous_chars():
    """一简对多繁的字必须靠词组字典定夺，按字转换会铸成大错。

    「历」对应「歷」（经历）与「曆」（历法/年号）两个字。年号「万历」若按字
    转换会变成「萬歷」，而史书里写作「萬曆」——差一个字就一条都查不到。
    """
    assert variants("万历") == ["万历", "萬曆"]
    assert variants("历法") == ["历法", "曆法"]
    # 「谷」在「山谷」里不简化，因此不该产出「山穀」这种变体
    assert variants("山谷") == ["山谷"]
    # 简繁同形的词没有变体
    assert variants("史") == ["史"]
    assert variants("秦始皇") == ["秦始皇"]


def test_variants_never_concatenated_across_scripts():
    """变体之间绝不串接 —— 串接会造出语料里根本不存在的词。"""
    q = build_fts5_query("项羽")
    for expr in (q["exact_query"], q["bigram_query"], q["char_and_query"]):
        assert "项 羽 項" not in (expr or "")
        assert "項 羽 项" not in (expr or "")


def test_search_one_db_traditional_text_found_by_simplified_query():
    """简体查询必须能命中繁体原文，并正确标记为精确命中。"""
    con = _mem_db()
    _add_mem_chunk(con, 1, source="chtxt", title="史記·項羽本紀", dynasty="西漢",
                   author="司馬遷", text="項羽者，下相人也，字羽。力能扛鼎，才氣過人。")
    rows = search_one_db(con, "项羽", limit=5)
    con.close()
    assert rows, "简体查询『项羽』应能命中繁体原文『項羽』"
    assert rows[0]["exact"] is True
    assert rows[0]["matchedForm"] == "項羽"  # 标明是以另一种字形命中
    assert rows[0]["title"] == "史記·項羽本紀"


def test_search_across_dbs_traditional_not_demoted_by_script():
    """繁体条目不得因字形与查询词不同而一律排在简体条目之后。

    若 exact 只按查询词原形判定，繁体史料的 exact 恒为 False，会被无条件
    压到所有简体命中之下 —— 变体扩展就白做了。
    """
    con = _mem_db()
    # 繁体、来源更权威（chtxt）
    _add_mem_chunk(con, 1, source="chtxt", title="史記·項羽本紀",
                   text="項羽引兵西屠咸陽，殺秦降王子嬰，燒秦宮室，火三月不滅。")
    # 简体、来源优先级低（daizhigev20）
    _add_mem_chunk(con, 2, source="daizhigev20", title="史记·项羽本纪（今译）",
                   text="项羽领兵西进屠咸阳，杀秦降王子婴，烧秦宫室，大火三月不灭。")
    rows = search_one_db(con, "项羽", limit=5)
    con.close()
    assert len(rows) == 2, f"简繁两条都应检索到，实际 {len(rows)} 条"
    assert all(r["exact"] for r in rows), "两条都是完整命中，不应有降级"
    # 来源更权威的繁体条目排第一
    assert rows[0]["source"] == "chtxt"
    assert rows[0]["matchedForm"] == "項羽"


def test_variants_disabled_by_env(monkeypatch):
    """``HISTORY_SCRIPT_VARIANTS=0`` 可关闭变体扩展（A/B 对比召回率时用）。"""
    monkeypatch.setenv("HISTORY_SCRIPT_VARIANTS", "0")
    assert variants("项羽") == ["项羽"]
    q = build_fts5_query("项羽")
    assert q["bigram_query"] == '"项 羽"'


def test_conversion_failure_degrades_gracefully(monkeypatch):
    """字典缺失/转换报错时退化为「只有原形」，绝不抛异常、绝不中断检索。"""
    import coworker.history.s2t as s2t

    monkeypatch.setattr(s2t, "converters", lambda: (None, None))
    assert s2t.variants("项羽") == ["项羽"]
    assert s2t.to_traditional("项羽") == "项羽"


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
