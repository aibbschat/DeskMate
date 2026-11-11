"""记忆文件层索引（三段检索）：切块 / 持久化 / 增量更新 / 向量。

为什么单独建索引
================
现网检索是「每次查询现读文件 + 纯 Python 子串扫描」：
- ``vault_store.search`` 逐文件读 534 个 md；
- ``manager._search_*`` 每次重读 MEMORY.md / USER.md；
- daily diary 只被 ``user_fact_context`` 覆盖最近 7 天、≤80 字的行。

本模块把记忆文件（USER.md / MEMORY.md / diary_memory.md / diary/*.md / vault/）
切成 chunk，落到 ``~/.deskmate/deskmate.db``（与认知层同库，不碰 entities.db），
带 embedding BLOB 与 FTS5 词表，供 ``file_search`` 走向量 + 关键词 + 锚融合。

增量口径
========
- 以 ``(mtime_ns, size)`` 做零读盘快路径；变化才读文件。
- 内容 hash 未变 → 只更新 stat，不重嵌。
- 内容变了 → 按 chunk 粒度比 hash，只重嵌变化块；消失的块删除。
- 新写入的数据无需改各写入方：文件 mtime 变化即失效信号，下一次检索前
  ``sync()`` 就会重建该文件，通常 < 1s。

fail-open
=========
任何异常不向上抛：索引不可用 → 调用方回落旧关键词路径；embedding 失败 →
chunk 与词表照常写入（embedding 留空），下次 ``sync()/embed_pending()`` 补齐。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 单条 chunk 上限（字符）。试过 300：长日记不再稀释，但 chunk 数涨到 1923、
# 噪声条目变多，同集 R@5 从 85.0% 降到 81.7%，故保留 600（数据见验收报告）。
MAX_CHUNK_CHARS = 600
# USER.md 的 bullet 通常很短；过长的（一条里塞了多个事实，如书稿路径）按句切，
# 否则「稿子在哪」这类问法会被整条长 bullet 稀释。
MAX_BULLET_CHARS = 220
# 日记视图按条切块（四段 old-02）：一条日记常是整段情绪叙述（300~400 字），
# 按 600 字整体编码时，用户问的「合同废了」只占 1/10，向量被情绪词稀释。
# 只对 diary_view 收紧到 260 字（按句切、不跨句），不动全局 600 —— 全局降
# 到 300 曾让 R@5 从 85.0% 掉到 81.7%（见验收报告）。
DIARY_VIEW_CHUNK_CHARS = 260
#: 切块算法版本；改任何切块参数都要 bump（老索引会自动重建）。
CHUNK_VERSION = "2026-11-11-diary260-v1"
# 首次全量 / 单次 sync 的 embedding 批大小
EMBED_BATCH = 64

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[a-z0-9][a-z0-9_.\-]{1,}")

# 功能词 bigram：查询里大量出现但不携带检索意图（「你记」「什么」「可以」）。
# 索引与查询两侧都过滤，保证口径一致。
_STOP_BIGRAMS = {
    "什么", "怎么", "怎样", "哪个", "哪些", "哪儿", "哪里", "这个", "那个",
    "这些", "那些", "可以", "应该", "是否", "一下", "我们", "你们", "他们",
    "自己", "现在", "时候", "的话", "一个", "没有", "不是", "就是", "还是",
    "因为", "所以", "但是", "如果", "然后", "之后", "之前", "以及", "关于",
    "告诉", "记得", "还记", "你记", "我记", "他记",
}
_STOP_CHARS = set("的了是我你他她它在和与就都也很吗呢吧啊呀哦这那哪谁")

# 索引来源：全部相对 deskmate base
_SOURCE_USER = "USER.md"
_SOURCE_MEMORY = "MEMORY.md"
_SOURCE_DIARY_VIEW = "diary_memory.md"
_SOURCE_DIARY_DIR = "diary"
_SOURCE_VAULT_DIR = "vault"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _norm(text: str) -> str:
    return "".join((text or "").split())


def tokenize(text: str) -> list[str]:
    """CJK 2-gram + 拉丁词。

    SQLite FTS5 的 unicode61 不切中文，整段会被当一个 token。这里把中文按
    2-gram 展开成独立 token，查询侧同样展开后用 OR 检索——与
    ``manager._extract_keywords`` / ``vault_store.search`` 的 bigram 口径一致。
    """
    t = (text or "").lower()
    out: set[str] = set()
    for run in _CJK_RE.findall(t):
        if len(run) == 1:
            if run not in _STOP_CHARS:
                out.add(run)
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg in _STOP_BIGRAMS:
                continue
            # 两字都是功能词（「的里」「我的」）没有检索价值
            if bg[0] in _STOP_CHARS and bg[1] in _STOP_CHARS:
                continue
            out.add(bg)
    out.update(_LATIN_RE.findall(t))
    return sorted(x for x in out if x.strip())


def _split_long(text: str, limit: int) -> list[str]:
    """把超长文本按句号/换行切到 limit 以内（尽量不腰斩）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    buf = ""
    for piece in re.split(r"(?<=[。！？!?；;\n])", text):
        if not piece:
            continue
        if len(buf) + len(piece) <= limit:
            buf += piece
        else:
            if buf.strip():
                parts.append(buf.strip())
            if len(piece) > limit:
                for i in range(0, len(piece), limit):
                    parts.append(piece[i : i + limit].strip())
                buf = ""
            else:
                buf = piece
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


# ---------------------------------------------------------------------------
# 切块
# ---------------------------------------------------------------------------
def _bullet_chunks(body: str, limit: int = MAX_BULLET_CHARS) -> list[str]:
    """把 ``- `` bullet 聚成块（续行并入上一条）。"""
    chunks: list[str] = []
    cur = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("- ", "· ", "* ")):
            if cur:
                chunks.extend(_split_long(cur, limit))
            cur = s
        elif cur:
            cur += "\n" + s
        else:
            cur = s
    if cur:
        chunks.extend(_split_long(cur, limit))
    return chunks


def _paragraph_chunks(body: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """按空行分段；过短段落合并，过长再切。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        if not buf:
            buf = p
        elif len(buf) + len(p) + 2 <= limit:
            buf += "\n\n" + p
        else:
            out.extend(_split_long(buf, limit))
            buf = p
    if buf:
        out.extend(_split_long(buf, limit))
    return out


def _chunk_user(text: str) -> list[tuple[str, str]]:
    """USER.md：``##`` 节 → bullet 一条一块；非 bullet 节按段。"""
    chunks: list[tuple[str, str]] = []
    sections = re.split(r"(?m)^##\s+", text or "")
    if sections and sections[0].strip():
        chunks.extend(("用户画像", c) for c in _paragraph_chunks(sections[0]))
    for sec in sections[1:]:
        lines = sec.splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        body = "\n".join(lines[1:])
        if re.search(r"(?m)^\s*[-*·]\s+", body):
            chunks.extend((title, c) for c in _bullet_chunks(body))
        else:
            chunks.extend((title, c) for c in _paragraph_chunks(body))
    return chunks


def _chunk_memory(text: str) -> list[tuple[str, str]]:
    """MEMORY.md：``§ 标题`` 为节，``- `` 条目一块。"""
    chunks: list[tuple[str, str]] = []
    section = "MEMORY"
    cur = ""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("§ "):
            if cur:
                chunks.extend((section, c) for c in _split_long(cur, MAX_CHUNK_CHARS))
                cur = ""
            section = s[2:].strip()
        elif s.startswith("- "):
            if cur:
                chunks.extend((section, c) for c in _split_long(cur, MAX_CHUNK_CHARS))
            cur = s
        elif cur and s:
            cur += "\n" + s
    if cur:
        chunks.extend((section, c) for c in _split_long(cur, MAX_CHUNK_CHARS))
    return chunks


def _chunk_diary_view(text: str) -> list[tuple[str, str]]:
    """diary_memory.md：空行分隔的日期条目，一条一块。"""
    out: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", text or ""):
        b = block.strip()
        if not b or b.startswith("§ "):
            # § 标题行单独丢弃；它下面的日期条目在后续 block
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})", b)
        section = m.group(1) if m else "日记沉淀"
        out.extend((section, c) for c in _split_long(b, DIARY_VIEW_CHUNK_CHARS))
    return out


def _chunk_diary_day(text: str) -> list[tuple[str, str]]:
    """diary/YYYY-MM-DD.md：``## HH:MM`` 一块；剥离元数据行。"""
    out: list[tuple[str, str]] = []
    parts = re.split(r"(?m)^##\s+", text or "")
    for part in parts[1:]:
        lines = part.splitlines()
        if not lines:
            continue
        section = lines[0].strip()
        body_lines = []
        for ln in lines[1:]:
            s = ln.strip()
            if not s:
                continue
            if s.startswith(("- 默契", "> ", "# ")):
                continue
            body_lines.append(s)
        body = "\n".join(body_lines)
        out.extend((section, c) for c in _split_long(body, MAX_CHUNK_CHARS))
    return out


def _chunk_vault(text: str) -> list[tuple[str, str]]:
    """vault：去 frontmatter，按段聚合。"""
    body = text or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]
    return [("", c) for c in _paragraph_chunks(body)]


def chunk_file(kind: str, text: str) -> list[tuple[str, str]]:
    if kind == "user":
        return _chunk_user(text)
    if kind == "memory":
        return _chunk_memory(text)
    if kind == "diary_view":
        return _chunk_diary_view(text)
    if kind == "diary_day":
        return _chunk_diary_day(text)
    if kind == "vault":
        return _chunk_vault(text)
    return [("", c) for c in _paragraph_chunks(text)]


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
class FileIndex:
    """记忆文件索引。短连接 + WAL，与 ExternalStore / DiaryStore 同模式。"""

    def __init__(
        self, base_dir: Optional[Path | str] = None, read_only: bool = False
    ) -> None:
        from deskmate.paths import deskmate_base_dir

        self.base = Path(base_dir) if base_dir else deskmate_base_dir()
        self.db_path = self.base / "deskmate.db"
        self.read_only = read_only
        if read_only:
            # 只读实例绝不建目录/建表/改 WAL：memory_search 是「检索权」，
            # 不能在用户目录里凭空造出一个 deskmate.db。
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
        else:
            self.base.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()
        self._matrix_cache: Optional[tuple[tuple[int, str], Any, list[str]]] = None
        self._lock = threading.Lock()

    # -- 连接 / schema ---------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, isolation_level=None
            )
        else:
            conn = sqlite3.connect(str(self.db_path), isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_chunks (
                    id              TEXT PRIMARY KEY,
                    source_kind     TEXT NOT NULL,
                    source_path     TEXT NOT NULL,
                    section         TEXT NOT NULL DEFAULT '',
                    chunk_no        INTEGER NOT NULL,
                    content         TEXT NOT NULL,
                    tokens          TEXT NOT NULL,
                    content_hash    TEXT NOT NULL,
                    embedding       BLOB,
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_dim   INTEGER NOT NULL DEFAULT 0,
                    updated_at      TEXT NOT NULL,
                    UNIQUE (source_path, chunk_no)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mchunks_kind "
                "ON memory_chunks(source_kind)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mchunks_path "
                "ON memory_chunks(source_path)"
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS memory_file_state (
                    source_path  TEXT PRIMARY KEY,
                    source_kind  TEXT NOT NULL,
                    mtime_ns     INTEGER NOT NULL,
                    size         INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    indexed_at   TEXT NOT NULL
                )
                """
            )
            # 独立 FTS 表（不挂 external content，避免触发器维护复杂度）
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5("
                "chunk_id UNINDEXED, tokens, tokenize='unicode61')"
            )
            # 切块算法版本（四段）：切块参数一变，老索引里的 chunk 边界就过期，
            # 但 mtime/size 没变 → sync 不会重切，检索会一直用旧边界（实测：
            # 日记 chunk 已改 260，老库仍是 600 的整块）。这里用版本号强制重建。
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_index_meta ("
                "k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT v FROM memory_index_meta WHERE k='chunk_version'"
            ).fetchone()
            if row is None or row[0] != CHUNK_VERSION:
                conn.execute("DELETE FROM memory_chunks")
                conn.execute("DELETE FROM memory_file_state")
                conn.execute("DELETE FROM memory_chunks_fts")
                conn.execute(
                    "INSERT INTO memory_index_meta(k, v) VALUES('chunk_version', ?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (CHUNK_VERSION,),
                )
                self._matrix_cache = None

    # -- 源文件枚举 ------------------------------------------------------
    def _iter_sources(self) -> Iterable[tuple[str, str, Path]]:
        """yield (source_kind, rel_path, abs_path)。"""
        pairs = [("user", _SOURCE_USER), ("memory", _SOURCE_MEMORY),
                 ("diary_view", _SOURCE_DIARY_VIEW)]
        for kind, rel in pairs:
            p = self.base / rel
            if p.is_file():
                yield kind, rel, p

        diary_dir = self.base / _SOURCE_DIARY_DIR
        if diary_dir.is_dir():
            for p in sorted(diary_dir.glob("*.md")):
                if ".bak" in p.name or ".sync-conflict" in p.name:
                    continue
                yield "diary_day", f"{_SOURCE_DIARY_DIR}/{p.name}", p

        vault_dir = self.base / _SOURCE_VAULT_DIR
        if vault_dir.is_dir():
            for p in sorted(vault_dir.rglob("*.md")):
                rel = p.relative_to(self.base).as_posix()
                if rel.startswith(f"{_SOURCE_VAULT_DIR}/archive/"):
                    continue
                if ".bak" in p.name or ".sync-conflict" in p.name:
                    continue
                yield "vault", rel, p

    # -- 增量同步 --------------------------------------------------------
    def sync(self, force: bool = False, max_files: Optional[int] = None) -> dict:
        """增量同步；返回统计。任何异常 fail-open 返回空统计。"""
        stats = {"scanned": 0, "changed": 0, "chunks": 0, "embedded": 0,
                 "skipped": 0, "removed": 0, "error": None, "ms": 0}
        t0 = datetime.datetime.now()
        try:
            with self._connect() as conn:
                states = {
                    r["source_path"]: r
                    for r in conn.execute("SELECT * FROM memory_file_state")
                }
                seen: set[str] = set()
                for kind, rel, path in self._iter_sources():
                    seen.add(rel)
                    stats["scanned"] += 1
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    old = states.get(rel)
                    if (not force and old and int(old["mtime_ns"]) == st.st_mtime_ns
                            and int(old["size"]) == st.st_size):
                        stats["skipped"] += 1
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    # 2026-11-11 复检修 TOCTOU：读完再取一次 stat，和读前不一致说明
                    # 文件在读取期间被改写（dream 旧路径就是非原子直写），这一轮
                    # 放弃记录 state——否则会把「旧内容 + 新 mtime」写进索引，
                    # 之后 sync 永远认为它没变，索引永久停在旧/半截内容。
                    try:
                        st2 = path.stat()
                    except OSError:
                        continue
                    if (st.st_mtime_ns, st.st_size) != (st2.st_mtime_ns, st2.st_size):
                        stats["skipped"] += 1
                        continue
                    st = st2
                    h = _sha1(text)
                    if not force and old and old["content_hash"] == h:
                        # 内容没变（touch / 重写同内容）：只更新 stat
                        conn.execute(
                            "UPDATE memory_file_state SET mtime_ns=?, size=?, indexed_at=? "
                            "WHERE source_path=?",
                            (st.st_mtime_ns, st.st_size, _now(), rel),
                        )
                        stats["skipped"] += 1
                        continue
                    if max_files is not None and stats["changed"] >= max_files:
                        continue
                    r = self._rebuild_one(conn, kind, rel, text, st)
                    stats["changed"] += 1
                    stats["chunks"] += r["chunks"]
                    stats["embedded"] += r["embedded"]
                # 源文件消失 → 删索引
                for rel in set(states) - seen:
                    ids = [r[0] for r in conn.execute(
                        "SELECT id FROM memory_chunks WHERE source_path=?", (rel,))]
                    self._delete_chunks(conn, ids)
                    conn.execute("DELETE FROM memory_file_state WHERE source_path=?", (rel,))
                    stats["removed"] += 1
        except Exception as exc:  # noqa: BLE001 —— fail-open
            stats["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("记忆文件索引同步失败（fail-open）：%s", stats["error"])
        stats["ms"] = int((datetime.datetime.now() - t0).total_seconds() * 1000)
        return stats

    def _delete_chunks(self, conn: sqlite3.Connection, ids: list[str]) -> None:
        if not ids:
            return
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM memory_chunks_fts WHERE chunk_id IN ({q})", ids)
        conn.execute(f"DELETE FROM memory_chunks WHERE id IN ({q})", ids)

    def _rebuild_one(
        self, conn: sqlite3.Connection, kind: str, rel: str, text: str,
        st: os.stat_result,
    ) -> dict:
        chunks = chunk_file(kind, text)
        content_hash = _sha1(text)
        old_rows = conn.execute(
            "SELECT chunk_no, content_hash, embedding FROM memory_chunks "
            "WHERE source_path=?",
            (rel,),
        ).fetchall()
        old = {int(r["chunk_no"]): r for r in old_rows}
        model = self._model_name()

        new_rows: list[tuple] = []
        pending: list[str] = []
        pending_idx: list[int] = []
        for i, (section, body) in enumerate(chunks):
            ch = _sha1(body)
            prev = old.get(i)
            embedding = None
            if prev and prev["content_hash"] == ch and prev["embedding"]:
                embedding = prev["embedding"]
            if embedding is None:
                pending.append(body)
                pending_idx.append(i)
            new_rows.append((i, section, body, ch, embedding))

        vectors: dict[int, Any] = {}
        if pending:
            vectors = self._embed(pending)
        embedded = 0
        for j, i in enumerate(pending_idx):
            if j in vectors:
                new_rows[i] = new_rows[i][:4] + (vectors[j],)
                embedded += 1

        ids = [self._chunk_id(rel, i) for i in range(len(chunks))]
        self._delete_chunks(conn, ids)
        now = _now()
        for i, (_no, section, body, ch, embedding) in enumerate(new_rows):
            cid = ids[i]
            dim = len(embedding) if embedding else 0
            conn.execute(
                "INSERT INTO memory_chunks(id, source_kind, source_path, section, "
                "chunk_no, content, tokens, content_hash, embedding, "
                "embedding_model, embedding_dim, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, kind, rel, section, i, body, " ".join(tokenize(body)), ch,
                 embedding, model if embedding else "", dim, now),
            )
            conn.execute(
                "INSERT INTO memory_chunks_fts(chunk_id, tokens) VALUES(?,?)",
                (cid, " ".join(tokenize(body))),
            )
        conn.execute(
            "INSERT INTO memory_file_state(source_path, source_kind, mtime_ns, size, "
            "content_hash, indexed_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(source_path) DO UPDATE SET source_kind=excluded.source_kind, "
            "mtime_ns=excluded.mtime_ns, size=excluded.size, "
            "content_hash=excluded.content_hash, indexed_at=excluded.indexed_at",
            (rel, kind, st.st_mtime_ns, st.st_size, content_hash, now),
        )
        return {"chunks": len(chunks), "embedded": embedded}

    # -- embedding -------------------------------------------------------
    @staticmethod
    def _model_name() -> str:
        try:
            from deskmate.memory import local_embed

            return local_embed.model_name()
        except Exception:
            return ""

    def _embed(self, texts: list[str]) -> dict[int, bytes]:
        """批量向量化；失败返回 {}（chunk 照常入库，留空待补）。"""
        from deskmate.memory import local_embed

        out: dict[int, bytes] = {}
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            vecs = local_embed.embed(batch)
            if not vecs:
                continue
            for off, vec in enumerate(vecs):
                if vec:
                    out[start + off] = _pack(vec)
        return out

    def embed_pending(self, limit: int = 200) -> int:
        """补齐未向量化的 chunk（失败或新增）。返回补齐条数。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, content FROM memory_chunks WHERE embedding IS NULL "
                "LIMIT ?", (limit,),
            ).fetchall()
            if not rows:
                return 0
            vecs = self._embed([r["content"] for r in rows])
            model = self._model_name()
            n = 0
            for i, row in enumerate(rows):
                if i not in vecs:
                    continue
                dim = len(vecs[i]) // 4
                conn.execute(
                    "UPDATE memory_chunks SET embedding=?, embedding_model=?, "
                    "embedding_dim=?, updated_at=? WHERE id=?",
                    (vecs[i], model, dim, _now(), row["id"]),
                )
                n += 1
        self._matrix_cache = None
        return n

    # -- 查询 ------------------------------------------------------------
    def search_keyword(self, query: str, limit: int = 30) -> list[dict]:
        toks = tokenize(query)
        if not toks:
            return []
        # 限制查询词数，避免超长 OR
        toks = toks[:48]
        match = " OR ".join(f'"{t}"' for t in toks)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT c.id, c.source_kind, c.source_path, c.section, "
                    "c.chunk_no, c.content, bm25(memory_chunks_fts) AS score "
                    "FROM memory_chunks_fts f "
                    "JOIN memory_chunks c ON c.id = f.chunk_id "
                    "WHERE memory_chunks_fts MATCH ? "
                    "ORDER BY score LIMIT ?",
                    (match, limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    def vector_search(self, qvec: list[float], limit: int = 30) -> list[dict]:
        """余弦检索。矩阵缓存按 (count, max(updated_at)) 失效。"""
        import numpy as np

        ids, mat, meta = self._load_matrix()
        if not ids or mat is None:
            return []
        q = np.asarray(qvec, dtype="float32")
        norms = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) + 1e-9)
        sims = (mat @ q) / (norms + 1e-9)
        top = np.argsort(-sims)[: max(1, int(limit))]
        out = []
        for i in top:
            m = meta[int(i)]
            out.append({
                "id": ids[int(i)], "score": float(sims[int(i)]),
                "source_kind": m["source_kind"], "source_path": m["source_path"],
                "section": m["section"], "chunk_no": m["chunk_no"],
                "content": m["content"],
            })
        return out

    def _load_matrix(self):
        import numpy as np

        with self._connect() as conn:
            head = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(MAX(updated_at),'') AS ts "
                "FROM memory_chunks WHERE embedding IS NOT NULL"
            ).fetchone()
            key = (int(head["n"]), head["ts"])
            if self._matrix_cache and self._matrix_cache[0] == key:
                return self._matrix_cache[1], self._matrix_cache[2], self._matrix_cache[3]
            rows = conn.execute(
                "SELECT id, source_kind, source_path, section, chunk_no, content, "
                "embedding, embedding_dim FROM memory_chunks "
                "WHERE embedding IS NOT NULL"
            ).fetchall()
        ids: list[str] = []
        vecs: list[Any] = []
        meta: list[dict] = []
        dim = 0
        for r in rows:
            arr = _unpack(r["embedding"], int(r["embedding_dim"] or 0))
            if arr is None or len(arr) == 0:
                continue
            if not dim:
                dim = len(arr)
            if len(arr) != dim:
                continue
            ids.append(r["id"])
            vecs.append(arr)
            meta.append({"source_kind": r["source_kind"], "source_path": r["source_path"],
                         "section": r["section"], "chunk_no": r["chunk_no"],
                         "content": r["content"]})
        mat = np.asarray(vecs, dtype="float32") if vecs else None
        self._matrix_cache = (key, ids, mat, meta)
        return ids, mat, meta

    def get_chunks(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, source_kind, source_path, section, chunk_no, content "
                f"FROM memory_chunks WHERE id IN ({q})", ids,
            ).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def chunks_containing(
        self, value: str, limit: int = 12, kinds: Optional[list[str]] = None
    ) -> list[dict]:
        """子串检索（锚通道用）：锚值精确出现在 chunk 里。"""
        v = (value or "").strip()
        if len(v) < 2:
            return []
        sql = ("SELECT id, source_kind, source_path, section, chunk_no, content "
               "FROM memory_chunks WHERE content LIKE ?")
        params: list[Any] = [f"%{v}%"]
        if kinds:
            sql += f" AND source_kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    def recent(self, kinds: Optional[list[str]] = None, limit: int = 3) -> list[dict]:
        """兜底召回：按时间取最近 chunk（不做相似度）。"""
        sql = ("SELECT id, source_kind, source_path, section, chunk_no, content "
               "FROM memory_chunks")
        params: list[Any] = []
        if kinds:
            sql += f" WHERE source_kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY source_path DESC, chunk_no DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    def by_section(
        self, section_contains: str, kinds: Optional[list[str]] = None, limit: int = 5
    ) -> list[dict]:
        """按节标题过滤（兜底召回取「硬规矩」「偏好与事实」用）。"""
        sql = ("SELECT id, source_kind, source_path, section, chunk_no, content "
               "FROM memory_chunks WHERE section LIKE ?")
        params: list[Any] = [f"%{section_contains}%"]
        if kinds:
            sql += f" AND source_kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += " ORDER BY source_path, chunk_no LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            with self._connect() as conn:
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    # -- 统计 ------------------------------------------------------------
    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
            emb = conn.execute(
                "SELECT COUNT(*) FROM memory_chunks WHERE embedding IS NOT NULL"
            ).fetchone()[0]
            by_kind = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT source_kind, COUNT(*) FROM memory_chunks GROUP BY 1")
            }
            files = conn.execute("SELECT COUNT(*) FROM memory_file_state").fetchone()[0]
        return {"chunks": total, "embedded": emb,
                "coverage": round(emb / total, 3) if total else 0.0,
                "files": files, "by_kind": by_kind,
                "model": self._model_name(),
                "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0}

    @staticmethod
    def _chunk_id(rel: str, no: int) -> str:
        return hashlib.sha1(f"{rel}|{no}".encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 向量打包（float32）
# ---------------------------------------------------------------------------
def _pack(vec: list[float]) -> bytes:
    import numpy as np

    return np.asarray(vec, dtype="float32").tobytes()


def _unpack(blob: Optional[bytes], dim: int):
    import numpy as np

    if not blob:
        return []
    arr = np.frombuffer(blob, dtype="float32")
    return arr[:dim] if dim and len(arr) >= dim else arr
