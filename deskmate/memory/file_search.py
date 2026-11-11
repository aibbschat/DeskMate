"""记忆文件层检索：关键词 + 向量 + 锚，三通道 RRF 融合。

设计理由（对应三段方案）
========================
1. **不能一刀切掉关键词**：专有名词、成句引用、编号（如「夜不收：绝命救援.docx」
   「14 人遇难」）在词面命中时比向量更准；向量负责「换个说法」的语义召回。
2. **RRF 而不是分数相加**：BM25 与余弦量纲不同，直接加权需要归一化且随语料漂移；
   Reciprocal Rank Fusion 只看排名，稳定、无需调参（k=60 为常用值）。
   通道权重按职责定：向量 1.2（语义主通道）、关键词 1.0（精确主通道）、
   锚 0.8（实体旁路）。
3. **原文精确子串置顶**：query ≥6 字且原文出现在 chunk 里时，直接加 1.0 分——
   这类查询（引用原句/文件全名）关键词应绝对优先，避免被同义向量结果挤掉。
4. **锚通道只做一跳**：复用 ``memory_anchors``（profile_item 的 topic/person），
   查询里出现锚值 → 取含该值的 chunk；再取同一 profile_item 上的邻座锚值，
   找含邻座锚值的 chunk（≤6 条）。不做 OpenIE 构图 / PPR / 两跳。

预算
====
- 单条 chunk ≤400 字（超长截断并加省略号）；
- 一次检索总输出 ≤1200 字；
- 兜底 ``backstop_chunks`` ≤3 条 / ≤400 字。

fail-open
=========
索引空 / 模型不可用 / 任何异常 → 返回 []；调用方回落旧关键词路径。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from deskmate.memory.file_index import FileIndex, _norm

logger = logging.getLogger(__name__)

RRF_K = 10
# 关键词略高于向量：专有名词/文件全名/成句引用时关键词更准（三段方案的明确要求）。
WEIGHT_VECTOR = 1.2
WEIGHT_KEYWORD = 1.3
WEIGHT_ANCHOR = 0.8
MAX_CHUNK_CHARS = 400
ANCHOR_NEIGHBOR_CAP = 6
# 锚值太长时不做子串检索（新闻标题类锚，检索价值低且易误命中）
ANCHOR_VALUE_MAX = 24


def _idx(base: Optional[Path | str] = None) -> FileIndex:
    return FileIndex(base)


def index_healthy(base: Optional[Path | str] = None) -> bool:
    try:
        st = _idx(base).stats()
        return st["chunks"] > 0 and st["coverage"] >= 0.9
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 锚通道
# ---------------------------------------------------------------------------
def _anchor_values(base: Path) -> list[str]:
    try:
        con = sqlite3.connect(str(base / "deskmate.db"))
        rows = con.execute(
            "SELECT DISTINCT anchor_value FROM memory_anchors "
            "WHERE anchor_kind IN ('person','topic','book') "
            "AND length(anchor_value) BETWEEN 2 AND ?",
            (ANCHOR_VALUE_MAX,),
        ).fetchall()
        con.close()
        return [r[0] for r in rows if r[0]]
    except sqlite3.Error:
        return []


def _anchor_neighbors(base: Path, values: list[str], cap: int = ANCHOR_NEIGHBOR_CAP) -> list[str]:
    """一跳：同一 profile_item 上共现的其它锚值（复用 memory_anchors 的语义）。"""
    if not values:
        return []
    try:
        con = sqlite3.connect(str(base / "deskmate.db"))
        q = ",".join("?" * len(values))
        item_ids = [r[0] for r in con.execute(
            f"SELECT DISTINCT item_id FROM memory_anchors WHERE item_kind='profile_item' "
            f"AND anchor_value IN ({q})", values,
        ).fetchall()]
        if not item_ids:
            con.close()
            return []
        q2 = ",".join("?" * len(item_ids))
        rows = con.execute(
            f"SELECT DISTINCT anchor_value FROM memory_anchors "
            f"WHERE item_kind='profile_item' AND item_id IN ({q2}) "
            f"AND anchor_kind IN ('person','topic','book') "
            f"AND length(anchor_value) BETWEEN 2 AND ?",
            (*item_ids, ANCHOR_VALUE_MAX),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return []
    seen = set(values)
    out: list[str] = []
    for (v,) in rows:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
        if len(out) >= cap:
            break
    return out


def _anchor_hits(idx: FileIndex, base: Path, query: str, limit: int = 12) -> list[dict]:
    """query 里出现的锚 → 含锚 chunk；再一次一跳邻座（纯增益）。"""
    nq = _norm(query)
    values = [v for v in _anchor_values(base) if _norm(v) and _norm(v) in nq]
    # 长锚优先（更具体），最多 4 个
    values.sort(key=len, reverse=True)
    values = values[:4]
    hits: list[dict] = []
    seen: set[str] = set()
    for v in values:
        for row in idx.chunks_containing(v, limit=limit):
            if row["id"] not in seen:
                seen.add(row["id"])
                row["via_anchor"] = v
                hits.append(row)
    neighbors = _anchor_neighbors(base, values)
    for v in neighbors:
        if len(hits) >= limit + ANCHOR_NEIGHBOR_CAP:
            break
        for row in idx.chunks_containing(v, limit=3):
            if row["id"] not in seen:
                seen.add(row["id"])
                row["via_anchor"] = f"一跳:{v}"
                hits.append(row)
    return hits


# ---------------------------------------------------------------------------
# 融合
# ---------------------------------------------------------------------------
def _rrf_add(scores: dict[str, float], rows: list[dict], weight: float) -> None:
    for rank, row in enumerate(rows, 1):
        cid = row.get("id")
        if not cid:
            continue
        scores[cid] = scores.get(cid, 0.0) + weight / (RRF_K + rank)


def search_memory_files(
    query: str,
    k: int = 8,
    budget_chars: int = 1200,
    base: Optional[Path | str] = None,
    kinds: Optional[list[str]] = None,
    section_contains: Optional[str] = None,
) -> list[dict]:
    """三通道检索并融合。返回按分数降序的 chunk 列表。

    ``kinds`` / ``section_contains`` 用于局部检索（如只取 USER.md 的
    「偏好与事实」节），过滤在融合前做，避免无关层挤占预算。
    """
    q = (query or "").strip()
    if not q:
        return []
    idx = _idx(base)
    b = idx.base

    # 1) 增量同步（失败不阻塞，用旧索引继续）
    try:
        idx.sync()
    except Exception as exc:  # noqa: BLE001
        logger.warning("索引同步失败（用旧索引继续）：%s", exc)

    # 2) 三通道候选
    kw_rows = idx.search_keyword(q, limit=30)
    vec_rows: list[dict] = []
    try:
        from deskmate.memory import local_embed

        qv = local_embed.embed([q])
        if qv and qv[0]:
            vec_rows = idx.vector_search(qv[0], limit=30)
    except Exception as exc:  # noqa: BLE001 —— 向量不可用则只用关键词
        logger.warning("查询向量化失败（退关键词）：%s", exc)
    anchor_rows = _anchor_hits(idx, b, q)

    def _keep(row: dict) -> bool:
        if kinds and row.get("source_kind") not in kinds:
            return False
        if section_contains and section_contains not in (row.get("section") or ""):
            return False
        return True

    kw_rows = [r for r in kw_rows if _keep(r)]
    vec_rows = [r for r in vec_rows if _keep(r)]
    anchor_rows = [r for r in anchor_rows if _keep(r)]

    # 3) RRF 融合
    scores: dict[str, float] = {}
    _rrf_add(scores, vec_rows, WEIGHT_VECTOR)
    _rrf_add(scores, kw_rows, WEIGHT_KEYWORD)
    _rrf_add(scores, anchor_rows, WEIGHT_ANCHOR)

    kw_ids = {r["id"] for r in kw_rows}
    vec_ids = {r["id"] for r in vec_rows}
    anchor_ids = {r["id"] for r in anchor_rows}

    # 4) 原文精确子串 → 置顶（引用原句 / 文件全名）
    nq = _norm(q)
    meta: dict[str, dict] = {}
    for rows in (vec_rows, kw_rows, anchor_rows):
        for r in rows:
            meta.setdefault(r["id"], r)
    for cid, row in meta.items():
        content = row.get("content", "")
        if len(nq) >= 6 and nq in _norm(content):
            scores[cid] = scores.get(cid, 0.0) + 1.0

    ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))

    # 5) 去重 + 多样性限制 + 预算
    #    MEMORY.md 的条目共用「创作/纠正」等节标题，按节限 2 会把同节里的
    #    正确条目挤掉；改为每节 4 条、每个文件 5 条，防止单个长文件刷屏。
    out: list[dict] = []
    per_section: dict[tuple[str, str], int] = {}
    per_file: dict[str, int] = {}
    used = 0
    for cid, score in ranked:
        row = meta.get(cid)
        if not row:
            continue
        path = row.get("source_path", "")
        key = (path, row.get("section", ""))
        if per_section.get(key, 0) >= 4 or per_file.get(path, 0) >= 12:
            continue
        content = (row.get("content") or "").strip()
        if not content:
            continue
        if len(content) > MAX_CHUNK_CHARS:
            content = content[: MAX_CHUNK_CHARS - 1] + "…"
        if used + len(content) > budget_chars and out:
            continue
        per_section[key] = per_section.get(key, 0) + 1
        per_file[path] = per_file.get(path, 0) + 1
        used += len(content)
        chans = []
        if cid in vec_ids:
            chans.append("vector")
        if cid in kw_ids:
            chans.append("keyword")
        if cid in anchor_ids:
            chans.append("anchor")
        out.append({
            "id": cid, "score": round(float(score), 6),
            "source_kind": row.get("source_kind", ""),
            "source_path": row.get("source_path", ""),
            "section": row.get("section", ""),
            "content": content,
            "via_anchor": row.get("via_anchor", ""),
            "channels": chans,
        })
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# 兜底召回
# ---------------------------------------------------------------------------
def backstop_chunks(
    base: Optional[Path | str] = None,
    limit: int = 3,
    max_chars: int = 400,
) -> list[dict]:
    """没主动查时的保底召回：硬规矩 + 偏好 + MEMORY 最近条目。

    不走向量、不做相似度，只按「高价值层 + 时间」取。预算 400 字，依据见 spec：
    总预算 6000 中 preference_hits 700 + journal 1400 已占 2100，兜底 400 约 6.7%。
    """
    idx = _idx(base)
    picks: list[dict] = []
    try:
        # 硬规矩：USER.md 的「硬规矩」节前 2 条
        picks.extend(idx.by_section("硬规矩", ["user"], limit=2))
        # 偏好：偏好与事实节前 1 条
        picks.extend(idx.by_section("偏好与事实", ["user"], limit=1))
        # MEMORY 最近 1 条
        picks.extend(idx.recent(["memory"], limit=1))
    except Exception:
        return picks
    out: list[dict] = []
    used = 0
    for r in picks:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 200:
            content = content[:199] + "…"
        if used + len(content) > max_chars and out:
            continue
        used += len(content)
        out.append({**r, "content": content})
        if len(out) >= limit:
            break
    return out


def format_hits(hits: list[dict], title: str = "## 相关记忆（按本轮问题临时检索）") -> str:
    """把命中格式化成注入块（与现有 markdown 风格一致）。"""
    if not hits:
        return ""
    lines = [title, ""]
    for h in hits:
        src = h.get("source_path", "")
        section = h.get("section", "")
        tag = f"{src}"
        if section:
            tag += f" · {section}"
        lines.append(f"- [{tag}] {h.get('content', '').strip()}")
    return "\n".join(lines)
