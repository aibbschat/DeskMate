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

简繁变体：语料是简繁混排的，每个降级档都会把查询词的简/繁两形**各自成短语再 OR**
（详见 :mod:`coworker.history.s2t`）。注意变体之间绝不串接 —— 串接会产生
``"项 羽 項"`` 这类语料里根本不存在的词。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .s2t import variants as _script_variants

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


def _phrase(chars: list[str]) -> str:
    """CJK 字符序列 → FTS5 短语（字间插空格，与灌库侧 tokenizeCJK 对齐）。"""
    return '"' + " ".join(chars) + '"'


def _or_join(parts: list[str]) -> Optional[str]:
    """OR 连接并去重（保序）；空列表返回 ``None`` 表示该档不参与检索。

    简繁两形常常只是部分字不同（``王安石`` 转繁仍是 ``王安石``），不去重会让
    同一短语在 MATCH 表达式里出现两遍。
    """
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " OR ".join(out) if out else None


def build_fts5_query(query: str) -> dict[str, Any]:
    """构建 FTS5 MATCH 表达式。

    返回 ``{"exact_query", "trigram_query", "bigram_query", "char_and_query",
    "chars", "variants"}``；空查询返回 ``{"empty": True}``；非 CJK 查询各档都返回
    带引号词的 OR 串。

    三条不变量（都是踩过坑才定下来的，别改回去）：

    1. **同义词不进 trigram/bigram**。同义词只以独立短语 OR 进精确档；拼到查询串
       尾部会生成跨词边界的虚假组合（查"变法"会拼出"变 法 改"/"法 改 革"）。
    2. **简繁变体各自成短语，绝不跨变体串接**。串接会造出语料里根本不存在的词
       （如"项 羽 項"）。做法是：变体内部滑动窗口，变体之间 OR。
    3. **char_and 档是「变体内 AND、变体间 OR」**。若把两个变体的字符混在一起
       AND，等于要求同一段文字里简繁并存，几乎不可能命中。
    """
    expanded = _expand_query(query)
    cleaned = _strip_fts_metachars(expanded)
    if not cleaned:
        return {"empty": True}

    orig = _strip_fts_metachars(query)

    if not _CJK_RE.search(cleaned):
        # 英文/数字：按词加引号 OR（无简繁变体）
        as_str = " OR ".join(f'"{w}"' for w in cleaned.split())
        return {
            "exact_query": as_str,
            "trigram_query": as_str,
            "bigram_query": None,
            "char_and_query": as_str,
            "chars": [],
            "variants": [orig] if orig else [],
        }

    chars = _cjk_chars(cleaned)
    if not chars:
        return {"empty": True}

    # 简繁变体的字符序列：原形排第一，其后依次是转繁/转简形式。
    # 变体由**原始查询**生成，同义词不参与转换（避免表达式无谓膨胀）。
    variants = _script_variants(orig) if orig else [orig]
    var_seqs: list[list[str]] = []
    seen_seq: set[str] = set()
    for v in variants:
        vc = _cjk_chars(v)
        if not vc:
            continue
        key = "".join(vc)
        if key in seen_seq:
            continue
        seen_seq.add(key)
        var_seqs.append(vc)
    if not var_seqs:
        var_seqs = [chars]

    if len(chars) == 1:
        # 单字查询：各档同义，只需把简繁两形 OR 起来（如 项/項）
        one = _or_join([_phrase(vc) for vc in var_seqs])
        return {
            "exact_query": one,
            "trigram_query": one,
            "bigram_query": None,
            "char_and_query": one,
            "chars": chars,
            "variants": variants,
        }

    # Exact 档：每个变体整个词一个连续短语
    exact_query = _or_join([_phrase(vc) for vc in var_seqs])
    # 同义词**各自独立成短语**再 OR，而不是拼到查询串尾部
    syns = _synonyms_for(query)
    if syns:
        syn_phrases = [_phrase(_cjk_chars(s)) for s in syns if _cjk_chars(s)]
        if syn_phrases:
            exact_query = _or_join([exact_query] + syn_phrases)

    # trigram / bigram：变体内部滑动窗口，变体之间 OR（不跨变体滑窗）
    trigrams = [_phrase(vc[i:i + 3]) for vc in var_seqs for i in range(len(vc) - 2)]
    trigram_query = _or_join(trigrams)

    bigram_query = None
    if 2 <= len(var_seqs[0]) < 5:
        bigrams = [_phrase(vc[i:i + 2]) for vc in var_seqs for i in range(len(vc) - 1)]
        bigram_query = _or_join(bigrams)

    # char_and：变体内 AND、变体间 OR
    and_parts: list[str] = []
    for vc in var_seqs:
        head = vc[:5]
        if len(head) >= 2:
            and_parts.append("(" + " AND ".join(head) + ")")
        elif head:
            and_parts.append(head[0])
    char_and_query = _or_join(and_parts) or " ".join(chars[:5])

    return {
        "exact_query": exact_query,
        "trigram_query": trigram_query,
        "bigram_query": bigram_query,
        "char_and_query": char_and_query,
        "chars": chars,
        "variants": variants,
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
    #
    # 注意必须**逐个变体**判：原文是繁体时，简体查询词 "项羽" 不会出现在
    # "項羽…" 里，若只判原形，所有繁体史料的 exact 都会是 False，从而被
    # 一律排到简体命中之后 —— 等于白做了变体扩展。
    cleaned_query = _strip_fts_metachars(query)
    forms = [v for v in (q.get("variants") or [cleaned_query]) if v]

    out = []
    for r in selected:
        (chunk_id, chunk_content, heading, article_id, title, author,
         dynasty, source, category, url, source_file, _rank) = r
        body = chunk_content or ""
        matched = next((v for v in forms if v in body), None)
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
            "exact": bool(matched),
            # 实际命中的字形。与用户输入不同即为「以另一种字形命中」，
            # 供引用时判断这条史料是原典繁体还是今译简体。
            "matchedForm": matched or "",
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
    # 变体逐组算、取最大覆盖：繁体条目对简体查询词的覆盖度恒为 0，
    # 不取最大就会被简体条目无条件压在下面。
    variant_bigrams = []
    for v in _script_variants(_strip_fts_metachars(query)):
        vc = _cjk_chars(v)
        bg = [vc[i] + vc[i + 1] for i in range(len(vc) - 1)]
        if bg:
            variant_bigrams.append(bg)

    def _coverage(row: dict[str, Any]) -> int:
        body = row.get("snippet", "")
        return max(
            (sum(1 for b in bg if b in body) for bg in variant_bigrams),
            default=0,
        )

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
