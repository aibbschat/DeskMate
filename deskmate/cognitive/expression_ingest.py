"""表达源回灌（F2）——把 learned_history 接进认知层与情感层。

背景（2026-09-05 巡检实测）
--------------------------
``learned_history`` 有 227 条他在聊天里明确说过的话，认知层与情感层
**一条都没碰过**。两个层当时唯一的真实输入源都是那 15 篇日记。

本模块把这张表接进来，两个方向：

1. 情感侧 ``ingest_emotions``：把尚未登记的自述并进情感时间线，
   让「他那天低落」不必非得写在日记里才被知道。
2. 认知侧 ``ingest_preferences``：把 preference / habit 类并进认知画像。

为什么只接 preference / habit（不接 tool / other）
-------------------------------------------------
``tool`` 94 条 + ``other`` 95 条多是工作流水：

    「第四章《稻粱谋》进入第三节创作，第二节止于第1022段」
    「史料库灌入几十部书籍（含《宋会要辑稿》…）」

这些是**进度记录**，不是他的主张。灌进认知画像只会稀释现有那批
0.95 置信度的价值观条目，让画像变吵。preference / habit 才是稳定的
「他是什么样的人」，值得进画像。

零 LLM、幂等、fail-open。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from deskmate.cognitive import expressions

logger = logging.getLogger(__name__)

_META_KEY = "learned_pref_ingested_ids"
_META_LAST = "expression_ingest_last_tick"

# 每次最多补几条：宁可慢慢长，也不要一次灌 35 条把画像冲淡。
_DEFAULT_PREF_BATCH = 10

# 一句话事实的置信度。系统听来的（聊天提炼）不如他访谈里亲口说的，
# 但比从日记猜的可靠——夹在中间。
_FACT_CONFIDENCE = 0.70

# fact 里的日期前缀：「2026-09-04: 用户表示…」「2026-07-31，用户偏好…」
# 实测分隔符既有冒号也有中文逗号，都得认。
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[:：，,、]?\s*")
# 主谓噪声：「用户明确表示」「他指出」「他希望」
_SUBJECT_NOISE = re.compile(
    r"^(用户|他|她)?(明确|曾|已经|一直|通常|一般|比较|特别)?"
    r"(表示|指出|说明|提到|反馈|强调|认为|觉得|喜欢|希望|不希望|偏好)?[:：]?\s*"
)

# 主语是 AI 的记录：那是**我们**做过什么（承诺/审阅/指出），不是他的主张。
# 实测混进过两条：「AI承诺后续写入前先查询」「AI审阅用户小说第二章时指出…」，
# 把它们当他的偏好存进画像，等于替他编立场。
_AI_SUBJECT = re.compile(r"^\s*(AI| ai|Ai|小桔|助手|模型)")

# ----------------------------------------------------------------------
# 人称闸门：长期记忆是**第三人称观察**，不是他的倾诉
# ----------------------------------------------------------------------
# 这些话的主语虽是"用户"，说的却是他对外部对象（AI/系统/工具）的要求或动作：
#   「用户希望AI变得更聪明」  → 对 AI 的期望，不是他的心情
#   「用户纠正AI，指出…」     → 一次纠错行为，不是他的心情
#   「用户提出进度不用补充」   → 工作方式，不是他的心情
# 把它们送进情绪判定，模型会抓到"希望""纠正"这类词当成心情——
# 实测 08-26 就因"用户希望AI…"被判成「顺利」，证据是"希望"两个字。
# 第三人称记录里，**只有指向他自身感受的**才算心情（如"享受孩子的依赖"）。
_OUTWARD_MARKS = (
    "希望AI", "期望AI", "希望 AI", "期望 AI", "希望ai", "期望 ai",
    "让AI", "让 AI", "让ai", "要求AI", "要求 AI",
    "纠正", "指出", "提出", "补充", "接入", "配置", "设置", "修改",
    "更新", "删除", "安装", "使用", "改为", "换成", "添加",
)


def looks_like_feeling(text: str) -> bool:
    """这段第三人称记录，读得出**他自己**的心情吗？

    只拦"指向外部的动作与期望"，不拦感受。判不了的交给
    ``emotion_sense``——它有自己的词表和三道闸门，会正确地选择不表态。
    """
    t = str(text or "").strip()
    if not t:
        return False
    return not any(m in t for m in _OUTWARD_MARKS)


def _topic_of(fact: str) -> str:
    """从一句话事实里取议题短语（去掉日期与「用户表示」这类套话）。

    例：「2026-09-04: 用户明确表示不希望再写古代记者题材的故事」
        → 「不希望再写古代记者题材的故事」
    """
    t = _DATE_PREFIX.sub("", str(fact or "").strip())
    t = _SUBJECT_NOISE.sub("", t).strip()
    # 日期也可能夹在句中（「用户于2026-07-29明确表示…」）：留在 topic 里
    # 会让同义条目因日期不同而无法合并，一并抹掉
    t = re.sub(r"\d{4}-\d{2}-\d{2}\s*[:：，,]?", "", t).strip()
    return (t[:24] or str(fact or "")[:24]).strip()


def _is_his_own(fact: str) -> bool:
    """这句话说的是**他自己**，还是我们（AI）的行为？"""
    t = _DATE_PREFIX.sub("", str(fact or "").strip())
    return not bool(_AI_SUBJECT.match(t))


# ----------------------------------------------------------------------
# 情感侧
# ----------------------------------------------------------------------
def ingest_emotions(
    days: int = 7,
    base_dir: str | Path | None = None,
    engine: Any = None,
) -> dict:
    """把近期表达源里尚未登记的自述并进情感时间线。

    日记不由这里处理——``record_today`` / ``backfill_from_diary`` 已负责。
    这里补的是**访谈与长期记忆**：它们此前根本没进过情感层。

    按天批量提交（一天一次判定），不逐条问模型。

    Returns: {"scanned", "days", "added", "note"}
    """
    stats: dict = {"scanned": 0, "days": 0, "added": 0, "note": ""}
    try:
        from deskmate.growth.emotion_timeline import EmotionTimeline

        tl = EmotionTimeline(base_dir=base_dir)
    except Exception as e:
        stats["note"] = f"timeline_unavailable: {e}"
        logger.debug("表达源回灌：情感时间线不可用，跳过: %s", e)
        return stats

    try:
        by_day = expressions.by_day(days=days, base_dir=base_dir)
    except Exception as e:
        stats["note"] = f"source_unavailable: {e}"
        return stats

    for day, exprs in sorted(by_day.items()):
        pending = []
        for e in exprs:
            if e.get("source") == expressions.DIARY:
                continue
            # 第三人称观察记录：只收读得出他自身感受的（见 looks_like_feeling）
            if e.get("source") == expressions.LEARNED and not looks_like_feeling(e["text"]):
                continue
            pending.append(e["text"])
        stats["scanned"] += len(pending)
        if not pending:
            continue
        saved = set(tl._statements_for(day))
        pending = [t for t in pending if t not in saved]
        if not pending:
            continue
        stats["days"] += 1
        try:
            if tl.record_statements(engine, pending, day=day):
                stats["added"] += 1
        except Exception as e:
            logger.warning("表达源回灌 %s 失败（跳过该天）: %s", day, e)

    stats["note"] = "ok"
    return stats


# ----------------------------------------------------------------------
# 认知侧
# ----------------------------------------------------------------------
def ingest_preferences(
    limit: int = _DEFAULT_PREF_BATCH,
    pm: Optional[Any] = None,
    base_dir: str | Path | None = None,
    profile_path: str | Path | None = None,
) -> dict:
    """把长期记忆里的 preference / habit 并进认知画像。

    ``fact`` 是**已经提炼好的一句话事实**（"用户明确表示不希望再写古代记者
    题材"），不是原始对话——不需要再过一遍 LLM 提取，那既不省钱也不更准。
    直接建条目，置信度压到 0.70：系统听来的，不是他坐下来专门答的。

    同 topic 已有条目 → 只补来源升置信，不重复建（避免同义反复刷条目）。

    Returns: {"scanned", "added", "merged", "note"}
    """
    stats: dict = {"scanned": 0, "added": 0, "merged": 0, "note": ""}
    try:
        from deskmate.cognitive.profile import CognitiveProfileManager
        from deskmate.cognitive.models import (
            HistoryEntry, SourceRef, Stance,
        )

        pm = pm or CognitiveProfileManager(
            profile_path=profile_path, layer="cognitive"
        )
    except Exception as e:
        stats["note"] = f"profile_unavailable: {e}"
        logger.debug("表达源回灌：认知画像不可用，跳过: %s", e)
        return stats

    try:
        rows = expressions.collect(
            days=365 * 5,  # 偏好不过期，全量看
            base_dir=base_dir,
            sources=(expressions.LEARNED,),
            learned_categories=("preference", "habit"),
        )
    except Exception as e:
        stats["note"] = f"source_unavailable: {e}"
        return stats

    try:
        done = set(pm.store.get_meta(_META_KEY, default=[]) or [])
    except Exception:
        done = set()

    budget = max(0, int(limit))
    touched = False
    for row in rows:
        rid = str(row.get("id", ""))
        if not rid or rid in done:
            continue
        stats["scanned"] += 1
        if budget <= 0:
            break
        budget -= 1
        done.add(rid)
        touched = True

        fact = str(row.get("text", "")).strip()
        if not fact:
            continue
        # 主语是 AI 的（"AI承诺…""AI审阅…"）= 我们的行为，不是他的主张
        if not _is_his_own(fact):
            continue
        topic = _topic_of(fact)
        try:
            src = SourceRef(
                type="conversation",
                title="长期记忆",
                excerpt=fact[:120],
                date=str(row.get("day", "")),
                confidence=_FACT_CONFIDENCE,
            )
            hit = None
            for s in pm.profile.stances:
                if s.topic == topic and getattr(s, "status", "active") == "active":
                    hit = s
                    break
            if hit is not None:
                hit.add_source(src)
                hit.history.append(HistoryEntry(
                    action="updated", old_value=hit.content, new_value=hit.content,
                    reason="长期记忆补充来源", source="长期记忆"))
                stats["merged"] += 1
                continue
            stance = Stance(
                topic=topic, content=fact[:200], category="偏好",
                tags=["长期记忆", "偏好"], confidence=_FACT_CONFIDENCE,
            )
            stance.add_source(src)
            stance.history.append(HistoryEntry(
                action="created", new_value=fact[:200],
                reason="长期记忆并入认知画像", source="长期记忆"))
            pm.profile.stances.append(stance)
            pm.profile.extraction_count += 1
            stats["added"] += 1
        except Exception as e:
            logger.warning("长期记忆并入画像失败（跳过 %s）: %s", rid, e)

    if touched:
        try:
            pm.save()
            pm.store.set_meta(_META_KEY, sorted(done))
        except Exception as e:
            logger.warning("长期记忆回灌进度写入失败（下次会重跑，幂等无害）: %s", e)
    stats["note"] = "ok"
    return stats


def maybe_ingest_on_tick(
    interval_hours: float = 4.0,
    days: int = 7,
    limit: int = _DEFAULT_PREF_BATCH,
    engine: Any = None,
) -> Optional[dict]:
    """tick 周期调度入口：带节流、幂等、fail-open。

    与 ``emotion_extract.maybe_extract_on_tick`` 同一范式。
    节流 4h：两侧都要读库，没必要跟着心跳跑。
    """
    try:
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore()
        last = store.get_meta(_META_LAST)
        now = time.time()
        if last is not None:
            try:
                if (now - float(last)) < interval_hours * 3600:
                    return None
            except (TypeError, ValueError):
                pass
        res = ingest_all(days=days, limit=limit, engine=engine)
        try:
            store.set_meta(_META_LAST, now)
        except Exception:
            logger.debug("表达源回灌时间戳写入失败（下次会重跑，幂等无害）")
        return res
    except Exception:
        logger.warning("表达源回灌 tick 失败（fail-open）", exc_info=True)
        return None


def ingest_all(
    days: int = 7,
    limit: int = _DEFAULT_PREF_BATCH,
    base_dir: str | Path | None = None,
    engine: Any = None,
    profile_path: str | Path | None = None,
) -> dict:
    """一次跑完两侧。供 tick 调用。"""
    out: dict = {"emotions": {}, "preferences": {}}
    try:
        out["emotions"] = ingest_emotions(days=days, base_dir=base_dir, engine=engine)
    except Exception as e:
        logger.warning("表达源情感回灌失败（fail-open）: %s", e)
        out["emotions"] = {"note": f"failed: {e}"}
    try:
        out["preferences"] = ingest_preferences(
            limit=limit, base_dir=base_dir, profile_path=profile_path
        )
    except Exception as e:
        logger.warning("表达源认知回灌失败（fail-open）: %s", e)
        out["preferences"] = {"note": f"failed: {e}"}
    return out
