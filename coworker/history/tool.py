"""The ``search_history`` tool — 百家学院权威史料库检索（本地 SQLite，零网络）。

史料库文件放在本地数据目录（默认 ``~/.deskmate/history/``，每个专家一个 ``.db``）。
与 ``web_search`` 并列，但面向史料：查证古籍原文、正史记载、典章制度时优先用它。
工具只读打开，无任何写路径。

数据目录解析优先级：
  ``HISTORY_DATA_DIR`` 环境变量 → ``DESKMATE_DIR``/``history`` → ``~/.deskmate/history``
专家库默认**自动扫描数据目录下所有 ``*.db``**（丢一个库文件进目录即可用），
也可用 ``HISTORY_EXPERTS`` 环境变量或调用参数显式指定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from .fts5 import list_sources, search_across_dbs

_DEFAULT_EXPERTS = ("duan-dai-shi", "dong-shi-gong")


def _discover_experts(data_dir: Path) -> list[str]:
    """扫描数据目录下所有 ``*.db`` 作为可检索库。

    不沿用硬编码白名单：实测库文件名可能与白名单不一致（如史部子集命名为
    ``shibu-shi.db``），硬编码会导致数据已就位却检索不到。扫目录可做到
    「丢一个 .db 进目录即可用」。
    """
    if not data_dir.is_dir():
        return list(_DEFAULT_EXPERTS)
    found = sorted(p.stem for p in data_dir.glob("*.db") if p.is_file())
    return found or list(_DEFAULT_EXPERTS)


def _resolve_experts(data_dir: Path, experts: Optional[list[str]] = None) -> list[str]:
    """专家列表解析优先级：显式传入 → ``HISTORY_EXPERTS`` → 扫描目录 → 兜底白名单。"""
    if experts:
        return list(experts)
    env = os.environ.get("HISTORY_EXPERTS")
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return _discover_experts(data_dir)


def _default_history_dir() -> Path:
    env = os.environ.get("HISTORY_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate"))
    return Path(base).expanduser() / "history"


_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_history",
        "description": (
            "检索百家学院权威史料库（正史/儒家经典/诗经，chtxt 精校优先）。"
            "用于查证古籍原文、正史记载、典章制度、历史人物事件等史料，返回每条结果的"
            "标题/章节/作者/朝代/来源/原文片段。引用时必须附上 dynasty/author/title/source "
            "四要素；source=chtxt 为精校可直接引用，其他来源须标注存疑。检索无结果时禁止"
            "编造，应如实说明史料库未收录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词或短语（中文会做四级降级：全词精确 → trigram → bigram → char AND）",
                },
                "experts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "可选：限定专家库 id 列表。默认自动扫描数据目录下所有 .db，"
                        "新增史料库直接放入目录即可，无需改代码。"
                    ),
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


def make_history_search_tool(
    data_dir: Optional[Path] = None,
    experts: Optional[list[str]] = None,
) -> Callable[..., Any]:
    """Build the ``search_history`` tool. ``data_dir``/``experts`` override resolution
    (used by tests)."""
    data_dir = data_dir or _default_history_dir()
    # 构造时解析一次；显式传入优先，其次是环境变量，最后扫目录
    base_experts = _resolve_experts(data_dir, experts)

    def search_history(
        query: str,
        experts: Optional[list[str]] = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"error": "query 不能为空"}

        # 用构造时解析的 base_experts，而不是重新读环境变量/扫目录，
        # 否则测试或调用方显式传入的 experts 会被悄悄覆盖掉。
        requested = list(experts) if experts else list(base_experts)
        n = min(20, max(1, int(limit or 8)))
        db_paths = [str(data_dir / f"{e}.db") for e in requested]
        db_paths = [p for p in db_paths if Path(p).is_file()]

        if not db_paths:
            sources = list_sources(data_dir, requested)
            return {
                "error": (
                    f"没有可用的史料库（HISTORY_DATA_DIR={data_dir}, "
                    f"experts={','.join(requested)}）。请先把史料库 .db 文件放到该目录。"
                ),
                "sources": sources,
            }

        results = search_across_dbs(db_paths, query, limit=n)
        return {"query": query, "count": len(results), "results": results}

    search_history.__name__ = "search_history"
    search_history.__doc__ = _SCHEMA["function"]["description"]
    search_history.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="search_history",
        category="web",
        risk_level="low",
        capabilities=["search"],
        requires_approval=False,
    )
    search_history.__coworker_schema__ = _SCHEMA
    return search_history
