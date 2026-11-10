"""按书注入纪律（2026-11-10 P1-B）：MEMORY.md / USER.md 的**读侧**隔离。

设计红线
--------
* **绝不改写** ``~/.deskmate/MEMORY.md`` / ``USER.md`` 文件本体——它们是萧哥的
  人工标注，是证据（设计文档 §10.4）。本模块只在拼上下文时过滤 / 标注。
* **认不出当前书就不猜、不过滤**（``infer_current_book`` 已实现「跨书讨论不猜」），
  此时只给「可能过时」标注，不误杀通用事实。
* **当前书是每轮请求的属性**，不是进程级全局状态：不写 ``active_project``，
  避免云端多会话共享一个 manager 实例时串味。
* 任何异常 fail-open 返回原文；``DESKMATE_BOOK_SCOPE=0`` 可整体灰度关闭。

本模块零 schema、不碰数据库、不进同步表；纯函数，便于回归锁。
"""

from __future__ import annotations

import os
import re
from typing import Any

_KILL_SWITCH = "DESKMATE_BOOK_SCOPE"

_STALE_NOTE = (
    "（原「当前创作」记录的是《{old}》的设定；当前在写《{current}》，"
    "该节可能过时，已不注入。回项目笔记核。）"
)
_UNKNOWN_NOTE = (
    "（下列「当前创作」是历史标注，可能过时；书里已经写了什么只认正文逐字直引，"
    "回项目笔记核。）"
)

_TITLE_RE = re.compile(r"《([^》]{1,30})》")
_SECTION_SPLIT_RE = re.compile(r"(?m)^§\s*")
_HEADING_SPLIT_RE = re.compile(r"(?m)^(##\s+.+?)\s*$")


def enabled() -> bool:
    return os.getenv(_KILL_SWITCH, "1") != "0"


def known_books(base_dir: Any) -> list[str]:
    """复用召回侧的书单（projects/ + local_intake 路径 + 最近 session）。"""
    try:
        from .user_fact_context import _known_books

        return list(_known_books(base_dir))
    except Exception:
        return []


def resolve_turn_book(
    base_dir: Any,
    query: str | None = None,
    *,
    active_project: str | None = None,
    known: list[str] | None = None,
) -> str:
    """解析本轮「当前在写哪本书」；判不出返回空串（= 不做书过滤）。

    顺序：query / 最近 session 书名 → active_project（仅当它长得像书名）。
    工作区目录名 / UUID / 路径会被 ``_looks_like_title`` 挡掉，不会污染书维度。
    """
    if not enabled():
        return ""
    try:
        from .user_fact_context import (
            _looks_like_title,
            _resolve_book,
            infer_current_book,
        )

        books = known if known is not None else known_books(base_dir)
        book = infer_current_book(base_dir, query, books)
        if book:
            return book
        ap = str(active_project or "").strip()
        if not ap:
            return ""
        resolved = _resolve_book(ap, books)
        if resolved:
            return resolved
        return ap if _looks_like_title(ap) else ""
    except Exception:
        return ""


def _mentioned_books(text: str, known: list[str]) -> list[str]:
    """文本里出现的已知书名（按归一名去重）。"""
    from .user_fact_context import _norm_book

    norm = _norm_book(text)
    hits: list[str] = []
    for book in known:
        nb = _norm_book(book)
        if len(nb) >= 2 and nb in norm and book not in hits:
            hits.append(book)
    # 去掉被更长书名包含的短名（「大明」与「大明梦华」同时命中时只留长的）
    return [b for b in hits if not any(
        b != other and _norm_book(b) in _norm_book(other) for other in hits
    )]


_FILTER_NOTE = (
    "（本分节已按当前书《{current}》过滤：属于其它书的条目未注入；"
    "书里已经写了什么只认正文逐字直引，记忆只是线索。）"
)


def filter_memory_sections_by_book(
    memory_text: str, current_book: str, known: list[str] | None = None
) -> str:
    """剔除「明确只属于另一本已知书」的 ``§`` 分节 / 条目，通用内容原样保留。

    两级判断（2026-11-10 实测真实 MEMORY.md 后补）：
    1. **整节**：仅当**分节标题本身**就是另一本书名（理想的 ``§ 夜不收 #stable``）
       才整节剔除。不能拿「整节正文只提到一本别的书」来判——实测真实
       ``§ 创作`` 只有一行带《夜不收》，其余是通用复盘，按整节判会误杀通用行。
    2. **条目**：真实 MEMORY.md 只有 ``§ 纠正 / § 工作 / § 创作`` 三节，
       里面按行混装多书裁定与复盘（实测 ``§ 纠正`` 35 行大半是夜不收）。
       这时逐行判断，只删「恰好只提到一本别的书」的行，通用行与跨书对照行保留。

    认不出当前书时完全不过滤。
    """
    if not enabled() or not current_book or not memory_text:
        return memory_text
    try:
        from .user_fact_context import _norm_book

        books = known if known is not None else []
        cur = _norm_book(current_book)

        def _other_only(text: str) -> bool:
            mentions = _mentioned_books(text, books)
            return len(mentions) == 1 and _norm_book(mentions[0]) != cur

        parts = _SECTION_SPLIT_RE.split(memory_text)
        out = [parts[0]]
        for part in parts[1:]:
            if not part.strip():
                continue
            lines = part.split("\n")
            head_mentions = _mentioned_books(lines[0] if lines else "", books)
            if len(head_mentions) == 1 and _norm_book(head_mentions[0]) != cur:
                continue  # 分节标题本身就是别的书名 → 整节剔除
            kept = [
                ln for ln in lines
                if not (ln.strip() and _other_only(ln))
            ]
            if len(kept) != len(lines):
                kept.insert(1, _FILTER_NOTE.format(current=current_book))
            out.append("§ " + "\n".join(kept))
        return "\n".join(out) if len(out) > 1 else memory_text
    except Exception:
        return memory_text


def filter_user_profile_by_book(
    user_text: str, current_book: str, known: list[str] | None = None
) -> str:
    """USER.md 的「当前创作」段按书隔离（不改写文件）。

    * 当前书确定且与段内书名不同 → 该段正文不注入，只留一行「可能过时」标注；
    * 当前书相同 → 原样保留；
    * 认不出当前书 → **不删内容**，只在段前加「可能过时」标注。
    """
    if not enabled() or not user_text:
        return user_text
    try:
        from .user_fact_context import _norm_book, _resolve_book

        books = known if known is not None else []
        chunks = _HEADING_SPLIT_RE.split(user_text)
        # chunks = [前言, 标题1, 正文1, 标题2, 正文2, ...]
        out: list[str] = [chunks[0]] if chunks else []
        for i in range(1, len(chunks), 2):
            heading = chunks[i]
            body = chunks[i + 1] if i + 1 < len(chunks) else ""
            if "当前创作" not in heading:
                out.append(heading + body)
                continue
            m = _TITLE_RE.search(heading)
            old = ""
            if m:
                old = _resolve_book(m.group(1), books) or m.group(1).strip()
            if current_book and old and _norm_book(old) != _norm_book(current_book):
                out.append(_STALE_NOTE.format(old=old, current=current_book) + "\n")
            elif not current_book or not old:
                out.append(_UNKNOWN_NOTE + "\n" + heading + body)
            else:
                out.append(heading + body)
        return "".join(out)
    except Exception:
        return user_text
