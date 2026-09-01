"""百家学院史料 FTS5 检索（Python 移植，零外部依赖，只用内置 ``sqlite3``）。

移植自 ``ai-experts-platform/backend/shared/fts5_search.js`` 与
``ai-forum-v3/mcp/baijia/src/fts5-search.js``，只保留历史考据官需要的子集。

三级降级（与生产一致）：
  1. Trigram OR —— 3 字滑动窗口精确短语，最高精度
  2. Bigram OR  —— 2 字滑动窗口，短查询兜底
  3. Char AND  —— 前 5 个 CJK 字符 AND，召回兜底
结果按 sourceOrder（chtxt > daizhigev20 > 其他）+ FTS5 rank 排序。

索引约定（由灌库侧保证，检索侧不重建）：``chunks.fts_content`` 是字间插空格的
预处理文本（``tokenizeCJK``），FTS5 用 ``tokenize='unicode61'`` 按空格切成单字，
因此 MATCH 短语写成 ``"字1 字2 字3"``。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

# CJK 统一码范围：常用中日韩统一表意文字 + 扩展A + 兼容表意文字
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

# 简单历史领域同义词（只保留与历史考据相关的，避免体积膨胀）
_SYNONYMS = {
    "皇帝": ["皇上", "天子", "陛下", "帝王"],
    "官员": ["大臣", "官吏", "朝臣", "百官"],
    "战争": ["战役", "打仗", "用兵", "征伐"],
    "制度": ["体制", "规制", "典制", "法度"],
    "经济": ["财政", "赋税", "钱粮", "食货"],
    "科举": ["察举", "选官", "九品", "进士"],
    "丞相": ["宰相", "首辅", "阁臣", "相国"],
    "宦官": ["太监", "内侍", "中官", "寺人"],
    "赋税": ["租庸调", "两税法", "一条鞭", "摊丁入亩"],
    "府兵": ["募兵", "卫所", "征兵", "世兵"],
    "变法": ["改革", "新政", "改制", "维新"],
}

# 来源优先级：精校古籍(chtxt) > 全量古籍(daizhigev20) > 其他
_SOURCE_ORDER_SQL = (
    "CASE WHEN a.source = 'chtxt' THEN 0 "
    "WHEN a.source = 'daizhigev20' THEN 1 ELSE 2 END"
)

# SELECT 列（只取 DDL 确认存在的字段）。注意：articles 表没有 url 字段，
# 用 file_path 兜底（mcp/baijia 的旧 SQL 引用了不存在的 a.url，这里已修正）。
_COLUMNS = (
    "c.id AS chunk_id, c.content AS chunk_content, c.heading AS heading, "
    "a.id AS article_id, a.title AS title, a.author AS author, "
    "a.dynasty AS dynasty, a.source AS source, a.category AS category, "
    "a.file_path AS url, a.source_file AS source_file"
)


def _expand_query(query: str) -> str:
    expanded = query
    for key, syns in _SYNONYMS.items():
        if key in query:
            for syn in syns:
                if syn not in query:
                    expanded += " " + syn
    return expanded


def _cjk_chars(text: str) -> list[str]:
    return [c for c in text if _CJK_RE.match(c)]


def _strip_fts_metachars(text: str) -> str:
    """去 FTS5 语法特殊字符 + 布尔关键字，防查询注入。"""
    cleaned = re.sub(r'["*^()]', " ", text)
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def build_fts5_query(query: str) -> dict[str, Any]:
    """构建 FTS5 MATCH 表达式。

    返回 ``{"trigram_query", "bigram_query", "char_and_query", "chars"}``；
    空查询返回 ``{"empty": True}``；非 CJK 查询三个查询都返回带引号词的 OR 串。
    """
    expanded = _expand_query(query)
    cleaned = _strip_fts_metachars(expanded)
    if not cleaned:
        return {"empty": True}

    if not _CJK_RE.search(cleaned):
        # 英文/数字：按词加引号 OR
        as_str = " OR ".join(f'"{w}"' for w in cleaned.split())
        return {
            "trigram_query": as_str,
            "bigram_query": None,
            "char_and_query": as_str,
            "chars": [],
        }

    chars = _cjk_chars(cleaned)
    if not chars:
        return {"empty": True}
    if len(chars) == 1:
        return {
            "trigram_query": f'"{chars[0]}"',
            "bigram_query": None,
            "char_and_query": f'"{chars[0]}"',
            "chars": chars,
        }

    trigrams = [
        f'"{chars[i]} {chars[i + 1]} {chars[i + 2]}"' for i in range(len(chars) - 2)
    ]
    trigram_query = " OR ".join(trigrams) if trigrams else None

    bigram_query = None
    if 2 <= len(chars) < 5:
        bigrams = [f'"{chars[i]} {chars[i + 1]}"' for i in range(len(chars) - 1)]
        bigram_query = " OR ".join(bigrams) if bigrams else None

    # Char AND 用原始查询字符（不做同义词扩展），限前 5 个 CJK
    orig = _strip_fts_metachars(query)
    orig_chars = _cjk_chars(orig)
    and_chars = (orig_chars if orig_chars else chars)[:5]
    char_and_query = " ".join(and_chars) if len(and_chars) >= 2 else " ".join(chars[:5])

    return {
        "trigram_query": trigram_query,
        "bigram_query": bigram_query,
        "char_and_query": char_and_query,
        "chars": chars,
    }


def _source_order(source: Optional[str]) -> int:
    return 0 if source == "chtxt" else 1 if source == "daizhigev20" else 2


def search_one_db(
    con: sqlite3.Connection,
    query: str,
    *,
    limit: int = 8,
    fetch_limit: int = 50,
) -> list[dict[str, Any]]:
    """在单个已打开的史料库上检索。

    返回结果数组（含 article/chunk 元数据 + snippet + sourceOrder）。
    三级降级：trigram → bigram → char AND，某级 FTS 语法不兼容时静默降级。
    """
    q = build_fts5_query(query)
    if q.get("empty"):
        return []

    rows: list[tuple] = []
    for tier in ("trigram_query", "bigram_query", "char_and_query"):
        if rows:
            break
        match_expr = q.get(tier)
        if not match_expr:
            continue
        try:
            rows = con.execute(
                f"SELECT {_COLUMNS}, rank FROM chunks_fts f "
                "JOIN chunks c ON c.id = f.rowid "
                "JOIN articles a ON a.id = c.article_id "
                f"WHERE chunks_fts MATCH ? "
                f"ORDER BY {_SOURCE_ORDER_SQL}, rank LIMIT ?",
                (match_expr, fetch_limit),
            ).fetchall()
        except sqlite3.Error:
            rows = []

    if not rows:
        return []

    # 每篇文章只取 rank 最高的 chunk；不足 limit 再补其他 chunk
    selected: list[tuple] = []
    seen_articles: set[Any] = set()
    for r in rows:
        if len(selected) >= limit:
            break
        if r[3] not in seen_articles:  # article_id 在 _COLUMNS 中第 4 列
            selected.append(r)
            seen_articles.add(r[3])
    for r in rows:
        if len(selected) >= limit:
            break
        if r not in selected:
            selected.append(r)

    out = []
    for r in selected:
        (chunk_id, chunk_content, heading, article_id, title, author,
         dynasty, source, category, url, source_file, _rank) = r
        out.append({
            "articleId": article_id,
            "chunkId": chunk_id,
            "title": title or "",
            "heading": heading or "",
            "author": author or "",
            "dynasty": dynasty or "",
            "source": source or "",
            "category": category or "",
            "url": url or "",
            "sourceFile": source_file or "",
            "snippet": (chunk_content or "")[:600],
            "sourceOrder": _source_order(source),
        })
    return out


def list_sources(
    data_dir: Path,
    experts: list[str],
) -> list[dict[str, Any]]:
    """列出当前可检索的专家库及其篇数与 source 标签（探活/调试用）。"""
    out = []
    for expert_id in experts:
        p = data_dir / f"{expert_id}.db"
        if not p.is_file():
            out.append({"expertId": expert_id, "available": False, "reason": "db 文件不存在"})
            continue
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            out.append({"expertId": expert_id, "available": False, "reason": str(exc)})
            continue
        try:
            n = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            sources = [r[0] for r in con.execute("SELECT DISTINCT source FROM articles")]
            out.append({"expertId": expert_id, "available": True, "articles": n, "sources": sources})
        except sqlite3.Error as exc:
            out.append({"expertId": expert_id, "available": False, "reason": str(exc)})
        finally:
            con.close()
    return out


def search_across_dbs(
    db_paths: list[str],
    query: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """跨多库检索 + 合并排序（sourceOrder 优先，同级别保持单库内 rank 原序）。"""
    per_db_limit = max(1, int(limit * 1.5))
    all_rows: list[dict[str, Any]] = []
    for p in db_paths:
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        except sqlite3.Error:
            # 某个库缺失或被锁，跳过不影响其他库
            continue
        try:
            rows = search_one_db(con, query, limit=per_db_limit)
            expert_id = Path(p).stem
            for r in rows:
                all_rows.append({"expertId": expert_id, **r})
        finally:
            con.close()
    all_rows.sort(key=lambda r: r["sourceOrder"])
    return all_rows[:limit]
