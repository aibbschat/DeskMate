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
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# 冷却状态文件：记录每个主题域上次搜索时间（epoch 秒）
_COOLDOWN_FILE = "agency/last_world_search.json"

# 单条事实最大长度（防超大 snippet 撑爆记忆）
_MAX_FACT_LEN = 240
# 一次摄入最多落几条
_MAX_FACTS_PER_TOPIC = 4

# 网络搜索这个「来源」的标识名（用于 external_sources 表）
_WEB_SEARCH_SOURCE_NAME = "网络搜索（自主摄入）"
# 兜底可信度：provider 没给权威等级时（如 duckduckgo），仍按「未经验证」处理
_WEB_SEARCH_CREDIBILITY = 0.5

# 豆包搜索 AuthInfoLevel → credibility。
#
# 这是「可信边界」从瞎猜变成有据可依的关键：以前所有搜索结果一律 0.5，
# 于是全部卡在 0.7 阈值下面走提议卡，闸门形同虚设。现在按信源权威度分级：
#   1 非常权威（央媒/政府/学术）→ 0.9  自动过闸
#   2 正常权威（主流媒体）       → 0.75 自动过闸（实测新闻绝大多数是这档）
#   3 一般                       → 0.45 走提议卡
#   4 低                         → 0.20 走提议卡
_AUTH_LEVEL_CREDIBILITY: dict[int, float] = {1: 0.9, 2: 0.75, 3: 0.45, 4: 0.2}


def credibility_from_auth_level(level: Any) -> float:
    """AuthInfoLevel → credibility。拿不到等级就退回兜底值（保守）。"""
    if level is None:
        return _WEB_SEARCH_CREDIBILITY
    try:
        return _AUTH_LEVEL_CREDIBILITY.get(int(level), _WEB_SEARCH_CREDIBILITY)
    except (TypeError, ValueError):
        return _WEB_SEARCH_CREDIBILITY


def _is_fresh(publish_time: str, max_age_days: float) -> bool:
    """按发布时间做新鲜度兜底校验。

    服务端 TimeRange 实测不可靠（放过过半年前的旧闻），而且「9月6日」这种
    不带年份的 query 会召回去年的同名日期。所以摄入端必须自己卡一道。

    ⚠ 拿不到发布时间时**放行**（返回 True）——只有豆包这类 provider 会返回
    PublishTime，其它 provider（duckduckgo）压根没有这个字段。若一律丢弃，
    换 provider 就等于摄入功能整体失效。此时只能退而信服务端 TimeRange，
    调用方会把这批计入 `unknown_age` 便于观察。
    """
    if not publish_time:
        return True
    if max_age_days <= 0:
        return True
    try:
        from datetime import datetime, timedelta, timezone

        s = publish_time.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= datetime.now(timezone.utc) - timedelta(days=max_age_days)
    except Exception:
        # 解析不了 → 无法证明新鲜，但也可能是新格式 → 放行（不误杀）
        return True


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


def _ensure_web_search_source(store: Any) -> str | None:
    """取（或建）「网络搜索」这个来源记录。幂等，重复调用不会建多个。

    来源级 credibility 只是兜底值——每条信息实际用不用，看它自己的
    AuthInfoLevel（见 credibility_from_auth_level）。
    """
    try:
        for src in store.list_sources(source_type="webpage"):
            if src.get("name") == _WEB_SEARCH_SOURCE_NAME:
                return src["id"]
        return store.add_source(
            _WEB_SEARCH_SOURCE_NAME,
            source_type="webpage",
            url="",
            credibility=_WEB_SEARCH_CREDIBILITY,
            fetch_interval_minutes=1440,
        )
    except Exception as e:
        logger.warning("world_ingestion: 取/建搜索来源失败: %s", e)
        return None


def _dedup_key(title: str) -> str:
    """标题归一化 key——同一事件多家媒体报道，标题只差站点后缀。

    「伊朗革命卫队称打击了美航母和驱逐舰-新华网」和
    「伊朗革命卫队称打击了美航母和驱逐舰」是同一件事，
    content_hash 去重抓不到（正文不同），得靠标题。
    """
    if not title:
        return ""
    t = re.split(r"[-_|]", title)[0].strip()  # 砍掉 -新华网 / _央广网 这类后缀
    t = re.sub(r"\s+", "", t)
    return t[:28]


def _store_to_untrusted_zone(
    base_dir: str,
    topic: str,
    results: list[dict[str, Any]],
    *,
    category: str = "news",
    max_age_days: float = 7,
) -> dict[str, Any]:
    """把搜索结果写进不可信区（external_items），返回统计。

    每条结果的可信度**按它自己的 AuthInfoLevel 定**，不再一刀切 0.5——
    权威媒体的报道能自动过闸，野鸡来源仍然卡在提议卡。

    去重由 ExternalStore 的 content_hash 唯一索引保证——同一条信息重复抓取天然幂等。
    """
    out = {"stored": 0, "duplicates": 0, "stale": 0, "ids": []}
    try:
        from deskmate.memory.external_store import ExternalStore

        store = ExternalStore(base_dir=base_dir)
    except Exception as e:
        logger.warning("world_ingestion: ExternalStore 不可用: %s", e)
        return out

    source_id = _ensure_web_search_source(store)
    if not source_id:
        return out

    # 已收过的标题 key（跨轮去重：昨天收过的新闻今天不该再收一遍）
    existing_keys: set[str] = set()
    try:
        for it in store.list_items(status=None, limit=300):
            k = ((it.get("extra") or {}).get("title_key") or "")
            if k:
                existing_keys.add(k)
    except Exception:
        pass

    for r in results[:_MAX_FACTS_PER_TOPIC]:
        try:
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or r.get("text") or "").strip()
            if not title and not snippet:
                continue
            # 新鲜度兜底：服务端 TimeRange 不可靠，这里按 PublishTime 再卡一道
            if not _is_fresh((r.get("publish_time") or ""), max_age_days):
                out["stale"] += 1
                continue
            # ⚠ 不要把 title 拼进 content：title 已单独存字段，
            # 管线摘要时会再拼一次「title：content」，这里塞进去就变成标题出现两遍。
            # 只保留【域】标签——检索时能看出这条属于哪个域，且标题不冗余。
            content = f"【{topic}】{snippet or title}".strip()
            if len(content) > _MAX_FACT_LEN:
                content = content[:_MAX_FACT_LEN] + "…"
            auth_level = r.get("auth_level")
            title_key = _dedup_key(title)
            if title_key and title_key in existing_keys:
                # 同一事件多家媒体报道（新华网/人民网各报一遍）→ 只收第一条
                out["duplicates"] += 1
                continue
            item = store.add_item(
                content,
                source_id=source_id,
                source_type="webpage",
                source_url=(r.get("url") or "").strip(),
                title=title or snippet[:40],
                category=category,
                credibility=credibility_from_auth_level(auth_level),
                extra={
                    "topic": topic,
                    "origin": "world_search",
                    "auth_level": auth_level,
                    "site_name": (r.get("site_name") or "").strip(),
                    "publish_time": (r.get("publish_time") or "").strip(),
                    "title_key": title_key,
                },
            )
            if item:
                # add_item 命中去重会返回已有条目（created_at 早于本轮）→ 判重复
                out["ids"].append(item["id"])
                out["stored"] += 1
                if title_key:
                    existing_keys.add(title_key)
        except Exception as e:
            logger.warning("world_ingestion: 写入不可信区失败: %s", e)
    return out


def run_world_ingestion(
    base_dir: str,
    topics: list[str],
    cfg: dict[str, Any] | None = None,
    topic_specs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """对白名单主题域做一轮信息摄入。

    流程：搜索 → 落不可信区（每条按 AuthInfoLevel 定可信度）→ 跑摄入流水线。
    达标的自动写入内部记忆；不达标的留在候选区，返回 awaiting 供提议卡使用。

    Args:
        base_dir: deskmate 根目录（~/.deskmate）
        topics:   白名单主题域列表
        cfg:      world_search 配置
                  （cooldown_hours / max_results / timelimit / per_tick_limit /
                    provider / trust_threshold / run_pipeline）
        topic_specs: 每个域的搜索规格（query 模板/时间窗/category/冷却/新鲜度）

    Returns:
        {"searched", "facts", "stored", "stale", "skipped", "summary", "pipeline", "queries"}
    """
    cfg = cfg or {}
    default_cooldown = float(cfg.get("cooldown_hours", 24))
    max_results = int(cfg.get("max_results", 5))
    default_timelimit = cfg.get("timelimit") or None
    per_tick_limit = int(cfg.get("per_tick_limit", 2))
    provider_name = str(cfg.get("provider") or "")
    trust_threshold = float(cfg.get("trust_threshold", 0.7))
    run_pipeline = bool(cfg.get("run_pipeline", True))

    result: dict[str, Any] = {
        "searched": [], "facts": [], "stored": 0, "stale": 0, "skipped": [],
        "summary": "", "pipeline": {}, "queries": {},
    }
    if not topics:
        return result

    from ..settings import resolve_topic_query, topic_spec

    specs = topic_specs if isinstance(topic_specs, dict) else None

    import time

    last = _load_cooldown(base_dir)
    # 每个域按自己的冷却判断（天下要闻 12h，人文现场 48h）
    due = []
    for t in topics:
        cd = float(topic_spec(t, specs).get("cooldown_hours", default_cooldown))
        if _is_due(last, t, cd):
            due.append(t)
    # 摊薄：每次 tick 最多实际搜 per_tick_limit 个域
    to_search = due[:per_tick_limit]
    skipped = [t for t in topics if t not in to_search]
    result["skipped"] = skipped

    # 指定 provider（豆包：唯一返回信源权威等级的）；失败则退回默认
    prov = None
    if provider_name:
        try:
            from coworker.web.providers import build_provider

            prov = build_provider(provider_name)
        except Exception as e:
            logger.warning("world_ingestion: provider %s 不可用，退回默认: %s", provider_name, e)
    try:
        from coworker.web.tool import make_web_search_tool

        search = make_web_search_tool(provider=prov)
    except Exception as e:
        logger.warning("world_ingestion: web_search 工具不可用: %s", e)
        result["summary"] = "搜索工具不可用"
        return result

    all_facts: list[str] = []
    stored_total = 0
    stale_total = 0
    for topic in to_search:
        try:
            spec = topic_spec(topic, specs)
            query = resolve_topic_query(topic, specs)
            result["queries"][topic] = query
            resp = search(
                query,
                max_results=max_results,
                timelimit=spec.get("timelimit") or default_timelimit,
            )
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
            # 落不可信区（不走内部记忆）：每条按自己的权威等级定可信度
            stored = _store_to_untrusted_zone(
                base_dir,
                topic,
                results,
                category=str(spec.get("category") or "news"),
                max_age_days=float(spec.get("max_age_days", 7)),
            )
            stored_total += stored.get("stored", 0)
            stale_total += stored.get("stale", 0)
            result["searched"].append(topic)
            # 刷新该域冷却
            last[topic] = time.time()
        except Exception as e:
            logger.warning("world_ingestion: 搜索异常 %s: %s", topic, e)
            continue

    _save_cooldown(base_dir, last)
    result["stored"] = stored_total
    result["stale"] = stale_total
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
        if stale_total:
            parts.append(f"{stale_total} 条太旧已丢弃")
        if promoted:
            parts.append(f"{promoted} 条通过验证入库")
        if awaiting:
            parts.append(f"{awaiting} 条可信度不足，等你裁决")
        result["summary"] = "，".join(parts)
    else:
        result["summary"] = "都在冷却期，本轮未搜"
    return result
