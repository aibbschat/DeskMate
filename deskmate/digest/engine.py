"""DeskMate 首页资讯卡片后端。

为桌搭模式的新会话首页提供定时更新的资讯胶囊：
- 文化、网文、AI、开源 四个分类；
- 每个分类先用 web_search 抓取最新网页结果；
- 再用 LLM 把搜索结果提炼成 3-4 条「标题 + 一句话摘要 + 搜索 query」卡片；
- 缓存到本地（默认 4 小时 TTL），过期自动刷新；
- 前端点击卡片后不是跳外链，而是把 query 交给 AI 搜索并总结。

失败时静默降级：返回旧缓存或空列表，绝不阻断首页加载。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from coworker.web.tool import make_web_search_tool

logger = logging.getLogger(__name__)


class DigestCategory(str, Enum):
    CULTURE = "culture"
    WEBNOVEL = "webnovel"
    AI = "ai"
    OPENSOURCE = "opensource"


@dataclass(frozen=True, slots=True)
class DigestItem:
    """一条资讯卡片。"""

    id: str
    category: str
    title: str
    summary: str
    query: str
    source_count: int
    freshness: str  # 如 "2 小时前"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 每个分类的搜索主题（更具体，减少无关结果）
_CATEGORY_QUERIES: dict[DigestCategory, list[str]] = {
    DigestCategory.CULTURE: [
        "中国传统文化 历史 考古 新发现 2026",
        "博物馆 展览 文化遗产 最新 2026",
    ],
    DigestCategory.WEBNOVEL: [
        "起点中文网 热门小说 榜单 2026",
        "晋江文学城 网络小说 新书 趋势 2026",
        "网络文学 作家 行业动态 2026",
    ],
    DigestCategory.AI: [
        "人工智能 大模型 最新发布 新闻 2026",
        "AI 工具 应用 新进展 2026",
    ],
    DigestCategory.OPENSOURCE: [
        "GitHub 热门开源项目 2026",
        "开源软件 新工具 推荐 2026",
    ],
}

_CATEGORY_LABELS: dict[DigestCategory, str] = {
    DigestCategory.CULTURE: "文化",
    DigestCategory.WEBNOVEL: "网文",
    DigestCategory.AI: "AI",
    DigestCategory.OPENSOURCE: "开源",
}

_DEFAULT_TTL_SECONDS = 4 * 60 * 60  # 4 小时
_CACHE_VERSION = 1


def _base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))


def _cache_path() -> Path:
    return _base_dir() / "digest_cache.json"


def _stable_id(category: DigestCategory, title: str) -> str:
    h = hashlib.sha1(f"{category.value}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"{category.value}-{h}"


def _relative_time(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def _read_cache() -> tuple[list[DigestItem], float] | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _CACHE_VERSION:
            return None
        items = [DigestItem(**i) for i in data.get("items", [])]
        return items, data.get("updated_at", 0)
    except Exception:
        logger.exception("read digest cache failed")
        return None


def _write_cache(items: list[DigestItem]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": _CACHE_VERSION,
                    "updated_at": time.time(),
                    "items": [i.to_dict() for i in items],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        logger.exception("write digest cache failed")


async def _llm_summarize(category: DigestCategory, search_results: list[dict[str, Any]]) -> list[DigestItem]:
    """用 LLM 把搜索结果提炼成资讯卡片。"""
    from deskmate.memory.llm import chat, llm_available

    if not search_results:
        return []
    if not llm_available():
        logger.warning("llm not available for digest summarization")
        return []

    system = (
        "你是一位资讯编辑。请根据下面网页搜索结果，为「{}」频道精选 3-4 条最值得展示的资讯。"
        "必须满足："
        "1. 只挑选与「{}」真正相关的结果，明显不相关的直接忽略；"
        "2. 每条输出 JSON 对象：title（不超过 18 个字，吸引眼球）、summary（一句话，不超过 50 字）、"
        "query（用于让 AI 再次搜索并总结该话题的查询词，5-15 字）；"
        "3. 只输出一个 JSON 数组，不要任何解释、Markdown 代码块或额外文字。"
    ).format(_CATEGORY_LABELS[category], _CATEGORY_LABELS[category])

    user_parts = []
    for i, r in enumerate(search_results[:8], 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        if title or snippet:
            user_parts.append(f"[{i}] {title}\n{snippet}")
    if not user_parts:
        return []

    user = "\n\n".join(user_parts)

    try:
        loop = asyncio.get_event_loop()
        raw = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: chat(system, user, temperature=0.5, max_tokens=1200)),
            timeout=90.0,
        )
    except Exception:
        logger.exception("digest llm summarize failed")
        return []

    # 清理可能的代码块
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("digest llm returned non-json: %s", text[:200])
        return []

    if not isinstance(rows, list):
        return []

    items: list[DigestItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).strip()
        summary = str(row.get("summary", "")).strip()
        query = str(row.get("query", "")).strip()
        if not title or not summary:
            continue
        if not query:
            query = title
        items.append(
            DigestItem(
                id=_stable_id(category, title),
                category=category.value,
                title=title,
                summary=summary,
                query=query,
                source_count=len(search_results),
                freshness="刚刚",
            )
        )
    return items


async def _fetch_one_category(category: DigestCategory) -> list[DigestItem]:
    """抓取并总结一个分类的资讯。"""
    try:
        tool = make_web_search_tool()
    except Exception:
        logger.exception("build web_search tool failed")
        return []

    # 一个分类下多个 query 并行搜，合并去重后交给 LLM
    queries = _CATEGORY_QUERIES[category]
    searches = [asyncio.to_thread(tool, q, 5) for q in queries]
    results_by_query = await asyncio.gather(*searches, return_exceptions=True)

    seen_urls: set[str] = set()
    merged: list[dict[str, Any]] = []
    for res in results_by_query:
        if isinstance(res, Exception):
            continue
        if not isinstance(res, dict) or "results" not in res:
            continue
        for r in res.get("results", []):
            url = r.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            merged.append(r)

    items = await _llm_summarize(category, merged)
    return items


async def _refresh_digest() -> list[DigestItem]:
    """刷新全部分类资讯。"""
    tasks = [_fetch_one_category(cat) for cat in DigestCategory]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items: list[DigestItem] = []
    for cat, res in zip(DigestCategory, results):
        if isinstance(res, Exception):
            logger.exception("fetch digest category %s failed", cat.value)
            continue
        all_items.extend(res)

    if all_items:
        _write_cache(all_items)
    return all_items


async def get_digest(
    *,
    force: bool = False,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """获取资讯卡片。

    Args:
        force: 为 True 时忽略缓存强制刷新。
        ttl_seconds: 缓存有效期。

    Returns:
        {"items": [...], "updated_at": float, "fresh": bool}
    """
    cached = _read_cache()
    now = time.time()

    if not force and cached is not None:
        items, updated_at = cached
        if now - updated_at < ttl_seconds:
            # 用缓存时更新 freshness 字段
            fresh_items = [
                DigestItem(
                    id=i.id,
                    category=i.category,
                    title=i.title,
                    summary=i.summary,
                    query=i.query,
                    source_count=i.source_count,
                    freshness=_relative_time(updated_at),
                )
                for i in items
            ]
            return {
                "items": [i.to_dict() for i in fresh_items],
                "updated_at": updated_at,
                "fresh": True,
            }

    items = await _refresh_digest()
    if not items and cached is not None:
        # 刷新失败但有旧缓存，静默降级
        items, updated_at = cached
        stale_items = [
            DigestItem(
                id=i.id,
                category=i.category,
                title=i.title,
                summary=i.summary,
                query=i.query,
                source_count=i.source_count,
                freshness=_relative_time(updated_at),
            )
            for i in items
        ]
        return {
            "items": [i.to_dict() for i in stale_items],
            "updated_at": updated_at,
            "fresh": False,
        }

    return {
        "items": [i.to_dict() for i in items],
        "updated_at": time.time() if items else (cached[1] if cached else 0),
        "fresh": bool(items),
    }
