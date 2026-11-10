"""记忆事实纪律回归（2026-11-10 萧哥终裁：已写正文事实是唯一事实基准）。

现场：跟小桔对《夜不收》情节人物时越纠越乱——六条互斥裁定同时 active，
凭空专名（弘敬业/韩效武/1518）照样进上下文。根因四层：

1. 写入层什么都算事实：learned_history 没有来源/效力/书名维度；
2. 冲突层只认「《书名》+ 反极性」，纠正类新条不取代旧错条 ⇒ 越纠越乱；
3. 注入层用「不是她猜的；优先据此作答」的权威口吻整块灌，且 SQL 无 ORDER BY
   ⇒ 取的是最老的 10 条；
4. 全局没有一句「记忆与正文对不上时须明说、以正文为准」。

本测试先锁住目标行为，再改实现（先红后绿）。所有用例只用 tmp_path，不碰真实库。
"""
from __future__ import annotations

import sqlite3

import pytest

from deskmate.memory import learned_store as ls
from deskmate.memory import user_fact_context as uf
from deskmate.memory.learned_store import LearnedStore


@pytest.fixture()
def store(tmp_path) -> LearnedStore:
    return LearnedStore(base_dir=tmp_path)


@pytest.fixture()
def canon_book(tmp_path) -> str:
    """P3 正文效力闸门：标 manuscript 必须给出命中本地正文的逐字引文。

    本组用例锁的是「取代规则」，引文与 fact 的相关性由
    ``tests/test_memory_canon_evidence.py`` 单独锁；这里只造最小 FTS5 档案，
    让正文效力在新契约下依然成立，不改变用例原本锁的价值。
    """
    folder = tmp_path / "16.夜不收"
    (folder / ".deskmate").mkdir(parents=True)
    con = sqlite3.connect(str(folder / ".deskmate" / "fts5.db"))
    try:
        con.execute(
            "CREATE VIRTUAL TABLE novels USING fts5("
            "file_path, title, content, tokenize='unicode61')"
        )
        con.execute(
            "INSERT INTO novels(file_path, title, content) VALUES (?, ?, ?)",
            (
                "夜不收：绝命救援.docx",
                "夜不收：绝命救援",
                "夜不收：绝命救援\n第十一章\n粮仓没有烧成，孔大贵炸开仓门后战死\n小队共十五人",
            ),
        )
        con.commit()
    finally:
        con.close()
    return str(folder)


def _statuses(store: LearnedStore) -> dict[str, str]:
    con = sqlite3.connect(store.db_path)
    try:
        rows = con.execute("SELECT fact, status FROM learned_history").fetchall()
    finally:
        con.close()
    return {fact: status for fact, status in rows}


# ---------------------------------------------------------------------------
# 1. 来源 / 效力层级：复用 source 列，零 schema 变更（同步白名单安全）
# ---------------------------------------------------------------------------

def test_source_helpers_exist_and_classify():
    for name in ("SOURCE_MANUSCRIPT", "SOURCE_AUTHOR_RULING", "SOURCE_CORRECTION",
                 "SOURCE_REASONING", "SOURCE_SETTING", "SOURCE_ROUNDTABLE",
                 "SOURCE_PROGRESS", "SOURCE_EXTERNAL", "NON_FACT_SOURCES",
                 "source_prefix", "source_book", "make_source",
                 "source_authority", "is_canon_source", "is_non_fact_source"):
        assert hasattr(ls, name), f"learned_store 缺少 {name}"

    assert ls.source_prefix("manuscript:夜不收") == "manuscript"
    assert ls.source_book("manuscript:夜不收") == "夜不收"
    assert ls.source_book("live") == ""
    assert ls.make_source("setting", "夜不收") == "setting:夜不收"
    assert ls.is_canon_source("manuscript:夜不收") is True
    assert ls.is_canon_source("setting:夜不收") is False
    assert ls.is_non_fact_source("reasoning:夜不收") is True
    assert ls.is_non_fact_source("roundtable") is True
    assert ls.is_non_fact_source("live") is False
    assert ls.is_non_fact_source("manuscript") is False
    assert ls.source_authority("manuscript") > ls.source_authority("author_ruling")
    assert ls.source_authority("author_ruling") > ls.source_authority("setting")
    assert ls.source_authority("setting") > ls.source_authority("roundtable")


def test_infer_source_falls_back_conservatively():
    """抽取器漏给 source（本地规则抽取/旧版 LLM 输出）时按类别与措辞降权。"""
    assert ls.infer_source("manuscript:夜不收", "work", "…") == "manuscript:夜不收"
    assert ls.infer_source("not-a-source", "preference", "…") == ls.SOURCE_USER
    assert ls.infer_source(None, "preference", "萧哥喜欢…") == ls.SOURCE_USER
    assert ls.infer_source(None, "correction", "不是X，是Y") == ls.SOURCE_CORRECTION
    assert ls.infer_source(None, "work", "萧盛设定龙山仓三面丘岗") == ls.SOURCE_SETTING
    assert ls.infer_source(None, "work", "萧盛完成第十章定稿") == ls.SOURCE_PROGRESS
    assert ls.infer_source(None, "work", "萧盛提到一件事") == ls.SOURCE_USER
    # 兜底结果必须能被效力分层识别
    assert ls.is_non_fact_source(ls.infer_source(None, "work", "萧盛设定…"))


# ---------------------------------------------------------------------------
# 2. 写入层：正文取代非正文、非正文不能推翻正文、纠正取代旧条
# ---------------------------------------------------------------------------

def test_manuscript_supersedes_non_fact_on_same_entity(store, canon_book):
    """正文事实写入时，同锚点的设定/推理/大纲旧条必须让位（治「什么都算事实」）。"""
    store.append(["《夜不收》第十一章粮仓被烧毁"], source="setting:夜不收")
    store.append(["《夜不收》第十一章粮仓未烧成，孔大贵炸开仓门后战死"],
                 source="manuscript:夜不收",
                 canon_quote="粮仓没有烧成", canon_folder=canon_book)
    st = _statuses(store)
    assert st["《夜不收》第十一章粮仓被烧毁"] == "superseded"
    assert st["《夜不收》第十一章粮仓未烧成，孔大贵炸开仓门后战死"] == "active"


def test_non_fact_cannot_supersede_manuscript(store, canon_book):
    """反极性的设定/推理不得推翻已写正文事实（原有取代逻辑的危险面）。"""
    # 「没有」命中 _DENY_RE ⇒ 两条 polarity 相反，旧实现会据此取代正文条
    canon = "《夜不收》粮仓没有烧成"
    store.append([canon], source="manuscript:夜不收",
                 canon_quote="粮仓没有烧成", canon_folder=canon_book)
    store.append(["《夜不收》粮仓烧成了"], source="setting:夜不收")
    st = _statuses(store)
    assert st[canon] == "active", "正文事实被设定推翻了"
    assert st["《夜不收》粮仓烧成了"] == "active"  # 新条保留但只是参考层线索


def test_correction_supersedes_previous_active_on_same_entity(store):
    """治「越纠越乱」：纠正类新条取代同锚点的旧错条，不再两条 active 并存。"""
    old = "《夜不收》东旗总旗主是韩效武"
    store.append([old], source="live")
    store.append(["《夜不收》东旗旗主是柳平原，韩效武是《国子监秘闻录》的人物"],
                 source="correction:夜不收")
    st = _statuses(store)
    assert st[old] == "superseded"
    active_old = [f for f, s in st.items() if s == "active" and "韩效武" in f]
    assert active_old == ["《夜不收》东旗旗主是柳平原，韩效武是《国子监秘闻录》的人物"]


def test_explicit_supersedes_by_text(store, canon_book):
    """调用方显式给出被取代文本时按 fp 精确取代（不误伤同书其它条）。"""
    old = "《夜不收》小队共十四人"
    keep = "《夜不收》作者是萧盛"
    store.append([old, keep], source="live")
    store.append(["《夜不收》小队共十五人"], source="manuscript:夜不收",
                 supersedes=[old],
                 canon_quote="小队共十五人", canon_folder=canon_book)
    st = _statuses(store)
    assert st[old] == "superseded"
    assert st[keep] == "active"


def test_plain_live_append_keeps_legacy_behaviour(store):
    """不传任何新参数时，行为与历史一致（既有调用点零改动可跑）。"""
    store.append(["《宋会要辑稿》在史料库"])
    n = store.append(["《宋会要辑稿》不在史料库"])
    st = _statuses(store)
    assert n == 1
    assert st["《宋会要辑稿》在史料库"] == "superseded"
    assert st["《宋会要辑稿》不在史料库"] == "active"


# ---------------------------------------------------------------------------
# 3. 注入层：去权威口吻、正文基准声明、来源分层、排序、字数上限
# ---------------------------------------------------------------------------

def test_injection_drops_authoritative_wording_and_states_canon_rule(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["萧哥写历史小说，已出版《明朝的裂变》"], source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="明朝的裂变")
    assert "你跟我说过的" in ctx          # 保留既有块头，兼容老断言
    assert "不是她猜的" not in ctx
    assert "优先据此作答" not in ctx
    assert "以正文为准" in ctx
    assert "对不上" in ctx


def test_non_fact_sources_go_to_reference_block_not_primary(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append(["《夜不收》第十一章粮仓未烧成"], source="manuscript:夜不收")
    store.append(["《夜不收》龙山仓地理设定：三面丘岗、仓门即江岸"],
                 source="setting:夜不收")
    ctx = uf.build_user_fact_context(tmp_path, query="夜不收 粮仓")
    primary = ctx.split("## 参考层")[0]
    assert "粮仓未烧成" in primary
    assert "地理设定" not in primary
    assert "参考层" in ctx
    assert "不得当作已写事实" in ctx
    assert "地理设定" in ctx.split("## 参考层")[1]


def test_no_query_returns_newest_not_oldest(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    for i in range(15):
        store.append([f"萧哥偏好记录第{i:02d}条"], source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="", limit=3)
    assert "第14条" in ctx
    assert "第13条" in ctx
    assert "第00条" not in ctx


def test_context_respects_char_cap(tmp_path):
    store = LearnedStore(base_dir=tmp_path)
    store.append([f"萧哥偏好记录第{i:03d}条 " + "长" * 300 for i in range(60)],
                 source="live")
    ctx = uf.build_user_fact_context(tmp_path, query="", limit=60)
    assert len(ctx) <= uf._FACT_CONTEXT_MAX_CHARS
