"""
画像冲突驱动的 gap 再生（方案 C：真冲突才问）

为什么需要它
------------
访谈引擎的燃料是 open gap，而 gap 是**存量消耗品**：
- 生产代码里 `add_gap(` 只被调用一次（`emotion_profile.ensure_emotional_gaps`，幂等只补一次）；
- 那批 gap 答完 → `open_gaps()=0` → `should_interview_now`（需 open gap ≥ 2）恒 False
  → 小桔再也不主动提问，且**静默失败**（无任何提示，用户只感觉"她不问了"）。

本模块让好奇心**可再生**：仅当出现**与既有画像真冲突**的信号时才生成 gap。
不是定时打卡式的"为了问而问"，而是「她真的困惑了才问」——人是会变的，
三个月前的答案不等于现在的答案，这才是活的了解。

⚠️ 2026-09-05（F3）：扫描范围从「只扫日记」扩到**他说过的所有话**
------------------------------------------------------------------
只扫日记时，再生机制形同虚设：他在访谈里答"低落时看电影、出去走走"，
却从未写进日记，于是这条跟画像里"回血靠投入创作"的张力**永远发现不了**。
访谈回答、聊天里的话同样能推翻画像，没理由只听日记本里的那一半。
现在走 ``cognitive/expressions.py`` 的统一表达源（日记 + 访谈 + 长期记忆），
显式注入 ``diary_store`` 时仍只扫它（兼容既有测试）。

为什么不复用 add_stance 自带的 _detect_contradiction
---------------------------------------------------
它只在**同一 topic** 内比较。而情感提取按极性拆 topic
（「关怀·关怀边界」/「关怀·关怀支持」是同维度的两个极性，却是**不同 topic**），
跨极性的真冲突会被静默漏掉：只新增条目，不生成 gap。本模块补的就是这个洞。

防伪 gap 的三道闸（历史踩过坑：模板文本含否定词被误判矛盾，产生僵尸 gap）
-------------------------------------------------------------------
1. **不看否定词，只看方向极性对**——同一维度内两个相反方向的词表；
2. **同极性不算冲突**——只是补充证据，绝不生成 gap；
3. **只检测"偏好类"维度**——情绪正负起伏是情境性的（有时开心有时郁闷很正常），
   问了也白问，属于噪声；只关心「关怀偏好」「回血方式」这类稳定偏好。

另有三重收敛：新文本同时命中两极（自相矛盾）跳过、同维度冲突只生成一次
（gap topic 去重）、只扫未处理过的新日记（已扫描 id 持久化）。

纯规则、零 LLM、fail-open（任何异常静默返回，绝不阻塞 tick）。
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from .profile import CognitiveProfileManager

logger = logging.getLogger(__name__)

_META_IDS = "conflict_scan_processed_ids"
_META_LAST = "conflict_scan_last_tick"

# gap topic 前缀：与 ensure_emotional_gaps 的「情感·xxx」区分，便于辨识来源
_GAP_PREFIX = "画像冲突"

# ----------------------------------------------------------------------
# 维度归类：用 tags 而非 category
# （访谈条目 category='访谈'，但 tags 带 care_preference / recovery_pattern；
#   日记条目 category='情感触发点'，tags 同样带维度标签 → tags 是唯一可靠口径）
# ----------------------------------------------------------------------
_DIM_BY_TAG: dict[str, str] = {
    "care_preference": "关怀偏好",
    "recovery_pattern": "回血方式",
}

# ----------------------------------------------------------------------
# 极性词表：同一维度内的两个相反方向
# ----------------------------------------------------------------------
_POLARITY_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "关怀偏好": {
        # 倾向独处消化、不希望被频繁打扰
        "boundary": (
            "不要打扰", "别打扰", "别催", "别管", "不用管", "不用理", "别问",
            "不想说", "自己消化", "一个人静静", "一个人待着", "想静静",
            "别来烦", "给空间", "给点空间", "不用陪", "让我静一静", "不想被打扰",
        ),
        # 希望被关心、被陪伴、被看见
        "support": (
            "被关怀", "关怀", "温柔的对待", "被理解", "陪伴", "陪着", "陪我",
            "聊聊天", "多聊聊", "问候", "在乎", "关心", "安慰", "鼓励",
            "想被关心", "希望有人问", "有人懂", "被看见",
        ),
    },
    "回血方式": {
        # 靠独处/安静/投入创作恢复
        "solitude": (
            "独处", "安静", "一个人", "独自", "自己待着", "散步", "走走",
            "吹吹风", "读书", "看书", "听音乐", "音乐", "写作", "史料",
            "睡觉", "休息", "静静",
        ),
        # 靠与人连接恢复
        "social": (
            "找人聊", "聊天", "倾诉", "找朋友", "朋友", "有人陪", "陪我",
            "说话", "谈心", "聚会", "一起", "约",
        ),
    },
}

_POLARITY_LABEL: dict[str, str] = {
    "boundary": "独处/边界",
    "support": "被关怀/陪伴",
    "solitude": "独处回血",
    "social": "连接回血",
}


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
def _hits(text: str, words: Iterable[str]) -> list[str]:
    t = text or ""
    return [w for w in words if w in t]


def _polarity_of(text: str, dim: str) -> dict[str, list[str]]:
    """文本在该维度各极性上命中的词。空 dict = 无信号；>1 个极性 = 自相矛盾。"""
    rules = _POLARITY_RULES.get(dim, {})
    out: dict[str, list[str]] = {}
    for pol, words in rules.items():
        h = _hits(text, words)
        if h:
            out[pol] = h
    return out


def _dim_of(stance) -> Optional[str]:
    """条目属于哪个偏好维度（按 tags 判定，取首个命中的维度标签）。"""
    tags = list(getattr(stance, "tags", []) or [])
    for t in tags:
        dim = _DIM_BY_TAG.get(t)
        if dim:
            return dim
    return None


def _normalize_ids(ids) -> set[str]:
    """已处理 id 归一化：给无前缀的旧记录补 ``d:``。

    2026-09-05 起 id 带源前缀（d:/i:/l:），而此前存量的 13 条是裸日记 id。
    不补前缀的话它们会被当成新内容重扫一遍——不是错误（gap 有 topic 去重），
    但会白跑检测，且首轮就把旧日记重新判一遍。
    """
    out: set[str] = set()
    for raw in ids or []:
        s = str(raw or "").strip()
        if not s:
            continue
        if s.startswith(("d:", "i:", "l:")):
            out.add(s)
        else:
            out.add(f"d:{s}")
    return out


def _existing_by_dim(pm: CognitiveProfileManager) -> dict[str, list]:
    """既有 active 条目按维度归类。"""
    by_dim: dict[str, list] = {}
    for s in pm.get().stances:
        if getattr(s, "status", "") != "active":
            continue
        dim = _dim_of(s)
        if dim:
            by_dim.setdefault(dim, []).append(s)
    return by_dim


def _open_gap_topics(pm: CognitiveProfileManager) -> set[str]:
    return {g.topic for g in pm.get().cognitive_gaps if g.status == "open"}


# ----------------------------------------------------------------------
# 冲突检测
# ----------------------------------------------------------------------
def detect_conflicts(text: str, pm: CognitiveProfileManager) -> list[dict]:
    """检测一条新表达与既有画像之间的真冲突（不落库，纯计算）。

    返回冲突描述列表；无冲突返回空列表。
    """
    if not (text or "").strip():
        return []
    by_dim = _existing_by_dim(pm)
    seen_topics = _open_gap_topics(pm)
    conflicts: list[dict] = []

    for dim, stances in by_dim.items():
        new_pol = _polarity_of(text, dim)
        if len(new_pol) != 1:
            continue  # 无信号，或文本自相矛盾（噪声，不值得问）
        new_side = next(iter(new_pol))

        for s in stances:
            old_pol = _polarity_of(getattr(s, "content", ""), dim)
            if len(old_pol) != 1:
                continue  # 既有条目本身方向不明，不做判断
            old_side = next(iter(old_pol))
            if old_side == new_side:
                continue  # 同极性 = 补充证据，非冲突

            topic = f"{_GAP_PREFIX}·{dim}"
            if topic in seen_topics:
                continue  # 该维度已有一个待澄清冲突，不重复堆积
            seen_topics.add(topic)

            conflicts.append({
                "dim": dim,
                "topic": topic,
                "old_side": old_side,
                "new_side": new_side,
                "old_item": s.id,
                "old_content": (getattr(s, "content", "") or "")[:60],
                "old_hits": old_pol[old_side][:3],
                "new_hits": new_pol[new_side][:3],
            })
    return conflicts


def _spawn_gaps(conflicts: list[dict], pm: CognitiveProfileManager) -> int:
    """把冲突落成 open gap。返回生成数。"""
    n = 0
    for c in conflicts:
        reason = (
            f"既有画像说他偏「{_POLARITY_LABEL.get(c['old_side'], c['old_side'])}」"
            f"（{'、'.join(c['old_hits'])}），但近期的表达出现了相反的信号"
            f"（{'、'.join(c['new_hits'])}）——是他想法变了，还是不同情境下不一样？"
        )
        try:
            pm.add_gap(
                topic=c["topic"],
                reason=reason,
                # 0.8 = 与「立场矛盾」同级的高优先级：一个就值得问，不必凑够
                # _TRIGGER_GAP_THRESHOLD(2)。见 interview.should_interview_now。
                priority=0.8,
                related_items=[c["old_item"]],
            )
            n += 1
            logger.info("冲突驱动生成 gap：%s（%s → %s）",
                        c["topic"], c["old_side"], c["new_side"])
        except Exception:
            logger.warning("生成冲突 gap 失败（跳过）: %s", c["topic"], exc_info=True)
    return n


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def _rows_from_diary(diary_store, limit: int) -> tuple[list[dict], str]:
    """（旧路径）只从日记取表达。显式注入 diary_store 时走这条。

    Returns: (rows, err) —— err 非空表示读取失败，调用方据此填 note。
    """
    try:
        page = diary_store.get_posts(limit=limit)
        items = page.get("items", []) if isinstance(page, dict) else []
    except Exception as e:
        logger.warning("读取日记失败，跳过冲突扫描: %s", e)
        return [], str(e)
    rows = []
    for p in items:
        pid = str(p.get("id", "") or "")
        if not pid:
            continue
        rows.append({"id": f"d:{pid}", "text": str(p.get("content", "") or "")})
    return rows, ""


def _rows_from_expressions(days: int, max_scan: int) -> list[dict]:
    """（新路径）从**他说过的所有话**取表达——不只日记。

    2026-09-05 修：此前只扫日记，于是访谈回答、聊天里说的那些推翻画像的话
    永远进不了视野——他答"低落时看电影、出去走走"，却从没写进日记，
    光扫日记就永远发现不了它跟"回血靠投入创作"的张力。
    """
    try:
        from . import expressions

        rows = expressions.collect(days=days)
    except Exception as e:
        logger.warning("表达源不可用，跳过冲突扫描: %s", e)
        return []
    # 只留近期、限条数：首跑面对 200+ 条存量时不至于一次爆量
    rows = [r for r in rows if str(r.get("text", "")).strip()]
    return rows[-max(1, int(max_scan)):]


def scan_and_spawn_gaps(
    pm: Optional[CognitiveProfileManager] = None,
    diary_store=None,
    limit: int = 200,
    days: int = 30,
    max_scan: int = 40,
) -> dict:
    """扫近期表达，检测与情感画像的真冲突并生成 gap。

    增量幂等：已扫描的表达 id 记在 profile_meta['conflict_scan_processed_ids']，
    历史内容不会重复触发（否则每次跑都会把旧日记翻出来当"新信号"）。
    id 带源前缀（``d:`` 日记 / ``i:`` 访谈 / ``l:`` 长期记忆）防碰撞。

    Args:
        diary_store: 显式注入时**只扫它**（兼容既有测试的假 store）；
            不传则走统一表达源（日记 + 访谈 + 长期记忆）。
        days: 不传 diary_store 时的回看天数。
        max_scan: 单次最多处理多少条，防首跑爆量。

    Returns:
        {"scanned": int, "new_posts": int, "conflicts": int, "gaps_added": int,
         "conflict_detail": list[dict], "note": str}
    """
    pm = pm or CognitiveProfileManager(layer="emotional")
    stats: dict = {
        "scanned": 0, "new_posts": 0, "conflicts": 0,
        "gaps_added": 0, "conflict_detail": [], "note": "",
    }

    if diary_store is not None:
        rows, err = _rows_from_diary(diary_store, limit)
        if err:
            stats["note"] = f"diary_read_failed: {err}"
            return stats
    else:
        rows = _rows_from_expressions(days=days, max_scan=max_scan)
        if not rows:
            stats["note"] = "no_expressions"
            return stats

    try:
        processed = _normalize_ids(pm.store.get_meta(_META_IDS, default=[]) or [])
    except Exception:
        processed = set()

    new_ids: list[str] = []
    for row in rows:
        rid = str(row.get("id", "") or "")
        if not rid:
            continue
        stats["scanned"] += 1
        if rid in processed:
            continue
        new_ids.append(rid)
        stats["new_posts"] += 1
        try:
            found = detect_conflicts(row.get("text", "") or "", pm)
        except Exception:
            logger.warning("冲突检测失败（跳过 %s）", rid, exc_info=True)
            continue
        if found:
            stats["conflicts"] += len(found)
            stats["conflict_detail"].extend(found)
            stats["gaps_added"] += _spawn_gaps(found, pm)

    if new_ids:
        processed.update(new_ids)
        try:
            pm.store.set_meta(_META_IDS, sorted(processed))
        except Exception:
            logger.warning("写入冲突扫描进度失败（下次会重扫，幂等无害）")

    stats["note"] = "ok" if stats["new_posts"] else "no_new_posts"
    return stats


def maybe_scan_conflicts_on_tick(
    interval_hours: float = 6.0,
    pm: Optional[CognitiveProfileManager] = None,
    diary_store=None,
    days: int = 30,
    max_scan: int = 40,
) -> Optional[dict]:
    """tick 周期调度入口：带节流、幂等、fail-open 的冲突扫描。

    这是让好奇心「可再生」的关键接线——不挂 tick 就是死代码。
    节流间隔默认 6h（比情感提取 4h 更长：冲突是低频事件，不必频扫）。

    diary_store 为 None 时扫**全部表达源**（日记+访谈+长期记忆）——
    只扫日记会让好奇心再生形同虚设，见本模块顶部说明。
    """
    try:
        pm = pm or CognitiveProfileManager(layer="emotional")
        last = pm.store.get_meta(_META_LAST)
        now = time.time()
        if last is not None:
            try:
                if (now - float(last)) < interval_hours * 3600:
                    return None
            except (TypeError, ValueError):
                pass
        res = scan_and_spawn_gaps(
            pm=pm, diary_store=diary_store, days=days, max_scan=max_scan
        )
        try:
            pm.store.set_meta(_META_LAST, now)
        except Exception:
            pass
        return res
    except Exception:
        logger.warning("冲突扫描 tick 失败（fail-open）", exc_info=True)
        return None
