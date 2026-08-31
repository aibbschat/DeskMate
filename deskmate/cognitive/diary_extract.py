"""
日记→认知提取器（③a 焊点）

把「小桔日记 / MomentFeed」的内容增量提取进认知画像 profile_items，
source_type='diary'，每条信号带证据溯源（excerpt + 日期）。

为什么需要它：
- cold_start 只处理 vault / MEMORY.md / style_profile，完全没碰日记；
- 日记是小桔与萧哥的私密共创记忆，含大量个人偏好、表达观、关系信号，
  是认知画像不可替代的来源；
- 日记会持续增长，所以必须**按 post id 增量幂等**——已处理的 id 记在
  profile_meta['diary_extracted_ids']，重跑只处理新帖，不重复、不丢旧数据。

提取策略（萧哥拍板：规则优先 + LLM 可选）：
- 默认规则优先：离线可跑、fail-open 不崩，适合首版与无 LLM 环境；
- use_llm=True 且 LLM 在线时，对新帖再调 CognitiveExtractor 做五维深度提取。

本模块只负责「焊」，不做访谈（③b InterviewEngine 是独立阶段，吃本模块
产出的 cognitive_gaps / open_gaps() 作输入契约）。
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Iterable, Optional

from .models import SourceRef
from .profile import CognitiveProfileManager, get_profile_manager

logger = logging.getLogger(__name__)

_META_KEY = "diary_extracted_ids"
_NEAR_DUP_RATIO = 0.85  # 内容相似度阈值：高于此视为同一则日记的不同版本，只抽一次


# ----------------------------------------------------------------------
# 已处理 id 的读写（幂等依据）
# ----------------------------------------------------------------------
def _load_processed_ids(pm: CognitiveProfileManager) -> set[str]:
    raw = pm.store.get_meta(_META_KEY, default=[])
    if isinstance(raw, list):
        return set(str(x) for x in raw)
    return set()


def _save_processed_ids(pm: CognitiveProfileManager, ids: Iterable[str]) -> None:
    pm.store.set_meta(_META_KEY, sorted(set(ids)))


# ----------------------------------------------------------------------
# 规则提取（离线、确定、fail-open）
# ----------------------------------------------------------------------
def _rule_extract(post: dict, pm: CognitiveProfileManager) -> int:
    """从单条日记用规则提取认知信号，写入 pm。返回新增/更新的条目数。"""
    pid = post.get("id", "")
    content = post.get("content", "") or ""
    created = post.get("createdAt", "")
    if not content.strip():
        return 0

    src = SourceRef(
        type="diary",
        title=f"diary/{pid}" if pid else "diary",
        excerpt=content[:200],
        date=created,
        confidence=0.55,
    )

    added = 0

    # ① 问题/异常类（丢档、bug）→ 不是认知信号，跳过
    if any(kw in content for kw in ("不见了", "丢失", "归档", "出错", "bug", "异常")):
        return 0

    # ② 隐私与安全感：私密花园 / 只有小桔看得见 / 安全感
    if any(kw in content for kw in ("私密花园", "安全感", "只有", "没人看得见", "私密")):
        _, op = pm.add_stance(
            topic="隐私与安全感",
            content="把小桔日记视为与 AI 共享的私密花园，强调内容只有小桔可见、有安全感；"
                    "重视私密表达空间，不愿对外暴露心事。",
            source=src,
            tags=["关系", "隐私", "安全感"],
        )
        added += 1
        logger.debug("diary rule → 隐私与安全感 (%s)", op)

    # ③ 借历史纪实曲笔抒发现实不平（高信号：用户自述动机）
    if any(kw in content for kw in ("写历史书", "纪实", "不平", "真话", "曲笔", "借", "历史人物", "发泄", "说多了", "麻烦", "环境")):
        src2 = SourceRef(
            type="diary",
            title=f"diary/{pid}" if pid else "diary",
            excerpt=content[:200],
            date=created,
            confidence=0.62,
        )
        # 立场：用历史纪实曲笔承载现实情绪
        _, op1 = pm.add_stance(
            topic="表达与创作观",
            content="以写历史小说/纪实为主业；当现实中有不平之事、不便直说时，会借历史人物与事件曲笔表达，"
                    "把心中话通过历史框架说出，是一种安全的发泄方式。",
            source=src2,
            tags=["创作观", "表达", "曲笔"],
        )
        added += 1
        logger.debug("diary rule → 表达与创作观 (%s)", op1)

        # 价值观排序：安全表达优先于直说真话
        _, op2 = pm.add_value_ranking(
            domain="表达",
            ranking=["安全表达（曲笔/借古）", "真实看法被听见", "叙事流畅", "戏剧效果"],
            content="在当前环境下直说真话易惹麻烦，故表达真实看法时优先选择安全的曲笔方式（借历史框架），"
                    "而非直接陈述。",
            source=src2,
            tradeoff_notes="真实 vs 安全：安全优先，真实看法通过历史隐喻间接传达。",
            tags=["价值观", "表达", "安全"],
        )
        added += 1
        logger.debug("diary rule → 表达价值观排序 (%s)", op2)

        # 思维框架：借古喻今 / 曲笔
        _, op3 = pm.add_thinking_framework(
            domain="表达/创作",
            steps=[
                "现实中遇不平之事或想说真话",
                "判断直说的环境风险",
                "转用历史纪实框架承载情绪",
                "借人物、事件曲笔表达（不直接陈述）",
                "在安全距离内完成情绪发泄",
            ],
            content="借古喻今 / 曲笔：用历史框架承载现实情绪与看法，规避直说风险，是一种安全的表达路径。",
            source=src2,
            tags=["思维框架", "表达", "曲笔"],
        )
        added += 1
        logger.debug("diary rule → 表达思维框架 (%s)", op3)

    return added


# ----------------------------------------------------------------------
# 近重复帖去重（避免同一则日记的多个版本触发矛盾级联）
# ----------------------------------------------------------------------
def _norm_content(text: str) -> str:
    """归一化：去 emote 标记、空白、标点，保留语义字符。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")  # 去 ![...](...) emote
    t = re.sub(r"[\s\W]+", "", t)
    return t.lower()


def _is_near_duplicate(text: str, seen: list[str]) -> bool:
    norm = _norm_content(text)
    if not norm:
        return False
    return any(difflib.SequenceMatcher(None, norm, s).ratio() >= _NEAR_DUP_RATIO for s in seen)


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def extract_diary_into_cognitive(
    pm: Optional[CognitiveProfileManager] = None,
    use_llm: bool = False,
    diary_store=None,
) -> dict:
    """
    把日记增量提取进认知画像。

    Args:
        pm: 画像管理器（默认全局单例）。
        use_llm: 是否对新帖追加 LLM 五维深度提取（需 LLM 在线，否则降级规则）。
        diary_store: 日记存储（默认 DiaryStore()）；测试可注入临时实例。

    Returns:
        {"processed": int, "new_posts": int, "rule_items": int,
         "llm_items": int, "open_gaps": int, "note": str}
    """
    pm = pm or get_profile_manager()
    if diary_store is None:
        from deskmate.growth.diary_store import DiaryStore

        diary_store = DiaryStore()

    processed = _load_processed_ids(pm)
    stats = {"processed": 0, "new_posts": 0, "rule_items": 0, "llm_items": 0, "open_gaps": 0, "note": ""}

    try:
        page = diary_store.get_posts(limit=200)
        items = page.get("items", []) if isinstance(page, dict) else []
    except Exception as e:
        logger.error("读取日记失败，跳过提取: %s", e)
        stats["note"] = f"diary_read_failed: {e}"
        return stats

    new_ids: list[str] = []
    seen_norms: list[str] = []
    for post in items:
        pid = post.get("id", "")
        if not pid or pid in processed:
            continue
        # 近重复帖：同一则日记的多个版本只抽第一篇，避免 manager 矛盾检测误判级联
        if _is_near_duplicate(post.get("content", ""), seen_norms):
            logger.debug("日记 %s 与已处理帖近重复，跳过提取（仍标记已处理）", pid)
            new_ids.append(pid)
            continue
        seen_norms.append(_norm_content(post.get("content", "")))
        new_ids.append(pid)
        stats["new_posts"] += 1
        try:
            stats["rule_items"] += _rule_extract(post, pm)
        except Exception as e:
            logger.warning("规则提取日记 %s 失败（跳过该帖）: %s", pid, e)

        # 可选 LLM 深度提取
        if use_llm:
            try:
                from deskmate.memory.llm import llm_available
                from .extractor import CognitiveExtractor

                if llm_available():
                    ext = CognitiveExtractor(profile_manager=pm)
                    res = ext.extract_from_text(
                        post.get("content", ""),
                        source_title=f"diary/{pid}",
                        source_type="diary",
                        source_date=post.get("createdAt", ""),
                    )
                    stats["llm_items"] += (
                        res.get("stances", 0)
                        + res.get("argument_patterns", 0)
                        + res.get("value_rankings", 0)
                        + res.get("thinking_frameworks", 0)
                    )
                else:
                    logger.info("LLM 不可用，跳过日记 %s 的 LLM 深度提取", pid)
            except Exception as e:
                logger.warning("LLM 提取日记 %s 失败（降级规则）: %s", pid, e)

    # 更新幂等依据 + 画像摘要
    if new_ids:
        processed.update(new_ids)
        _save_processed_ids(pm, processed)
        try:
            profile = pm.get()
            if profile.summary:
                profile.summary = (
                    f"{profile.summary.rstrip('。')}；"
                    f"已从 {len(processed)} 条日记增量提取认知信号。"
                )
            else:
                profile.summary = f"已从 {len(processed)} 条日记增量提取认知信号。"
            pm.save()
        except Exception as e:
            logger.warning("更新日记提取摘要失败（不影响数据）: %s", e)

    stats["processed"] = len(processed)
    try:
        stats["open_gaps"] = len(pm.get().open_gaps())
    except Exception:
        pass
    stats["note"] = "ok" if stats["new_posts"] else "no_new_posts"
    return stats
