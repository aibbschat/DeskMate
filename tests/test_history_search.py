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
from coworker.history.fts5 import _era_of, _facet_merge, _source_order
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
    # 原词进 exact 档；**同义词不再混入 exact**，只在独立的 synonym 档
    assert q["exact_query"] == '"变 法" OR "變 法"'
    assert '"改 革"' in q["synonym_query"]
    assert '"改 革"' not in q["exact_query"]
    # trigram 只用原始查询字符，不再产出跨词边界的虚假组合
    assert "变 法 改" not in (q["trigram_query"] or "")
    # char_and 是「变体内 AND、变体间 OR」——
    # 若把所有变体的字符混在一起 AND，等于要求同一段里简繁并存，几乎不可能命中
    assert q["char_and_query"] == "(变 AND 法) OR (變 AND 法)"


def test_synonyms_never_contaminate_exact_tier():
    """同义词绝不进 exact 档——这是最危险的一类误命中。

    ``_synonyms_for`` 是**按子串匹配**的：查「王安石变法」会命中「变法」的同义词表，
    扩展出 改革/新政/改制/维新。这些二字词在古籍里遍地都是，混进 exact 档后
    《隋書》讲礼服制度、《舊唐書》讲地名沿革的段落全被召回，还被标成 ``exact``
    （"完整命中，可直接引用"）。结果看着权威，实则与查询毫无关系。
    """
    q = build_fts5_query("王安石变法")
    assert q["exact_query"] == '"王 安 石 变 法" OR "王 安 石 變 法"'
    for syn in ("改 革", "新 政", "改 制", "维 新"):
        assert syn not in q["exact_query"]
        assert syn in q["synonym_query"]


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
    # 同义词独立成短语进 synonym 档，绝不与原词串接成虚假长词
    # （旧行为把同义词拼在尾部，会生成"变 法 改"/"法 改 革"这类不存在的词）
    q = build_fts5_query("变法")
    assert q["exact_query"] == '"变 法" OR "變 法"'
    assert q["synonym_query"].startswith('"改 革"')  # 同义词各自独立
    assert "变 法 改" not in (q["synonym_query"] or "")
    assert "法 改 革" not in (q["synonym_query"] or "")


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
    category: str = "",
) -> None:
    con.execute(
        "INSERT INTO articles (id, source, source_file, title, category, dynasty, "
        "author, content, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, source, f"f{cid}", title, category, dynasty, author, text, f"h{cid}"),
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


# ---------------------------------------------------------------------------
# 史料分层（era）与分面交错排序
#
# 这些用例锁住一条容易被"优化"掉的设计：排序**不是**把原典恒顶在前的线性
# 权威链。官修正史有立场，若让原典占满前排，使用者就看不见后世考异，
# 交叉比对无从谈起。因此原典与研究著作按 2:1 交错，演义一律沉底。
# ---------------------------------------------------------------------------


def test_era_of_by_category():
    assert _era_of(category="正史") == "primary"
    assert _era_of(category="编年") == "primary"
    assert _era_of(category="政书") == "primary"
    assert _era_of(category="j.史書") == "primary"
    assert _era_of(category="研究著作") == "scholar"
    assert _era_of(category="文评") == "context"
    assert _era_of(category="文总集") == "context"


def test_era_of_fiction_wins_over_broader_rules():
    """「演义」必须最先匹配，否则会被更宽泛的规则误判成旁证。"""
    assert _era_of(category="演义") == "fiction"
    assert _era_of(category="隋唐演义") == "fiction"
    # 书名里带"演义"二字也要认出来（category 缺失时靠书名兜底）
    assert _era_of(category="", title="东西汉演义") == "fiction"


def test_era_of_scholar_fallback_by_author():
    """无 category 但有明确作者 → 判为研究专著（古籍通常缺 author 字段）。"""
    assert _era_of(category="", title="明朝的裂变", author="萧盛") == "scholar"


def test_era_of_unknown_falls_back_to_context():
    assert _era_of(category="", title="某书") == "context"


def _rows_with_eras(
    spec: list[tuple[str, str]],
    tier: str = "exact",
) -> list[dict]:
    """按 (era, 书名) 造一批假结果行，供分面合并测试使用。

    默认带 ``matchTier``：真实返回体总有这个字段，缺了会被入场门槛判成弱命中
    而排除出交错，测出来的就是假象。
    """
    return [
        {
            "era": era,
            "title": title,
            "snippet": f"{title}片段{i}",
            "matchTier": tier,
            "exact": tier == "exact",
        }
        for i, (era, title) in enumerate(spec)
    ]


def test_facet_merge_interleaves_primary_and_scholar():
    """原典再多，研究著作也必须在前排出现——否则等于看不见异见。"""
    rows = _rows_with_eras(
        [("primary", f"原典{i}") for i in range(6)]
        + [("scholar", f"专著{i}") for i in range(3)]
    )
    out = _facet_merge(rows, limit=6)
    eras = [r["era"] for r in out]
    # 2:1 交错：前三条里应含 2 条原典 + 1 条专著
    assert eras[:3] == ["primary", "primary", "scholar"]
    assert eras.count("scholar") == 2


def test_facet_merge_pushes_fiction_to_the_bottom():
    """演义只能占剩余名额，绝不能挤掉任何一条真史料。"""
    rows = _rows_with_eras(
        [("primary", f"原典{i}") for i in range(4)]
        + [("scholar", "专著0")]
        + [("fiction", f"演义{i}") for i in range(5)]
    )
    out = _facet_merge(rows, limit=8)
    titles = [r["title"] for r in out]
    # 演义排在所有原典/专著之后
    first_fiction = min(i for i, t in enumerate(titles) if t.startswith("演义"))
    last_real = max(i for i, t in enumerate(titles) if not t.startswith("演义"))
    assert first_fiction > last_real


def test_facet_merge_degrades_when_only_one_era_present():
    """只有古籍（尚未灌入研究著作）时，行为退化成普通相关度排序。"""
    rows = _rows_with_eras([("primary", f"原典{i}") for i in range(5)])
    out = _facet_merge(rows, limit=3)
    assert [r["title"] for r in out] == ["原典0", "原典1", "原典2"]


def test_facet_merge_respects_limit():
    rows = _rows_with_eras(
        [("primary", f"原典{i}") for i in range(10)]
        + [("scholar", f"专著{i}") for i in range(10)]
    )
    assert len(_facet_merge(rows, limit=7)) == 7


def test_search_one_db_tags_era():
    """era 必须出现在返回体里，模型才知道这条能不能当史料引。"""
    con = _mem_db()
    _add_mem_chunk(con, 1, source="chtxt", title="明史", category="正史",
                   text="胡惟庸自杨宪被诛后，总中书之政。")
    _add_mem_chunk(con, 2, source="scholar", title="明朝的裂变", category="研究著作",
                   author="萧盛", dynasty="当代", text="胡惟庸案是皇权与相权的总清算。")
    rows = search_one_db(con, "胡惟庸", limit=5)
    by_title = {r["title"]: r for r in rows}
    assert by_title["明史"]["era"] == "primary"
    assert by_title["明史"]["eraLabel"] == "原典"
    assert by_title["明朝的裂变"]["era"] == "scholar"
    assert by_title["明朝的裂变"]["eraLabel"] == "研究著作"


def test_source_order_gives_scholar_same_rank_as_daizhigev20():
    """研究著作属可直引的学术著作，不该落到「其他」那一档。"""
    assert _source_order("chtxt") == 0
    assert _source_order("scholar") == _source_order("daizhigev20")
    assert _source_order("scholar") < _source_order("unknown-source")


def test_facet_merge_weak_scholar_yields_to_primary():
    """弱命中（char_and 档）的研究著作不进前排，避免顺带提及挤掉高度相关的原典。

    实测教训：查「诸葛亮」时，明代生活专著里一句顺带提及，按固定 2:1 硬挤进
    第 3 位，把《三國志》顶掉了。交叉比对的前提是两边都言之有物，不是硬凑名额。
    """
    rows = [
        {"era": "primary", "title": f"原典{i}", "matchTier": "exact", "snippet": ""}
        for i in range(4)
    ] + [
        {"era": "scholar", "title": "专著弱", "matchTier": "char_and",
         "exact": False, "snippet": ""}
    ]
    out = _facet_merge(rows, limit=4)
    assert "专著弱" not in [r["title"] for r in out]


def test_facet_merge_strong_scholar_still_interleaves():
    """命中到 bigram 及以上的研究著作照常参与交错。"""
    rows = [
        {"era": "primary", "title": f"原典{i}", "matchTier": "exact", "snippet": ""}
        for i in range(3)
    ] + [
        {"era": "scholar", "title": "专著强", "matchTier": "bigram",
         "exact": False, "snippet": ""}
    ]
    out = _facet_merge(rows, limit=4)
    titles = [r["title"] for r in out]
    assert titles.index("专著强") <= 3


def test_facet_merge_exact_scholar_interleaves_even_at_low_tier():
    """完整命中查询词的，即使档位低也该进前排（档位是降级路径，不代表不相关）。"""
    rows = [
        {"era": "primary", "title": f"原典{i}", "matchTier": "exact", "snippet": ""}
        for i in range(3)
    ] + [
        {"era": "scholar", "title": "专著准", "matchTier": "char_and",
         "exact": True, "snippet": ""}
    ]
    out = _facet_merge(rows, limit=4)
    assert "专著准" in [r["title"] for r in out]


def test_long_query_still_generates_bigram():
    """5 字以上查询也要生成 bigram。

    原先限制「>=5 字不生成 bigram」，理由是怕宽泛；实测恰恰相反——长查询跳过
    bigram 后直接掉到单字 AND，区分度断崖式下跌（查「郑和下西洋」会命中含
    「南郑」「西」「和」的《文献通考》段落）。bigram 远比单字 AND 精确。
    """
    q = build_fts5_query("郑和下西洋")
    assert q["bigram_query"] is not None
    assert '"郑 和"' in q["bigram_query"]
    assert '"西 洋"' in q["bigram_query"]
    # trigram 仍在，作为比 bigram 更精确的一档
    assert '"下 西 洋"' in q["trigram_query"]


def test_weak_bigram_matches_sink_to_the_bottom():
    """只命中一两个 bigram 的结果要沉底，它们是字面巧合而非史实相关。"""
    rows = [
        {"era": "primary", "title": "真相关", "matchTier": "exact",
         "snippet": "郑和下西洋的宝船", "exact": True},
        {"era": "primary", "title": "字面巧合", "matchTier": "bigram",
         "snippet": "南下西洋诸国", "exact": False},
    ]
    out = _facet_merge(rows, limit=2)
    assert out[0]["title"] == "真相关"


def test_weak_primary_sinks_below_strong_scholar():
    """弱命中的**原典**必须排在强命中的**研究著作**之后。

    这是真实踩过的坑：查「一条鞭法」时《隋書》《魏書》靠「条鞭」这类常见二字
    组合被召回，它们与原典强命中同属 primary 组。若只按 era 分批交错，这些
    八竿子打不着的段落会占掉前排半壁江山，把萧盛《明朝的裂变》里真正讨论
    一条鞭法的段落挤到看不见的地方。强/弱必须先于 era 二分。
    """
    rows = [
        {"era": "primary", "title": "隋書", "matchTier": "bigram", "exact": False,
         "snippet": "条鞭之刑", "_weak": True},
        {"era": "scholar", "title": "明朝的裂变", "matchTier": "exact", "exact": True,
         "snippet": "一条鞭法将赋役合并"},
        {"era": "primary", "title": "明史", "matchTier": "exact", "exact": True,
         "snippet": "一条鞭法者"},
    ]
    out = _facet_merge(rows, limit=3)
    assert [r["title"] for r in out] == ["明史", "明朝的裂变", "隋書"]


def test_synonym_tier_sinks_to_the_bottom():
    """synonym 档无条件算弱命中，即使它的覆盖度数值看起来不低。

    覆盖度衡量的是"查询词的 bigram 在片段里出现了几个"，而 synonym 档匹配的
    根本不是查询词（查「王安石变法」命中「新政」），覆盖度对它无意义。
    不显式判弱，它就可能混进前排冒充精确命中。
    """
    rows = [
        # 生产里 synonym 行由 search_across_dbs 打上 _weak 再交给 _facet_merge
        {"era": "primary", "title": "講新政的隋書", "matchTier": "synonym",
         "exact": False, "snippet": "頒新政以變法制", "_weak": True},
        {"era": "primary", "title": "續資治通鑑", "matchTier": "exact",
         "exact": True, "snippet": "公弼以王安石變法，數勸其務安靜"},
    ]
    out = _facet_merge(rows, limit=2)
    assert out[0]["title"] == "續資治通鑑"


def test_facet_merge_strips_internal_marks():
    """``_weak`` 只是内部分档标记，不能漏进给模型看的工具输出。"""
    rows = [
        {"era": "primary", "title": "明史", "matchTier": "bigram", "exact": False,
         "snippet": "条鞭", "_weak": True},
    ]
    out = _facet_merge(rows, limit=1)
    assert "_weak" not in out[0]
    assert "_weak" not in rows[0]
