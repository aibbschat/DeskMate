"""
认知画像冷启动——从已有的 vault/MEMORY/style_profile 中提取初始认知画像。

策略：
- 不需要重新读所有书，从已有结构化数据中提取和归纳
- vault 知识 → 立场和价值观（用户的创作偏好、写作原则）
- MEMORY.md → 思维框架和论证模式（用户的纠正、决策、工作流约定）
- style_profile → 论证偏好和表达习惯
- 冷启动的置信度较低，需要后续每日读书的认知提取和用户校准来提高

目标：让用户立刻能看到第一版「它理解的我」，而不是"等它读十本书再说"。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .models import SourceRef, _now
from .profile import CognitiveProfileManager, get_profile_manager


def _base_dir() -> Path:
    return Path(os.path.expanduser("~/.deskmate"))


def _read_vault_entries() -> list[dict]:
    """读取 vault 中的所有条目，返回简化的列表。"""
    vault_dir = _base_dir() / "vault"
    entries = []
    if not vault_dir.exists():
        return entries

    for category_dir in vault_dir.iterdir():
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        for f in category_dir.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                entries.append({
                    "category": category,
                    "title": f.stem,
                    "content": content,
                    "path": str(f),
                })
            except Exception:
                continue
    return entries


def _read_memory_md() -> str:
    """读取 MEMORY.md。"""
    p = _base_dir() / "MEMORY.md"
    if p.exists():
        return p.read_text(encoding="utf-8", errors="ignore")
    return ""


def _read_style_profile() -> dict:
    """读取 style_profile.json。"""
    p = _base_dir() / "style_profile.json"
    if p.exists():
        import json
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def cold_start(
    profile_manager: Optional[CognitiveProfileManager] = None,
    use_llm: bool = False,
) -> dict:
    """
    冷启动认知画像——从已有数据中提取初始认知特征。

    Args:
        profile_manager: 画像管理器（默认用全局单例）
        use_llm: 是否使用 LLM 做深度提取（默认 False，冷启动用规则提取，快速生成第一版）

    Returns:
        冷启动统计：{"stances": n, "argument_patterns": n, "value_rankings": n, "thinking_frameworks": n, "sources_used": n}
    """
    pm = profile_manager or get_profile_manager()

    # 如果已经有数据了，不重复冷启动
    existing = pm.get()
    if existing.stances or existing.argument_patterns or existing.value_rankings:
        return {"skipped": True, "reason": "profile already has data", "existing_stats": existing.stats()}

    stats = {"stances": 0, "argument_patterns": 0, "value_rankings": 0, "thinking_frameworks": 0, "sources_used": 0}

    # ---------------------------------------------------------------
    # 1. 从 vault 提取立场和价值观
    # ---------------------------------------------------------------
    vault_entries = _read_vault_entries()
    stats["sources_used"] += len(vault_entries)

    for entry in vault_entries:
        content = entry["content"]
        title = entry["title"]
        category = entry["category"]

        # 从 preferences 类 vault 提取创作立场和价值观
        if category == "preferences":
            _extract_preferences_from_vault(pm, content, title, stats)

        # 从 corrections 类 vault 提取思维框架（用户纠正 AI 的方式反映了用户的思维方式）
        elif category == "corrections":
            _extract_corrections_from_vault(pm, content, title, stats)

        # 从 work 类 vault 提取创作相关的立场
        elif category == "work":
            _extract_work_from_vault(pm, content, title, stats)

    # ---------------------------------------------------------------
    # 2. 从 MEMORY.md 提取思维框架和论证模式
    # ---------------------------------------------------------------
    memory_md = _read_memory_md()
    if memory_md:
        stats["sources_used"] += 1
        _extract_from_memory_md(pm, memory_md, stats)

    # ---------------------------------------------------------------
    # 3. 从 style_profile 提取论证偏好和表达习惯
    # ---------------------------------------------------------------
    style = _read_style_profile()
    if style:
        stats["sources_used"] += 1
        _extract_from_style_profile(pm, style, stats)

    # 更新画像摘要
    profile = pm.get()
    active_stances = profile.active_stances()
    if active_stances:
        topics = list(set(s.topic for s in active_stances if s.topic))[:3]
        profile.summary = f"冷启动：从{vault_entries and len(vault_entries) or 0}条vault、MEMORY.md、style_profile中提取了{len(active_stances)}条立场等认知特征，置信度待后续阅读和校准提高。"
        pm.save()

    return stats


def _extract_preferences_from_vault(pm: CognitiveProfileManager, content: str, title: str, stats: dict):
    """从 preferences 类 vault 提取创作立场和价值观。"""
    source = SourceRef(
        type="vault",
        title=f"vault/preferences/{title}",
        excerpt=content[:200],
        confidence=0.6,  # vault 是用户明确设定的偏好，置信度中等
    )

    # 规则提取：检测常见的创作偏好关键词
    content_lower = content.lower()

    # 史料严谨性
    if any(kw in content for kw in ["史料", "正史", "考据", "严谨", "查资料", "拒绝自媒体", "来源"]):
        pm.add_stance(
            topic="历史真实性",
            content="创作中高度重视史料真实性和来源可靠性，拒绝无来源的自媒体信息，偏好正史和可查证的资料。",
            source=source,
            tags=["创作观", "史料"],
        )
        stats["stances"] += 1

        pm.add_value_ranking(
            domain="创作",
            ranking=["史料真实", "叙事流畅", "读者易懂", "戏剧效果"],
            content="在历史小说创作中，史料真实性优先于叙事流畅和戏剧效果。",
            source=source,
            tags=["价值观", "创作"],
        )
        stats["value_rankings"] += 1

    # 主题内核
    if any(kw in content for kw in ["主题", "内核", "价值", "意义", "深度"]):
        pm.add_stance(
            topic="创作主题",
            content="每部作品必须有明确的主题内核，不能只是情节堆砌，作品需要有超越故事本身的价值和意义。",
            source=source,
            tags=["创作观", "主题"],
        )
        stats["stances"] += 1

    # 不代笔
    if any(kw in content for kw in ["不代笔", "自己写", "助手", "不续写", "每一个字"]):
        pm.add_stance(
            topic="创作分工",
            content="小说正文由作者本人创作，AI 只负责灵感、思路、资料、建议，不代笔续写。",
            source=source,
            tags=["创作观", "分工"],
        )
        stats["stances"] += 1


def _extract_corrections_from_vault(pm: CognitiveProfileManager, content: str, title: str, stats: dict):
    """从 corrections 类 vault 提取思维框架（用户纠正 AI 的方式反映了用户的思维方式）。"""
    source = SourceRef(
        type="vault",
        title=f"vault/corrections/{title}",
        excerpt=content[:200],
        confidence=0.55,
    )

    # 检测用户是否强调"根因分析"
    if any(kw in content for kw in ["根因", "根本原因", "技术层面", "工程", "bug", "不是态度"]):
        pm.add_thinking_framework(
            domain="技术问题",
            steps=["定位根因", "区分工程问题和态度问题", "用工程手段解决", "验证修复", "文档记录"],
            content="遇到技术问题时，先定位根本原因，区分是工程问题还是态度问题，用工程手段解决而不是靠自我约束，修复后验证并记录。",
            source=source,
            tags=["思维框架", "技术"],
        )
        stats["thinking_frameworks"] += 1

    # 检测用户是否强调"多平台同步"
    if any(kw in content for kw in ["同步", "多平台", "关联", "全局意识", "主动"]):
        pm.add_thinking_framework(
            domain="内容发布",
            steps=["修改一处", "主动想到所有关联平台", "同步更新", "验证一致性"],
            content="同一内容发布在多平台时，修改一处后应主动想到并处理所有关联平台，不要只做明确交待的部分。",
            source=source,
            tags=["思维框架", "工作流"],
        )
        stats["thinking_frameworks"] += 1


def _extract_work_from_vault(pm: CognitiveProfileManager, content: str, title: str, stats: dict):
    """从 work 类 vault 提取创作相关的立场。"""
    source = SourceRef(
        type="vault",
        title=f"vault/work/{title}",
        excerpt=content[:200],
        confidence=0.5,
    )

    # 检测创作计划中的主题思考
    if any(kw in content for kw in ["主题内核", "主题", "内核", "价值"]):
        # 不重复添加（如果 preferences 里已经加过了）
        existing = [s for s in pm.get().stances if s.topic == "创作主题" and s.status == "active"]
        if not existing:
            pm.add_stance(
                topic="创作主题",
                content="每部作品必须有明确的主题内核，不能只是情节堆砌。",
                source=source,
                tags=["创作观"],
            )
            stats["stances"] += 1


def _extract_from_memory_md(pm: CognitiveProfileManager, memory_md: str, stats: dict):
    """从 MEMORY.md 提取思维框架和论证模式。"""
    source = SourceRef(
        type="memory",
        title="MEMORY.md",
        excerpt=memory_md[:200],
        confidence=0.5,
    )

    # 检测"不确定就说不确定"的原则
    if any(kw in memory_md for kw in ["不确定", "没读完", "不比", "不评", "不断", "读了多少说多少"]):
        pm.add_argument_pattern(
            content="在知识和判断上保持诚实，不确定就说不确定，没读完就不比不评不断，不凭残存摘要瞎编。",
            pattern_type="诚实声明",
            evidence_preference="只基于已读内容，不 extrapolate",
            source=source,
            tags=["论证模式", "知识诚实"],
        )
        stats["argument_patterns"] += 1

    # 检测官职称谓的严谨性
    if any(kw in memory_md for kw in ["府尹", "知府", "府台", "官称", "职位", "不能混用"]):
        pm.add_stance(
            topic="历史细节",
            content="对历史细节（如官职称谓）要求严格，不同职位不能混用，不拿明代小说佐证宋代官称。",
            source=source,
            tags=["创作观", "历史细节"],
        )
        stats["stances"] += 1


def _extract_from_style_profile(pm: CognitiveProfileManager, style: dict, stats: dict):
    """从 style_profile 提取论证偏好和表达习惯。"""
    source = SourceRef(
        type="style_profile",
        title="style_profile.json",
        excerpt=str(style.get("summary", ""))[:200],
        confidence=0.55,
    )

    features = style.get("features", {})
    if not features:
        return

    # 从句式特征提取表达习惯
    sentence_style = features.get("sentence_style", {})
    if isinstance(sentence_style, dict) and sentence_style:
        avg_len = sentence_style.get("avg_length", 0)
        if avg_len and avg_len > 25:
            pm.add_argument_pattern(
                content=f"写作句式偏长（平均句长{avg_len:.0f}字），倾向于用复杂句承载多层信息，表达密度高。",
                pattern_type="句式风格",
                source=source,
                tags=["表达习惯", "句式"],
            )
            stats["argument_patterns"] += 1

    # 从文言密度提取风格偏好
    lexical = features.get("lexical", {})
    if isinstance(lexical, dict) and lexical:
        classical_density = lexical.get("classical_density", 0)
        if classical_density and classical_density > 0.1:
            pm.add_stance(
                topic="语言风格",
                content=f"写作语言有较高的文言密度（{classical_density:.1%}），偏好文雅、有历史感的表达，而非口语化白话。",
                source=source,
                tags=["创作观", "语言风格"],
            )
            stats["stances"] += 1

    # 从风格摘要提取整体风格
    summary = style.get("summary", "")
    if summary:
        pm.add_argument_pattern(
            content=f"整体写作风格：{summary[:150]}",
            pattern_type="整体风格",
            source=source,
            tags=["表达习惯", "整体风格"],
        )
        stats["argument_patterns"] += 1
