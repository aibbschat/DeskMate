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

#: 文件层 → (source_kind 列表, section 关键字)
LAYERS: dict[str, tuple[list[str], Optional[str]]] = {
    "hard_rule": (["user"], "硬规矩"),
    "preference": (["user"], "偏好与事实"),
    "memory": (["memory"], None),
    "diary": (["diary_view", "diary_day"], None),
    "vault": (["vault"], None),
}

#: 非文件层：项目记忆 / learned_history / 文风桶。
#: 2026-11-11 复检修（m5）：内置工具与 MCP 侧同名工具原本能力不一致，且内置
#: 后注册会**遮蔽** MCP 版（ToolRegistry 同名覆盖），导致项目记忆与 learned_history
#: 在桌面端整块搜不到。这里把三层补进来，做成 MCP 版的语义超集。
EXTRA_LAYERS = ("project", "learned", "style")

_TERM_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]{2,}")
_STYLE_QUERY_RE = re.compile(r"文风|风格|笔法|语感|文笔|行文|审稿|写作习惯")


def _query_terms(q: str) -> set[str]:
    """项目/文风这类无索引层的轻量匹配词：中文取 2-gram，英文取 2 字以上词。"""
    out: set[str] = set()
    for run in _TERM_RE.findall(q or ""):
        if re.fullmatch(r"[A-Za-z0-9_]+", run):
            out.add(run.lower())
            continue
        if len(run) <= 4:
            out.add(run)
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return {t for t in out if t}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "检索小桔自己的长期记忆（只读）：用户画像（硬规矩/偏好）、全局记忆、"
            "日记、Obsidian vault、项目记忆（projects/）、长期记忆库"
            "（learned_history 里的口述事实/创作裁定）、文风桶（书稿/聊天风格画像）。"
            "用于回答「我之前说过什么」「那本书的稿子在哪」「他喜欢的音色」"
            "「他写东西什么文风」这类需要翻旧账的问题。\n"
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
                    "enum": [
                        "", "hard_rule", "preference", "memory", "diary", "vault",
                        "project", "learned", "style",
                    ],
                    "description": (
                        "可选：限定记忆层。留空表示全层检索（文件层 + 项目记忆 + "
                        "长期记忆库；文风桶仅在问及文风/风格时带入）。"
                    ),
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


def _collect_project(query: str, root: Path, limit: int) -> list[dict[str, Any]]:
    """项目记忆（projects/<name>/*.md）只读检索；无索引依赖，直接读文件。"""
    from deskmate.memory.project_memory import list_projects, load_project_memory

    terms = _query_terms(query)
    if not terms:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for name in list_projects(root):
        for fname, content in load_project_memory(root, name).items():
            low = content.lower()
            score = sum(1 for t in terms if t.lower() in low)
            if not score:
                continue
            scored.append((score, {
                "content": content[:1000],
                "source": str(root / "projects" / name / fname),
                "section": f"项目记忆:{name}",
                "score": 0.9,
            }))
    scored.sort(key=lambda x: -x[0])
    return [h for _, h in scored[:limit]]


def _collect_learned(query: str, root: Path, limit: int) -> list[dict[str, Any]]:
    """learned_history（口述事实/创作裁定）只读检索，口径与每轮注入同源。"""
    from deskmate.memory.user_fact_context import search_user_facts

    hits = search_user_facts(root, query, limit=limit, read_only=True)
    return [
        {
            "content": str(fact),
            "source": f"learned_history:{src}" if src else "learned_history",
            "section": "learned_history",
            "date": str(ts or "")[:10],
            "score": round(0.85 - i * 0.01, 3),
        }
        for i, (ts, fact, src) in enumerate(hits)
    ]


def _collect_style(root: Path) -> list[dict[str, Any]]:
    """文风桶（书稿桶 / 聊天桶）只读读取；不建库、不迁移。"""
    from deskmate.style_profile_store import (
        BUCKET_CHAT,
        BUCKET_MANUSCRIPT,
        format_profile_readonly,
    )

    out: list[dict[str, Any]] = []
    for bucket, label in ((BUCKET_MANUSCRIPT, "文风·书稿桶"), (BUCKET_CHAT, "文风·聊天桶")):
        try:
            text = format_profile_readonly(root, bucket)
        except Exception:  # noqa: BLE001 —— 画像坏了当没有，不拖垮检索
            continue
        if text and text.strip():
            out.append({
                "content": text,
                "source": "style_profile",
                "section": label,
                "score": 0.6,
            })
    return out


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
        if layer and layer not in LAYERS and layer not in EXTRA_LAYERS:
            return {
                "error": "layer 只能是 ''/" + "/".join((*LAYERS, *EXTRA_LAYERS)),
                "count": 0,
                "results": [],
            }
        n = min(20, max(1, int(limit or 8)))
        hits: list[dict[str, Any]] = []
        warning = ""
        if layer not in EXTRA_LAYERS:
            kinds, sec_kw = LAYERS.get(layer, (None, None))
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
            except Exception as exc:  # noqa: BLE001 —— 索引坏了不拖垮会话
                if layer:
                    return {"error": f"索引不可用：{exc}", "count": 0, "results": []}
                # 全层检索：索引坏了仍可查项目/库/文风，别整轮丢光。
                warning = f"索引不可用：{exc}"
        # 非文件层（项目记忆 / learned_history / 文风桶）：fail-open，坏了当没查到。
        # 全层检索时给每个补充层限流，避免它们把 n 个槽位全占掉（尤其文风桶
        # 只在问文风时才带，必须留得下）。
        all_mode = layer == ""
        extras: list[dict[str, Any]] = []
        for fn, want in (
            (lambda: _collect_style(root),
             layer == "style" or (all_mode and bool(_STYLE_QUERY_RE.search(q)))),
            (lambda: _collect_project(q, root, min(2, n) if all_mode else n),
             layer in ("", "project")),
            (lambda: _collect_learned(q, root, min(3, n) if all_mode else n),
             layer in ("", "learned")),
        ):
            if not want:
                continue
            try:
                extras.extend(fn())
            except Exception:  # noqa: BLE001
                pass

        # 文件层在前、补充层在后；总条数超限时先压文件层，保证补充层能露头
        # （否则 8 条文件命中会把「项目/库/文风」整层挤掉，等于没补）。
        if extras and hits:
            extras = extras[: max(2, n // 2)]
            ordered = hits[: max(0, n - len(extras))] + extras
        else:
            ordered = hits + extras
        out: list[dict[str, Any]] = []
        for h in ordered:
            d = h.get("date") or _entry_date(h)
            # 日期过滤：能解析出日期的才过滤；无日期的条目保留（宁多勿漏）
            if d:
                if since and d < since:
                    continue
                if until and d > until:
                    continue
            out.append({
                "content": h.get("content", ""),
                "source": h.get("source_path") or h.get("source", ""),
                "section": h.get("section", ""),
                "date": d,
                "score": h.get("score", 0.0),
            })
        # 文件层在前、补充层在后；超限时截尾部（补充层优先保留）。
        out = out[:n]
        res = {"query": q, "layer": layer, "count": len(out), "results": out}
        if warning:
            res["warning"] = warning
        return res

    memory_search.__name__ = "memory_search"
    memory_search.__doc__ = _SCHEMA["function"]["description"]
    memory_search.__coworker_schema__ = _SCHEMA
    return memory_search
