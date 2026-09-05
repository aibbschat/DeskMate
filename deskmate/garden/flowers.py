"""花开记录 —— 把「值得记的事」开成花园里的花。

设计要点（复刻 ``growth/badges.py`` 的声明式注册表模式）：
1. **声明与判定分离**：``FLOWER_KINDS`` 是唯一权威映射表，加一种花 = 加一行，
   不动任何判定逻辑。判定统一走 ``maybe_bloom``。
2. **幂等**：``flower_id = "{kind}:{fingerprint}"``，同一次事件（指纹一致）只开一朵；
   同一批历史数据首次上线时自然补开，之后不再重复。
3. **零 LLM、真实可溯源**：``quote`` 一律摘事件原文（note/message/subject 等真实字段），
   取不到就留空，**绝不编造**；``title`` 由模板渲染（对齐 ``DiaryWriter`` 的零 LLM 范式）。
4. **只开「值得记的事」**：从 ``GrowthState.recent_events`` 按 kind 映射，
   日常痕迹类（chat/memory_learned 这类 0.5 分痕迹）不开花，避免花园长满杂草。
   历史徽章（``badges_unlocked``，非 backfilled）首次上线补开成勋章花。

fail-open：任何异常静默，不影响个人中心与主动推理主流程。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from deskmate.garden.state import (
    FlowerRecord,
    GardenState,
    load_garden,
    save_garden,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 花种注册表：event kind -> (flower_kind, label, bed, mood_tint)
# ---------------------------------------------------------------------------
# 分档依据（对齐 diary_inspiration._EVENT_WEIGHTS）：
#   只让「里程碑 / 反思 / 协作」类（权重 >= 2.0）开花，
#   日常痕迹（chat / memory_learned 等）不开花。
FLOWER_KINDS: dict[str, tuple[str, str, str, str]] = {
    # 里程碑 —— 第一次 / 拍板 / 新建
    "skill_created": ("starflower", "星芒草", "milestone", "顺利"),
    "creation_approved": ("sunflower", "向日葵", "milestone", "顺利"),
    "badge_unlocked": ("badge_flower", "勋章花", "badge", "顺利"),
    # 反思 —— 有情感含量
    "user_corrected": ("dandelion", "蒲公英", "milestone", "成长"),
    "creation_rework": ("dandelion", "蒲公英", "milestone", "成长"),
    # 协作 —— 一起做成一件事
    "roundtable": ("bellflower", "风铃草", "milestone", "顺利"),
    "suggestion_accepted": ("hyacinth", "风信子", "milestone", "顺利"),
    "skill_evolved": ("sprout", "新芽", "milestone", "顺利"),
    "writing": ("iris", "鸢尾", "milestone", "平静"),
    "report": ("daisy", "雏菊", "milestone", "顺利"),
}

# 反向：flower_kind -> 展示信息（供前端渲染与图鉴；desc 为零 LLM 花语）
FLOWER_INFO: dict[str, dict] = {
    "starflower": {"label": "星芒草", "emoji": "✨", "desc": "第一次学会新东西，亮一颗星。"},
    "sunflower": {"label": "向日葵", "emoji": "🌻", "desc": "你拍板做成一件事，它朝你开。"},
    "badge_flower": {"label": "勋章花", "emoji": "🏅", "desc": "一枚勋章长成了一朵花。"},
    "dandelion": {"label": "蒲公英", "emoji": "🌼", "desc": "改过的路都算数，它替你记着。"},
    "bellflower": {"label": "风铃草", "emoji": "🔔", "desc": "圆桌上敲定的，风铃草摇了摇。"},
    "hyacinth": {"label": "风信子", "emoji": "💐", "desc": "我的建议被收下，开了一朵。"},
    "sprout": {"label": "新芽", "emoji": "🌱", "desc": "技能又长一截，冒个新芽。"},
    "iris": {"label": "鸢尾", "emoji": "🪻", "desc": "写下的字，开成了鸢尾。"},
    "daisy": {"label": "雏菊", "emoji": "🌼", "desc": "一份报告的雏菊，安安静静。"},
}

# 小桔在花开处的「回响」（P1 花园回响）：按花种零 LLM 模板 + 真实事件变量。
ECHO_TEMPLATES: dict[str, str] = {
    "starflower": "第一次学会新东西，我把这颗星芒草记进花园。",
    "sunflower": "你拍板的那一刻，向日葵就朝着你开了。",
    "badge_flower": "又一块勋章，在花园里长成了一朵花。",
    "dandelion": "改过的路都算数，蒲公英替你收着这些弯。",
    "bellflower": "圆桌上敲定的事，风铃草摇了摇，算是应了一声。",
    "hyacinth": "我的建议被收下，风信子悄悄开了一朵。",
    "sprout": "技能又长了一截，花园里冒出了个新芽。",
    "iris": "你写下的字，在这儿开成了鸢尾。",
    "daisy": "这份报告，是一朵雏菊，安安静静地谢幕。",
}

BED_LABELS: dict[str, str] = {
    "memory": "记忆圃",
    "diary": "日记圃",
    "badge": "勋章圃",
    "milestone": "里程碑圃",
}


def _echo_for(flower_kind: str, subject: str) -> str:
    """小桔的回响：模板 + 真实 subject 片段；不编造事实。"""
    text = ECHO_TEMPLATES.get(flower_kind, "这一朵，开得正是时候。")
    if subject:
        return text  # 模板已含情感，subject 已进 title
    return text


def _subject_of(e: dict) -> str:
    """事件的「主体」——用于指纹去重（同一技能反复进化不该重复计数）。"""
    for key in ("skill_name", "note", "message", "subject", "key", "title", "name"):
        v = e.get(key)
        if v:
            return str(v)
    return ""


def _quote_of(e: dict) -> str:
    """事件的真实原文摘录；取不到留空（零 LLM，禁止编造）。"""
    for key in ("note", "message", "detail", "subject", "title", "content"):
        v = e.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _fingerprint(e: dict) -> str:
    return f"{e.get('ts', '')}|{e.get('kind', '')}|{_subject_of(e)}"


def _today() -> str:
    return datetime.date.today().isoformat()


def _local_date_from_ts(ts: str) -> str:
    """把事件时间戳换算成**本地日期**；取不到返回空串。

    事件的 ``ts`` 一律带 UTC 偏移（形如 ``2026-09-04T02:43:18+00:00``），而花的
    ``date`` 是给人看的本地日期。若直接用 ``date.today()``，跨天时段开出的花会
    系统性标错一天，且与按 ``source_ts`` 统计的 ``year_stats`` 口径打架。
    """
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10] if len(raw) >= 10 else ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date().isoformat()


# 素材账本回溯上限：recent_events 只有 50 条易失窗口，滚出去的事件靠账本补回。
# 花开本身幂等（flower_id = kind + 指纹），重复扫描不会重复开花。
MATERIAL_LOOKBACK_FILES = 120
MATERIAL_SCAN_CAP = 3000


def _material_events(base_dir: Path | str | None) -> list[dict]:
    """读取日记素材账本里的历史事件，补上滚出 recent_events 窗口的部分。

    账本是 append-only 的全量事件流水（``diary/materials/YYYY-MM-DD.jsonl``），
    不会像 ``recent_events`` 那样被截断。只读，任何异常返回已有结果。
    """
    try:
        base = Path(base_dir) if base_dir else Path(os.path.expanduser("~/.deskmate"))
        mdir = base / "diary" / "materials"
        if not mdir.is_dir():
            return []
        out: list[dict] = []
        for fp in sorted(mdir.glob("*.jsonl"), reverse=True)[:MATERIAL_LOOKBACK_FILES]:
            try:
                for line in fp.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        out.append(rec)
                        if len(out) >= MATERIAL_SCAN_CAP:
                            return out
            except Exception:
                continue
        return out
    except Exception:
        logger.debug("garden material ledger scan failed", exc_info=True)
        return []


def _title_for(kind: str, label: str, subject: str) -> str:
    """零 LLM 模板短标题。"""
    snippet = subject[:14] if subject else "记下一件事"
    return f"{label} · {snippet}"


# ---------------------------------------------------------------------------
# 判定：从成长状态里开出新花
# ---------------------------------------------------------------------------


def _candidate_flowers(growth, base_dir: Path | str | None = None) -> list[FlowerRecord]:
    """从 GrowthEngine.state 生成「应开但可能尚未开」的花（不查幂等）。

    数据源两路：``recent_events``（快，但只有 50 条易失窗口）＋ 日记素材账本
    （append-only 全量流水，把滚出窗口的事件补回来）。花 id 幂等，重复不重复开。
    """
    out: list[FlowerRecord] = []
    state = growth.state
    seen: set[str] = set()

    # 1) recent_events 按 kind 映射（账本补回的历史事件一并参与）
    events = list(getattr(state, "recent_events", None) or [])
    events = events + _material_events(base_dir)
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind", ""))
        spec = FLOWER_KINDS.get(kind)
        if not spec:
            continue
        flower_kind, label, bed, _default_mood = spec
        fp = _fingerprint(e)
        fid = f"{flower_kind}:{fp}"
        if fid in seen:
            continue
        seen.add(fid)
        # 发生日（本地时区），不是发现日 —— 补开的历史花才不会全挤在今天
        date = _local_date_from_ts(e.get("ts", "")) or _today()
        # 情绪定色：当日真实情感优先，其次情感画像按事件类型，最后花种默认
        mood = _mood_for_event(base_dir, date, kind, _default_mood)
        out.append(FlowerRecord(
            flower_id=fid,
            kind=flower_kind,
            bed=bed,
            title=_title_for(kind, label, _subject_of(e)),
            quote=_quote_of(e),
            date=date,
            source_ts=str(e.get("ts", "") or ""),
            mood_tint=mood,
            echo=_echo_for(flower_kind, _subject_of(e)),
        ))

    # 2) 历史徽章补开（时间可考才开，backfilled 不算）
    badges = getattr(state, "badges_unlocked", None) or {}
    for badge_id, unlocked_at in badges.items():
        if not badge_id or unlocked_at == "backfilled":
            continue
        fid = f"badge_flower:badge:{badge_id}"
        if fid in seen:
            continue
        seen.add(fid)
        badge_date = str(unlocked_at)[:10] if unlocked_at else _today()
        out.append(FlowerRecord(
            flower_id=fid,
            kind="badge_flower",
            bed="badge",
            title=_title_for("badge_unlocked", "勋章花", badge_id),
            quote="",
            date=badge_date,
            source_ts=str(unlocked_at or ""),
            mood_tint=_mood_for_event(base_dir, badge_date, "badge_unlocked", "顺利"),
            echo=_echo_for("badge_flower", badge_id),
        ))

    return out


# ---------------------------------------------------------------------------
# 情绪定色：接情感时间线（情感层 → 花园的接线）
# ---------------------------------------------------------------------------
# 花园是情感层的表达载体，但历史上花的 mood_tint 只从「事件类型」静态查表——
# skill_evolved 恒为「顺利」，于是赶稿压力最大那几天开出的花也一片「顺利」，
# 与 EmotionTimeline 记下的真实状态对不上（2026-09-05 实测 119 朵里 108 朵不符）。
#
# 情感时间线的状态词（EmotionTimeline._infer_emotion 产出）与花的 mood_tint
# 是两套词表，这里显式对齐。前端 GardenFlowerSVG 的 MOOD_TINT 需同步覆盖。
_TIMELINE_MOOD: dict[str, str] = {
    "压力大": "压力",
    "低落": "低落",
    "忙碌": "忙碌",
    "顺利": "顺利",
    "平静": "平静",
}


def _mood_from_timeline(base_dir: Path | str | None, date: str) -> str:
    """取某一天的真实情感状态（映射为花的 mood_tint），查不到返回空串。

    情感时间线按天记快照，而花开的日期是「事件发生日」（见 _local_date_from_ts），
    两边都是本地日期，可以直接对上——补开的历史花也能按当天真实心情定色。

    fail-open：读不到 / 异常都返回 ""，由调用方回落到事件类型查表。
    """
    if not date:
        return ""
    try:
        from deskmate.growth.emotion_timeline import EmotionTimeline

        snap = EmotionTimeline(base_dir=base_dir)._data.get(date)
        if not isinstance(snap, dict):
            return ""
        return _TIMELINE_MOOD.get(str(snap.get("state", "") or ""), "")
    except Exception:
        logger.debug("garden: read emotion timeline failed", exc_info=True)
        return ""


def _mood_for_event(base_dir: Path | str | None, date: str, kind: str, default: str) -> str:
    """花的情绪定色，优先级：**当日真实情感 > 情感画像按事件类型 > 花种默认**。

    真实情感来自 EmotionTimeline（按天快照，含 mood 与当日事件统计）；
    情感画像的 mood_tint_for_event 只按事件类型静态查表，是次优解。
    """
    real = _mood_from_timeline(base_dir, date)
    if real:
        return real
    try:
        from deskmate.cognitive.emotion_profile import mood_tint_for_event

        return mood_tint_for_event(kind) or default
    except Exception:
        return default  # fail-open：画像读取失败用默认


def retint_flowers(garden: GardenState, base_dir: Path | str | None = None) -> int:
    """按当日真实情感重算已有花的 mood_tint（幂等，只改有据可依的那些）。

    存量花在接线前已按「事件类型」定色并落库，不重算的话旧花永远停在错误的
    颜色上。只在情感时间线里**查得到当天快照且值与当前不同**时才改——
    查不到（那天没跑 tick）保持原样，绝不把有颜色的花清成空白。

    Returns: 本次被改色的花朵数；无改动返回 0。任何异常返回 0（fail-open）。
    """
    try:
        from deskmate.growth.emotion_timeline import EmotionTimeline

        data = EmotionTimeline(base_dir=base_dir)._data
        if not isinstance(data, dict) or not data:
            return 0
        changed = 0
        for f in garden.flowers.values():
            if not isinstance(f, dict):
                continue
            snapshot = data.get(str(f.get("date", "") or ""))
            if not isinstance(snapshot, dict):
                continue
            mood = _TIMELINE_MOOD.get(str(snapshot.get("state", "") or ""), "")
            if mood and f.get("mood_tint") != mood:
                f["mood_tint"] = mood
                changed += 1
        return changed
    except Exception:
        logger.debug("garden: retint flowers failed", exc_info=True)
        return 0


def maybe_bloom(base_dir: Path | str | None = None) -> list[dict]:
    """把成长状态里的新事件开成花，写入花园；返回新增花的 dict 列表。

    幂等：已存在于 ``garden.flowers`` 的花不会重复开。
    首次上线：历史徽章 / 近事件自动补开，花园一打开就有内容。

    Returns:
        本次新增的花朵（dict）；无新增返回 []。任何异常返回 []（fail-open）。
    """
    try:
        from deskmate.growth.engine import GrowthEngine

        growth = GrowthEngine(base_dir=base_dir)
        garden = load_garden(base_dir)
        candidates = _candidate_flowers(growth, base_dir)
        fresh = [f for f in candidates if f.flower_id not in garden.flowers]
        # 存量花按当日真实情感重新定色（幂等）。接线前落库的花是按事件类型
        # 定色的，不重算就永远停在错误颜色上——只改情感时间线有据可依的那些。
        retinted = retint_flowers(garden, base_dir)
        if not fresh and not retinted:
            return []
        for f in fresh:
            garden.flowers[f.flower_id] = f.to_dict()
        _log(garden, {
            "type": "bloom",
            "at": _today(),
            "count": len(fresh),
            "retinted": retinted,
            "ids": [f.flower_id for f in fresh],
        })
        save_garden(garden, base_dir)
        return [f.to_dict() for f in fresh]
    except Exception:
        logger.exception("garden maybe_bloom failed")
        return []


def _log(garden: GardenState, entry: dict) -> None:
    garden.garden_log.append(entry)


# ---------------------------------------------------------------------------
# 只读快照（供 API）
# ---------------------------------------------------------------------------


def flower_stats(garden: GardenState) -> dict:
    """花开统计：总数 + 各花圃数量 + 各花种数量。"""
    by_bed: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for f in garden.flowers.values():
        bed = f.get("bed", "memory")
        kind = f.get("kind", "")
        by_bed[bed] = by_bed.get(bed, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "total": len(garden.flowers),
        "by_bed": by_bed,
        "by_kind": by_kind,
    }


def flora_snapshot(garden: GardenState) -> list[dict]:
    """花园图鉴（P1）：每种花的定义 + 已解锁数量。零 LLM，花语为静态文案。"""
    stats: dict[str, int] = {}
    for f in garden.flowers.values():
        kind = f.get("kind", "")
        if kind:
            stats[kind] = stats.get(kind, 0) + 1
    out = []
    for kind, info in FLOWER_INFO.items():
        out.append({
            "kind": kind,
            "label": info["label"],
            "emoji": info["emoji"],
            "desc": info["desc"],
            "unlocked": stats.get(kind, 0),
        })
    return out


# ---------------------------------------------------------------------------
# P2 · 成长线 / 浇水 / 点亮 / 年花 / 年鉴归档
# ---------------------------------------------------------------------------

# 花园的「树」成长线：花朵总数 → 树等级（纯函数派生，零 LLM，真实计数驱动）。
# (threshold, stage, title, desc) —— threshold 为达到该等级所需最少花朵数。
GARDEN_TREES: list[tuple[int, str, str, str]] = [
    (0,   "seed",    "一粒种子", "种子刚入土，第一片叶子正在探头。"),
    (10,  "sprout",  "一株小苗", "十朵花排成行，小苗有了自己的样子。"),
    (30,  "sapling", "一棵花树", "三十朵花开满枝，它长成了一棵花树。"),
    (60,  "bloom",   "一树繁花", "六十朵花，繁花满枝，树影都香了。"),
    (100, "canopy",  "亭亭如盖", "一百朵花，这棵花树已经亭亭如盖。"),
]


def tree_stage(flower_count: int) -> dict:
    """由真实花朵总数派生出树等级（确定性，无状态写入）。"""
    n = max(0, int(flower_count))
    stage = GARDEN_TREES[0]
    for row in GARDEN_TREES:
        if n >= row[0]:
            stage = row
        else:
            break
    threshold, key, title, desc = stage
    # 下一档信息（进度展示用）
    nxt = None
    for row in GARDEN_TREES:
        if row[0] > n:
            nxt = row
            break
    return {
        "key": key,
        "title": title,
        "desc": desc,
        "count": n,
        "threshold": threshold,
        "next": {"title": nxt[2], "threshold": nxt[0]} if nxt else None,
    }


def _flower_year(f: dict) -> str:
    """花的归属年份（source_ts 优先，其次 date；取不到年份按 0000）。"""
    raw = str(f.get("source_ts", "") or f.get("date", "") or "")
    return raw[:4] if len(raw) >= 4 and raw[:4].isdigit() else "0000"


def year_stats(garden: GardenState) -> dict[str, int]:
    """按年份统计已开的花数（从真实花朵 source_ts/date 派生，纯函数）。"""
    out: dict[str, int] = {}
    for f in garden.flowers.values():
        y = _flower_year(f)
        out[y] = out.get(y, 0) + 1
    return out


def water_today(base_dir: Path | str | None = None, now: Optional[datetime.datetime] = None) -> dict:
    """每天首次进园自动「浇一次水」（幂等 water:date）。

    确定性：挂在本地上班/到访时刻，不依赖任何检测。每天最多一次，
    写 garden_log 可回看，返回今日是否新浇 + 连续浇水天数。

    Returns:
        ``{watered, streak, today}`` —— watered 是否本次新浇，streak 连续天数（含今天）。
        任何异常返回 ``{watered: False, streak: 0, today: ""}``（fail-open）。
    """
    try:
        garden = load_garden(base_dir)
        now = now or datetime.datetime.now()
        today = now.date().isoformat()
        is_new = today not in garden.water_log
        if is_new:
            garden.water_log[today] = now.isoformat(timespec="seconds")
            _log(garden, {"type": "water", "date": today, "at": today})
        # 连续天数：从今天往前数连续存在浇水的天数
        streak = 0
        cursor = now.date()
        while garden.water_log.get(cursor.isoformat()) is not None:
            streak += 1
            cursor -= datetime.timedelta(days=1)
        save_garden(garden, base_dir)
        return {"watered": is_new, "streak": streak, "today": today}
    except Exception:
        logger.exception("garden water_today failed")
        return {"watered": False, "streak": 0, "today": ""}


def lit_flower(garden: GardenState, flower_id: str, now: Optional[datetime.datetime] = None) -> dict:
    """用户主动「点亮」一朵花（幂等）：花带 ⭐ 高亮，永久保留，可回看。

    Args:
        garden: 已加载的花园状态（调用方负责保存）
        flower_id: 要点亮的花朵 id（不存在则忽略）

    Returns:
        ``{lit: bool, flower_id, total}`` —— lit 是否本次新点亮，total 累计点亮数。
    """
    now = now or datetime.datetime.now()
    if not flower_id or flower_id not in garden.flowers:
        return {"lit": False, "flower_id": flower_id, "total": len(garden.lit_flowers)}
    is_new = flower_id not in garden.lit_flowers
    if is_new:
        garden.lit_flowers[flower_id] = now.isoformat(timespec="seconds")
        _log(garden, {"type": "lit", "flower_id": flower_id, "at": now.isoformat(timespec="seconds")})
    return {"lit": is_new, "flower_id": flower_id, "total": len(garden.lit_flowers)}


def year_flower_ensure(garden: GardenState, now: Optional[datetime.datetime] = None) -> dict:
    """跨年首次进园时结算「年花」（幂等）：确保今年的年花记录存在。

    年花 = 四季轮回叙事的一环：每一年的首次来访都会在花园里留下一枚年轮，
    记录这年你第一次回到花园的日子。当年已开的花数由前端/API 从 source_ts
    实时统计（year_stats），不在年花记录里重复落库，避免两处数字不一致。

    Returns:
        ``{year, first_visit_date, fresh}`` —— fresh 是否本次新建。
    """
    now = now or datetime.datetime.now()
    year = str(now.year)
    existing = garden.year_flowers.get(year)
    if existing:
        return {
            "year": year,
            "first_visit_date": str(existing.get("first_visit_date", "") or ""),
            "fresh": False,
        }
    garden.year_flowers[year] = {
        "year": year,
        "first_visit_date": now.date().isoformat(),
    }
    _log(garden, {"type": "year_flower", "year": year, "at": now.date().isoformat()})
    return {"year": year, "first_visit_date": now.date().isoformat(), "fresh": True}


# 主花园展示封顶：超过后最旧的移入「花园年鉴」归档（cap 500，防无限增长拖垮前端）
FLOWER_CAP = 200
ARCHIVE_CAP = 500


def maybe_archive(garden: GardenState) -> list[dict]:
    """花朵超过封顶时，把最旧的移入年鉴（幂等，仅调用方在改动后触发）。

    只读性保证：归档花保留原始字段，可从年鉴回看；主花园保持最近 FLOWER_CAP 朵。
    Returns: 本次归档的花 dict 列表；无则 []。
    """
    if len(garden.flowers) <= FLOWER_CAP:
        return []
    sorted_flowers = sorted(
        garden.flowers.values(),
        key=lambda x: str(x.get("source_ts", "") or x.get("date", "")),
        reverse=True,
    )
    keep = sorted_flowers[:FLOWER_CAP]
    overflow = sorted_flowers[FLOWER_CAP:]
    keep_ids = {f.get("flower_id", "") for f in keep}
    garden.flowers = {k: v for k, v in garden.flowers.items() if k in keep_ids}
    garden.archive = (overflow + garden.archive)[:ARCHIVE_CAP]
    _log(garden, {"type": "archive", "count": len(overflow), "at": datetime.date.today().isoformat()})
    return overflow


def _water_snapshot(garden: GardenState) -> dict:
    """从 ``water_log`` 真实派生浇水状态（GET /garden 无副作用，故只读不写）。

    此前恒返回 ``streak: 0 / today: ""``，靠前端拿 visit 的返回值回填；一旦
    visit 请求慢或失败，「今天明明浇过水」也会显示成连续 0 天。
    """
    try:
        today = _today()
        streak = 0
        cursor = datetime.date.today()
        while garden.water_log.get(cursor.isoformat()) is not None:
            streak += 1
            cursor -= datetime.timedelta(days=1)
        # watered 取「今天是否已浇」（= 展示语义），不是 visit 的「本次是否新浇」：
        # 一天内第二次进园时后者为 False，但今天确实浇过了。
        return {
            "watered": bool(garden.water_log.get(today)),
            "streak": streak,
            "today": today if garden.water_log.get(today) else "",
        }
    except Exception:
        logger.debug("garden water snapshot failed", exc_info=True)
        return {"watered": False, "streak": 0, "today": ""}


def garden_snapshot(garden: GardenState) -> dict:
    """花园快照（供 GET /garden 返回）：树等级 + 年花 + 浇水 + 点亮 + 年鉴摘要。

    全部从真实状态派生，零 LLM。任何单项异常不影响整体（fail-open 由调用方兜底）。
    """
    tree = tree_stage(len(garden.flowers))
    return {
        "tree": tree,
        "year_flowers": [dict(v) for v in sorted(garden.year_flowers.values(), key=lambda x: x.get("year", ""))],
        "years": year_stats(garden),
        "water": _water_snapshot(garden),
        "lit": {
            "count": len(garden.lit_flowers),
            "ids": sorted(garden.lit_flowers.keys()),
        },
        "archive": {
            "count": len(garden.archive),
            "years": _archive_years(garden.archive),
        },
    }


def _archive_years(archive: list[dict]) -> list[dict]:
    """年鉴按年份分组摘要：{year, count, sample_title}。"""
    by_year: dict[str, list[str]] = {}
    for f in archive:
        y = _flower_year(f)
        by_year.setdefault(y, []).append(str(f.get("title", "") or ""))
    out = []
    for y in sorted(by_year.keys(), reverse=True):
        titles = [t for t in by_year[y] if t]
        out.append({"year": y, "count": len(by_year[y]), "sample_title": titles[0] if titles else ""})
    return out
