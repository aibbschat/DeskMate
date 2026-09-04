"""
日记→情感画像提取器（情感层 MVP）

把「小桔日记 / MomentFeed」内容里的**真实情感信号**增量提取进情感画像
（profile_items layer='emotional'），source_type='diary'，每条带证据溯源。

为什么需要它（区别于认知层 diary_extract）：
- 认知层提取的是"他怎么想"（立场/价值观/思维框架），情感层提取的是"他怎么感觉"
  （什么让他情绪波动 / 他靠什么回血 / 他想要怎样的关心 / 情绪状态基线）；
- 真实情感只能来自用户的**亲口表达**，日记是主要来源；
- 现有情绪时间线（emotion_timeline）是**规则推断**的标签（有 user_corrected 就
  标"压力大"），不是真实情绪，不能直接当画像填充物——本模块补上"真实情感"这一环。

提取策略（与认知层一致：规则优先 + 幂等增量）：
- 规则优先：情感词库命中 → 提取情感条目，fail-open 不崩；
- 按 post id 增量幂等：已处理 id 记在 profile_meta['emotion_diary_extracted_ids']；
- 近重复帖去重，避免同模板/同内容重复触发。

复用认知层 schema：layer='emotional' 零 DDL（store 已支持 load/persist by layer）。
维度模型复用认知层四种 dataclass，用 dimension 区分情感维度：
- emotional_trigger  → stance：什么情境触发他情绪波动（正/负）
- recovery_pattern  → stance：他靠什么回血 / 恢复状态
- care_preference   → stance：他偏好的被关怀方式 / 边界
- emotional_state   → value_ranking：情绪状态基线（频次排序）

三个维度的"进度条"只是前端展示；本模块负责把真实数据填进画像。
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Iterable, Optional

from .models import SourceRef
from .profile import CognitiveProfileManager, get_profile_manager

logger = logging.getLogger(__name__)

_META_KEY = "emotion_diary_extracted_ids"
_NEAR_DUP_RATIO = 0.85  # 与认知层一致：高于此视为同一则日记不同版本

# ----------------------------------------------------------------------
# 情感词库（规则提取的核心词典）
# 结构：词 → 情感信号类别（正/负），供触发与方向判断
# ----------------------------------------------------------------------
_POSITIVE_EMOTION_WORDS = [
    "写顺", "顺利", "真好", "踏实", "安心", "安全感", "温暖", "舒服",
    "开心", "高兴", "喜欢", "满意", "放松", "舒心", "平静", "期待",
    "希望", "欣慰", "喜悦", "开心", "成就感", "舒服", "治愈",
]
_NEGATIVE_EMOTION_WORDS = [
    "压力", "郁闷", "烦躁", "焦虑", "难过", "沮丧", "低落", "失望",
    "累", "疲惫", "烦", "恼火", "不安", "担心", "卡住",
    "受阻", "瓶颈", "写不下去", "找不到感觉", "失联",
]
_ENERGY_SOURCE_WORDS = [
    "史料", "历史", "读书", "查阅", "整理", "写作", "重读", "翻看",
    "史料原文", "古籍", "笔记", "安静", "独处", "散步", "喝茶", "音乐",
    "小说", "影视", "孩子", "家人", "小桔日记", "日记",
]
_CARE_PREFERENCE_WORDS = [
    "不要打扰", "别催", "给空间", "别管", "不用管", "不用理",
    "关心进度", "问进度", "帮忙", "搭把手", "提醒", "肯定", "认可",
    "别问", "不想说", "自己消化", "一个人静静", "别来烦", "一个人待着",
]

# ----------------------------------------------------------------------
# 已处理 id 的读写（幂等依据，独立于认知层 meta key）
# ----------------------------------------------------------------------
def _load_processed_ids(pm: CognitiveProfileManager) -> set[str]:
    raw = pm.store.get_meta(_META_KEY, default=[])
    if isinstance(raw, list):
        return set(str(x) for x in raw)
    return set()


def _save_processed_ids(pm: CognitiveProfileManager, ids: Iterable[str]) -> None:
    pm.store.set_meta(_META_KEY, sorted(set(ids)))


# ----------------------------------------------------------------------
# 情感提取规则（从单条日记提取情感信号）
# ----------------------------------------------------------------------
def _emotion_rule_extract(post: dict, pm: CognitiveProfileManager) -> int:
    """从单条日记提取情感信号写入情感画像。返回新增/更新条目数。"""
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
        confidence=0.58,
    )

    added = 0

    # ① 问题/异常类日记（丢档/bug）→ 情绪是"烦躁/困扰"，但不是稳定情感信号，跳过
    if any(kw in content for kw in ("不见了", "丢失", "归档", "出错", "bug", "异常")):
        return 0

    # ② 情感触发点：正向（写作顺畅/有安全感/被治愈）
    pos_hits = [w for w in _POSITIVE_EMOTION_WORDS if w in content]
    if pos_hits:
        # 根据语境细分触发点
        if any(kw in content for kw in ("安全感", "私密", "温暖", "安静", "只有")):
            topic = "私密空间与安全感"
            content_str = (
                "在小桔日记这样的私密空间里表达让他感到安心、温暖、有安全感——"
                "这里没有外人，可以毫无顾忌地写下心事。"
            )
        elif any(kw in content for kw in ("写顺", "顺利", "写", "章节", "第三章", "顺手")):
            topic = "写作顺畅"
            content_str = (
                "写作顺利、章节写顺时情绪明显变好（" + "、".join(pos_hits[:3]) + "），"
                "创作进入状态是最让他愉悦的时刻。"
            )
        else:
            topic = "正向情绪来源"
            content_str = (
                "在以下情境中情绪好转：" + "、".join(pos_hits[:3]) +
                "（" + content[:40] + "…）"
            )
        _, op = pm.add_stance(
            topic=f"情感触发·{topic}",
            content=content_str,
            source=src,
            tags=["情感层", "emotional_trigger", "positive"] + pos_hits[:2],
            category="情感触发点",
        )
        added += 1
        logger.debug("emotion rule → 正向触发 %s (%s)", topic, op)

    # ③ 情感触发点：负向（压力/郁闷/卡住/遗憾）
    neg_hits = [w for w in _NEGATIVE_EMOTION_WORDS if w in content]
    if neg_hits:
        if any(kw in content for kw in ("卡住", "瓶颈", "写不下去", "找不到感觉", "难", "受阻")):
            topic = "写作受阻"
            content_str = (
                "写作卡住、找不到感觉时情绪明显低落（" + "、".join(neg_hits[:3]) + "），"
                "创作受阻是他主要的负面情绪来源。"
            )
        elif any(kw in content for kw in ("社会", "不平", "现象", "现实", "新闻")):
            topic = "现实不平"
            content_str = (
                "看到社会上不平之事、奇怪现象会让他心情郁闷（" +
                "、".join(neg_hits[:3]) + "），且常借写纪实类历史书抒发。"
            )
        else:
            topic = "负面情绪来源"
            content_str = (
                "在以下情境中情绪低落：" + "、".join(neg_hits[:3]) +
                "（" + content[:40] + "…）"
            )
        _, op = pm.add_stance(
            topic=f"情感触发·{topic}",
            content=content_str,
            source=src,
            tags=["情感层", "emotional_trigger", "negative"] + neg_hits[:2],
            category="情感触发点",
        )
        added += 1
        logger.debug("emotion rule → 负向触发 %s (%s)", topic, op)

    # ③b 创作理想与现实落差（高信号：写作中的深层情绪）
    if any(kw in content for kw in ("遗憾", "忧伤", "无奈", "本应", "本来设定", "想写成", "失望")) and any(
        kw in content for kw in ("爽文", "写", "小说", "书", "人物", "设定", "考据", "制度")
    ):
        _, op = pm.add_stance(
            topic="情感触发·创作理想落差",
            content=(
                "创作时存在理想与现实的落差：本想写成某种效果（如爽文），"
                "但考据/创作中认识到现实的限制（如历史体制下小人物无法翻身），"
                "由此感到遗憾、忧伤、无奈——这是写作中重要的深层情绪来源。"
            ),
            source=src,
            tags=["情感层", "emotional_trigger", "negative", "创作理想", "遗憾"],
            category="情感触发点",
        )
        added += 1
        logger.debug("emotion rule → 创作理想落差 (%s)", op)

    # ④ 回血方式：靠什么恢复状态
    energy = [w for w in _ENERGY_SOURCE_WORDS if w in content]
    if energy and any(kw in content for kw in ("写", "读", "翻", "重", "安静", "独处", "史料", "查", "整理", "找", "看")):
        _, op = pm.add_stance(
            topic="回血方式",
            content=(
                "情绪恢复靠投入创作/史料：" + "、".join(energy[:4]) +
                "——如重读旧稿找回感觉、查史料、在安静环境独处。"
            ),
            source=src,
            tags=["情感层", "recovery_pattern"] + energy[:3],
            category="回血方式",
        )
        added += 1
        logger.debug("emotion rule → 回血方式 (%s)", op)

    # ⑤ 关怀偏好：想要怎样的被关怀方式（含边界）
    care = [w for w in _CARE_PREFERENCE_WORDS if w in content]
    if care:
        # 判断是"边界型"（别打扰）还是"支持型"（帮/肯定）
        if any(kw in content for kw in ("不要", "别", "不用", "不想", "自己")):
            topic = "关怀边界"
            content_str = (
                "倾向独处消化、不希望被频繁打扰（" + "、".join(care[:3]) + "），"
                "关怀时需注意给空间。"
            )
        else:
            topic = "关怀支持"
            content_str = (
                "希望得到具体支持/认可（" + "、".join(care[:3]) + "），"
                "而非空泛的问候。"
            )
        _, op = pm.add_stance(
            topic=f"关怀·{topic}",
            content=content_str,
            source=src,
            tags=["情感层", "care_preference"] + care[:3],
            category="关怀偏好",
        )
        added += 1
        logger.debug("emotion rule → 关怀偏好 (%s)", op)

    return added


# ----------------------------------------------------------------------
# 近重复帖去重（复用认知层逻辑）
# ----------------------------------------------------------------------
def _norm_content(text: str) -> str:
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
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
def extract_diary_into_emotion(
    pm: Optional[CognitiveProfileManager] = None,
    diary_store=None,
) -> dict:
    """把日记增量提取进情感画像（layer='emotional'）。

    Args:
        pm: 情感画像管理器（默认新建 emotional 层 manager）。
        diary_store: 日记存储（默认 DiaryStore()）；测试可注入临时实例。

    Returns:
        {"processed": int, "new_posts": int, "emotion_items": int,
         "emotional_trigger": int, "recovery_pattern": int, "care_preference": int,
         "note": str}
    """
    pm = pm or CognitiveProfileManager(layer="emotional")
    if diary_store is None:
        from deskmate.growth.diary_store import DiaryStore

        diary_store = DiaryStore()

    processed = _load_processed_ids(pm)
    stats = {
        "processed": 0, "new_posts": 0, "emotion_items": 0,
        "emotional_trigger": 0, "recovery_pattern": 0, "care_preference": 0,
        "note": "",
    }

    try:
        page = diary_store.get_posts(limit=200)
        items = page.get("items", []) if isinstance(page, dict) else []
    except Exception as e:
        logger.error("读取日记失败，跳过情感提取: %s", e)
        stats["note"] = f"diary_read_failed: {e}"
        return stats

    new_ids: list[str] = []
    seen_norms: list[str] = []
    for post in items:
        pid = post.get("id", "")
        if not pid or pid in processed:
            continue
        if _is_near_duplicate(post.get("content", ""), seen_norms):
            new_ids.append(pid)
            continue
        seen_norms.append(_norm_content(post.get("content", "")))
        new_ids.append(pid)
        stats["new_posts"] += 1
        try:
            n = _emotion_rule_extract(post, pm)
            stats["emotion_items"] += n
        except Exception as e:
            logger.warning("情感提取日记 %s 失败（跳过该帖）: %s", pid, e)

    # 统计各维度（从画像读取，而非累加，保证口径一致）
    profile = pm.get()
    for s in profile.stances:
        tags = list(getattr(s, "tags", []) or [])
        if "emotional_trigger" in tags:
            stats["emotional_trigger"] += 1
        elif "recovery_pattern" in tags:
            stats["recovery_pattern"] += 1
        elif "care_preference" in tags:
            stats["care_preference"] += 1

    if new_ids:
        processed.update(new_ids)
        _save_processed_ids(pm, processed)
        try:
            profile = pm.get()
            if profile.summary:
                profile.summary = (
                    f"{profile.summary.rstrip('。')}；"
                    f"已从 {len(processed)} 条日记提取情感画像信号。"
                )
            else:
                profile.summary = f"已从 {len(processed)} 条日记提取情感画像信号。"
            pm.save()
        except Exception as e:
            logger.warning("更新情感提取摘要失败（不影响数据）: %s", e)

    stats["processed"] = len(processed)
    stats["note"] = "ok" if stats["new_posts"] else "no_new_posts"
    return stats


def maybe_extract_on_tick(
    interval_hours: float = 4.0,
    pm: Optional["CognitiveProfileManager"] = None,
    diary_store=None,
) -> Optional[dict]:
    """tick 周期调度入口：带节流、幂等、fail-open 的情感画像增量提取。

    设计意图：让日记自然沉淀进情感画像（"越用越懂你"），不再依赖手动跑。

    - 节流：profile_meta['emotion_extract_last_tick'] 记录上次运行（持久化，跨重启）；
      间隔未到直接返回 None，避免每 tick 扫库。
    - 幂等：extract_diary_into_emotion 按 post id 去重，重复运行无副作用。
    - fail-open：任何异常静默返回 None，绝不阻塞 tick 主流程。
    """
    try:
        pm = pm or CognitiveProfileManager(layer="emotional")
        last = pm.store.get_meta("emotion_extract_last_tick")
        now = time.time()
        if last is not None:
            try:
                if (now - float(last)) < interval_hours * 3600:
                    return None
            except (TypeError, ValueError):
                pass
        res = extract_diary_into_emotion(pm=pm, diary_store=diary_store)
        try:
            pm.store.set_meta("emotion_extract_last_tick", now)
        except Exception:
            pass
        return res
    except Exception:
        logger.warning("情感画像 tick 提取失败（fail-open）", exc_info=True)
        return None
