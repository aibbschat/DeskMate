"""结构化实体记忆：把「人物圣经」从 LLM 重写的散文改造成可核验、可增删的实体库。

## 为什么要有这一层

旧实现（v1）是：LLM 通读正文 → 输出 Markdown 散文 → 增量更新时把**整篇圣经**再喂给
LLM 重写一遍。这条链路有两个致命缺陷：

1. **无核验**：LLM 抽取时若脑补出正文里根本不存在的人物/关系（如凭"学正""子时"
   两个真词编出一整条"李慕白与沈清音的禁书之约"），没有任何环节能发现。
2. **幻影不死**：合并规则是"新章节优先、无冲突则保留"，且只把现有圣经截前一半喂进
   模型。幻影一旦写进圣经就永远删不掉，还会随每次增量越滚越牢。

v2 的根治思路：

- 抽取产物是**结构化条目**，每条**强制携带 evidence（正文逐字引文）**；
- 入库前过一道**确定性核验闸门**（纯 Python，不含 LLM）：人名必须在正文出现、引文
  必须能在来源正文里命中，否则直接判为幻觉丢弃；
- 合并是**确定性 upsert**（按实体 key 覆盖/补全 + 冲突留痕），不再让 LLM 重写全文，
  因此增量绝不会产生"旧错误残留"；
- 四份 Markdown 圣经改为**从实体库确定性渲染**，供 prompt 注入，接口对外不变。

本模块只负责「存储 + 核验 + 渲染」，不调用任何 LLM，因此可被完整单测覆盖。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "Record",
    "KIND_CHARACTER",
    "KIND_RELATION",
    "KIND_EVENT",
    "KIND_SETTING",
    "STATUS_CONFIRMED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "entities_db_path",
    "connect",
    "verify_records",
    "upsert_records",
    "list_records",
    "delete_record",
    "revoke_unsupported",
    "stats",
    "render_bible",
    "next_source_seq",
    "get_meta",
    "set_meta",
    "has_data",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

KIND_CHARACTER = "character"
KIND_RELATION = "relation"
KIND_EVENT = "event"
KIND_SETTING = "setting"

_KINDS = (KIND_CHARACTER, KIND_RELATION, KIND_EVENT, KIND_SETTING)

# confirmed：人名与引文都在正文中逐字命中，可作为正典注入 prompt。
# pending  ：人名命中但引文只部分命中——可能是 LLM 转述而非照抄，降级为「待确认」，
#            仍渲染但明确标注，不作为确定事实。
# rejected ：人名或引文在正文中查无实据 = 幻觉，永不渲染、永不注入。
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"

_STATUS_RANK = {STATUS_REJECTED: 0, STATUS_PENDING: 1, STATUS_CONFIRMED: 2}

_DB_NAME = "entities.db"

# 引文核验参数
_MIN_EVIDENCE_CHARS = 6      # 归一化后短于此的引文视为无效证据
_FUZZY_WINDOW = 8            # 模糊匹配的滑窗长度（汉字数）
_FUZZY_HIT_RATIO = 0.6       # 滑窗命中率达标即判 pending（LLM 转述/微调标点）

# 渲染时每节最多列出的条目数，避免 prompt 爆炸
_RENDER_LIMIT = {
    KIND_CHARACTER: 60,
    KIND_RELATION: 80,
    KIND_EVENT: 60,
    KIND_SETTING: 40,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    key          TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    subject      TEXT NOT NULL DEFAULT '',
    object       TEXT NOT NULL DEFAULT '',
    rel_type     TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '',
    source_file  TEXT NOT NULL DEFAULT '',
    source_seq   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    confidence   REAL NOT NULL DEFAULT 0,
    aliases      TEXT NOT NULL DEFAULT '[]',
    history      TEXT NOT NULL DEFAULT '[]',
    first_seen   REAL NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind, status);
CREATE INDEX IF NOT EXISTS idx_entities_seq  ON entities(source_seq);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Record:
    """一条待入库的实体事实。

    ``evidence`` 是**唯一的信任来源**：核验闸门只认它能否在正文里找到。
    """

    kind: str
    name: str = ""
    subject: str = ""
    object: str = ""
    rel_type: str = ""
    detail: str = ""
    evidence: str = ""
    source_file: str = ""
    source_seq: int = 0
    aliases: list[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    confidence: float = 0.0
    reject_reason: str = ""

    # -- 派生 ---------------------------------------------------------------

    @property
    def key(self) -> str:
        """归一化实体键：同一实体的多次抽取必须落到同一个 key 才能 upsert。"""
        if self.kind == KIND_RELATION:
            a, b = _norm(self.subject), _norm(self.object)
            # 关系无向化：A|B 与 B|A 视为同一条，避免同一对人物存两份互相打架
            return "|".join(sorted([a, b]))
        return _norm(self.name)

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.key}"

    def display(self) -> str:
        if self.kind == KIND_RELATION:
            return f"{self.subject} ↔ {self.object}"
        return self.name


# --------------------------------------------------------------------------- #
# 文本归一化与证据核验（纯确定性，无 LLM）
# --------------------------------------------------------------------------- #

_NON_CONTENT = re.compile(r"\W+", re.UNICODE)


def _norm(s: str) -> str:
    """归一化：剥掉所有标点、空白、换行，只保留文字内容本身。

    这样"「他抬手按住书页」"与"他抬手，按住书页。"能等价匹配——LLM 复述引文时
    改动标点是常态，但**改动文字**就说明它在编。
    """
    if not s:
        return ""
    return _NON_CONTENT.sub("", str(s))


def _fuzzy_hit_ratio(needle: str, haystack: str) -> float:
    """滑窗命中率：把 needle 切成定长片段，看多少比例能在 haystack 找到。

    用于区分「LLM 照抄原文」「LLM 转述原文」「LLM 凭空捏造」三种情况。
    """
    if not needle or not haystack:
        return 0.0
    if len(needle) <= _FUZZY_WINDOW:
        return 1.0 if needle in haystack else 0.0
    windows = [
        needle[i:i + _FUZZY_WINDOW]
        for i in range(0, len(needle) - _FUZZY_WINDOW + 1, max(1, _FUZZY_WINDOW // 2))
    ]
    if not windows:
        return 0.0
    hits = sum(1 for w in windows if w in haystack)
    return hits / len(windows)


def _names_of(rec: Record) -> list[str]:
    """该条目声称存在的所有专名（人物名 / 关系双方 / 别名）。"""
    names: list[str] = []
    if rec.kind == KIND_RELATION:
        names.extend([rec.subject, rec.object])
    elif rec.kind == KIND_CHARACTER:
        names.append(rec.name)
        names.extend(rec.aliases or [])
    return [n for n in names if n and n.strip()]


def verify_records(
    records: Iterable[Record],
    *,
    source_text: str,
    full_text: str = "",
) -> list[Record]:
    """**核验闸门**：逐条判定 confirmed / pending / rejected。

    这是根治幻觉的核心。判定顺序（任一步失败即 rejected）：

    1. **专名核验**：人物名、关系双方必须在全书正文里逐字出现。凭空捏造的人名
       （如正文 0 命中的"李慕白""沈清音"）在这一步就被砍掉。
    2. **引文核验**：evidence 必须能在**该条目所出自的那批正文**里命中。
       - 完整命中 → confirmed
       - 滑窗命中率 ≥ 阈值 → pending（LLM 转述，可用但标注待确认）
       - 命中率过低 / 引文过短 → rejected

    Args:
        records: 待核验条目。
        source_text: 该批条目所依据的正文原文（抽取时喂给 LLM 的那一批）。
        full_text: 全书正文，用于专名核验。缺省时退化为只用 source_text。

    Returns:
        同一批 records（原地标注 status/confidence/reject_reason 后返回）。
    """
    src = _norm(source_text)
    full = _norm(full_text) if full_text else src

    out: list[Record] = []
    for rec in records:
        if rec.kind not in _KINDS:
            rec.status = STATUS_REJECTED
            rec.reject_reason = f"未知实体类型 {rec.kind}"
            out.append(rec)
            continue

        # --- 1) 专名核验：正文里查无此人 = 幻觉 ---------------------------
        bogus = [n for n in _names_of(rec) if _norm(n) and _norm(n) not in full]
        if bogus:
            rec.status = STATUS_REJECTED
            rec.confidence = 0.0
            rec.reject_reason = "正文中不存在该专名：" + "、".join(bogus)
            out.append(rec)
            continue

        # 关系/人物必须有名字，否则无从核验
        if rec.kind in (KIND_CHARACTER, KIND_RELATION) and not _names_of(rec):
            rec.status = STATUS_REJECTED
            rec.reject_reason = "缺少人物名"
            out.append(rec)
            continue

        # --- 2) 引文核验 --------------------------------------------------
        ev = _norm(rec.evidence)
        if len(ev) < _MIN_EVIDENCE_CHARS:
            rec.status = STATUS_REJECTED
            rec.confidence = 0.0
            rec.reject_reason = "缺少有效原文引证"
            out.append(rec)
            continue

        if ev in src:
            rec.status = STATUS_CONFIRMED
            rec.confidence = 1.0
            rec.reject_reason = ""
        else:
            ratio = _fuzzy_hit_ratio(ev, src)
            if ratio >= _FUZZY_HIT_RATIO:
                rec.status = STATUS_PENDING
                rec.confidence = round(ratio, 3)
                rec.reject_reason = ""
            else:
                rec.status = STATUS_REJECTED
                rec.confidence = round(ratio, 3)
                rec.reject_reason = f"原文中找不到该引证（命中率 {ratio:.0%}）"
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


def entities_db_path(bible_dir: Path) -> Path:
    return Path(bible_dir) / _DB_NAME


def connect(bible_dir: Path) -> sqlite3.Connection:
    """打开（必要时创建）实体库。调用方负责 close。"""
    p = entities_db_path(bible_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def set_meta(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (k, str(v)),
    )
    conn.commit()


def next_source_seq(conn: sqlite3.Connection) -> int:
    """下一个可用的来源序号。序号越大代表章节越靠后 / 抽取越新。

    增量更新据此判断"新章节的描写优先于旧数据"，无需 LLM 裁决。
    """
    row = conn.execute("SELECT MAX(source_seq) AS m FROM entities").fetchone()
    return int((row["m"] or 0)) + 1


def upsert_records(
    conn: sqlite3.Connection,
    records: Iterable[Record],
    *,
    keep_rejected: bool = False,
) -> dict[str, int]:
    """**确定性合并**：按实体 key upsert，冲突留痕，不调用任何 LLM。

    合并规则（完全可预测，这正是取代"LLM 整篇重写"的关键）：

    - 新条目 source_seq **≥** 旧条目 → 覆盖 detail / rel_type / evidence，
      若关键字段发生变化则把旧值追加进 history（保留"曾经是敌、后来结盟"这类演变）。
    - 新条目 source_seq **<** 旧条目 → 只补空字段，绝不覆盖更新的描写。
    - status 取二者较优（confirmed > pending）。
    - rejected 条目默认**不入库**（keep_rejected=True 时入库以便审计）。

    Returns: {"inserted": n, "updated": n, "rejected": n}
    """
    now = time.time()
    counts = {"inserted": 0, "updated": 0, "rejected": 0}

    for rec in records:
        if rec.status == STATUS_REJECTED:
            counts["rejected"] += 1
            if not keep_rejected:
                logger.info(
                    "圣经核验拦截幻觉条目 [%s] %s：%s",
                    rec.kind, rec.display() or rec.detail[:20], rec.reject_reason,
                )
                continue
        if not rec.key:
            counts["rejected"] += 1
            continue

        row = conn.execute(
            "SELECT * FROM entities WHERE id = ?", (rec.id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """INSERT INTO entities
                   (id, kind, key, name, subject, object, rel_type, detail, evidence,
                    source_file, source_seq, status, confidence, aliases, history,
                    first_seen, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.id, rec.kind, rec.key, rec.name, rec.subject, rec.object,
                    rec.rel_type, rec.detail, rec.evidence, rec.source_file,
                    rec.source_seq, rec.status, rec.confidence,
                    json.dumps(rec.aliases or [], ensure_ascii=False), "[]",
                    now, now,
                ),
            )
            counts["inserted"] += 1
            continue

        newer = rec.source_seq >= int(row["source_seq"] or 0)
        history = json.loads(row["history"] or "[]")

        if newer:
            # 关键字段变更留痕：关系类型翻转是最需要被看见的演变
            if rec.rel_type and rec.rel_type != (row["rel_type"] or ""):
                history.append({
                    "at": now,
                    "field": "rel_type",
                    "from": row["rel_type"],
                    "to": rec.rel_type,
                    "seq": rec.source_seq,
                })
            merged = {
                "name": rec.name or row["name"],
                "subject": rec.subject or row["subject"],
                "object": rec.object or row["object"],
                "rel_type": rec.rel_type or row["rel_type"],
                "detail": rec.detail or row["detail"],
                "evidence": rec.evidence or row["evidence"],
                "source_file": rec.source_file or row["source_file"],
                "source_seq": max(rec.source_seq, int(row["source_seq"] or 0)),
            }
        else:
            # 旧批次只补空缺，不得回退更新的事实
            merged = {
                "name": row["name"] or rec.name,
                "subject": row["subject"] or rec.subject,
                "object": row["object"] or rec.object,
                "rel_type": row["rel_type"] or rec.rel_type,
                "detail": row["detail"] or rec.detail,
                "evidence": row["evidence"] or rec.evidence,
                "source_file": row["source_file"] or rec.source_file,
                "source_seq": int(row["source_seq"] or 0),
            }

        status = (
            rec.status
            if _STATUS_RANK.get(rec.status, 0) > _STATUS_RANK.get(row["status"], 0)
            else row["status"]
        )
        aliases = sorted(set(json.loads(row["aliases"] or "[]")) | set(rec.aliases or []))

        conn.execute(
            """UPDATE entities SET name=?, subject=?, object=?, rel_type=?, detail=?,
                   evidence=?, source_file=?, source_seq=?, status=?, confidence=?,
                   aliases=?, history=?, last_updated=?
               WHERE id=?""",
            (
                merged["name"], merged["subject"], merged["object"], merged["rel_type"],
                merged["detail"], merged["evidence"], merged["source_file"],
                merged["source_seq"], status, max(rec.confidence, float(row["confidence"] or 0)),
                json.dumps(aliases, ensure_ascii=False),
                json.dumps(history, ensure_ascii=False)[:8000],
                now, rec.id,
            ),
        )
        counts["updated"] += 1

    conn.commit()
    return counts


def list_records(
    conn: sqlite3.Connection,
    *,
    kind: Optional[str] = None,
    statuses: Iterable[str] = (STATUS_CONFIRMED, STATUS_PENDING),
    limit: int = 0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM entities WHERE status IN (%s)" % ",".join(
        "?" for _ in tuple(statuses)
    )
    params: list = list(statuses)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY source_seq ASC, id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def delete_record(conn: sqlite3.Connection, record_id: str) -> bool:
    """精确删除一条实体——这是 v1 散文圣经做不到的（只能整篇重写）。"""
    cur = conn.execute("DELETE FROM entities WHERE id = ?", (record_id,))
    conn.commit()
    return cur.rowcount > 0


def revoke_unsupported(conn: sqlite3.Connection, full_text: str) -> list[str]:
    """全库复核：把正文里已查无实据的条目降为 rejected，返回被撤销的条目 id。

    用途有二：
    1. 从 v1 散文圣经迁移过来的旧数据，用当前正文重新过一遍闸门；
    2. 用户删改了正文（某人物被写没了）后，圣经能**跟着收缩**——v1 永远只增不减。
    """
    full = _norm(full_text)
    revoked: list[str] = []
    for row in conn.execute(
        "SELECT * FROM entities WHERE status != ?", (STATUS_REJECTED,)
    ).fetchall():
        names: list[str] = []
        if row["kind"] == KIND_RELATION:
            names = [row["subject"], row["object"]]
        elif row["kind"] == KIND_CHARACTER:
            names = [row["name"], *json.loads(row["aliases"] or "[]")]
        bogus = [n for n in names if n and _norm(n) and _norm(n) not in full]
        if bogus:
            conn.execute(
                "UPDATE entities SET status=?, last_updated=? WHERE id=?",
                (STATUS_REJECTED, time.time(), row["id"]),
            )
            revoked.append(row["id"])
    conn.commit()
    return revoked


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in conn.execute(
        "SELECT kind, status, COUNT(*) AS c FROM entities GROUP BY kind, status"
    ):
        out[f"{row['kind']}.{row['status']}"] = int(row["c"])
    total = conn.execute("SELECT COUNT(*) AS c FROM entities").fetchone()
    out["total"] = int(total["c"])
    return out


# --------------------------------------------------------------------------- #
# 渲染成四份圣经 Markdown（确定性，不经 LLM）
# --------------------------------------------------------------------------- #

_BIBLE_BANNER = (
    "<!-- deskmate-bible: v2 structured; 由 .deskmate/entities.db 确定性渲染，"
    "请勿手工编辑本文件（会在下次刷新时被覆盖）。每条事实均附正文引证。 -->"
)


def _clip(s: str, n: int = 60) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _evidence_note(row: sqlite3.Row) -> str:
    ev = _clip(row["evidence"], 50)
    src = row["source_file"] or ""
    mark = "" if row["status"] == STATUS_CONFIRMED else "【待确认】"
    parts = [p for p in (mark, f"依据：「{ev}」" if ev else "", f"（{src}）" if src else "") if p]
    return " ".join(parts)


def render_bible(conn: sqlite3.Connection) -> dict[str, str]:
    """把实体库渲染成 CHARACTERS / OUTLINE / TIMELINE / NOTES 四份 Markdown。

    与 v1 的本质区别：这里**没有任何生成**，纯粹是把已核验的事实排版输出。
    LLM 无法在这一步引入新东西。
    """
    chars = list_records(conn, kind=KIND_CHARACTER, limit=_RENDER_LIMIT[KIND_CHARACTER])
    rels = list_records(conn, kind=KIND_RELATION, limit=_RENDER_LIMIT[KIND_RELATION])
    events = list_records(conn, kind=KIND_EVENT, limit=_RENDER_LIMIT[KIND_EVENT])
    settings = list_records(conn, kind=KIND_SETTING, limit=_RENDER_LIMIT[KIND_SETTING])

    pending: list[sqlite3.Row] = [
        r for r in (*chars, *rels, *events, *settings) if r["status"] == STATUS_PENDING
    ]

    # ---- CHARACTERS ----
    lines = [_BIBLE_BANNER, "## CHARACTERS", "", "### 人物"]
    if chars:
        for r in chars:
            alias = json.loads(r["aliases"] or "[]")
            alias_s = f"（又称 {'、'.join(alias)}）" if alias else ""
            lines.append(f"- **{r['name']}**{alias_s}：{_clip(r['detail'], 80) or '—'}")
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
    else:
        lines.append("- （无已核验人物）")

    lines += ["", "### 关系"]
    if rels:
        for r in rels:
            rt = r["rel_type"] or "其他"
            # 两端人名**分别**加粗：圆桌用 `**([^*]+?)**` 正则从圣经里捞人名做全书
            # 检索兜底，若把整行包成一个加粗块，捞到的会是"甲 —[敌]→ 乙"这种非人名。
            lines.append(
                f"- **{r['subject']}** —[{rt}]→ **{r['object']}**："
                f"{_clip(r['detail'], 80) or '—'}"
            )
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
            hist = json.loads(r["history"] or "[]")
            for h in hist[-2:]:
                if h.get("field") == "rel_type":
                    lines.append(
                        f"  - 关系演变：{h.get('from') or '（原未定）'} → {h.get('to')}"
                    )
    else:
        lines.append("- （无已核验关系）")
    characters_md = "\n".join(lines)

    # ---- OUTLINE ----
    lines = [_BIBLE_BANNER, "## OUTLINE", ""]
    if events:
        for r in events:
            lines.append(f"- {_clip(r['name'] or r['detail'], 90)}")
            if r["detail"] and r["name"]:
                lines.append(f"  - {_clip(r['detail'], 100)}")
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
    else:
        lines.append("- （无已核验情节节点）")
    outline_md = "\n".join(lines)

    # ---- TIMELINE ----
    lines = [_BIBLE_BANNER, "## TIMELINE", ""]
    if events:
        lines.append("按正文出现顺序排列（序号即抽取批次，不代表故事内绝对纪年）：")
        lines.append("")
        for i, r in enumerate(events, 1):
            src = f"（{r['source_file']}）" if r["source_file"] else ""
            lines.append(f"{i}. {_clip(r['name'] or r['detail'], 80)} {src}")
    else:
        lines.append("- （无已核验事件）")
    timeline_md = "\n".join(lines)

    # ---- NOTES ----
    lines = [_BIBLE_BANNER, "## NOTES", "", "### 世界观 / 势力 / 地点"]
    if settings:
        for r in settings:
            lines.append(f"- **{r['name']}**：{_clip(r['detail'], 90) or '—'}")
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
    else:
        lines.append("- （无已核验设定）")

    lines += ["", "### 待确认（引文未逐字命中，不可作为确定事实）"]
    if pending:
        for r in pending[:30]:
            lines.append(f"- [{r['kind']}] {_clip(r['name'] or r['detail'], 70)}")
    else:
        lines.append("- （无）")

    st = stats(conn)
    lines += [
        "",
        "### 核验统计",
        f"- 已核验条目：{sum(v for k, v in st.items() if k.endswith('.' + STATUS_CONFIRMED))}",
        f"- 待确认条目：{sum(v for k, v in st.items() if k.endswith('.' + STATUS_PENDING))}",
        "- 说明：凡正文中查无实据的人物 / 关系已在入库前被自动剔除，不会出现在本圣经中。",
    ]
    notes_md = "\n".join(lines)

    return {
        "CHARACTERS": characters_md,
        "OUTLINE": outline_md,
        "TIMELINE": timeline_md,
        "NOTES": notes_md,
    }


def has_data(bible_dir: Path) -> bool:
    """实体库是否已存在且有已核验数据（决定 read_bible 走 v2 还是回退 v1）。"""
    p = entities_db_path(bible_dir)
    if not p.is_file():
        return False
    try:
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM entities WHERE status != ?", (STATUS_REJECTED,)
        ).fetchone()
        conn.close()
        return int(row["c"]) > 0
    except Exception:
        return False
