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
import logging
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


def _title_for(kind: str, label: str, subject: str) -> str:
    """零 LLM 模板短标题。"""
    snippet = subject[:14] if subject else "记下一件事"
    return f"{label} · {snippet}"


# ---------------------------------------------------------------------------
# 判定：从成长状态里开出新花
# ---------------------------------------------------------------------------


def _candidate_flowers(growth) -> list[FlowerRecord]:
    """从 GrowthEngine.state 生成「应开但可能尚未开」的花（不查幂等）。"""
    out: list[FlowerRecord] = []
    state = growth.state
    seen: set[str] = set()

    # 1) recent_events 按 kind 映射
    for e in list(getattr(state, "recent_events", None) or []):
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind", ""))
        spec = FLOWER_KINDS.get(kind)
        if not spec:
            continue
        flower_kind, label, bed, mood = spec
        fp = _fingerprint(e)
        fid = f"{flower_kind}:{fp}"
        if fid in seen:
            continue
        seen.add(fid)
        out.append(FlowerRecord(
            flower_id=fid,
            kind=flower_kind,
            bed=bed,
            title=_title_for(kind, label, _subject_of(e)),
            quote=_quote_of(e),
            date=_today(),
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
        out.append(FlowerRecord(
            flower_id=fid,
            kind="badge_flower",
            bed="badge",
            title=_title_for("badge_unlocked", "勋章花", badge_id),
            quote="",
            date=str(unlocked_at)[:10] if unlocked_at else _today(),
            source_ts=str(unlocked_at or ""),
            mood_tint="顺利",
            echo=_echo_for("badge_flower", badge_id),
        ))

    return out


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
        candidates = _candidate_flowers(growth)
        fresh = [f for f in candidates if f.flower_id not in garden.flowers]
        if not fresh:
            return []
        for f in fresh:
            garden.flowers[f.flower_id] = f.to_dict()
        _log(garden, {
            "type": "bloom",
            "at": _today(),
            "count": len(fresh),
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


def garden_snapshot(garden: GardenState) -> dict:
    """花园快照（供 GET /garden 返回）：树等级 + 年花 + 浇水 + 点亮 + 年鉴摘要。

    全部从真实状态派生，零 LLM。任何单项异常不影响整体（fail-open 由调用方兜底）。
    """
    tree = tree_stage(len(garden.flowers))
    return {
        "tree": tree,
        "year_flowers": [dict(v) for v in sorted(garden.year_flowers.values(), key=lambda x: x.get("year", ""))],
        "years": year_stats(garden),
        "water": {
            "streak": 0,  # 今日是否已浇/连续天数由 visit 结算后回填（GET 无副作用）
            "today": "",
        },
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
