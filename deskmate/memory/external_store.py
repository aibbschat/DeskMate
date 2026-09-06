"""ExternalStore — 外部信息摄入存储层（不可信区，与内部记忆物理隔离）。

收口所有从外部世界拉取的信息（RSS / 邮件 / API / 网页 / 手动录入），
作为小桔决策的「原料库」。与内部记忆（memories / vault / learned_history）
**物理同库但语义隔离**：外部信息永远是 ``ingestion_status < 'digested'`` 的
原始/候选数据，不经过认知层评估不会升格为内部记忆。

设计要点（呼应「数据库必须可拓展」的长期约束）：
- **单一物理库**：与认知层 / 日记 / learned_history 共用 ``deskmate.db``，
  备份是同一原子单元；加新来源类型只是多一行 ``source_type``，零 DDL 变更。
- **不可信隔离**：外部信息带 ``credibility``（来源可信度）+ ``ingestion_status``
  （raw → summarized → embedded → digested → archived），只有 ``digested``
  及以上的条目才允许被认知层引用为决策依据。
- **去重幂等**：``content_hash``（归一化后 md5）唯一索引，同一信息重复抓取
  天然幂等（INSERT OR IGNORE）。
- **TTL 过期**：``expires_at`` 为空 = 永不过期；非空则后台清理任务定期归档。
- **fail-open**：任何 DB 异常都不向上抛，保证主聊天链路不被外部信息存储拖垮。

表：
- ``external_sources``  订阅源（RSS / email / api / webpage / manual）
- ``external_items``    外部信息条目（含 embedding BLOB，延迟生成）
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Optional

_DEFAULT_DIR = Path.home() / ".deskmate"
_DB_NAME = "deskmate.db"

# ingestion_status 状态机：raw（原始）→ summarized（已摘要）→ embedded（已向量化）
# → digested（已消化，可被认知层引用）→ archived（已归档/过期）
STATUS_RAW = "raw"
STATUS_SUMMARIZED = "summarized"
STATUS_EMBEDDED = "embedded"
STATUS_DIGESTED = "digested"
STATUS_ARCHIVED = "archived"

# 来源类型枚举（开放，新类型直接传字符串即可，无需改 DDL）
SOURCE_RSS = "rss"
SOURCE_EMAIL = "email"
SOURCE_API = "api"
SOURCE_WEBPAGE = "webpage"
SOURCE_MANUAL = "manual"

# ---------------------------------------------------------------------------
# 默认 TTL（天）——外部信息必须有过期时间，否则「去年的信息今年还用来决定」。
# 萧哥 2026-09-06 拍板：按来源分档。
#
# 规则优先级（从最具体到最宽泛）：
#   1. 订阅源自己的 default_ttl_days（手动为某个源单独指定时）
#   2. 类别 _TTL_BY_CATEGORY（news/finance 这类时效性强的，压到 7 天）
#   3. 来源类型 _TTL_BY_SOURCE
#   4. _FALLBACK_TTL_DAYS（未知类型按最保守处理）
#
# None = 永不过期。只有「手动录入」是 None——那是萧哥自己存的东西，
# 属于「我的东西」，不该被时效性规则误杀。
# ---------------------------------------------------------------------------
_TTL_BY_SOURCE: dict[str, Optional[int]] = {
    SOURCE_RSS: 30,
    SOURCE_WEBPAGE: 30,
    SOURCE_API: 90,
    SOURCE_EMAIL: 90,
    SOURCE_MANUAL: None,
}
_TTL_BY_CATEGORY: dict[str, Optional[int]] = {
    "news": 7,
    "finance": 7,
    "tech": 30,
}
_FALLBACK_TTL_DAYS: Optional[int] = 30

# 可信度闸门：低于此值的来源，其信息不会自动升格为内部记忆（走提议卡等人裁决）。
DEFAULT_TRUST_THRESHOLD = 0.7


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _content_hash(text: str) -> str:
    """内容指纹：忽略空白差异，用于去重。与 learned_store._fingerprint 同思路。"""
    return hashlib.md5(re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()


def _new_id(prefix: str = "ext") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _expires_at_from_days(days: Optional[int]) -> Optional[str]:
    """把「N 天后过期」换算成 ISO 时间戳。days 为 None/<=0 返回 None（永不过期）。"""
    if days is None:
        return None
    days = int(days)
    if days <= 0:
        return None
    return (
        datetime.datetime.now().astimezone()
        + datetime.timedelta(days=days)
    ).isoformat(timespec="seconds")


def resolve_ttl_days(
    source_type: str = SOURCE_MANUAL,
    category: str = "",
    source_ttl: Optional[int] = None,
) -> Optional[int]:
    """按「源配置 > 类别 > 来源类型 > 兜底」的优先级解析默认 TTL（天）。

    返回 None 表示永不过期。这是外部信息过期机制的单一决策点——
    任何写入路径都该走这里，而不是让调用方自己（忘记）传 expires_at。
    """
    # 1. 订阅源显式指定的最优先
    if source_ttl is not None:
        try:
            v = int(source_ttl)
            return v if v > 0 else None
        except (TypeError, ValueError):
            pass
    # 2. 类别（news/finance 这类时效性强，压短）
    cat = (category or "").strip().lower()
    if cat in _TTL_BY_CATEGORY:
        return _TTL_BY_CATEGORY[cat]
    # 3. 来源类型
    st = (source_type or "").strip().lower()
    if st in _TTL_BY_SOURCE:
        return _TTL_BY_SOURCE[st]
    # 4. 兜底：未知类型按最保守
    return _FALLBACK_TTL_DAYS


class ExternalStore:
    """外部信息摄入存储层。短连接 + WAL（与 LearnedStore / BlobStore 同模式）。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base = Path(base_dir) if base_dir else _DEFAULT_DIR
        self.db_path = self.base / _DB_NAME
        self.base.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # 连接 / schema
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _add_column(self, table: str, col: str, col_type: str) -> None:
        """兼容已存在的库：缺失列才补（SQLite 无 ADD COLUMN IF NOT EXISTS）。"""
        try:
            with self._connect() as conn:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.Error:
            pass

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            # ---- 订阅源 ----
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_sources (
                    id                    TEXT PRIMARY KEY,
                    name                  TEXT NOT NULL,
                    type                  TEXT NOT NULL DEFAULT 'manual',
                    url                   TEXT DEFAULT '',
                    config_json           TEXT DEFAULT '{}',
                    enabled               INTEGER NOT NULL DEFAULT 1,
                    credibility           REAL NOT NULL DEFAULT 0.5,
                    fetch_interval_minutes INTEGER NOT NULL DEFAULT 60,
                    last_fetched_at      TEXT,
                    last_fetch_error      TEXT,
                    created_at            TEXT NOT NULL,
                    updated_at            TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sources_enabled "
                "ON external_sources(enabled, type)"
            )
            # 兼容已存在的库：补 default_ttl_days 列（SQLite 无 ADD COLUMN IF NOT EXISTS）
            self._add_column("external_sources", "default_ttl_days", "INTEGER")

            # ---- 外部信息条目 ----
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS external_items (
                    id                    TEXT PRIMARY KEY,
                    source_id             TEXT,
                    source_type           TEXT DEFAULT 'manual',
                    source_url            TEXT DEFAULT '',
                    title                 TEXT DEFAULT '',
                    content               TEXT NOT NULL,
                    summary               TEXT DEFAULT '',
                    content_hash          TEXT NOT NULL,
                    published_at          TEXT,
                    fetched_at            TEXT NOT NULL,
                    expires_at            TEXT,
                    credibility           REAL NOT NULL DEFAULT 0.5,
                    category              TEXT DEFAULT 'other',
                    tags_json             TEXT DEFAULT '[]',
                    embedding             BLOB,
                    embedding_model       TEXT DEFAULT '',
                    embedding_dim         INTEGER DEFAULT 0,
                    ingestion_status      TEXT NOT NULL DEFAULT 'raw',
                    related_memory_ids_json TEXT DEFAULT '[]',
                    extra_json            TEXT DEFAULT '{}',
                    created_at            TEXT NOT NULL,
                    updated_at            TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES external_sources(id) ON DELETE SET NULL
                )
                """
            )
            # 去重：同一 content_hash 只存一条（INSERT OR IGNORE 幂等）
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_items_hash "
                "ON external_items(content_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_source "
                "ON external_items(source_id, fetched_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_status "
                "ON external_items(ingestion_status, fetched_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_expires "
                "ON external_items(expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_category "
                "ON external_items(category, fetched_at DESC)"
            )
            # 有向量且未归档的条目（语义检索的候选集）
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_items_embeddable "
                "ON external_items(ingestion_status) WHERE embedding IS NOT NULL"
            )

    # ------------------------------------------------------------------
    # 订阅源管理
    # ------------------------------------------------------------------
    def add_source(
        self,
        name: str,
        source_type: str = SOURCE_MANUAL,
        url: str = "",
        *,
        config: Optional[dict] = None,
        credibility: float = 0.5,
        fetch_interval_minutes: int = 60,
        enabled: bool = True,
        default_ttl_days: Optional[int] = None,
    ) -> Optional[str]:
        """新增订阅源，返回 source_id。失败返回 None（fail-open）。

        default_ttl_days: 该来源的默认过期天数（覆盖类型分档）。
            None = 不过期；不给值则按类别 > 来源类型的分档自动算。
        """
        sid = _new_id("src")
        now = _now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO external_sources"
                    "(id, name, type, url, config_json, enabled, credibility, "
                    "fetch_interval_minutes, default_ttl_days, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sid, name.strip(), source_type, url.strip(),
                        json.dumps(config or {}, ensure_ascii=False),
                        1 if enabled else 0,
                        max(0.0, min(1.0, credibility)),
                        max(1, int(fetch_interval_minutes)),
                        default_ttl_days,
                        now, now,
                    ),
                )
            return sid
        except sqlite3.Error:
            return None

    def get_source(self, source_id: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM external_sources WHERE id=?", (source_id,)
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._source_row_to_dict(row) if row else None

    def list_sources(self, *, enabled_only: bool = False, source_type: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM external_sources WHERE 1=1"
        params: list[Any] = []
        if enabled_only:
            query += " AND enabled=1"
        if source_type:
            query += " AND type=?"
            params.append(source_type)
        query += " ORDER BY created_at DESC"
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        return [self._source_row_to_dict(r) for r in rows]

    def update_source(self, source_id: str, **fields) -> bool:
        """更新订阅源字段（name / url / config / credibility / enabled / fetch_interval_minutes / default_ttl_days / last_fetched_at / last_fetch_error）。"""
        allowed = {
            "name", "url", "config", "credibility", "enabled",
            "fetch_interval_minutes", "default_ttl_days",
            "last_fetched_at", "last_fetch_error",
        }
        sets: list[str] = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "config":
                sets.append("config_json=?")
                params.append(json.dumps(v or {}, ensure_ascii=False))
            elif k == "enabled":
                sets.append("enabled=?")
                params.append(1 if v else 0)
            elif k == "credibility":
                sets.append("credibility=?")
                params.append(max(0.0, min(1.0, float(v))))
            else:
                sets.append(f"{k}=?")
                params.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_now_iso())
        params.append(source_id)
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE external_sources SET {', '.join(sets)} WHERE id=?",
                    params,
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def delete_source(self, source_id: str) -> bool:
        """删除订阅源（关联条目保留，source_id 置 NULL）。"""
        try:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM external_sources WHERE id=?", (source_id,))
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 外部信息条目写入
    # ------------------------------------------------------------------
    def add_item(
        self,
        content: str,
        *,
        source_id: Optional[str] = None,
        source_type: str = SOURCE_MANUAL,
        source_url: str = "",
        title: str = "",
        summary: str = "",
        published_at: Optional[str] = None,
        expires_at: Optional[str] = None,
        credibility: Optional[float] = None,
        category: str = "other",
        tags: Optional[list[str]] = None,
        extra: Optional[dict] = None,
    ) -> Optional[dict]:
        """写入一条外部信息。重复（同 content_hash）返回已有条目，不重复插入。

        返回写入/已存在的条目 dict；失败返回 None。
        credibility 缺省时取来源可信度（source_id 对应源的 credibility）。
        """
        content = (content or "").strip()
        if not content:
            return None
        chash = _content_hash(content)

        # 先查是否已存在（去重幂等）
        existing = self.get_item_by_hash(chash)
        if existing:
            return existing

        # 解析来源可信度 + source_type（source_id 存在时，缺省字段从来源记录补）
        cred = credibility
        src = None
        if source_id:
            src = self.get_source(source_id)
            if src:
                if cred is None:
                    cred = src.get("credibility", 0.5)
                if not source_type or source_type == SOURCE_MANUAL:
                    # 调用方未显式指定 source_type（或用了默认值），从来源记录补
                    source_type = src.get("type", source_type)
        if cred is None:
            cred = 0.5

        # 过期时间：调用方没传就自动算分档 TTL。
        # 这是「外部信息必须有过期机制」的落地点——不能指望每个调用方都记得传，
        # 漏传就等于永不过期，去年的信息今年还会被拿来决策。
        if expires_at is None:
            src_ttl = src.get("default_ttl_days") if src else None
            ttl_days = resolve_ttl_days(source_type, category, src_ttl)
            expires_at = _expires_at_from_days(ttl_days)

        iid = _new_id("item")
        now = _now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO external_items"
                    "(id, source_id, source_type, source_url, title, content, summary, "
                    "content_hash, published_at, fetched_at, expires_at, credibility, "
                    "category, tags_json, ingestion_status, extra_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        iid, source_id, source_type, source_url.strip(),
                        title.strip(), content, summary.strip(), chash,
                        published_at, now, expires_at,
                        max(0.0, min(1.0, cred)),
                        category,
                        json.dumps(tags or [], ensure_ascii=False),
                        STATUS_RAW,
                        json.dumps(extra or {}, ensure_ascii=False),
                        now, now,
                    ),
                )
        except sqlite3.Error:
            return None

        # INSERT OR IGNORE 可能因并发命中已有行，重新查一次确保返回正确条目
        item = self.get_item_by_hash(chash)
        return item or self.get_item(iid)

    def add_items_batch(self, items: list[dict]) -> dict:
        """批量写入外部信息。items 每项字段同 add_item。

        返回 {"inserted": n, "duplicates": n, "failed": n, "ids": [...]}。
        """
        result = {"inserted": 0, "duplicates": 0, "failed": 0, "ids": []}
        for it in items:
            try:
                added = self.add_item(
                    it.get("content", ""),
                    source_id=it.get("source_id"),
                    source_type=it.get("source_type", SOURCE_MANUAL),
                    source_url=it.get("source_url", ""),
                    title=it.get("title", ""),
                    summary=it.get("summary", ""),
                    published_at=it.get("published_at"),
                    expires_at=it.get("expires_at"),
                    credibility=it.get("credibility"),
                    category=it.get("category", "other"),
                    tags=it.get("tags"),
                    extra=it.get("extra"),
                )
                if added:
                    # 判断是新增还是重复：fetched_at 与当前时间差 < 2 秒视为新增
                    result["ids"].append(added["id"])
                    result["inserted"] += 1
                else:
                    result["failed"] += 1
            except Exception:
                result["failed"] += 1
        return result

    # ------------------------------------------------------------------
    # 外部信息条目读取
    # ------------------------------------------------------------------
    def get_item(self, item_id: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM external_items WHERE id=?", (item_id,)
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._item_row_to_dict(row) if row else None

    def get_item_by_hash(self, content_hash: str) -> Optional[dict]:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM external_items WHERE content_hash=?", (content_hash,)
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._item_row_to_dict(row) if row else None

    def list_items(
        self,
        *,
        source_id: Optional[str] = None,
        source_type: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
        include_archived: bool = False,
        only_embeddable: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """列出外部信息条目，按 fetched_at 倒序。"""
        query = "SELECT * FROM external_items WHERE 1=1"
        params: list[Any] = []
        if source_id:
            query += " AND source_id=?"
            params.append(source_id)
        if source_type:
            query += " AND source_type=?"
            params.append(source_type)
        if category:
            query += " AND category=?"
            params.append(category)
        if status:
            query += " AND ingestion_status=?"
            params.append(status)
        if not include_archived:
            query += " AND ingestion_status != ?"
            params.append(STATUS_ARCHIVED)
        if only_embeddable:
            query += " AND embedding IS NOT NULL"
        # 不过期过滤：expires_at 为空或晚于当前时间
        query += " AND (expires_at IS NULL OR expires_at >= ?)"
        params.append(_now_iso())
        query += " ORDER BY fetched_at DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(int(limit), 500)))
        params.append(max(0, int(offset)))
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        return [self._item_row_to_dict(r) for r in rows]

    def search_items_keyword(self, query: str, *, limit: int = 20, trusted_only: bool = True) -> list[dict]:
        """关键词检索（LIKE 子串匹配，title + content + summary）。

        这是语义检索不可用时的兜底路径。中文按子串匹配，英文按大小写不敏感。

        trusted_only（默认 True）= 可信闸门，只返回 ``digested``（已验证）条目；
        传 False 可检索不可信区（仅供调试/人工翻查，不应进对话上下文）。
        """
        query = (query or "").strip()
        if not query:
            return []
        # 拆关键词：空格分隔，中文整体作为一个关键词
        keywords = [kw for kw in re.split(r"\s+", query) if kw]
        if not keywords:
            return []
        where_parts = []
        params: list[Any] = []
        for kw in keywords:
            where_parts.append("(title LIKE ? OR content LIKE ? OR summary LIKE ?)")
            like = f"%{kw}%"
            params.extend([like, like, like])
        where = " AND ".join(where_parts)
        # 可信闸门（默认只放行已验证）+ 不过期
        if trusted_only:
            where += " AND ingestion_status = ?"
            params.append(STATUS_DIGESTED)
        else:
            where += " AND ingestion_status != ?"
            params.append(STATUS_ARCHIVED)
        where += " AND (expires_at IS NULL OR expires_at >= ?)"
        params.append(_now_iso())
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM external_items WHERE {where} "
                    f"ORDER BY fetched_at DESC LIMIT ?",
                    params + [max(1, min(int(limit), 100))],
                ).fetchall()
        except sqlite3.Error:
            return []
        return [self._item_row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 条目状态更新（ingestion_status 推进 / embedding 写入 / 摘要 / 关联记忆）
    # ------------------------------------------------------------------
    def update_status(self, item_id: str, status: str) -> bool:
        """推进 ingestion_status（raw → summarized → embedded → digested → archived）。"""
        valid = {STATUS_RAW, STATUS_SUMMARIZED, STATUS_EMBEDDED, STATUS_DIGESTED, STATUS_ARCHIVED}
        if status not in valid:
            return False
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE external_items SET ingestion_status=?, updated_at=? WHERE id=?",
                    (status, _now_iso(), item_id),
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def update_summary(self, item_id: str, summary: str) -> bool:
        """写入 AI 摘要，并推进状态到 summarized。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE external_items SET summary=?, ingestion_status=?, updated_at=? "
                    "WHERE id=? AND ingestion_status IN (?, ?)",
                    (summary.strip(), STATUS_SUMMARIZED, _now_iso(),
                     item_id, STATUS_RAW, STATUS_SUMMARIZED),
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def update_embedding(self, item_id: str, embedding: list[float], model: str = "") -> bool:
        """写入向量（BLOB），并推进状态到 embedded。

        embedding 是 float 列表，序列化为 array.array('f') 的二进制。
        """
        if not embedding:
            return False
        import array
        blob = array.array("f", [float(v) for v in embedding]).tobytes()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE external_items SET embedding=?, embedding_model=?, embedding_dim=?, "
                    "ingestion_status=?, updated_at=? WHERE id=?",
                    (blob, model, len(embedding), STATUS_EMBEDDED, _now_iso(), item_id),
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def mark_digested(self, item_id: str, related_memory_ids: Optional[list[str]] = None) -> bool:
        """标记为已消化（可被认知层引用为决策依据），并记录关联的内部记忆 ID。"""
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE external_items SET ingestion_status=?, related_memory_ids_json=?, "
                    "updated_at=? WHERE id=?",
                    (
                        STATUS_DIGESTED,
                        json.dumps(related_memory_ids or [], ensure_ascii=False),
                        _now_iso(), item_id,
                    ),
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def update_category_tags(self, item_id: str, *, category: Optional[str] = None, tags: Optional[list[str]] = None) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if category is not None:
            sets.append("category=?")
            params.append(category)
        if tags is not None:
            sets.append("tags_json=?")
            params.append(json.dumps(tags, ensure_ascii=False))
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(_now_iso())
        params.append(item_id)
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    f"UPDATE external_items SET {', '.join(sets)} WHERE id=?", params
                )
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------
    # 向量加载（供语义检索使用）
    # ------------------------------------------------------------------
    def load_embeddable_items(
        self,
        *,
        include_archived: bool = False,
        trusted_only: bool = True,
    ) -> list[dict]:
        """加载有向量、未过期的条目，供语义检索在内存中算余弦相似度。

        trusted_only（默认 True）就是**可信闸门**：只返回 ``digested``（已验证）
        的条目。raw / summarized / embedded 都属于不可信区，**不进检索结果**，
        物理上不可能影响小桔的判断。调试时才显式传 trusted_only=False。

        返回 [{id, title, content, summary, embedding(list[float]), credibility,
               fetched_at, category, source_type, expires_at}]。
        向量从 BLOB 反序列化为 float 列表。
        """
        import array
        query = (
            "SELECT id, title, content, summary, embedding, credibility, fetched_at, "
            "category, source_type, expires_at "
            "FROM external_items WHERE embedding IS NOT NULL"
        )
        params: list[Any] = []
        if trusted_only:
            # 可信闸门：只有验证过的才可被认知层引用
            query += " AND ingestion_status = ?"
            params.append(STATUS_DIGESTED)
        elif not include_archived:
            query += " AND ingestion_status != ?"
            params.append(STATUS_ARCHIVED)
        query += " AND (expires_at IS NULL OR expires_at >= ?)"
        params.append(_now_iso())
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            (iid, title, content, summary, blob, cred, fetched, cat, stype, exp) = r
            try:
                vec = list(array.array("f", blob))
            except Exception:
                continue
            out.append({
                "id": iid,
                "title": title or "",
                "content": content or "",
                "summary": summary or "",
                "embedding": vec,
                "credibility": float(cred or 0.5),
                "fetched_at": fetched or "",
                "category": cat or "other",
                "source_type": stype or "",
                "expires_at": exp or "",
            })
        return out

    # ------------------------------------------------------------------
    # TTL / 统计 / 清理
    # ------------------------------------------------------------------
    def archive_expired(self) -> int:
        """将过期条目标记为 archived（不删除，保留可追溯）。返回归档条数。"""
        now = _now_iso()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE external_items SET ingestion_status=?, updated_at=? "
                    "WHERE expires_at IS NOT NULL AND expires_at < ? AND ingestion_status != ?",
                    (STATUS_ARCHIVED, now, now, STATUS_ARCHIVED),
                )
            return cur.rowcount
        except sqlite3.Error:
            return 0

    def stats(self) -> dict:
        """总量 + 按状态 / 来源类型 / 分类统计。"""
        try:
            with self._connect() as conn:
                total = conn.execute("SELECT COUNT(*) FROM external_items").fetchone()[0]
                by_status = {
                    r[0]: r[1] for r in conn.execute(
                        "SELECT ingestion_status, COUNT(*) FROM external_items GROUP BY ingestion_status"
                    )
                }
                by_source_type = {
                    r[0]: r[1] for r in conn.execute(
                        "SELECT source_type, COUNT(*) FROM external_items GROUP BY source_type"
                    )
                }
                by_category = {
                    r[0]: r[1] for r in conn.execute(
                        "SELECT category, COUNT(*) FROM external_items "
                        "WHERE ingestion_status != ? GROUP BY category",
                        (STATUS_ARCHIVED,),
                    )
                }
                embeddable = conn.execute(
                    "SELECT COUNT(*) FROM external_items WHERE embedding IS NOT NULL"
                ).fetchone()[0]
                sources_count = conn.execute(
                    "SELECT COUNT(*) FROM external_sources WHERE enabled=1"
                ).fetchone()[0]
        except sqlite3.Error:
            return {"total": 0, "by_status": {}, "by_source_type": {}, "by_category": {},
                    "embeddable": 0, "enabled_sources": 0}
        return {
            "total": int(total),
            "by_status": by_status,
            "by_source_type": by_source_type,
            "by_category": by_category,
            "embeddable": int(embeddable),
            "enabled_sources": int(sources_count),
        }

    # ------------------------------------------------------------------
    # 行转 dict
    # ------------------------------------------------------------------
    @staticmethod
    def _source_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "url": row["url"] or "",
            "config": json.loads(row["config_json"] or "{}"),
            "enabled": bool(row["enabled"]),
            "credibility": float(row["credibility"]),
            "fetch_interval_minutes": int(row["fetch_interval_minutes"]),
            "default_ttl_days": row["default_ttl_days"],
            "last_fetched_at": row["last_fetched_at"] or "",
            "last_fetch_error": row["last_fetch_error"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _item_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "source_id": row["source_id"] or "",
            "source_type": row["source_type"] or "",
            "source_url": row["source_url"] or "",
            "title": row["title"] or "",
            "content": row["content"] or "",
            "summary": row["summary"] or "",
            "content_hash": row["content_hash"],
            "published_at": row["published_at"] or "",
            "fetched_at": row["fetched_at"],
            "expires_at": row["expires_at"] or "",
            "credibility": float(row["credibility"]),
            "category": row["category"] or "",
            "tags": json.loads(row["tags_json"] or "[]"),
            "has_embedding": row["embedding"] is not None,
            "embedding_model": row["embedding_model"] or "",
            "embedding_dim": int(row["embedding_dim"] or 0),
            "ingestion_status": row["ingestion_status"],
            "related_memory_ids": json.loads(row["related_memory_ids_json"] or "[]"),
            "extra": json.loads(row["extra_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
