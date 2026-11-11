"""``memory_search`` — 小桔的**只读**记忆检索工具（四段·检索权）。

为什么只读
----------
四段只开检索权，不开写入权：记忆写入仍走既有两条管线
（对话后抽取 / 用户显式 remember）。理由与方案一致——检索错了只是多读一段，
写错了会把机器文本升格成用户画像（2026-11-11 那批伪造条目的教训）。
本模块**不提供任何写接口**，测试里有一条断言守着（见
``tests/test_memory_search_tool.py::test_tool_is_read_only``）。

返回内容
--------
每条带 ``content`` / ``source``（源文件）/ ``section`` / ``date`` / ``score``，
模型引用时必须标注来源与时间，禁止把检索结果当用户刚说的话。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from deskmate.memory.file_search import search_memory_files
from deskmate.paths import deskmate_base_dir

#: 层 → (source_kind 列表, section 关键字)
LAYERS: dict[str, tuple[list[str], Optional[str]]] = {
    "hard_rule": (["user"], "硬规矩"),
    "preference": (["user"], "偏好与事实"),
    "memory": (["memory"], None),
    "diary": (["diary_view", "diary_day"], None),
    "vault": (["vault"], None),
}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "检索小桔自己的长期记忆（只读）：用户画像（硬规矩/偏好）、全局记忆、"
            "日记、Obsidian vault。用于回答「我之前说过什么」「那本书的稿子在哪」"
            "「他喜欢的音色」这类需要翻旧账的问题。\n"
            "返回每条记忆的原文、来源文件、分节与日期；引用时必须带上来源与日期。"
            "没检索到就如实说没有，禁止编造记忆内容。本工具只能读，不能改。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要检索的自然语言问题或关键词（中文原句即可）",
                },
                "layer": {
                    "type": "string",
                    "enum": ["", "hard_rule", "preference", "memory", "diary", "vault"],
                    "description": "可选：限定记忆层。留空表示全层检索。",
                },
                "since": {
                    "type": "string",
                    "description": "可选：起始日期 YYYY-MM-DD（按条目自带日期过滤，无日期的条目不受影响）",
                },
                "until": {
                    "type": "string",
                    "description": "可选：截止日期 YYYY-MM-DD",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限（默认 8，最大 20）",
                },
            },
            "required": ["query"],
        },
    },
}


def _entry_date(hit: dict) -> str:
    """条目日期：优先分节名（日记按日期分节），否则从正文头部找第一个日期。"""
    sec = (hit.get("section") or "").strip()
    m = _DATE_RE.search(sec)
    if m:
        return m.group(0)
    m = _DATE_RE.search((hit.get("content") or "")[:80])
    return m.group(0) if m else ""


def make_memory_search_tool(base: Optional[Path | str] = None) -> Callable[..., Any]:
    """构造只读检索工具。``base`` 在**构造期固化**（与 hook 同样的理由：
    不能在线程/调用时才读环境变量，否则会回落到真实目录）。"""
    root = Path(base) if base else deskmate_base_dir()

    def memory_search(
        query: str,
        layer: str = "",
        since: str = "",
        until: str = "",
        limit: int = 8,
    ) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            return {"error": "query 不能为空", "count": 0, "results": []}
        layer = (layer or "").strip()
        if layer and layer not in LAYERS:
            return {
                "error": f"layer 只能是 ''/" + "/".join(LAYERS),
                "count": 0,
                "results": [],
            }
        kinds, sec_kw = LAYERS.get(layer, (None, None))
        n = min(20, max(1, int(limit or 8)))
        try:
            hits = search_memory_files(
                q,
                k=n,
                budget_chars=1600,
                base=root,
                kinds=kinds,
                section_contains=sec_kw,
                read_only=True,
            )
        except Exception as exc:  # noqa: BLE001 —— 索引坏了当「没查到」，不拖垮会话
            return {"error": f"索引不可用：{exc}", "count": 0, "results": []}

        out: list[dict[str, Any]] = []
        for h in hits:
            d = _entry_date(h)
            # 日期过滤：能解析出日期的才过滤；无日期的条目保留（宁多勿漏）
            if d:
                if since and d < since:
                    continue
                if until and d > until:
                    continue
            out.append({
                "content": h.get("content", ""),
                "source": h.get("source_path", ""),
                "section": h.get("section", ""),
                "date": d,
                "score": h.get("score", 0.0),
            })
        return {"query": q, "layer": layer, "count": len(out), "results": out}

    memory_search.__name__ = "memory_search"
    memory_search.__doc__ = _SCHEMA["function"]["description"]
    memory_search.__coworker_schema__ = _SCHEMA
    return memory_search
