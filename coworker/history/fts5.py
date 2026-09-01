"""百家学院史料 FTS5 检索（Python 移植，零外部依赖，只用内置 ``sqlite3``）。

移植自 ``ai-experts-platform/backend/shared/fts5_search.js`` 与
``ai-forum-v3/mcp/baijia/src/fts5-search.js``，只保留历史考据官需要的子集。

四级降级（与生产一致，本机另加精确档）：
  0. Exact 全词短语 —— 整个查询词作为一个连续短语，**最高精度**（本机新增）
  1. Trigram OR   —— 3 字滑动窗口精确短语
  2. Bigram OR    —— 2 字滑动窗口，短查询兜底
  3. Char AND     —— 前 5 个 CJK 字符 AND，召回兜底

为何要加 Exact 档：只靠 trigram 时，5 字词会被拆成 3 个 3-gram（如"王安石变法"
→"王安石"/"安石变"/"石变法"），命中任意一个即算命中，导致查"王安石变法"会捞回
只含"王安石"的无关段落。Exact 档把整个词作为一个短语查，精确命中优先返回，
不足再由后续档补齐。

结果按「精确档优先 → sourceOrder（chtxt > daizhigev20 > 其他）→ rank」排序。

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

# 结果来源优先级：精校古籍(chtxt) > 全量古籍(daizhigev20) > 其他
_SOURCE_ORDER_SQL = (
    "CASE WHEN a.source = 'chtxt' THEN 0 "
    "WHEN a.source = 'daizhigev20' THEN 1 ELSE 2 END"
)

# 降级档顺序（索引越小越精确），同时作为返回给模型的 ``matchTier`` 标签，
# 让模型能判断每条史料命中的可靠程度。
_TIERS = ("exact_query", "trigram_query", "bigram_query", "char_and_query")
_TIER_LABELS = {
    "exact_query": "exact",
    "trigram_query": "trigram",
    "bigram_query": "bigram",
    "char_and_query": "char_and",
}

# SELECT 列（只取 DDL 确认存在的字段）。注意：articles 表没有 url 字段，
# 用 file_path 兜底（mcp/baijia 的旧 SQL 引用了不存在的 a.url，这里已修正）。
_COLUMNS = (
    "c.id AS chunk_id, c.content AS chunk_content, c.heading AS heading, "
    "a.id AS article_id, a.title AS title, a.author AS author, "
    "a.dynasty AS dynasty, a.source AS source, a.category AS category, "
    "a.file_path AS url, a.source_file AS source_file"
)


def _synonyms_for(query: str) -> list[str]:
    """命中查询中的同义词词组，返回待扩展的同义词列表。"""
    out: list[str] = []
    for key, syns in _SYNONYMS.items():
        if key in query:
            out.extend(s for s in syns if s not in query)
    return out


def _expand_query(query: str) -> str:
    syns = _synonyms_for(query)
    return query + (" " + " ".join(syns) if syns else "")


def _cjk_chars(text: str) -> list[str]:
    return [c for c in text if _CJK_RE.match(c)]


def _strip_fts_metachars(text: str) -> str:
    """去 FTS5 语法特殊字符 + 布尔关键字，防查询注入。"""
    cleaned = re.sub(r'["*^()]', " ", text)
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def build_fts5_query(query: str) -> dict[str, Any]:
    """构建 FTS5 MATCH 表达式。

    返回 ``{"exact_query", "trigram_query", "bigram_query", "char_and_query",
    "chars"}``；空查询返回 ``{"empty": True}``；非 CJK 查询各档都返回带引号词的 OR 串。

    ``exact_query`` 用**原始查询**的字符（不做同义词扩展），否则同义词会污染
    精确短语匹配。
    """
    expanded = _expand_query(query)
    cleaned = _strip_fts_metachars(expanded)
    if not cleaned:
        return {"empty": True}

    if not _CJK_RE.search(cleaned):
        # 英文/数字：按词加引号 OR
        as_str = " OR ".join(f'"{w}"' for w in cleaned.split())
        return {
            "exact_query": as_str,
            "trigram_query": as_str,
            "bigram_query": None,
            "char_and_query": as_str,
            "chars": [],
        }

    chars = _cjk_chars(cleaned)
    if not chars:
        return {"empty": True}
    if len(chars) == 1:
        one = f'"{chars[0]}"'
        return {
            "exact_query": one,
            "trigram_query": one,
            "bigram_query": None,
            "char_and_query": one,
            "chars": chars,
        }

    # Exact 档：原始查询全部 CJK 字符组成的连续短语
    orig = _strip_fts_metachars(query)
    orig_chars = _cjk_chars(orig)
    exact_chars = orig_chars if len(orig_chars) >= 2 else chars
    exact_query = '"' + " ".join(exact_chars) + '"'
    # 同义词**各自独立成短语**再 OR，而不是拼到查询串尾部——
    # 拼接会让 trigram/bigram 生成跨词边界的虚假组合（查"变法"会拼出
    # "变 法 改"/"法 改 革"/"改 革 新"，命中完全无关的段落）。
    syns = _synonyms_for(query)
    if syns:
        syn_phrases = " OR ".join(
            '"' + " ".join(_cjk_chars(s)) + '"' for s in syns if _cjk_chars(s)
        )
        if syn_phrases:
            exact_query += " OR " + syn_phrases

    # trigram/bigram 只用**原始查询**字符，避免同义词串接污染
    base = orig_chars if len(orig_chars) >= 2 else chars
    trigrams = [f'"{base[i]} {base[i + 1]} {base[i + 2]}"' for i in range(len(base) - 2)]
    trigram_query = " OR ".join(trigrams) if trigrams else None

    bigram_query = None
    if 2 <= len(base) < 5:
        bigrams = [f'"{base[i]} {base[i + 1]}"' for i in range(len(base) - 1)]
        bigram_query = " OR ".join(bigrams) if bigrams else None

    # Char AND 用原始查询字符（不做同义词扩展），限前 5 个 CJK
    orig = _strip_fts_metachars(query)
    orig_chars = _cjk_chars(orig)
    and_chars = (orig_chars if orig_chars else chars)[:5]
    char_and_query = " ".join(and_chars) if len(and_chars) >= 2 else " ".join(chars[:5])

    return {
        "exact_query": exact_query,
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
    逐档累加重排：精确档结果排最前，不足 ``fetch_limit`` 再由后续降级档补齐
    （而不是"某档有结果就停"）。各档内部按 sourceOrder + rank 排序。
    """
    q = build_fts5_query(query)
    if q.get("empty"):
        return []

    sql = (
        f"SELECT {_COLUMNS}, rank FROM chunks_fts f "
        "JOIN chunks c ON c.id = f.rowid "
        "JOIN articles a ON a.id = c.article_id "
        f"WHERE chunks_fts MATCH ? "
        f"ORDER BY {_SOURCE_ORDER_SQL}, rank LIMIT ?"
    )

    rows: list[tuple] = []
    seen_chunks: set[Any] = set()
    seen_content: set[str] = set()
    tier_of: dict[Any, int] = {}
    for tier_idx, tier in enumerate(_TIERS):
        if len(rows) >= fetch_limit:
            break
        match_expr = q.get(tier)
        if not match_expr:
            continue
        try:
            tier_rows = con.execute(sql, (match_expr, fetch_limit)).fetchall()
        except sqlite3.Error:
            # 某档 FTS 语法不兼容时静默跳过，继续下一档
            continue
        for r in tier_rows:
            if len(rows) >= fetch_limit:
                break
            chunk_id, content = r[0], (r[1] or "")
            # 内容指纹去重：同一段文字可能被重复灌入不同 chunk（实测《元史》有）
            fingerprint = content[:200]
            if chunk_id in seen_chunks or (fingerprint and fingerprint in seen_content):
                continue
            seen_chunks.add(chunk_id)
            if fingerprint:
                seen_content.add(fingerprint)
            tier_of[chunk_id] = tier_idx
            rows.append(r)

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

    # 逐档累加后 rows 已是「精确优先」顺序；跨库合并时也要保住这个顺序，
    # 因此给每条结果打上 exact 标记（完整查询词是否出现在该 chunk 正文中）。
    cleaned_query = _strip_fts_metachars(query)

    out = []
    for r in selected:
        (chunk_id, chunk_content, heading, article_id, title, author,
         dynasty, source, category, url, source_file, _rank) = r
        body = chunk_content or ""
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
            "exact": bool(cleaned_query and cleaned_query in body),
            # 命中档位，供模型判断这条史料有多可靠
            "matchTier": _TIER_LABELS[_TIERS[tier_of.get(chunk_id, len(_TIERS) - 1)]],
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
    """跨多库检索 + 合并排序。

    排序键为 ``(非精确, 档位, sourceOrder)``：完整包含查询词的结果一律排最前；
    其余按匹配档位（trigram > bigram > char AND）排序，**档位优先于来源优先级**
    ——避免出现"来源更权威但内容其实无关"的条目压过真正相关的条目。
    """
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
    _TIER_RANK = {"exact": 0, "trigram": 1, "bigram": 2, "char_and": 3}
    # 同档位内的次级排序：命中了查询词多少个相邻二字片段。降级档里
    # "只匹配到一半关键词"的条目应让位给"匹配得更全"的条目，
    # 否则会出现来源更权威但内容其实无关的条目压过真正相关的条目。
    q_chars = _cjk_chars(_strip_fts_metachars(query))
    bigrams = [q_chars[i] + q_chars[i + 1] for i in range(len(q_chars) - 1)]

    def _coverage(row: dict[str, Any]) -> int:
        body = row.get("snippet", "")
        return sum(1 for b in bigrams if b in body) if bigrams else 0

    all_rows.sort(
        key=lambda r: (
            not r.get("exact", False),
            _TIER_RANK.get(r.get("matchTier", "char_and"), 3),
            -_coverage(r),
            r["sourceOrder"],
        )
    )

    # 跨库去重：同一部书可能同时存在于多个库（如《新唐書》在 duan-dai-shi 与史部
    # 子集中都有），排序后再按内容指纹去重，保证留下的是最相关的那条。
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in all_rows:
        fingerprint = r.get("snippet", "")[:200]
        if fingerprint and fingerprint in seen:
            continue
        if fingerprint:
            seen.add(fingerprint)
        deduped.append(r)
    return deduped[:limit]
