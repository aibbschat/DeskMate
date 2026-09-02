"""The ``search_history`` tool — 百家学院权威史料库检索（本地 SQLite，零网络）。

史料库文件放在本地数据目录（默认 ``~/.deskmate/history/``，每个专家一个 ``.db``）。
与 ``web_search`` 并列，但面向史料：查证古籍原文、正史记载、典章制度时优先用它。
工具只读打开，无任何写路径。

数据目录解析优先级：
  ``HISTORY_DATA_DIR`` 环境变量 → ``DESKMATE_DIR``/``history`` → ``~/.deskmate/history``
专家库默认**自动扫描数据目录下所有 ``*.db``**（丢一个库文件进目录即可用），
也可用 ``HISTORY_EXPERTS`` 环境变量或调用参数显式指定。

简繁变体：语料简繁混排，检索时查询词的简/繁两形都会查（详见 :mod:`.s2t`）。
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
            "检索本地权威史料库（零网络）。查证古籍原文、正史记载、典章制度、"
            "历史人物事件时用，返回标题/章节/作者/朝代/来源/原文片段。\n"
            "语料简繁混排（正史原文多繁体、现代整理本多简体），检索已自动覆盖简繁两形；"
            "结果的 matchedForm 若与你的查询词字形不同，说明该条是以另一种字形命中，"
            "**引用时请照录原文片段，不要自行转简或转繁**。\n"
            "结果按 era 分四层，**可靠性与用途各不相同**：\n"
            "- primary 原典（正史/编年/政书/史部）：一手材料，但官修正史受政治与权力"
            "影响存在讳饰、颠倒、删改，不等于事实本身，须与后世研究交叉验证。\n"
            "- scholar 研究著作（近现代、当代学者专著）：考证与异见所在，用于质疑和"
            "校正原典。引用写「作者+书名」，不写朝代（作者是今人）。\n"
            "- context 旁证文献（文总集、文评）：可佐证时代风气，不作史实依据。\n"
            "- fiction 演义小说：小说家言，**禁止**作为史料引用。\n"
            "结果已按「原典与研究著作交错」排列，便于交叉比对。**回答争议性或评价性"
            "史实判断时，须同时呈现原典记载与后世研究的不同说法，指出分歧，再判断哪种"
            "更可信及理由；只引一家、把正史当定论视为不合格。**\n"
            "**每条结果都带 matchTier 字段，可信度差别很大，务必甄别：**\n"
            "- exact：完整包含查询词，可直接引用。\n"
            "- trigram：命中三字连续片段，可用但需核对上下文。\n"
            "- bigram / char_and：仅命中二字或散字组合，**很可能是字面巧合而非史实相关**"
            "（查「郑和下西洋」时《舊唐書》会因「下西洋」这类常见组合被召回）。\n"
            "- synonym：只命中查询词的**近义概念**，原文未必讨论过你查的那个东西"
            "（查「王安石变法」时命中含「新政」二字的段落）。仅在前四档都无结果时出现，"
            "**必须读完 snippet 确认真的相关才能引用**。\n"
            "bigram 及以下各档都必须结合 snippet 判断是否真的相关，**确认无关则不要引用**。\n"
            "引用须附朝代/作者/书名/来源四要素；source=chtxt 为精校本可直接引用，"
            "其他来源须标注存疑。无结果时禁止编造，如实说明史料库未收录。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索关键词或短语。中文会做简繁变体扩展（语料简繁混排，"
                        "简繁两形都会查）+ 四级降级（全词精确 → trigram → bigram → char AND）。"
                    ),
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
