"""私人资料库（非史料）灌库模块 —— 资料检索统一架构的「私人通道」。

设计定位（萧哥 2026-09-23 拍板）：
- 所有资料栓索统一放一块：底层一套 FTS 引擎（``coworker/history/fts5.py``），
  上层分两个逻辑通道 —— 史料通道（``search_history``，百家学院权威史料，当史料引用）
  + 私人通道（``search_private``，萧哥的私人资料，当私人资料引用，不当史料）。
- 分类 = 库：每个分类一个 ``private-<分类名>.db``，丢进 history 目录即被自动扫到
  （与史料库 ``*.db`` 同构，零代码扩展，随时加）。
- 增量灌库：分类首次写入自动建库；追加资料走增量 INSERT（不每次全量重建）。
- 入库 schema 与 ``fts5.search_one_db`` 的 ``_COLUMNS`` 对齐，使其可直接被
  ``search_across_dbs`` 检索。

红线：
- 神圣稿件（国子监秘闻录等）路径子串匹配强制排除，绝不读取。
- 零 LLM、fail-open：任何一步炸了不影响主链路。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 文件名安全：保留中文，替换文件系统非法字符
_INVALID_FN = re.compile(r'[/\\:*?"<>|]')

# 与 fts5.js / build_history_index 对齐的 CJK 范围
_CJK_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff])")
_MULTI_SPACE_RE = re.compile(r"\s+")

# 神圣稿件清单 —— **只此一处引用**，真身在 deskmate/sacred_paths.py。
# 两份副本必然漂移（2026-09-23 巡检：两份都已漏防），故这里再导出而非重定义。
from deskmate.sacred_paths import SACRED_FRAGMENTS as _SACRED_FRAGMENTS  # noqa: E402

# 扫描时跳过的隐藏 / 系统目录
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "target", "dist", "build", ".next", ".idea", ".vscode", ".cargo",
    "Library", "Caches", ".Trash",
}

# 文本类扩展名（零依赖解析）
_TEXT_EXTS = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}
# 需外部库解析的扩展名
_PDF_EXTS = {".pdf"}
_DOCX_EXTS = {".docx"}
# 单次摄入支持的全部扩展名
_SUPPORTED_EXTS = _TEXT_EXTS | _PDF_EXTS | _DOCX_EXTS

# 单文件读取上限（字节），防超大文件拖垮
_MAX_READ_BYTES = 20_000_000  # 20MB
# 切块参数
_CHUNK_MAX = 600       # 单块最大字数
_CHUNK_OVERLAP = 80     # 块间重叠字数

_PRIVATE_SOURCE = "private"  # articles.source 标记


def _tokenize_cjk(text: str) -> str:
    """CJK 字符间插空格，与 fts5.js / build_history_index 对齐。"""
    return _MULTI_SPACE_RE.sub(" ", _CJK_RE.sub(r" \1 ", text)).strip()


def _safe_category(category: str) -> str:
    """分类名 → 安全文件名片段（保留中文，去除文件系统非法字符）。"""
    return _INVALID_FN.sub("_", (category or "").strip()) or "未分类"


def _resolve_history_dir(history_dir: str | Path | None = None) -> Path:
    if history_dir:
        d = Path(history_dir).expanduser()
    else:
        env = os.environ.get("HISTORY_DATA_DIR")
        if env:
            d = Path(env).expanduser()
        else:
            base = os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate"))
            d = Path(base).expanduser() / "history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path(category: str, history_dir: str | Path | None = None) -> Path:
    return _resolve_history_dir(history_dir) / f"private-{_safe_category(category)}.db"


def _is_sacred(path: Path) -> bool:
    """判定统一委托 :func:`deskmate.sacred_paths.is_sacred`（**含符号链接解析**）。

    2026-09-23 巡检修复：原本地实现只查字面路径的 ``parts``——配置目录里放一个
    ``dirlink -> ~/国子监秘闻录``，遍历/直传时路径分段里没有任何敏感词，
    于是被静默读走（已实测复现）。改为委托后，``_walk`` / ``scan_sources`` /
    ``ingest_files`` 三个调用点同时获得真实路径防护。

    新代码请直接用 ``deskmate.sacred_paths.guarded_file``（连扩展名白名单一起校验）。
    """
    from deskmate.sacred_paths import is_sacred as _canonical_is_sacred

    return _canonical_is_sacred(path)


# ---------------------------------------------------------------------------
# 建库 + schema（与 fts5.search_one_db 的 _COLUMNS 对齐）
# ---------------------------------------------------------------------------
def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            source TEXT,
            source_file TEXT,
            title TEXT,
            category TEXT,
            dynasty TEXT,
            author TEXT,
            content TEXT,
            content_hash TEXT,
            file_path TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL,
            chunk_index INTEGER,
            heading TEXT,
            content TEXT,
            fts_content TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            content, heading, tokenize='unicode61'
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_article ON chunks(article_id);
        CREATE INDEX IF NOT EXISTS idx_articles_path ON articles(file_path);
        """
    )
    con.commit()


# ---------------------------------------------------------------------------
# 解析（pdf/docx 用可选 import，装好即用；未装则跳过该类文件）
# ---------------------------------------------------------------------------
def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()[:_MAX_READ_BYTES]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_pdf(path: Path) -> str | None:
    try:
        import pdfplumber  # noqa
    except ImportError:
        logger.warning("pdfplumber 未安装，跳过 %s", path)
        return None
    try:
        texts = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t:
                    texts.append(t)
        return "\n\n".join(texts)
    except Exception as exc:  # fail-open
        logger.warning("pdf 解析失败 %s: %s", path, exc)
        return None


def _read_docx(path: Path) -> str | None:
    try:
        import docx  # python-docx
    except ImportError:
        logger.warning("python-docx 未安装，跳过 %s", path)
        return None
    try:
        document = docx.Document(str(path))
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())
    except Exception as exc:  # fail-open
        logger.warning("docx 解析失败 %s: %s", path, exc)
        return None


def _extract_text(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in _TEXT_EXTS:
        return _read_text(path)
    if ext in _PDF_EXTS:
        return _read_pdf(path)
    if ext in _DOCX_EXTS:
        return _read_docx(path)
    return None


# ---------------------------------------------------------------------------
# 切块：按空行分段，合并短段至接近 max_chars，块间重叠 overlap 字
# ---------------------------------------------------------------------------
def _chunk_text(text: str, max_chars: int = _CHUNK_MAX, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        paras = [text.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 1 > max_chars:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def _file_hash(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 16), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return ""


def _delete_article(con: sqlite3.Connection, article_id: int) -> None:
    """删 article + 其 chunks + chunks_fts 对应行（FTS 普通模式需手动同步）。"""
    chunk_ids = [r[0] for r in con.execute(
        "SELECT id FROM chunks WHERE article_id = ?", (article_id,)
    ).fetchall()]
    for cid in chunk_ids:
        con.execute("DELETE FROM chunks_fts WHERE rowid = ?", (cid,))
    con.execute("DELETE FROM chunks WHERE article_id = ?", (article_id,))
    con.execute("DELETE FROM articles WHERE id = ?", (article_id,))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def ingest_files(
    paths: list[str],
    category: str,
    history_dir: str | Path | None = None,
) -> dict[str, Any]:
    """解析 + 切块 + 增量灌库到 ``private-<category>.db``。

    幂等：同 ``file_path`` 且 ``content_hash`` 不变 → 跳过；hash 变了 → 删旧重灌（更新）。
    返回统计 ``{ingested, updated, skipped, errors}``。
    """
    result: dict[str, Any] = {"ingested": 0, "updated": 0, "skipped": 0, "errors": []}
    if not category or not paths:
        return result
    dbp = _db_path(category, history_dir)
    # 懒连接：只有真正要灌的文件出现时才建库/连接，避免全被排除时残留空 .db
    con: sqlite3.Connection | None = None
    try:
        for raw in paths:
            p = Path(raw).expanduser()
            if not p.is_file() or _is_sacred(p):
                result["skipped"] += 1
                continue
            ext = p.suffix.lower()
            if ext not in _SUPPORTED_EXTS:
                result["skipped"] += 1
                continue
            try:
                text = _extract_text(p)
                if not text or not text.strip():
                    result["skipped"] += 1
                    continue
                if con is None:
                    con = sqlite3.connect(str(dbp))
                    _ensure_schema(con)
                h = _file_hash(p)
                row = con.execute(
                    "SELECT id, content_hash FROM articles WHERE file_path = ?",
                    (str(p),),
                ).fetchone()
                is_update = False
                if row and row[1] == h:
                    result["skipped"] += 1
                    continue
                if row:
                    _delete_article(con, row[0])
                    is_update = True
                cur = con.execute(
                    "INSERT INTO articles (source, source_file, title, category, "
                    "dynasty, author, content, content_hash, file_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        _PRIVATE_SOURCE, p.name, p.stem or p.name, category,
                        "", "", text[:500], h, str(p),
                    ),
                )
                aid = cur.lastrowid
                chunks = _chunk_text(text)
                for i, ch in enumerate(chunks):
                    ccur = con.execute(
                        "INSERT INTO chunks (article_id, chunk_index, heading, content, fts_content) "
                        "VALUES (?,?,?,?,?)",
                        (aid, i, p.stem or p.name, ch, _tokenize_cjk(ch)),
                    )
                    cid = ccur.lastrowid
                    con.execute(
                        "INSERT INTO chunks_fts (rowid, content, heading) VALUES (?,?,?)",
                        (cid, _tokenize_cjk(ch), p.stem or p.name),
                    )
                con.commit()
                if is_update:
                    result["updated"] += 1
                else:
                    result["ingested"] += 1
            except Exception as exc:  # fail-open
                logger.exception("私人资料灌库失败: %s", p)
                if con is not None:
                    con.rollback()
                result["errors"].append({"path": raw, "error": str(exc)})
    finally:
        if con is not None:
            con.close()
    return result


def list_categories(history_dir: str | Path | None = None) -> list[str]:
    d = _resolve_history_dir(history_dir)
    cats: list[str] = []
    for p in sorted(d.glob("private-*.db")):
        cats.append(p.stem[len("private-"):])
    return cats


def _walk(base: Path):
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            name = entry.name
            if name in _SKIP_DIRS or name.startswith(".") or _is_sacred(entry):
                continue
            yield from _walk(entry)
        elif entry.is_file():
            yield entry


def scan_sources(
    dirs: list[str],
    history_dir: str | Path | None = None,
    max_candidates: int = 500,
) -> list[dict[str, Any]]:
    """轻量扫描：只列文件（路径/名称/大小/mtime/类型），**不读内容**。

    解决「数据多了每次重扫慢」的隐患——内容读取推迟到勾选灌库时。
    """
    candidates: list[dict[str, Any]] = []
    for raw in dirs or []:
        base = Path(raw).expanduser()
        if not base.is_dir():
            continue
        for path in _walk(base):
            if len(candidates) >= max_candidates:
                break
            if _is_sacred(path):
                continue
            if path.suffix.lower() not in _SUPPORTED_EXTS:
                continue
            try:
                st = path.stat()
                candidates.append({
                    "path": str(path),
                    "name": path.name,
                    "size": int(st.st_size),
                    "mtime": int(st.st_mtime),
                    "ext": path.suffix.lower(),
                })
            except OSError:
                continue
        if len(candidates) >= max_candidates:
            break
    return candidates


def clean_missing(
    category: str,
    history_dir: str | Path | None = None,
) -> int:
    """策略 B：清理已删源的向量。列出该分类库里所有 article 的 ``file_path``，
    磁盘上不存在的 → 删 article + chunks（+chunks_fts）→ 返回清理条数。
    懒惰自动清理：下次扫描/调用时触发，不实时监控文件系统。
    """
    dbp = _db_path(category, history_dir)
    if not dbp.is_file():
        return 0
    con = sqlite3.connect(str(dbp))
    removed = 0
    try:
        rows = con.execute("SELECT id, file_path FROM articles").fetchall()
        for aid, fp in rows:
            if not Path(fp).expanduser().is_file():
                _delete_article(con, aid)
                removed += 1
        con.commit()
    finally:
        con.close()
    return removed
