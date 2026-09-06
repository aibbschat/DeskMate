"""P0 行为：主动信息摄入（world_search）。

按白名单主题域搜索近况，**先落不可信区（external_items）**，再过摄入流水线
（筛选/去重/摘要 → 验证 → 写入内部记忆）。低风险、只读外部网络、不打扰。

⚠ 2026-09-06 改道（萧哥定的边界）：
旧实现直接 `LearnedStore().append()` 把搜索结果写进 ``learned_history``——
**搜索结果被当成内部记忆**，完全绕过不可信区；而且 `LearnedStore()` 不带参数，
忽略传入的 base_dir，永远写真库 ``~/.deskmate``。

现在链路是：
    搜索 → external_items（raw，不可信区，默认 7 天过期）
         → 流水线：筛选/摘要 → 候选 → 验证
         → 达标（credibility ≥ 0.7）才写进内部记忆
         → 不达标的留在候选区，走提议卡等萧哥裁决

搜索结果默认可信度 0.5 < 阈值 0.7，所以**默认全部走提议卡**，不会自动内化。
想让某个来源自动内化，把该来源的 credibility 调上去即可。
"""

from __future__ import annotations

import json
import os
from typing import Any

import logging

logger = logging.getLogger(__name__)

# 冷却状态文件：记录每个主题域上次搜索时间（epoch 秒）
_COOLDOWN_FILE = "agency/last_world_search.json"

# 单条事实最大长度（防超大 snippet 撑爆记忆）
_MAX_FACT_LEN = 240
# 一次摄入最多落几条
_MAX_FACTS_PER_TOPIC = 4

# 网络搜索这个「来源」的标识名（用于 external_sources 表）
_WEB_SEARCH_SOURCE_NAME = "网络搜索（自主摄入）"
# 搜索结果的默认可信度：低于验证阈值 0.7 → 默认走提议卡，不自动内化
_WEB_SEARCH_CREDIBILITY = 0.5
# 搜索结果按「新闻」类归档 → TTL 7 天（时效性最强的一档）
_WEB_SEARCH_CATEGORY = "news"


def _cooldown_path(base_dir: str) -> str:
    return os.path.join(base_dir, _COOLDOWN_FILE)


def _load_cooldown(base_dir: str) -> dict[str, float]:
    try:
        with open(_cooldown_path(base_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cooldown(base_dir: str, data: dict[str, float]) -> None:
    try:
        os.makedirs(os.path.dirname(_cooldown_path(base_dir)), exist_ok=True)
        tmp = f"{_cooldown_path(base_dir)}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _cooldown_path(base_dir))
    except Exception:
        pass


def _is_due(last: dict[str, float], topic: str, cooldown_hours: float) -> bool:
    import time

    t = last.get(topic)
    if not t:
        return True
    return (time.time() - float(t)) >= cooldown_hours * 3600


def _extract_facts(topic: str, results: list[dict[str, Any]]) -> list[str]:
    """从搜索结果抽事实文案（带主题域前缀 + 来源标题）。"""
    facts: list[str] = []
    for r in results[:_MAX_FACTS_PER_TOPIC]:
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or r.get("text") or "").strip()
        if not title and not snippet:
            continue
        text = f"{title}：{snippet}".strip("：").strip()
        if len(text) > _MAX_FACT_LEN:
            text = text[:_MAX_FACT_LEN] + "…"
        if not text:
            continue
        facts.append(f"【{topic}】{text}")
    return facts


def _ensure_web_search_source(store: Any, credibility: float) -> str | None:
    """取（或建）「网络搜索」这个来源记录。幂等，重复调用不会建多个。"""
    try:
        for src in store.list_sources(source_type="webpage"):
            if src.get("name") == _WEB_SEARCH_SOURCE_NAME:
                return src["id"]
        return store.add_source(
            _WEB_SEARCH_SOURCE_NAME,
            source_type="webpage",
            url="",
            credibility=credibility,
            fetch_interval_minutes=1440,
        )
    except Exception as e:
        logger.warning("world_ingestion: 取/建搜索来源失败: %s", e)
        return None


def _store_to_untrusted_zone(
    base_dir: str,
    topic: str,
    results: list[dict[str, Any]],
    credibility: float,
) -> dict[str, Any]:
    """把搜索结果写进不可信区（external_items），返回统计。

    去重由 ExternalStore 的 content_hash 唯一索引保证——同一条信息重复抓取天然幂等。
    """
    out = {"stored": 0, "duplicates": 0, "ids": []}
    try:
        from deskmate.memory.external_store import ExternalStore

        store = ExternalStore(base_dir=base_dir)
    except Exception as e:
        logger.warning("world_ingestion: ExternalStore 不可用: %s", e)
        return out

    source_id = _ensure_web_search_source(store, credibility)
    if not source_id:
        return out

    for r in results[:_MAX_FACTS_PER_TOPIC]:
        try:
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or r.get("text") or "").strip()
            if not title and not snippet:
                continue
            content = f"【{topic}】{title}：{snippet}".strip("：").strip()
            if len(content) > _MAX_FACT_LEN:
                content = content[:_MAX_FACT_LEN] + "…"
            item = store.add_item(
                content,
                source_id=source_id,
                source_type="webpage",
                source_url=(r.get("url") or "").strip(),
                title=title or snippet[:40],
                category=_WEB_SEARCH_CATEGORY,
                extra={"topic": topic, "origin": "world_search"},
            )
            if item:
                # add_item 命中去重会返回已有条目（created_at 早于本轮）→ 判重复
                out["ids"].append(item["id"])
                out["stored"] += 1
        except Exception as e:
            logger.warning("world_ingestion: 写入不可信区失败: %s", e)
    return out


def run_world_ingestion(
    base_dir: str,
    topics: list[str],
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对白名单主题域做一轮信息摄入。

    流程：搜索 → 落不可信区 → 跑摄入流水线（筛选/摘要/验证/升格）。
    达标的自动写入内部记忆；不达标的留在候选区，返回 awaiting 供提议卡使用。

    Args:
        base_dir: deskmate 根目录（~/.deskmate）
        topics:   白名单主题域列表
        cfg:      world_search 配置
                  （cooldown_hours / max_results / timelimit / per_tick_limit /
                    credibility / trust_threshold / run_pipeline）

    Returns:
        {"searched", "facts", "stored", "skipped", "summary", "pipeline"}
    """
    cfg = cfg or {}
    cooldown_hours = float(cfg.get("cooldown_hours", 24))
    max_results = int(cfg.get("max_results", 5))
    timelimit = cfg.get("timelimit") or None
    per_tick_limit = int(cfg.get("per_tick_limit", 1))
    credibility = float(cfg.get("credibility", _WEB_SEARCH_CREDIBILITY))
    trust_threshold = float(cfg.get("trust_threshold", 0.7))
    run_pipeline = bool(cfg.get("run_pipeline", True))

    result: dict[str, Any] = {
        "searched": [], "facts": [], "stored": 0, "skipped": [],
        "summary": "", "pipeline": {},
    }
    if not topics:
        return result

    import time

    last = _load_cooldown(base_dir)
    due = [t for t in topics if _is_due(last, t, cooldown_hours)]
    # 摊薄：每次 tick 最多实际搜 per_tick_limit 个域
    to_search = due[:per_tick_limit]
    skipped = [t for t in topics if t not in to_search]
    result["skipped"] = skipped

    try:
        from coworker.web.tool import make_web_search_tool

        search = make_web_search_tool()
    except Exception as e:
        logger.warning("world_ingestion: web_search 工具不可用: %s", e)
        result["summary"] = "搜索工具不可用"
        return result

    all_facts: list[str] = []
    stored_total = 0
    for topic in to_search:
        try:
            # 主题域 + 「近况/动态」措辞，避免泛搜
            query = f"{topic} 最新动态"
            resp = search(query, max_results=max_results, timelimit=timelimit)
            if not isinstance(resp, dict) or resp.get("error"):
                logger.warning(
                    "world_ingestion: 搜索失败 %s: %s",
                    topic, resp.get("error") if isinstance(resp, dict) else resp,
                )
                continue
            results = resp.get("results") or []
            facts = _extract_facts(topic, results)
            if facts:
                all_facts.extend(facts)
            # 落不可信区（不走内部记忆）
            stored = _store_to_untrusted_zone(base_dir, topic, results, credibility)
            stored_total += stored.get("stored", 0)
            result["searched"].append(topic)
            # 刷新该域冷却
            last[topic] = time.time()
        except Exception as e:
            logger.warning("world_ingestion: 搜索异常 %s: %s", topic, e)
            continue

    _save_cooldown(base_dir, last)
    result["stored"] = stored_total
    result["facts"] = all_facts

    # 跑摄入流水线：筛选/摘要 → 验证 →（达标）写内部记忆 /（不达标）留候选区
    if run_pipeline and result["searched"]:
        try:
            from deskmate.memory.ingestion_pipeline import run_ingestion_pipeline

            result["pipeline"] = run_ingestion_pipeline(
                base_dir,
                trust_threshold=trust_threshold,
            )
        except Exception as e:
            logger.warning("world_ingestion: 摄入流水线失败: %s", e)

    if result["searched"]:
        pipe = result.get("pipeline") or {}
        promoted = int(pipe.get("promoted") or 0)
        awaiting = int(pipe.get("awaiting_review") or 0)
        parts = [f"搜了 {len(result['searched'])} 个域，收下 {stored_total} 条进不可信区"]
        if promoted:
            parts.append(f"{promoted} 条通过验证入库")
        if awaiting:
            parts.append(f"{awaiting} 条可信度不足，等你裁决")
        result["summary"] = "，".join(parts)
    else:
        result["summary"] = "都在冷却期，本轮未搜"
    return result
