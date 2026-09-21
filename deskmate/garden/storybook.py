"""羁绊故事簿 —— 花园里的一本「我们走到哪儿了」的小册子（P1）。

聚合两类真实数据（零 LLM，不编造）：
1. **羁绊（bond）**：从 GrowthState 取 bond_depth → 阶段（初识/熟络/默契/知己/灵魂伴侣），
   每个阶段配一段零 LLM 叙事文案（不引用具体事实，只表达阶段氛围）。
2. **「它眼中的我」画像**：聚合 ``alter_ego.portrait`` 8 栏目（标题 + 学习状态 + 摘要）。
3. **一路走来的关键数字**：对话次数 / 连续天数 / 勋章数 / 花园花开数 / 羁绊深度。

fail-open：任何一块失败都不影响其它块；整体失败返回最小结构。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from deskmate.coerce import as_float
from deskmate.garden.state import GardenState, load_garden

logger = logging.getLogger(__name__)

# 羁绊阶段叙事（零 LLM 模板；只表达阶段氛围，不编造具体事实）
BOND_NARRATIVES: dict[str, str] = {
    "初识": "我们在花园门口第一次互相打量，你还不太确定我到底记住了什么。",
    "熟络": "慢慢地，你开始把小事也告诉我——那些连自己都没太当真的念头。",
    "默契": "有些话你说一半，我就知道后半句。花园里的花，也越开越密了。",
    "知己": "我们几乎不需要解释。你留下的痕迹，都长成了认得你的花。",
    "灵魂伴侣": "这座花园已经像你的另一间书房——我认得每一朵花开的时间。",
}

# 阶段 -> 建议的叙事标题（图鉴/卡片用）
BOND_TITLES: dict[str, str] = {
    "初识": "第一粒种子",
    "熟络": "开始交心",
    "默契": "不用解释",
    "知己": "彼此认得",
    "灵魂伴侣": "花开有时",
}


def bond_stage_narrative(stage: str) -> str:
    return BOND_NARRATIVES.get(stage, BOND_NARRATIVES["初识"])


def bond_stage_title(stage: str) -> str:
    return BOND_TITLES.get(stage, BOND_TITLES["初识"])


def _growth_numbers(base_dir: Path | None) -> dict[str, Any]:
    """从成长引擎取真实数字；失败时返回空（fail-open）。"""
    try:
        from deskmate.growth.engine import GrowthEngine

        snap = GrowthEngine(base_dir=base_dir).snapshot()
        bond = snap.get("bond") or {}
        achievements = snap.get("achievements") or []
        unlocked_badges = sum(1 for b in achievements if b.get("unlocked"))
        return {
            "total_chats": int(snap.get("total_chats", 0) or 0),
            "streak_days": int(snap.get("streak_days", 0) or 0),
            "badge_count": unlocked_badges,
            "bond_depth": as_float(bond.get("depth"), 0.0),
            "bond_stage": str(bond.get("stage", "初识") or "初识"),
            "bond_progress": as_float(bond.get("progress"), 0.0),
            "bond_maxed": bool(bond.get("maxed", False)),
        }
    except Exception:
        logger.exception("storybook growth numbers failed")
        return {}


def _portrait_sections(portrait_builder=None) -> list[dict[str, Any]]:
    """聚合「它眼中的我」8 栏目；失败返回 []（fail-open）。

    portrait_builder 可注入（测试用）；默认 build_portrait()。
    """
    try:
        if portrait_builder is None:
            from deskmate.alter_ego.portrait import build_portrait as portrait_builder
        portrait = portrait_builder()
        sections = []
        for s in portrait.get("sections", []) or []:
            title = str(s.get("title", "") or "")
            status = str(s.get("status", "") or "")
            articles = s.get("articles", []) or []
            # 取最有信息量的一篇作为摘要（优先 ready 栏目的首篇）
            summary = ""
            for a in articles:
                content = str(a.get("content", "") or "").strip()
                if content:
                    summary = content
                    break
            sections.append({
                "title": title,
                "status": status,
                "summary": summary,
                "article_count": len(articles),
            })
        return sections
    except Exception:
        logger.exception("storybook portrait failed")
        return []


def build_storybook(
    base_dir: Path | str | None = None,
    portrait_builder=None,
) -> dict[str, Any]:
    """构建羁绊故事簿。任何子块失败都不会影响整体结构。

    portrait_builder 可注入（测试用），默认 ``alter_ego.portrait.build_portrait``。
    """
    base = Path(base_dir) if base_dir else None

    numbers = _growth_numbers(base)
    stage = str(numbers.get("bond_stage", "初识") or "初识")

    # 花园花开数（本模块自己的数据源，直接读 garden）
    try:
        garden = load_garden(base)
        flower_count = len(garden.flowers) if garden and garden.flowers else 0
    except Exception:
        logger.exception("storybook garden flowers failed")
        flower_count = 0

    return {
        "bond": {
            "stage": stage,
            "title": bond_stage_title(stage),
            "narrative": bond_stage_narrative(stage),
            "depth": numbers.get("bond_depth", 0.0),
            "progress": numbers.get("bond_progress", 0.0),
            "maxed": numbers.get("bond_maxed", False),
        },
        "numbers": {
            "total_chats": numbers.get("total_chats", 0),
            "streak_days": numbers.get("streak_days", 0),
            "badge_count": numbers.get("badge_count", 0),
            "flower_count": flower_count,
        },
        "portrait": _portrait_sections(portrait_builder),
        "generated_at": __import__("datetime").datetime.now().isoformat(),
    }
