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

from deskmate.memory.file_index import FileIndex, _norm, tokenize
from deskmate.paths import deskmate_base_dir

logger = logging.getLogger(__name__)

RRF_K = 10
# 关键词略高于向量：专有名词/文件全名/成句引用时关键词更准（三段方案的明确要求）。
WEIGHT_VECTOR = 1.2
WEIGHT_KEYWORD = 1.3
WEIGHT_ANCHOR = 0.8
#: 关键词头名 bonus（见 search_memory_files 中的注释）；0 = 关闭
TOP1_KEYWORD_BONUS = 0.10
MAX_CHUNK_CHARS = 400
ANCHOR_NEIGHBOR_CAP = 6
# 锚值太长时不做子串检索（新闻标题类锚，检索价值低且易误命中）
ANCHOR_VALUE_MAX = 24
# 预算不足时，剩余空间至少能放一行才截断保留；否则宁可跳过（避免只截出半句）
MIN_TRUNC_CHARS = 120
#: 单条输出下限（预算 ÷ k 的兜底）。200 字 ≈ 一条 bullet / 一段日记；
#: 配合 _snippet 的命中句窗口，既能装下 8 条，也不会把命中句切掉。
MIN_ITEM_CHARS = 200

# 四段的「闲聊门」：这类输入本身没有信息需求，检索必然只能塞噪声。
# 只认整句寒暄，不做子串匹配——「早写完了吗」这种带内容的不能被误判。
_CHITCHAT_RE = re.compile(
    r"^(?:早|早安|早上好|上午好|中午好|下午好|晚上好|晚安|嗨|hi|hello|hey|"
    r"在吗|在么|在不在|嗯+|哦+|喔|好|好的|好滴|收到|了解|明白|谢谢|谢了|"
    r"辛苦了|辛苦|哈+|哈哈+|嘿+|ok|okay|行|可以|没事)[\s!！。~～?？]*$",
    re.IGNORECASE,
)


def is_chitchat(text: str) -> bool:
    """整轮只是一句寒暄/确认（无实体、无诉求）时返回 True。

    四段风险 #1「闲聊也注入噪声」的判定入口：命中即不做记忆检索注入，
    只保留兜底（硬规矩/最近条目）。必须整句都在白名单里（最长 12 字），
    「早写完了吗」这类带内容的一律不算。
    """
    t = (text or "").strip()
    if not (1 <= len(t) <= 12):
        return False
    return bool(_CHITCHAT_RE.match(t))


def gate_hits(
    hits: list[dict],
    min_vec: float = 0.32,
    rel: float = 0.5,
    max_items: int = 6,
) -> list[dict]:
    """四段的相关性闸：把「靠 RRF 尾部分数挤进来」的无关条目挡在注入之外。

    保留规则（满足任一）：
    - 第一条（最高分）永远保留；
    - 向量余弦 ≥ ``min_vec``（语义上有真实相近，不是纯排名漂移）；
    - 命中 ≥2 个通道（关键词/向量/锚互相印证）；
    - 分数 ≥ 头部 × ``rel``（同一梯队）。
    ``hits`` 为空时原样返回；不改变条目内容，只做过滤。
    """
    if not hits:
        return []
    top = float(hits[0].get("score") or 0.0)
    kept: list[dict] = []
    for i, h in enumerate(hits):
        if i == 0:
            kept.append(h)
            continue
        chans = h.get("channels") or []
        vec = float(h.get("vec_score") or 0.0)
        score = float(h.get("score") or 0.0)
        if vec >= min_vec or len(chans) >= 2 or (top and score >= top * rel):
            kept.append(h)
        if len(kept) >= max_items:
            break
    return kept



def _idx(base: Optional[Path | str] = None) -> FileIndex:
    return FileIndex(base)


def _has_indexable_sources(base: Path) -> bool:
    for name in ("USER.md", "MEMORY.md", "diary_memory.md"):
        if (base / name).is_file():
            return True
    for d in ("diary", "vault"):
        if (base / d).is_dir():
            return True
    return False


def index_healthy(base: Optional[Path | str] = None) -> bool:
    """索引是否可用于「新检索路径」（只读探测，绝不建表/清表）。

    2026-11-11 复检修：原实现 ``_idx(base).stats()`` 会构造 FileIndex，进而执行
    ``_ensure_schema``——CHUNK_VERSION 一变就 DELETE 全部索引表，健康检查本身
    成了破坏者；随后 chunks=0 判不健康，唯一重建入口（search_memory_files 里的
    sync）又被这个判定门控，永久锁死、永不自举。

    这里改为直接读库计数；同时去掉「向量覆盖率 ≥0.9」的一票否决——纯关键词
    FTS5 通道本身就是可用的降级形态，embedding 挂掉不该让整条检索静默关闭。
    """
    b = Path(base) if base else deskmate_base_dir()
    db = b / "deskmate.db"
    if not db.is_file():
        return _has_indexable_sources(b)  # 有源文件：让 sync 首次自举
    try:
        con = sqlite3.connect(str(db))
        try:
            chunks = con.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return _has_indexable_sources(b)  # 表还没建：交给 sync
    if chunks <= 0:
        return _has_indexable_sources(b)  # 版本升级后的空索引：交给 sync 重建
    return True


# ---------------------------------------------------------------------------
# 锚通道
# ---------------------------------------------------------------------------
def _anchor_values(base: Path) -> list[str]:
    db = base / "deskmate.db"
    if not db.is_file():
        return []  # 只读：不能在用户目录里凭空造库
    try:
        con = sqlite3.connect(str(db))
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
    db = base / "deskmate.db"
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(str(db))
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


def _entity_hits(idx: FileIndex, base: Path, query: str, limit: int = 12) -> list[dict]:
    """实体链接通道（B-1）：query 里的实体 → 含该实体的 chunk；再一次一跳共现。

    词典来自 ``known_persons`` + 书名/主题 + 别名归一，零 LLM。索引未回填 /
    词典为空时返回 []，调用方继续走旧锚通道（纯增益，不回退已有召回）。
    """
    try:
        from deskmate.memory.entity_link import load_lexicon, match_entities

        ents = match_entities(query, load_lexicon(base), limit=4)
    except Exception:  # noqa: BLE001 —— 实体通道任何异常都不影响检索
        return []
    if not ents:
        return []
    values = [canonical for _, canonical, _ in ents]
    qtokens = set(tokenize(query))
    # 高频实体（如「萧盛」488 个 chunk）按路径截 12 条会漏掉目标；先全量取
    # （封顶 400）再做词面重合重排，保证「实体命中的 chunk 里，最贴 query 的」
    # 排在前面。400 条 × token 交集在毫秒级，检索耗时可接受。
    rows = idx.entity_chunks(values, limit=400)
    for row in rows:
        tokens = set((row.pop("tokens", "") or "").split())
        row["_overlap"] = len(qtokens & tokens)
    rows.sort(key=lambda r: (-r["_overlap"], -len(str(r.get("entity_value") or ""))))
    hits: list[dict] = []
    seen: set[str] = set()
    for row in rows[:limit]:
        cid = row.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row.pop("_overlap", None)
        row["via_anchor"] = row.pop("entity_value", "") or values[0]
        hits.append(row)
    for v in idx.entity_neighbors(values):
        if len(hits) >= limit + ANCHOR_NEIGHBOR_CAP:
            break
        for row in idx.entity_chunks([v], limit=3):
            cid = row.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            row["via_anchor"] = f"一跳:{v}"
            row.pop("entity_value", None)
            row.pop("tokens", None)
            hits.append(row)
    return hits


def _anchor_hits(idx: FileIndex, base: Path, query: str, limit: int = 12) -> list[dict]:
    """query 里出现的锚 → 含锚 chunk；再一次一跳邻座（纯增益）。

    顺序很重要：**旧锚通道排在前，实体链接补在尾部**。实体通道覆盖的是
    known_persons 全量（李昖这类高频人名动辄上百 chunk），若让它插到前面，
    会把原本靠锚命中、只差一名的条目挤出 top8（实测 ent-08 1→0）。旧锚先占位
    后，实体只做「原来没有的召回」，保证不丢已有名次。
    """
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
    for row in _entity_hits(idx, base, query, limit=limit):
        if len(hits) >= limit + ANCHOR_NEIGHBOR_CAP:
            break
        if row["id"] not in seen:
            seen.add(row["id"])
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


def _snippet(content: str, query: str, cap: int) -> str:
    """把超长条目截成「命中句附近」的窗口，而不是永远砍掉尾巴。

    四段实测：日记条目「……我今天两本书的合同废了……」长 228 字，按头部截 200
    正好把命中句切掉，检索对了却读不到。这里以 query 与正文的最长公共子串
    位置为中心取窗口（左 1/3、右 2/3），窗口外补省略号；找不到公共片段时
    退回头部截断。
    """
    if len(content) <= cap:
        return content
    raw_q = (query or "").strip()
    pos = -1
    # 从长到短找 query 的子串在正文中的原始位置（≥2 字才有意义）
    for ln in (8, 6, 4, 3, 2):
        if len(raw_q) < ln:
            continue
        for i in range(0, len(raw_q) - ln + 1):
            j = content.find(raw_q[i : i + ln])
            if j >= 0:
                pos = j
                break
        if pos >= 0:
            break
    if pos < 0:
        # 退一步：归一化后比对，再按比例映射回原文位置
        nq, nc = _norm(raw_q), _norm(content)
        for ln in (8, 6, 4, 3, 2):
            if len(nq) < ln:
                continue
            for i in range(0, len(nq) - ln + 1):
                j = nc.find(nq[i : i + ln])
                if j >= 0:
                    pos = int(j / max(1, len(nc)) * len(content))
                    break
            if pos >= 0:
                break
    if pos < 0:
        return content[: cap - 1] + "…"
    budget = max(20, cap - 2)  # 两端省略号也占位
    start = max(0, pos - budget // 3)
    end = min(len(content), start + budget)
    start = max(0, end - budget)
    head = "…" if start > 0 else ""
    tail = "…" if end < len(content) else ""
    return head + content[start:end] + tail


def search_memory_files(
    query: str,
    k: int = 8,
    budget_chars: int = 1600,
    base: Optional[Path | str] = None,
    kinds: Optional[list[str]] = None,
    section_contains: Optional[str] = None,
    read_only: bool = False,
) -> list[dict]:
    """三通道检索并融合。返回按分数降序的 chunk 列表。

    ``kinds`` / ``section_contains`` 用于局部检索（如只取 USER.md 的
    「偏好与事实」节），过滤在融合前做，避免无关层挤占预算。

    ``read_only=True`` 供 ``memory_search`` 工具使用：不建库、不同步索引、
    不懒加载模型——只读已有索引，索引不存在就返回空（宁缺不写）。
    """
    q = (query or "").strip()
    if not q:
        return []
    if read_only:
        try:
            idx = FileIndex(base, read_only=True)
        except Exception:  # noqa: BLE001 —— 没有索引就当没查到
            return []
    else:
        idx = _idx(base)
    b = idx.base

    # 1) 增量同步（失败不阻塞，用旧索引继续）
    if not read_only:
        try:
            idx.sync()
        except Exception as exc:  # noqa: BLE001
            logger.warning("索引同步失败（用旧索引继续）：%s", exc)

    # 2) 三通道候选
    kw_rows = idx.search_keyword(q, limit=30)
    vec_rows: list[dict] = []
    try:
        from deskmate.memory import local_embed

        # 只读模式不触发模型下载：模型已在本进程载入才走向量通道
        qv = local_embed.embed([q], allow_load=not read_only)
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
    # 四段（old-02）：RRF 只看排名，关键词第 1 的条目（1.3/11≈0.118）会输给
    # 「两通道都排 5~8」的条目（1.2/15+1.3/15≈0.167），于是精确词面命中被挤
    # 出 top8。给关键词通道的**头名**加一个固定 bonus（不按名次递减）：
    # 只抬「最像这问题的那一条」，不影响其余排序。
    if kw_rows:
        _cid = kw_rows[0].get("id")
        if _cid:
            scores[_cid] = scores.get(_cid, 0.0) + TOP1_KEYWORD_BONUS

    kw_ids = {r["id"] for r in kw_rows}
    vec_ids = {r["id"] for r in vec_rows}
    anchor_ids = {r["id"] for r in anchor_rows}
    # 四段：把向量余弦透传给调用方（注入层 gate_hits 用它判断「语义是否真的接近」，
    # RRF 分数只反映排名，无法区分「真相关」与「尾部挤入」）。
    vec_score = {r["id"]: float(r.get("score") or 0.0) for r in vec_rows}

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
        # 四段（old-02a/02b、loc-01b）：单条输出上限收到 200 字。旧口径是 400，
        # 前 3 条长 chunk 就能吃满 1200 预算，把「精确命中但排名第 5」的条目挤掉
        # （实测：日记「两本书的合同废了」关键词通道第 1，融合后因预算整条丢失）。
        # 200 字 ≈ 一条 bullet / 一段日记，既保留命中句，又能装下 5~6 条。
        item_cap = max(MIN_ITEM_CHARS, budget_chars // max(1, k))
        content = _snippet(content, q, item_cap)
        remain = budget_chars - used
        if len(content) > remain:
            # 四段（loc-01b）：预算不够时**不要再整条丢弃**。旧逻辑 `continue`
            # 让「融合排名进前 5、但正文偏长」的条目直接消失，用户问的正是它。
            # 现在只要剩余预算放得下一行（≥120 字），就截到 remaining 保留头部。
            if remain < MIN_TRUNC_CHARS:
                continue
            content = content[: remain - 1] + "…"
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
            "vec_score": round(vec_score.get(cid, 0.0), 4),
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
