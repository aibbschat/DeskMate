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
import threading
try:
    import fcntl  # Unix 文件锁（跨进程串行）；Windows 下不可用，降级为仅进程内锁
except ImportError:  # pragma: no cover - Windows 平台
    fcntl = None
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 跨进程可重入写锁：串行化所有圣经写路径（build 的 DELETE+upsert / incremental /
# 回流 / 审阅决议 / 删除），避免并发任务交错造成「清完库又被旧批次 upsert 进空库」
# 或并发 inject_bible 发布损坏的 md。读路径（read_bible / render_bible）不加锁。
#
# 为什么是文件锁而非 threading.Lock：圣经写路径分布在多个 OS 进程——sidecar 主进程
# （app.py 的 after_action 回流、build_bible）与 novel_memory MCP server（独立进程，
# 有自己的写工具）。threading.Lock 只管进程内，两个进程同时写同一本 entities.db 时
# 完全无效；SQLite 自身写锁能防物理损坏，但防不住「增量抽完 → build 清库 → 增量写进
# 空库」这类逻辑交错。故用 fcntl.flock 锁 ~/.deskmate/write.lock 覆盖跨进程。
# 进程内嵌套（build_bible 临界区里再调 upsert_records）由 threading.RLock 兜底。


class _BibleWriteLock:
    """跨进程可重入写锁：fcntl.flock（跨进程）+ threading.RLock（进程内嵌套）。

    进程内用 RLock 串行，支持 build_bible 临界区内再调 upsert_records 的嵌套
    （同一进程同一线程重复进入只计深度，不会自锁）；跨进程用 fcntl.flock 串行，
    覆盖 sidecar 主进程与 novel_memory MCP server 两个独立进程对同一本
    entities.db 的并发写。

    锁文件不可用时（权限/磁盘故障/Windows 无 fcntl）降级为仅进程内 RLock，
    fail-open——绝不因锁故障卡死圣经读写（任务执行时圣经读写必须 fail-open）。
    """

    def __init__(self, lock_file: Path):
        self._file = Path(lock_file)
        self._thread = threading.RLock()
        self._fd = None
        self._depth = 0

    def __enter__(self):
        self._thread.acquire()
        try:
            if self._depth == 0 and self._fd is None:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                self._fd = open(self._file, "a+")
                if fcntl is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)
        except OSError:
            # 锁文件不可用：降级为无文件锁（仅靠进程内 RLock），fail-open
            if self._fd is not None:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None
            logger.warning("圣经写锁(%s)获取失败，降级为进程内锁", self._file)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
                self._fd.close()
            except OSError:
                pass
            self._fd = None
        self._thread.release()


# 全局跨进程写锁实例：所有圣经写路径共享同一把锁文件 ~/.deskmate/write.lock，
# 因此 sidecar 主进程与 novel_memory MCP server 两个独立进程天然串行化。
_WRITE_LOCK = _BibleWriteLock(Path.home() / ".deskmate" / "write.lock")

__all__ = [
    "Record",
    "KIND_CHARACTER",
    "KIND_RELATION",
    "KIND_EVENT",
    "KIND_SETTING",
    "ROLE_NARRATIVE",
    "ROLE_ALLUSION",
    "TIER_MANUSCRIPT",
    "TIER_EXTERNAL",
    "TIER_ROUNDTABLE",
    "TIER_DIALOGUE",
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
    "list_needs_review",
    "review_resolve",
    "review_report",
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

# 角色类别：区分「本作叙事角色」与「仅在对白/旁白中被提及或引用的历史/典故人物」。
# 后者（allusion）并非本作出场人物，只是角色聊天时举的例子（如嬴政、王安石），
# 必须明确标注，避免被圆桌/编辑专家误当作本作角色展开讨论或编造其剧情。
ROLE_NARRATIVE = "narrative"   # 本作出场/叙事角色（默认）
ROLE_ALLUSION = "allusion"     # 对话/典故中提及引用，非本作角色

_KINDS = (KIND_CHARACTER, KIND_RELATION, KIND_EVENT, KIND_SETTING)

# confirmed：人名与引文都在正文中逐字命中，可作为正典注入 prompt。
# pending  ：人名命中但引文只部分命中——可能是 LLM 转述而非照抄，降级为「待确认」，
#            仍渲染但明确标注，不作为确定事实。
# rejected ：人名或引文在正文中查无实据 = 幻觉，永不渲染、永不注入。
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"
STATUS_REJECTED = "rejected"

_STATUS_RANK = {STATUS_REJECTED: 0, STATUS_PENDING: 1, STATUS_CONFIRMED: 2}

# 信任分层（tier）：标明每条事实的「出身」，下游据此决定能否盲目采信。
# manuscript ：来自书稿正文（build_bible 抽取），经 verify_records 闸口，最可信。
# external   ：外部史料核验标注（预留，当前未启用自动写入）。
# roundtable ：圆桌综合报告回流（经 upsert_bible_from_text，tier=roundtable）。
# dialogue   ：对话 / 产出文本回流（A 闭环，经 upsert_bible_from_text，tier=dialogue）。
# 信任排序由高到低：manuscript > external > roundtable > dialogue。
# 非 manuscript 的条目一律 needs_review=1，需作者人工确认后才作确定事实，避免「一错全信」。
TIER_MANUSCRIPT = "manuscript"
TIER_EXTERNAL = "external"
TIER_ROUNDTABLE = "roundtable"
TIER_DIALOGUE = "dialogue"

_TIER_RANK = {
    TIER_MANUSCRIPT: 4,
    TIER_EXTERNAL: 3,
    TIER_ROUNDTABLE: 2,
    TIER_DIALOGUE: 1,
}
_TIER_LABEL = {
    TIER_MANUSCRIPT: "书稿直引",
    TIER_EXTERNAL: "外部史料",
    TIER_ROUNDTABLE: "圆桌共识",
    TIER_DIALOGUE: "对话推断",
}

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
    role_category TEXT NOT NULL DEFAULT 'narrative',
    history      TEXT NOT NULL DEFAULT '[]',
    tier         TEXT NOT NULL DEFAULT 'manuscript',
    needs_review INTEGER NOT NULL DEFAULT 0,
    author_confirmed INTEGER NOT NULL DEFAULT 0,
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
    role_category: str = ROLE_NARRATIVE
    tier: str = TIER_MANUSCRIPT
    needs_review: bool = False
    author_confirmed: bool = False
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


def _safe_int(v, default: int = 0) -> int:
    """把可能是脏值（如被错位写入的文件名字符串）安全转成 int，避免单条坏数据
    炸掉整库写入。无法解析时回退 default。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return default


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


def _schema_columns(schema: str, table: str = "entities") -> dict[str, str]:
    """从建表 DDL 里解析出 {列名: 列定义}，供存量库补齐。

    只取 CREATE TABLE 内的顶层列定义（按括号深度切分，跳过嵌套），
    返回顺序即 DDL 书写顺序。
    """
    m = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?%s\s*\((.*?)\)\s*;" % re.escape(table),
        schema,
        re.S | re.I,
    )
    if not m:
        return {}
    body = m.group(1)
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))

    cols: dict[str, str] = {}
    for raw in parts:
        s = " ".join(raw.split())
        if not s:
            continue
        # 跳过表级约束（PRIMARY KEY(...)、UNIQUE(...) 等），它们不是列
        head = s.split()[0].upper()
        if head in ("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"):
            continue
        name = s.split()[0].strip("`\"'")
        cols[name] = s[len(s.split()[0]):].strip()
    return cols


def _ensure_columns(conn: sqlite3.Connection, table: str = "entities") -> list[str]:
    """把 _SCHEMA 里声明的列全部补齐到存量库，返回本次新增的列名。

    根治「迁移漏列」：以前每加一列都要手写一段 if，漏一个就炸
    （本次事故：tier / needs_review 从未被补进老库）。
    SQLite 无 ADD COLUMN IF NOT EXISTS，只能逐列比对 PRAGMA 后 ALTER。

    约束：NOT NULL 列必须带非 NULL 默认值（_SCHEMA 里都是这么写的）；
    主键 / UNIQUE 列跳过（SQLite 不允许 ALTER 追加），这类列老库本就有。
    """
    declared = _schema_columns(_SCHEMA, table)
    if not declared:
        return []
    have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
    added: list[str] = []
    for name, ddl in declared.items():
        if name in have:
            continue
        if "PRIMARY KEY" in ddl.upper() or ddl.upper().startswith("UNIQUE"):
            continue  # SQLite 不支持 ALTER 追加主键/唯一约束
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
        added.append(name)
    return added


def connect(bible_dir: Path) -> sqlite3.Connection:
    """打开（必要时创建）实体库。调用方负责 close。"""
    p = entities_db_path(bible_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # 存量库迁移：把 _SCHEMA 声明的列全部补齐（tier / needs_review /
    # role_category / author_confirmed 及以后新增的列都走这条通用路径）
    added = _ensure_columns(conn, "entities")
    if added:
        conn.commit()
        logger.info("entities 存量库补齐列: %s", ", ".join(added))
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


def _db_file_of(conn: sqlite3.Connection) -> str | None:
    """取主库文件路径（用于快照回滚）。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _snapshot_db(conn: sqlite3.Connection, keep: int = 5) -> None:
    """upsert / revoke 前对实体库做一次安全快照（VACUUM INTO），出错可回滚。

    加固的第一原则：加固动作本身不能成为新的崩溃源。任何异常只记日志，
    绝不拖累主流程（任务执行时圣经读写必须 fail-open）。
    """
    try:
        db_file = _db_file_of(conn)
        if not db_file:
            return
        db_path = Path(db_file)
        snap_dir = db_path.parent / "_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        # 同毫秒内多次快照可能撞名 → 加 uuid 后缀防覆盖
        snap_path = snap_dir / f"{db_path.stem}-{stamp}-{uuid.uuid4().hex[:8]}.db"
        conn.execute("VACUUM INTO ?", (str(snap_path),))
        snaps = sorted(
            snap_dir.glob(f"{db_path.stem}-*.db"),
            key=lambda p: p.stat().st_mtime,
        )
        for old in snaps[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        logger.exception("圣经快照失败（不影响主流程）")


def upsert_records(
    conn: sqlite3.Connection,
    records: Iterable[Record],
    *,
    keep_rejected: bool = False,
) -> dict[str, int]:
    """**确定性合并**：按实体 key upsert，冲突留痕，不调用任何 LLM。

    进程级写锁串行化：避免并发的 build / 增量 / 回流交错写库。
    """
    with _WRITE_LOCK:
        return _upsert_records_locked(conn, records, keep_rejected=keep_rejected)


def _upsert_records_locked(
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
    _snapshot_db(conn)
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
            nr = (
                1
                if (rec.tier != TIER_MANUSCRIPT or rec.status != STATUS_CONFIRMED or rec.needs_review)
                else 0
            )
            conn.execute(
                """INSERT INTO entities
                   (id, kind, key, name, subject, object, rel_type, detail, evidence,
                    source_file, source_seq, status, confidence, aliases,
                    role_category, history, tier, needs_review, author_confirmed,
                    first_seen, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.id, rec.kind, rec.key, rec.name, rec.subject, rec.object,
                    rec.rel_type, rec.detail, rec.evidence, rec.source_file,
                    rec.source_seq, rec.status, rec.confidence,
                    json.dumps(rec.aliases or [], ensure_ascii=False),
                    rec.role_category or ROLE_NARRATIVE, "[]",
                    rec.tier, nr, 1 if rec.author_confirmed else 0,
                    now, now,
                ),
            )
            counts["inserted"] += 1
            continue

        # P0-3：作者显式驳回的条目（status=rejected）不被后续回流复活。
        # 作者已否决的事实，除非重新被显式确认，否则保持 rejected——
        # 永不渲染、永不注入（与插入路径拦截幻觉的语义一致）。
        # 设计取舍（Hermes 复检认可）：作者否决 > 一切，即使书稿正文后续真的重写
        # 到该人物，upsert 也永远跳过；若驳回误判，目前只能手工改 DB 翻转 status。
        if row["status"] == STATUS_REJECTED:
            continue

        newer = rec.source_seq >= _safe_int(row["source_seq"])
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
                "source_seq": max(rec.source_seq, _safe_int(row["source_seq"])),
                # 角色类别是稳定属性：新批次给出则以新为准，否则保留旧值
                "role_category": rec.role_category or row["role_category"]
                or ROLE_NARRATIVE,
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
                "source_seq": _safe_int(row["source_seq"]),
                "role_category": row["role_category"] or rec.role_category
                or ROLE_NARRATIVE,
            }

        status = (
            rec.status
            if _STATUS_RANK.get(rec.status, 0) > _STATUS_RANK.get(row["status"], 0)
            else row["status"]
        )
        aliases = sorted(set(json.loads(row["aliases"] or "[]")) | set(rec.aliases or []))

        # 信任分层合并：保留较高信任的出身（manuscript > external > roundtable > dialogue）
        merged_tier = (
            rec.tier
            if _TIER_RANK.get(rec.tier, 0) >= _TIER_RANK.get(row["tier"], 0)
            else row["tier"]
        )
        # 需审阅 = 任一方标记 / 非书稿直引 / 未确认
        # P0-2：作者已显式确认（author_confirmed）的条目，即使出身非书稿直引，
        # 也不再被打回「待作者确认」队列——否则「批准 → 回流 → 又待审」无限循环。
        merged_needs_review = (
            1
            if (rec.needs_review or bool(row["needs_review"])
                or merged_tier != TIER_MANUSCRIPT or status != STATUS_CONFIRMED)
            else 0
        )
        if bool(row["author_confirmed"]):
            # 作者确认优先：不被后续回流降级回 needs_review
            merged_needs_review = 0

        conn.execute(
            """UPDATE entities SET name=?, subject=?, object=?, rel_type=?, detail=?,
                   evidence=?, source_file=?, source_seq=?, status=?, confidence=?,
                   aliases=?, role_category=?, tier=?, needs_review=?, author_confirmed=?,
                   history=?, last_updated=?
               WHERE id=?""",
            (
                merged["name"], merged["subject"], merged["object"], merged["rel_type"],
                merged["detail"], merged["evidence"], merged["source_file"],
                merged["source_seq"], status,
                max(rec.confidence, float(row["confidence"] or 0)),
                json.dumps(aliases, ensure_ascii=False),
                merged["role_category"], merged_tier, merged_needs_review,
                bool(row["author_confirmed"]),
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
    with _WRITE_LOCK:
        cur = conn.execute("DELETE FROM entities WHERE id = ?", (record_id,))
        conn.commit()
        return cur.rowcount > 0


def revoke_unsupported(conn: sqlite3.Connection, full_text: str) -> list[str]:
    """全库复核：把正文里已查无实据的条目降为 rejected，返回被撤销的条目 id。

    用途有二：
    1. 从 v1 散文圣经迁移过来的旧数据，用当前正文重新过一遍闸门；
    2. 用户删改了正文（某人物被写没了）后，圣经能**跟着收缩**——v1 永远只增不减。

    P1-5 防护在调用侧（incremental / upsert）：verify_text 骤短（暂读不到正文）
    时不调用本函数，避免把真实人物误杀为 rejected。
    """
    with _WRITE_LOCK:
        full = _norm(full_text)
        _snapshot_db(conn)
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
# 作者审阅回路（信任分层落地：非书稿直引的条目需人工确认）
# --------------------------------------------------------------------------- #


def list_needs_review(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """列出所有需作者人工确认的条目（非书稿直引 或 未确认）。"""
    return list(conn.execute(
        "SELECT * FROM entities WHERE needs_review = 1 AND status != ? "
        "ORDER BY tier, kind, id",
        (STATUS_REJECTED,),
    ).fetchall())


def review_resolve(conn: sqlite3.Connection, rid: str, accept: bool) -> bool:
    """作者审阅决议。

    - accept=True：标记**已确认**（status=confirmed, needs_review=0,
      author_confirmed=1）。author_confirmed 使后续回流不再把它打回「待作者确认」
      队列，彻底收敛审阅（P0-2：原先只置 needs_review=0，下次回流因
      status!=confirmed / tier!=manuscript 又被置回，无限循环）。
    - accept=False：作者显式驳回（status=rejected）。rejected 条目不被后续回流
      复活（P0-3，见 upsert 更新分支）。

    返回是否命中并更新。条目不存在时返回 False（fail-open，不拖累主流程）。
    """
    with _WRITE_LOCK:
        row = conn.execute("SELECT * FROM entities WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return False
        if accept:
            conn.execute(
                "UPDATE entities SET status=?, needs_review=0, author_confirmed=1, "
                "last_updated=? WHERE id=?",
                (STATUS_CONFIRMED, time.time(), rid),
            )
        else:
            conn.execute(
                "UPDATE entities SET status=?, needs_review=0, last_updated=? WHERE id=?",
                (STATUS_REJECTED, time.time(), rid),
            )
        conn.commit()
    return True


def review_report(conn: sqlite3.Connection) -> str:
    """生成「圣经审阅」报告 Markdown，列出待作者确认的条目供人工批阅。"""
    rows = list_needs_review(conn)
    lines = [
        "<!-- deskmate-bible-review: 由实体库确定性生成，列出需作者人工确认的条目 -->",
        "## 圣经审阅（待作者确认）",
        "",
        f"共 {len(rows)} 条需确认。非「书稿直引」的条目来自对话 / 圆桌回流，"
        "请核对史实后再标记确认或撤销。",
        "",
    ]
    for r in rows:
        tier = r["tier"] or TIER_MANUSCRIPT
        label = _TIER_LABEL.get(tier, tier)
        lines.append(
            f"- **[{label}] {r['name'] or r['detail']}** "
            f"（{r['kind']} · id=`{r['id']}`）"
        )
        if r["detail"]:
            lines.append(f"  - {_clip(r['detail'], 120)}")
        note = _evidence_note(r)
        if note:
            lines.append(f"  - {note}")
    if not rows:
        lines.append("- （无待确认条目，圣经均为书稿直引且已确认）")
    return "\n".join(lines)


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
    tier = row["tier"] or TIER_MANUSCRIPT
    # P0-2：作者已确认的条目（author_confirmed），即使出身非书稿直引也不再标「待作者确认」。
    needs = (
        bool(row["needs_review"])
        or row["status"] != STATUS_CONFIRMED
        or (tier != TIER_MANUSCRIPT and not bool(row["author_confirmed"]))
    )
    marks: list[str] = []
    if tier != TIER_MANUSCRIPT:
        marks.append(f"【{_TIER_LABEL.get(tier, tier)}】")
    if needs:
        marks.append("【待作者确认】")
    parts = [p for p in (*marks, f"依据：「{ev}」" if ev else "", f"（{src}）" if src else "") if p]
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
    # 按角色类别分两组渲染：本作叙事角色（narrative）与对话/典故提及的历史人物
    # （allusion，非本作角色）。后者明确标注，避免被圆桌/编辑专家误当作本作人物。
    lines = [_BIBLE_BANNER, "## CHARACTERS", ""]
    narrative = [
        r for r in chars
        if (r["role_category"] or ROLE_NARRATIVE) != ROLE_ALLUSION
    ]
    allusion = [
        r for r in chars
        if (r["role_category"] or ROLE_NARRATIVE) == ROLE_ALLUSION
    ]

    lines.append("### 人物（本作角色）")
    if narrative:
        for r in narrative:
            alias = json.loads(r["aliases"] or "[]")
            alias_s = f"（又称 {'、'.join(alias)}）" if alias else ""
            lines.append(f"- **{r['name']}**{alias_s}：{_clip(r['detail'], 80) or '—'}")
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
    else:
        lines.append("- （无已核验人物）")

    lines.append("")
    lines.append("### 对话／典故中提及的历史人物（非本作角色）")
    if allusion:
        lines.append(
            "（以下仅为角色对白中举例或引用的历史人物，并非本作出场角色，"
            "请勿将其作为本作人物展开分析或编造其情节。）"
        )
        for r in allusion:
            alias = json.loads(r["aliases"] or "[]")
            alias_s = f"（又称 {'、'.join(alias)}）" if alias else ""
            lines.append(f"- **{r['name']}**{alias_s}：{_clip(r['detail'], 80) or '—'}")
            note = _evidence_note(r)
            if note:
                lines.append(f"  - {note}")
    else:
        lines.append("- （无）")

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
