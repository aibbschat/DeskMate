"""The ``search_private`` tool — 萧哥的私人资料检索（本地 SQLite，零网络）。

与 ``search_history`` 并列，但明确逻辑隔离：这是萧哥的**私人资料**（写作教程、随笔、
非史料书等），**不是史料**。小桔检索后引用时必须标注来源为「私人资料」，不得按史料规范
（朝代/作者/书名）引用——这是 compliance 红线的延伸（史料严谨性不可被私人资料污染）。

底层复用 ``coworker.history.fts5`` 的同一套 FTS 引擎与四级降级，只过滤 ``private-*.db``，
与史料库同目录、同引擎，零额外检索逻辑。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from .fts5 import search_across_dbs


def _default_history_dir() -> Path:
    env = os.environ.get("HISTORY_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate"))
    return Path(base).expanduser() / "history"


def _discover_private_categories(data_dir: Path) -> list[str]:
    """扫描 ``private-*.db``，返回分类名（去掉前缀）。"""
    if not data_dir.is_dir():
        return []
    return sorted(
        p.stem[len("private-"):] for p in data_dir.glob("private-*.db") if p.is_file()
    )


def _resolve_db_paths(data_dir: Path, categories: Optional[list[str]]) -> list[str]:
    cats = list(categories) if categories else _discover_private_categories(data_dir)
    paths = [str(data_dir / f"private-{c}.db") for c in cats]
    return [p for p in paths if Path(p).is_file()]


_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_private",
        "description": (
            "检索萧哥的**本地私人资料库**（零网络）。资料是萧哥主动摄入的写作教程、"
            "随笔、非史料书籍、研究笔记等，按分类组织。\n"
            "**重要**：这是萧哥的私人资料，**不是史料**，引用时务必标注来源为"
            "「私人资料 / 萧哥的X分类」，不得套用史料规范（朝代/作者/书名/典制考据）。"
            "它仅供辅助萧哥的创作与写作，不具备史料权威性。\n"
            "语料简繁混排，检索已自动覆盖简繁两形；返回每条带 category（分类）、"
            "sourceFile（来源文件）、snippet（原文片段）。默认跨所有私人分类检索；"
            "传 categories 可限定某几个分类。无结果时如实说明资料库未收录，禁止编造。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或短语（中文简繁变体自动扩展 + 四级降级）",
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：限定分类名列表（如 ['写作教程']）。默认跨所有私人分类。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果上限（默认 8，最大 20）",
                },
            },
            "required": ["query"],
        },
    },
}


def make_private_search_tool(
    data_dir: Optional[Path] = None,
) -> Callable[..., Any]:
    data_dir = data_dir or _default_history_dir()

    def search_private(
        query: str,
        categories: Optional[list[str]] = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        db_paths = _resolve_db_paths(data_dir, categories)
        if not db_paths:
            return {
                "error": (
                    f"私人资料库为空（HISTORY_DATA_DIR={data_dir}）。"
                    "先在「本地摄入」面板把资料摄入并归类。"
                ),
                "count": 0,
                "results": [],
            }
        n = min(20, max(1, int(limit or 8)))
        results = search_across_dbs(db_paths, query, limit=n)
        # 覆盖 era：私人资料不当史料分层，避免被误当史料引用
        for r in results:
            r["era"] = "private"
            r["eraLabel"] = "私人资料"
        return {"query": query, "count": len(results), "results": results}

    search_private.__name__ = "search_private"
    search_private.__doc__ = _SCHEMA["function"]["description"]
    try:
        search_private.__aisuite_tool_metadata__ = ai.ToolMetadata(
            name="search_private",
            category="local",
            risk_level="low",
            capabilities=["search"],
            requires_approval=False,
        )
    except Exception:
        pass
    search_private.__coworker_schema__ = _SCHEMA
    return search_private
