"""P0 行为：主动信息摄入（world_search）。

按白名单主题域搜索近况，抽要点记入长期记忆（learned_history）。低风险、只读外部
网络、写本地记忆，不打扰、不占对话通道——正好走 diary/garden 那条「后台自主动作」范式。

设计要点：
- 每个主题域有冷却时间（默认 24h），避免狂刷 API。
- 抽出的事实经 LearnedStore().append 落库，自带 fp 幂等（同一事实不重复记）。
- 事实文案带主题域前缀 + 来源标题，便于以后按域检索、也便于对话时「答得上」。
- fail-open：任何异常都返回空结果，绝不阻断自主层主链路。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import logging

logger = logging.getLogger(__name__)

# 冷却状态文件：记录每个主题域上次搜索时间（epoch 秒）
_COOLDOWN_FILE = "agency/last_world_search.json"

# 单条事实最大长度（防超大 snippet 撑爆记忆）
_MAX_FACT_LEN = 240
# 一次摄入最多落几条
_MAX_FACTS_PER_TOPIC = 4


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


def run_world_ingestion(
    base_dir: str,
    topics: list[str],
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对白名单主题域做一轮信息摄入。

    Args:
        base_dir: deskmate 根目录（~/.deskmate）
        topics:   白名单主题域列表
        cfg:      world_search 配置（cooldown_hours / max_results / timelimit / per_tick_limit）

    Returns:
        {"searched": [...], "facts": [...], "stored": N, "skipped": [...], "summary": "..."}
    """
    cfg = cfg or {}
    cooldown_hours = float(cfg.get("cooldown_hours", 24))
    max_results = int(cfg.get("max_results", 5))
    timelimit = cfg.get("timelimit") or None
    per_tick_limit = int(cfg.get("per_tick_limit", 1))

    result: dict[str, Any] = {"searched": [], "facts": [], "stored": 0, "skipped": [], "summary": ""}
    if not topics:
        return result

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
    for topic in to_search:
        try:
            # 主题域 + 「近况/动态」措辞，避免泛搜
            query = f"{topic} 最新动态"
            resp = search(query, max_results=max_results, timelimit=timelimit)
            if not isinstance(resp, dict) or resp.get("error"):
                logger.warning("world_ingestion: 搜索失败 %s: %s", topic, resp.get("error") if isinstance(resp, dict) else resp)
                continue
            results = resp.get("results") or []
            facts = _extract_facts(topic, results)
            if facts:
                all_facts.extend(facts)
            result["searched"].append(topic)
            # 刷新该域冷却
            last[topic] = time.time()
        except Exception as e:
            logger.warning("world_ingestion: 搜索异常 %s: %s", topic, e)
            continue

    _save_cooldown(base_dir, last)

    if all_facts:
        try:
            from deskmate.memory.learned_store import LearnedStore

            n = LearnedStore().append(all_facts)
            result["stored"] = n
            result["facts"] = all_facts
        except Exception as e:
            logger.warning("world_ingestion: 落库失败: %s", e)

    if result["searched"]:
        result["summary"] = (
            f"搜了 {len(result['searched'])} 个域，记下了 {result['stored']} 条新信息"
            + (f"（{result['searched'][0]} 等）" if len(result["searched"]) > 1 else f"（{result['searched'][0]}）")
        )
    else:
        result["summary"] = "都在冷却期，本轮未搜"
    return result
