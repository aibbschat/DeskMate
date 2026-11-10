"""DeskMate 首页资讯卡片后端。

为桌搭模式的新会话首页提供定时更新的资讯胶囊，聚焦「行业风向」：
- 网文 / 影视·短剧·AI剧 / AI圈 / 开源 四个分类；
- 每个分类先用 web_search（时间窗限定近一周）抓取最新网页结果；
- 再用 LLM 把搜索结果提炼成 3-4 条「标题 + 一句话摘要 + 搜索 query」卡片，
  且只保留明显是近期（近 1-2 周）动态、能反映风向/趋势的条目，旧闻/档案直接丢弃；
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
    WEBNOVEL = "webnovel"      # 网文风向
    ENT = "ent"                # 影视·短剧·AI剧 风向
    AI = "ai"                  # AI圈最新（Agent编程/大模型/公司动态）
    OPENSOURCE = "opensource"  # 开源最新项目


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


# 每个分类的搜索主题（具体到风向/细分赛道，减少「文化类噪音」式泛泛结果）
_CATEGORY_QUERIES: dict[DigestCategory, list[str]] = {
    DigestCategory.WEBNOVEL: [
        "起点 番茄 网文 平台 新政策 风向 2026",
        "网络文学 爆款 新书 榜单 趋势 2026",
        "网文 IP 影视化 改编 动态 2026",
    ],
    DigestCategory.ENT: [
        "短剧 行业 爆款 风向 最新 2026",
        "影视 剧集 热度 趋势 最新 2026",
        "AI 短剧 AI 剧 制作 新动态 2026",
    ],
    DigestCategory.AI: [
        "AI Agent 编程 工具 新发布 最新 2026",
        "大模型 发布 新模型 最新 2026",
        "DeepSeek 豆包 通义 价格 调整 融资 动态 2026",
    ],
    DigestCategory.OPENSOURCE: [
        "GitHub 开源 项目 新发布 热门 2026",
        "开源 AI 框架 工具 新动态 2026",
    ],
}

_CATEGORY_LABELS: dict[DigestCategory, str] = {
    DigestCategory.WEBNOVEL: "网文",
    DigestCategory.ENT: "影视短剧",
    DigestCategory.AI: "AI圈",
    DigestCategory.OPENSOURCE: "开源",
}

# 每个分类的「主题关键词」：搜索结果标题/摘要若不含任一关键词，视为跑题噪声，
# 在进 LLM 前提纯，避免 DDGS 对泛词返回的重卡销量、政府假期、Google 首页之类污染提炼。
_CATEGORY_KEYWORDS: dict[DigestCategory, tuple[str, ...]] = {
    DigestCategory.WEBNOVEL: (
        "网文", "小说", "起点", "番茄", "晋江", "作者", "平台", "ip", "改编",
        "文学", "连载", "签约", "稿酬", "作家",
    ),
    DigestCategory.ENT: (
        "剧", "短剧", "影视", "电影", "综艺", "播出", "票房", "主演", "导演",
        "网剧", "动漫", "纪录片", "长视频", "微短剧",
    ),
    DigestCategory.AI: (
        "ai", "人工智能", "大模型", "模型", "智能体", "agent", "机器人", "算力",
        "芯片", "openai", "deepseek", "字节", "谷歌", "微软", "claude", "gpt",
        "编程", "算法", "神经网络", "多模态",
    ),
    DigestCategory.OPENSOURCE: (
        "开源", "github", "代码", "框架", "项目", "程序员", "源码", "rust",
        "python", "开发", "仓库", "star", "贡献",
    ),
}


def _relevant(result: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    """结果标题/摘要是否命中本分类主题关键词，未命中视为跑题噪声。"""
    blob = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    return any(k.lower() in blob for k in keywords)


# 抓取时间窗：只取近一周的结果，从源头掐掉旧闻
_TIMELIMIT = "w"

# 搜索结果里混入了大量成人/垃圾站（DDGS 对中文 query 的召回噪声很大）：
# 这些结果一旦进 LLM，要么污染提炼、要么直接让模型返回非法 JSON 导致整类失败。
# 因此在进 LLM 前必须按「域名特征 + 成人关键词」全部滤掉。
_SPAM_DOMAIN_HINTS = (
    "aydvjch", "cc/category", "onlyfans", "xpicvid", "porns", "porny",
    "xvideos", "tnaflix", "redtube", "youjizz", "cpc", "h_ent", "91porn",
    "jable", "heydouga", "mgstage", "avmoo", "javbus",
)
_ADULT_KEYWORDS = (
    "成人", "探花", "黑料", "裸", "av", "番号", "约炮", "性爱", "色情",
    "onlyfans", "asmr", "巨乳", "御姐", "骚", "强奸", "激情", "网红黑料",
)


def _is_spam(result: dict[str, Any]) -> bool:
    """判断一条搜索结果是否是成人/垃圾站，需进 LLM 前剔除。"""
    url = str(result.get("url", "")).lower()
    title = str(result.get("title", "")).lower()
    snippet = str(result.get("snippet", "")).lower()
    blob = f"{url} {title} {snippet}"
    if any(h in url for h in _SPAM_DOMAIN_HINTS):
        return True
    if any(k in blob for k in _ADULT_KEYWORDS):
        return True
    return False

_DEFAULT_TTL_SECONDS = 4 * 60 * 60  # 4 小时
_CACHE_VERSION = 2  # 分类与查询已重构，旧缓存不兼容

# 资讯胶囊升格进长期记忆后的过期天数：趋势类信息会过时，带过期满足「外部信息要过期」红线。
# 用户拍板：写作营不接，只接 digest（AI 圈网文观察日记），且**直接升格进内部记忆**，
# 不走 L2 提议卡 / 不可信区候选队列。这里 7 天与 external_store 的 news/finance 档对齐。
_DIGEST_MEMORY_TTL_DAYS = 7


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


def _category_label(category_value: str) -> str:
    """把分类值安全映射成展示标签（网文/影视短剧/AI圈/开源）。非法值回退原值。"""
    try:
        return _CATEGORY_LABELS.get(DigestCategory(category_value), category_value)
    except ValueError:
        return category_value


def _persist_to_memory(items: list[DigestItem], base_dir: Path | None = None) -> int:
    """把资讯胶囊**直接升格**写入长期记忆（learned_history），带「外部·文化产业」溯源标。

    用户拍板（2026-09-06）：写作营不接，只接 digest（AI 圈网文观察日记），且直接升格进
    内部记忆，不走 L2 提议卡 / 不可信区候选队列。

    设计要点：
    - 溯源标「（外部·文化产业）」与外部信息 intake 的 provenance 约定一致，检索/展示时
      一眼能区分是小桔自己观察到的产业风向，而非用户口述事实；
    - fact 由「分类标签 + 标题 + 摘要」构成，配合 ``LearnedStore.append`` 的
      ``INSERT OR IGNORE``（按 fact 的 md5 指纹去重），保证同一条观察在多次刷新间
      幂等，不会重复堆积；
    - 带 ``valid_until`` 过期（默认 7 天）：趋势类信息会过时，满足「外部信息要过期」红线；
      因冲突走 IGNORE，重复写入不会刷新已存在条目的过期时间，过期语义稳定；
    - 任何异常 fail-open：记忆写入失败绝不拖累首页资讯链路。
    """
    if not items:
        return 0
    try:
        import datetime as _dt

        from deskmate.memory.learned_store import SOURCE_EXTERNAL, LearnedStore

        bd = base_dir or _base_dir()
        expiry = (
            _dt.datetime.now() + _dt.timedelta(days=_DIGEST_MEMORY_TTL_DAYS)
        ).isoformat(timespec="seconds")
        facts: list[str] = []
        for it in items:
            label = _category_label(it.category)
            facts.append(f"（外部·文化产业）{label}风向：{it.title}——{it.summary}")
        # 首页资讯是外部观察，不是萧哥口述：显式标 external（进参考层 + 7 天过期）。
        # 不标 → 数据层会按措辞兜底，但语义应由调用方说清，不能靠猜。
        return LearnedStore(base_dir=bd).append(
            facts, valid_until=expiry, source=SOURCE_EXTERNAL
        )
    except Exception as e:  # fail-open
        logger.warning("digest persist to memory failed: %s", e)
        return 0


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

    now = time.strftime("%Y年%m月%d日", time.localtime())
    # 注意：prompt 需简洁克制。早期版本带「recency 强约束 + 只选近期动态」等苛刻指令，
    # 实测会让模型对部分分类（如出版）直接返回空响应；简化后稳定产出合法 JSON。
    system = (
        "你是专注「{label}」行业风向的资讯编辑。下面是一组近一周的网页搜索结果（当前日期 {date}）。\n"
        "请精选 3-4 条最能反映近期行业风向/趋势的资讯，忽略明显旧闻与无关内容。\n"
        "只输出一个 JSON 数组，每条含：title（不超过 18 字，吸引眼球）、summary（一句话不超过 50 字，"
        "点明风向/意义）、query（5-15 字，用于让 AI 再次搜索该话题的查询词）、"
        "recency（依据时间线索取 '近期' / '本周' / '本月' 之一）。\n"
        "不要任何解释、Markdown 代码块或额外文字。"
    ).format(label=_CATEGORY_LABELS[category], date=now)

    user_parts = []
    for i, r in enumerate(search_results[:6], 1):
        title = r.get("title", "")
        snippet = r.get("snippet", "")
        if title or snippet:
            user_parts.append(f"[{i}] {title}\n{snippet}")
    if not user_parts:
        return []

    user = "\n\n".join(user_parts)

    last_raw = ""
    rows: Any = None
    # 空响应（模型服务限流常见）或解析失败时重试，逐次降温 + 退避
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(1.0 * (attempt + 1))  # 退避
        try:
            loop = asyncio.get_event_loop()
            raw = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: chat(system, user, temperature=0.3 if attempt else 0.5, max_tokens=1200),
                ),
                timeout=45.0,
            )
        except Exception:
            logger.exception("digest llm summarize failed (attempt %d)", attempt)
            continue
        last_raw = raw
        if not raw or not raw.strip():
            continue  # 重试
        # 容错解析：模型可能在 JSON 前后加寒暄/解释文字，提取首个 [ 到末个 ] 之间的内容
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("digest llm returned non-json (attempt %d): %s", attempt, raw[:300])
            continue  # 重试
        if isinstance(parsed, list) and parsed:
            rows = parsed
            break

    if not rows:
        if last_raw:
            logger.warning("digest llm gave no usable items: %s", last_raw[:300])
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
        recency = str(row.get("recency", "")).strip()
        if not title or not summary:
            continue
        # 护栏：LLM 若判定为旧闻则丢弃（理论上 prompt 已要求不选入）
        if recency == "旧闻":
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

    # 一个分类下多个 query 并行搜（限定近一周），合并去重后交给 LLM
    queries = _CATEGORY_QUERIES[category]
    searches = [asyncio.to_thread(tool, q, 5, _TIMELIMIT) for q in queries]
    results_by_query = await asyncio.gather(*searches, return_exceptions=True)

    seen_keys: set[str] = set()
    merged: list[dict[str, Any]] = []
    for res in results_by_query:
        if isinstance(res, Exception):
            continue
        if not isinstance(res, dict) or "results" not in res:
            continue
        for r in res.get("results", []):
            url = r.get("url", "")
            title = str(r.get("title", "")).strip()
            # 按「url + 标题」双重去重，并剔除成人/垃圾站
            key = f"{url}|{title}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if _is_spam(r):
                continue
            if not _relevant(r, _CATEGORY_KEYWORDS[category]):
                continue
            merged.append(r)

    if not merged:
        logger.info("digest %s 无可用（非垃圾）搜索结果，跳过", category.value)
        return []

    items = await _llm_summarize(category, merged)
    return items


async def _refresh_digest() -> list[DigestItem]:
    """刷新全部分类资讯。

    设计要点（面向模型服务限流/超时的现实）：
    - 加载旧缓存作为兜底与增量合并基础；
    - 适度并发（信号量=2）逐类抓取+提炼，每类带独立超时，避免单类卡死拖垮整轮；
    - **增量写盘 + 合并**：某类成功后立即把「该类新结果 + 其余类旧缓存」写回，
      因此即使后续分类超时/失败，已成功的分类也落盘、不丢失；
    - 某类本轮失败则沿用上次该类的条目，避免单次抖动把某类资讯清空一整个缓存周期。
    """
    cached = _read_cache()
    # 以旧缓存为基底：键=分类，值=条目列表。成功后覆盖，失败则保留。
    # 只接纳当前有效分类的条目，避免历史/异常分类污染结果。
    valid_cats = {c.value for c in DigestCategory}
    merged_by_cat: dict[str, list[DigestItem]] = {}
    if cached is not None:
        for it in cached[0]:
            if it.category in valid_cats:
                merged_by_cat.setdefault(it.category, []).append(it)

    sem = asyncio.Semaphore(2)

    def _flush() -> None:
        all_items = [i for items in merged_by_cat.values() for i in items]
        if all_items:
            _write_cache(all_items)

    any_fresh = False
    fresh_items: list[DigestItem] = []  # 本轮成功新抓取的条目，用于落长期记忆

    async def _guarded(cat: DigestCategory) -> None:
        nonlocal any_fresh
        async with sem:
            try:
                items = await asyncio.wait_for(_fetch_one_category(cat), timeout=75)
            except Exception:
                logger.exception("digest category %s failed/timeout", cat.value)
                return  # 保留 merged_by_cat 中该类的旧条目（若有）
            if items:
                # 增量合并：本类用新结果替换，其他类沿用缓存，立即写盘
                merged_by_cat[cat.value] = items
                any_fresh = True
                fresh_items.extend(items)
                _flush()

    await asyncio.gather(*[_guarded(cat) for cat in DigestCategory])
    _flush()
    all_items = [i for items in merged_by_cat.values() for i in items]

    # 直接升格进内部记忆：只落本轮新抓取的条目（缓存条目已在过往轮次落过；
    # append 的 INSERT OR IGNORE 保证重复写入不刷新已存在条目的过期时间）。
    if fresh_items:
        try:
            _persist_to_memory(fresh_items)
        except Exception:  # 兜底，绝不拖累资讯链路
            pass

    return all_items, any_fresh


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

    items, refreshed = await _refresh_digest()
    if not refreshed and cached is not None:
        # 整轮刷新都没能成功产出新数据，但有旧缓存 → 静默降级到旧缓存
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
        "fresh": bool(items) and refreshed,
    }
