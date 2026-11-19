"""小说正文 FTS5 检索域（从 core.py 机械抽取，PR16/#33）。

建索引 / 全文检索 / 修订记录 / what_changed / 片段截取。行为与抽取前一致。
共享 helper 与常量经父模块 core 命名空间解析（_L），保持兼容。
core.py 通过 re-export 保持 from deskmate.novel_memory.core import X 不变。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path

# _L 懒代理父模块（复检修复）：避免子模块被「首个直接导入」时与父模块 re-export 循环导入。
# 仅在函数调用访问 _L.X 时解析父模块（此时必已完整加载）；monkeypatch(父, X) 经 getattr 实时生效。
class _LazyParent:
    def __getattr__(self, name):
        from deskmate.novel_memory import core as _m
        return getattr(_m, name)
_L = _LazyParent()

logger = logging.getLogger(__name__)


def _fts5_db_path(folder: str) -> Path:
    """返回 FTS5 数据库的完整路径（folder 可为目录或单个稿件文件）。"""
    return _L._bible_root(folder) / _L._BIBLE_DIR / _L._FTS5_DB


def _fts5_connection(folder: str) -> sqlite3.Connection | None:
    """返回 FTS5 数据库连接，若不存在则返回 None。"""
    db_path = _fts5_db_path(folder)
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def build_fts5_index(folder: str, force: bool = False, *, exclude_aux: bool = False) -> int:
    """构建或增量更新 FTS5 全文索引。

    扫描所有文本文件，将内容写入 SQLite FTS5 虚拟表，并在覆盖前记录修订 diff。

    变更判据为 **mtime + size 双条件**：两者都没变才跳过。单看 mtime 会漏检
    "同一毫秒内二次保存"以及 rsync/git checkout 等保留 mtime 的写入；size 从
    stat 里顺手就能拿到，零额外成本却能兜住绝大多数等长之外的漏检。

    Args:
        force: 忽略 mtime/size，逐文件读取正文与库内版本做内容比对。用于
            怀疑漏检时的兜底全量校准（成本=读一遍全部稿件）。

    返回本次索引的文件数。
    """
    scan_root, _ = _L._resolve_root(folder)
    db_path = _fts5_db_path(folder)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        # 创建 FTS5 虚拟表（如果不存在）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS novels USING fts5(
                file_path,
                title,
                content,
                tokenize='unicode61'
            )
        """)

        # 创建文件跟踪表（记录 mtime + size 以实现增量索引）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_tracker (
                file_path TEXT PRIMARY KEY,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)
        # 旧库补 size 列（历史库没有该列，缺省 -1 表示"未知"，会强制走一次内容比对）
        _cols = {r["name"] for r in conn.execute("PRAGMA table_info(file_tracker)")}
        if "size" not in _cols:
            conn.execute("ALTER TABLE file_tracker ADD COLUMN size INTEGER DEFAULT -1")

        # 修订记录表：稿件每次变更都存一条「旧→新」差异，供"我今天改了哪"回放
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manuscript_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                title TEXT,
                ts REAL NOT NULL,
                old_hash TEXT,
                new_hash TEXT,
                added INTEGER DEFAULT 0,
                removed INTEGER DEFAULT 0,
                diff TEXT
            )
        """)

        # 收集所有文本文件
        text_files = _L._iter_text_files(scan_root)
        # 全书检索应聚焦小说叙事正文；排除授权协议/合同/大纲/简介/目录等辅助或
        # 法律文档，否则检索会返回账户户名、作者签名等非叙事噪声（与 scan_novel
        # 的 exclude_aux 约定保持一致，圣经重建已默认排除）。
        if exclude_aux:
            text_files = [p for p in text_files if not _L._is_aux_file(p)]

        indexed_count = 0
        for p in text_files:
            rel_path = _L._rel_name(p, scan_root)
            st = p.stat()
            current_mtime = st.st_mtime
            current_size = st.st_size

            # 检查 mtime + size 是否都未变（二者存在 file_tracker，不在 FTS5 表）
            tracker_row = conn.execute(
                "SELECT mtime, size FROM file_tracker WHERE file_path = ?",
                (rel_path,),
            ).fetchone()

            if not force and tracker_row is not None:
                same_mtime = abs(tracker_row["mtime"] - current_mtime) < 0.001
                tracked_size = tracker_row["size"]
                # size 为 -1 代表旧库无此列记录，视为未知 → 不敢跳过
                same_size = tracked_size is not None and tracked_size >= 0 \
                    and tracked_size == current_size
                if same_mtime and same_size:
                    continue  # 未变化，跳过

            text = _L._read_file_text(p)
            if not text.strip():
                continue

            title = p.stem  # 文件名（不含扩展名）作为标题

            # 取上一版全文（修订 diff 的基线，存于 FTS5 表 content 列）
            old_row = conn.execute(
                "SELECT content FROM novels WHERE file_path = ?",
                (rel_path,),
            ).fetchone()
            old_text = old_row["content"] if old_row else ""

            # 内容其实没变（touch / 元数据改动 / force 全量扫），只刷新指纹，不算一次索引
            if old_text == text:
                conn.execute(
                    "INSERT OR REPLACE INTO file_tracker (file_path, mtime, size, indexed_at)"
                    " VALUES (?, ?, ?, ?)",
                    (rel_path, current_mtime, current_size, time.time()),
                )
                continue

            # 删除旧记录（如果有），再插入新记录
            conn.execute("DELETE FROM novels WHERE file_path = ?", (rel_path,))
            conn.execute(
                "INSERT INTO novels (file_path, title, content) VALUES (?, ?, ?)",
                (rel_path, title, text),
            )
            conn.execute(
                "INSERT OR REPLACE INTO file_tracker (file_path, mtime, size, indexed_at)"
                " VALUES (?, ?, ?, ?)",
                (rel_path, current_mtime, current_size, time.time()),
            )

            # 修订回顾：有上一版且内容不同 → 记录差异（绝不影响主索引流程）
            if old_text and old_text != text:
                _record_revision(conn, rel_path, title, old_text, text)

            indexed_count += 1

        # 垃圾回收：清理已从磁盘删除的文件的旧索引（Bug B 修复）。
        # 之前只增/改不删：作者删掉某章稿件后，FTS5 检索仍会返回「死章节」片段。
        current_rel = {_L._rel_name(p, scan_root) for p in text_files}
        if current_rel:
            placeholders = ",".join("?" for _ in current_rel)
            conn.execute(
                f"DELETE FROM novels WHERE file_path NOT IN ({placeholders})",
                tuple(sorted(current_rel)),
            )
            conn.execute(
                f"DELETE FROM file_tracker WHERE file_path NOT IN ({placeholders})",
                tuple(sorted(current_rel)),
            )
        else:
            conn.execute("DELETE FROM novels")
            conn.execute("DELETE FROM file_tracker")

        conn.commit()
        return indexed_count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 稿件修订回顾（版本快照 + diff）
# ---------------------------------------------------------------------------

# what_changed 摘要里每次修订最多展开的 diff 行数（库内存的是完整 diff）
_DIFF_PREVIEW_LINES = 50


def _record_revision(conn: sqlite3.Connection, rel_path: str, title: str,
                     old_text: str, new_text: str) -> None:
    """记录一次稿件修订：把旧→新全文的差异存入 manuscript_revisions。

    增强功能，任何异常都只记日志、绝不阻断主索引流程。
    """
    try:
        from deskmate.perception.diff_changes import diff_text, summarize_changes
        import hashlib

        # max_chars=None：入库必须存完整 diff。8000 截断是给 prompt/展示用的，
        # 存档若被腰斩，大章节的后半段修订就永久无法回放了。
        diff = diff_text(old_text, new_text, label=rel_path, max_chars=None)
        stats = summarize_changes(old_text, new_text)
        old_hash = hashlib.md5(old_text.encode("utf-8")).hexdigest()[:12]
        new_hash = hashlib.md5(new_text.encode("utf-8")).hexdigest()[:12]
        conn.execute(
            """INSERT INTO manuscript_revisions
               (file_path, title, ts, old_hash, new_hash, added, removed, diff)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (rel_path, title, time.time(), old_hash, new_hash,
             stats["added"], stats["removed"], diff),
        )
    except Exception as e:  # 修订是增强项，绝不能拖累主索引
        logger.warning("记录稿件修订失败（已忽略）：%s", e)


def list_revisions(folder: str, file: str | None = None, limit: int = 20) -> list[dict]:
    """列出稿件修订记录（按时间倒序）。file 指定则只列该文件。

    返回 dict 列表，字段含 file_path / title / ts / added / removed / diff 等。
    """
    conn = _fts5_connection(folder)
    if conn is None:
        return []
    try:
        sql = "SELECT * FROM manuscript_revisions"
        params: list = []
        if file:
            sql += " WHERE file_path = ?"
            params.append(file)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _parse_since(since: str) -> float:
    """把 'today'/'yesterday'/'3d'/'2天'/ISO 解析成时间戳阈值。"""
    import datetime

    now = datetime.datetime.now()
    s = (since or "today").strip().lower()
    if s in ("today", "今天"):
        return datetime.datetime(now.year, now.month, now.day).timestamp()
    if s in ("yesterday", "昨天"):
        d = now - datetime.timedelta(days=1)
        return datetime.datetime(d.year, d.month, d.day).timestamp()
    m = re.match(r"^(\d+)\s*(d|天|day)", s)
    if m:
        return (now - datetime.timedelta(days=int(m.group(1)))).timestamp()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(since, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def what_changed(folder: str, since: str = "today") -> str:
    """汇总自某时刻以来的稿件修订，返回人类可读摘要（对标 Hermes 的"改了哪几处"）。

    会先执行一次增量索引（build_fts5_index），确保捕获用户最新编辑，
    再汇总阈值之后的所有修订记录。桌搭在用户问"我今天改了哪/改了什么"时调用。
    """
    import datetime

    try:
        build_fts5_index(folder)
    except Exception as e:
        logger.warning("修订回顾前置索引失败（继续用已有记录）：%s", e)

    threshold = _parse_since(since)
    revs = list_revisions(folder, limit=200)
    revs = [r for r in revs if r.get("ts", 0) >= threshold]
    if not revs:
        return (f"（自 {since} 起未检测到稿件修订记录。"
                "若你已编辑稿件，请先刷新圣经/索引，让系统捕获这次变更后再问我。）")

    by_file: dict[str, list[dict]] = {}
    for r in revs:
        by_file.setdefault(r["file_path"], []).append(r)

    lines = [f"自 {since} 起，你共修订了 {len(by_file)} 个文件、{len(revs)} 次：", ""]
    for fp, items in by_file.items():
        added = sum(i.get("added", 0) for i in items)
        removed = sum(i.get("removed", 0) for i in items)
        lines.append(f"▸ {fp}（{len(items)} 次修订，新增 {added} / 删除 {removed} 处）")
        for i in items:
            ts = datetime.datetime.fromtimestamp(i["ts"]).strftime("%m-%d %H:%M")
            lines.append(f"    · {ts} 的修订：")
            diff = (i.get("diff") or "").strip()
            if diff and diff != "（无变更）":
                body = [dl for dl in diff.splitlines()
                        if not dl.startswith(("+++", "---", "@@", "Index"))]
                for dl in body[:_DIFF_PREVIEW_LINES]:
                    lines.append(f"      {dl}")
                # 库里存的是完整 diff，这里只是展示截断——必须说明，否则用户
                # 会以为"就改了这些"，正是当初 8000 硬截断埋下的同一类误导。
                if len(body) > _DIFF_PREVIEW_LINES:
                    lines.append(
                        f"      …（此处还有 {len(body) - _DIFF_PREVIEW_LINES} 行改动未展开，"
                        f"用 novel_revisions 查看完整 diff）"
                    )
        lines.append("")
    return "\n".join(lines)


def novel_search_fts5(query: str, folder: str, max_results: int = 10) -> str:
    """使用 FTS5 进行全文检索。

    如果 FTS5 索引不存在，自动回退到 keyword-based novel_recall。
    支持 CJK 查询：FTS5 的 unicode61 tokenizer 不原生支持 CJK 分词，
    因此对于 CJK 密集的查询会使用 LIKE 作为回退。
    """
    db_path = _fts5_db_path(folder)

    # 索引不存在 -> 回退到 keyword 检索
    if not db_path.exists():
        return _L._keyword_recall(query, folder, max_snippets=max_results)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:

            # 检查是否包含 CJK 字符（中文、日文、韩文）
            has_cjk = bool(re.search(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", query))
    
            if has_cjk:
                # FTS5 unicode61 不原生支持 CJK 分词，回退到 LIKE 搜索
                # 提取关键词（≥2字），每个关键词用 LIKE 匹配
                kws = list(dict.fromkeys(
                    w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query)
                    if len(w) >= 2
                ))[:8]
    
                if not kws:
                    return _L._keyword_recall(query, folder, max_snippets=max_results)
    
                # 构建 LIKE 子句（转义 % 和 _ 以防止 LIKE 模式注入）
                like_clauses = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in kws)
                like_params = [f"%{kw.replace('%', '\\%').replace('_', '\\_')}%" for kw in kws]
                sql = f"""
                    SELECT file_path, title, content
                    FROM novels
                    WHERE {like_clauses}
                    ORDER BY rowid
                    LIMIT ?
                """
                rows = conn.execute(sql, (*like_params, max_results)).fetchall()
            else:
                # 非 CJK 查询：使用 FTS5 原生 MATCH
                # 对查询进行简单转义（移除 FTS5 特殊字符）
                sanitized = re.sub(r'[^\w\s]', ' ', query).strip()
                if not sanitized:
                    return _L._keyword_recall(query, folder, max_snippets=max_results)
    
                rows = conn.execute(
                    "SELECT file_path, title, content, rank FROM novels WHERE content MATCH ? ORDER BY rank LIMIT ?",
                    (sanitized, max_results),
                ).fetchall()
        finally:
            conn.close()

        if not rows:
            # FTS5 无结果 -> 回退到 keyword 检索
            return _L._keyword_recall(query, folder, max_snippets=max_results)

        # 格式化结果
        parts: list[str] = []
        for i, row in enumerate(rows, 1):
            file_path = row["file_path"]
            content = row["content"]
            # 查找查询词在内容中的位置，提取上下文片段
            snippet = _extract_snippet(content, query, context_chars=200)
            parts.append(f"[{i}] {file_path}")
            parts.append(f"    {snippet}")
            parts.append("")

        # 附加圣经摘要
        bible = _L.read_bible(folder)
        if bible:
            bible_parts: list[str] = []
            if bible.get("CHARACTERS"):
                bible_parts.append(bible["CHARACTERS"][:4000])
            for tag in ("OUTLINE", "TIMELINE", "NOTES"):
                if bible.get(tag):
                    bible_parts.append(f"## {tag}\n{bible[tag][:1500]}")
            if bible_parts:
                parts.append("【人物圣经摘要】")
                parts.append("\n\n".join(bible_parts))

        return "\n".join(parts)

    except Exception as e:
        # 任何 FTS5 异常 -> 安全回退到 keyword 检索
        logger.warning("FTS5 search failed, falling back to keyword recall: %s", e)
        return _L._keyword_recall(query, folder, max_snippets=max_results)


def _extract_snippet(text: str, query: str, context_chars: int = 200) -> str:
    """从文本中提取包含查询词的最佳上下文片段。"""
    # 提取查询中的关键词
    kws = [w for w in re.split(r"[\s，。、？！,.?!；：“”‘’…—()\[\]【】-]+", query) if len(w) >= 2]
    if not kws:
        return text[:context_chars * 2].strip()

    # 找到第一个关键词的位置
    best_pos = -1
    for kw in kws:
        pos = text.find(kw)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos == -1:
        return text[:context_chars * 2].strip()

    start = max(0, best_pos - context_chars)
    end = min(len(text), best_pos + context_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet




# Bug A 修复：回退路径圣经摘要的 CHARACTERS 预算（保持原行为量级，单位：字符）
_BIBLE_CHAR_BUDGET = 6000


