"""史料库检索 — 百家学院权威史料（本地 SQLite，零网络），与 ``web_search`` 并列、史料优先。"""

from __future__ import annotations

from .fts5 import build_fts5_query, list_sources, search_across_dbs, search_one_db
from .tool import make_history_search_tool, make_library_check_tool

__all__ = [
    "build_fts5_query",
    "list_sources",
    "search_across_dbs",
    "search_one_db",
    "make_history_search_tool",
    "make_library_check_tool",
]
