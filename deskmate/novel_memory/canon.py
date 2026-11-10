"""正文证据锚点（2026-11-10 P3 · 方案 B，零 schema）。

把「已写正文是唯一事实基准」从一句 prompt 变成**可校验、可回查**的事实：

* 复用按书的 FTS5 正文索引 ``<书稿目录>/.deskmate/fts5.db``（core.py 已建），
  不新建同步表、不改 ``SYNC_TABLES``、不动 ``business_cols``。
* 段号口径：``novels.content`` 按 ``\\n`` 切分的 **1-based 行号**（F 段号）。
  实测《夜不收》索引 content 为 98,468 字符、一个 docx 一行，逐段以换行分隔。
* 引文校验只允许本地发生：云端没有书稿正文，``verify_quote`` 返回
  ``ok=False``；写入侧据此把 ``manuscript`` 降级为 ``author_ruling``，
  绝不臆造「正文事实」。
* 只读派生索引，**不读、不写作者原稿**；路径先 resolve 再校验文件存在。

对应 docs/memory-discipline-design.md §5 / §9 P3。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

#: 书稿根目录（只读发现，一级子目录，不递归扫个人文件）
DEFAULT_BOOK_ROOT = Path.home() / "Documents" / "已出版"

_PUNCT_RE = re.compile(r"[\s，,。.！!？?；;、：:（）()「」『』《》\"'“”·…—\-]+")


def norm_quote(text: str) -> str:
    """归一引文：去标点与空白，便于跨版本（笔误/换行）逐字比对。"""
    return _PUNCT_RE.sub("", str(text or "")).strip()


def quote_hash(text: str) -> str:
    return hashlib.sha256(norm_quote(text).encode("utf-8")).hexdigest()[:16]


def fts5_path(folder: str | Path) -> Path:
    return Path(folder).resolve() / ".deskmate" / "fts5.db"


def discover_fts5_folders(book_id: str, *, root: str | Path | None = None) -> list[Path]:
    """按书名找候选书稿目录（只读、一级 glob，找不到返回空列表）。"""
    bid = norm_quote(book_id)
    if not bid:
        return []
    base = Path(root) if root else DEFAULT_BOOK_ROOT
    out: list[Path] = []
    try:
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            # 目录名常带「16.」出版序号前缀
            name = re.sub(r"^\d+[.．、]\s*", "", child.name)
            if bid in norm_quote(name) or norm_quote(name) in bid:
                if fts5_path(child).is_file():
                    out.append(child)
    except OSError:
        pass
    return out


def _novels(folder: str | Path) -> list[tuple[str, str, str]]:
    """只读 FTS5 索引，返回 (file_path, title, content)。"""
    db = fts5_path(folder)
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [
            (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""))
            for r in con.execute("SELECT file_path, title, content FROM novels")
        ]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def paragraphs(folder: str | Path, file_path: str | None = None) -> list[dict[str, Any]]:
    """返回段级列表：``{file_path, para_no, text}``，para_no 为 1-based 行号。"""
    out: list[dict[str, Any]] = []
    for fp, _title, content in _novels(folder):
        if file_path and fp != file_path:
            continue
        for i, line in enumerate(str(content).split("\n"), 1):
            if not line.strip():
                continue
            out.append({"file_path": fp, "para_no": i, "text": line.rstrip()})
    return out


def resolve_quote(
    folder: str | Path, quote: str, *, book_id: str = ""
) -> dict[str, Any]:
    """在正文索引里定位引文，返回 (book, file_path, para_no, verbatim, hash)。

    找不到时 ``ok=False`` + ``reason``；调用方据此拒绝 ``source=manuscript``。
    """
    needle = norm_quote(quote)
    if len(needle) < 4:
        return {"ok": False, "reason": "引文过短（<4 字），不足以定位正文"}
    book = book_id or Path(folder).name
    try:
        for para in paragraphs(folder):
            if needle in norm_quote(para["text"]):
                return {
                    "ok": True,
                    "book": book,
                    "file_path": para["file_path"],
                    "para_no": para["para_no"],
                    "verbatim": para["text"],
                    "quote": str(quote),
                    "quote_hash": quote_hash(quote),
                }
    except Exception as exc:  # noqa: BLE001 —— fail-open，不让校验拖垮写入
        return {"ok": False, "reason": f"正文索引读取失败：{exc}"}
    return {"ok": False, "reason": "引文未在本地正文中命中"}


def get_verbatim_paragraph(
    folder: str | Path, file_path: str, para_no: int, *, context: int = 1
) -> dict[str, Any]:
    """按 (文件, 段号) 回查逐字原文；可带前后各 N 段上下文。"""
    paras = paragraphs(folder, file_path)
    by_no = {p["para_no"]: p["text"] for p in paras}
    if para_no not in by_no:
        return {"ok": False, "reason": "该段号在本地正文中不存在"}
    lo, hi = max(1, para_no - context), para_no + context
    return {
        "ok": True,
        "book": Path(folder).name,
        "file_path": file_path,
        "para_no": para_no,
        "verbatim": by_no[para_no],
        "context": [{"para_no": n, "text": by_no[n]} for n in range(lo, hi + 1) if n in by_no],
    }


def verify_quote(
    quote: str | None, book_id: str = "", *, folder: str | Path | None = None
) -> dict[str, Any]:
    """服务端闸门：引文命中本地正文才允许标 manuscript。

    显式给了 folder 就只查它；否则按书名在书稿根目录里找候选（最多 3 个）。
    云端/无正文 → 一律 ``ok=False``，由写入侧降级，不臆造事实基准。
    """
    if not quote:
        return {"ok": False, "reason": "未提供正文逐字引文"}
    folders: list[Path] = []
    if folder:
        folders = [Path(folder)]
    elif book_id:
        folders = discover_fts5_folders(book_id)[:3]
    if not folders:
        return {"ok": False, "reason": "本地没有该书的正文档案（云端不做正文校验）"}
    for f in folders:
        result = resolve_quote(f, quote, book_id=book_id)
        if result.get("ok"):
            return result
    return {"ok": False, "reason": "引文未在任何本地正文档案中命中"}
