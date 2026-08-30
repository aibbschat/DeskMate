"""
认知画像管理——加载、保存、增量更新、变化检测、历史留痕。

核心设计：
- 增量更新：新提取的特征和已有画像对比，判断是新增/补充/变化/矛盾
- 变化留痕：每次更新都记录历史，支持回溯
- 矛盾检测：新特征和已有特征矛盾时，标记为 questioned，生成 cognitive_gap
- 可扩展：五个维度的更新逻辑统一，新增维度只需加一个方法
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .models import (
    CognitiveProfile,
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    CognitiveGap,
    SourceRef,
    HistoryEntry,
    _now,
)


def _default_profile_path() -> Path:
    return Path(os.path.expanduser("~/.deskmate/cognitive_profile.json"))


class CognitiveProfileManager:
    """认知画像管理器——负责加载、保存、增量更新。"""

    def __init__(self, profile_path: Optional[str | Path] = None):
        self.profile_path = Path(profile_path) if profile_path else _default_profile_path()
        self.profile: Optional[CognitiveProfile] = None
        self._load()

    def _load(self):
        if self.profile_path.exists():
            try:
                self.profile = CognitiveProfile.from_json(
                    self.profile_path.read_text(encoding="utf-8")
                )
            except Exception:
                self.profile = CognitiveProfile()
        else:
            self.profile = CognitiveProfile()

    def save(self):
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        self.profile.last_updated = _now()
        self.profile_path.write_text(self.profile.to_json(), encoding="utf-8")

    def get(self) -> CognitiveProfile:
        return self.profile

    # ------------------------------------------------------------------
    # 增量更新：立场
    # ------------------------------------------------------------------
    def add_stance(
        self,
        topic: str,
        content: str,
        source: SourceRef,
        tags: Optional[list[str]] = None,
        category: str = "",
    ) -> tuple[Stance, str]:
        """
        增量添加一个立场。自动判断：
        - 新立场 → 添加
        - 已有立场的补充 → 更新来源和置信度
        - 立场变化 → 旧立场标记 superseded，添加新立场，保留历史
        - 立场矛盾 → 两者都标记 questioned，生成 cognitive_gap

        返回：(立场对象, 操作类型: created/updated/superseded/contradiction)
        """
        # 查找相似的已有立场（同 topic，active 状态）
        existing = [
            s for s in self.profile.stances
            if s.topic == topic and s.status == "active"
        ]

        if not existing:
            # 新立场
            stance = Stance(
                topic=topic,
                content=content,
                category=category or "立场",
                tags=tags or [],
            )
            stance.add_source(source)
            stance.history.append(HistoryEntry(
                action="created",
                new_value=content,
                reason="新立场提取",
                source=source.title,
            ))
            self.profile.stances.append(stance)
            self.profile.extraction_count += 1
            self.save()
            return stance, "created"

        # 有已有立场，判断关系
        target = existing[0]  # 取第一个（同 topic 应该只有一个 active）

        # 简单矛盾检测：内容是否明显相反（包含"不"/"反对"/"拒绝"等否定词）
        is_contradiction = self._detect_contradiction(target.content, content)

        if is_contradiction:
            # 矛盾：两者都标记 questioned，生成 cognitive_gap
            target.mark_questioned(reason=f"与新提取的立场矛盾：{content[:50]}")
            new_stance = Stance(
                topic=topic,
                content=content,
                category=category or "立场",
                tags=tags or [],
                status="questioned",
            )
            new_stance.add_source(source)
            new_stance.history.append(HistoryEntry(
                action="created",
                new_value=content,
                reason="矛盾立场提取，待访谈澄清",
                source=source.title,
            ))
            self.profile.stances.append(new_stance)

            # 生成认知盲区（待访谈澄清）
            gap = CognitiveGap(
                topic=f"立场矛盾：{topic}",
                reason=f"已有立场「{target.content[:30]}...」与新立场「{content[:30]}...」矛盾，需访谈澄清是思想变化还是情境差异",
                priority=0.8,
                related_items=[target.id, new_stance.id],
            )
            self.profile.cognitive_gaps.append(gap)

            self.profile.extraction_count += 1
            self.save()
            return new_stance, "contradiction"

        # 判断是否是立场变化（内容差异大但不矛盾）
        is_significant_change = self._detect_significant_change(target.content, content)

        if is_significant_change:
            # 立场变化：旧立场标记 superseded，添加新立场
            target.mark_superseded(
                new_item_id="pending",
                reason=f"立场变化，新立场：{content[:50]}",
            )
            new_stance = Stance(
                topic=topic,
                content=content,
                category=category or "立场",
                tags=tags or [],
            )
            new_stance.add_source(source)
            new_stance.history.append(HistoryEntry(
                action="created",
                new_value=content,
                reason=f"立场变化，取代旧立场 {target.id}",
                source=source.title,
            ))
            target.history[-1].source = f"superseded_by:{new_stance.id}"
            self.profile.stances.append(new_stance)
            self.profile.extraction_count += 1
            self.save()
            return new_stance, "superseded"

        # 补充：更新来源和置信度
        target.add_source(source)
        if tags:
            for t in tags:
                if t not in target.tags:
                    target.tags.append(t)
        target.history.append(HistoryEntry(
            action="updated",
            old_value=target.content,
            new_value=target.content,
            reason="补充来源，置信度提高",
            source=source.title,
        ))
        self.profile.extraction_count += 1
        self.save()
        return target, "updated"

    # ------------------------------------------------------------------
    # 增量更新：论证模式（简化版，逻辑同立场）
    # ------------------------------------------------------------------
    def add_argument_pattern(
        self,
        content: str,
        source: SourceRef,
        pattern_type: str = "",
        evidence_preference: str = "",
        tags: Optional[list[str]] = None,
    ) -> tuple[ArgumentPattern, str]:
        existing = [
            p for p in self.profile.argument_patterns
            if p.status == "active" and (p.pattern_type == pattern_type or p.content[:20] == content[:20])
        ]

        if not existing:
            pattern = ArgumentPattern(
                content=content,
                pattern_type=pattern_type,
                evidence_preference=evidence_preference,
                tags=tags or [],
            )
            pattern.add_source(source)
            pattern.history.append(HistoryEntry(
                action="created", new_value=content, reason="新论证模式提取", source=source.title,
            ))
            self.profile.argument_patterns.append(pattern)
            self.profile.extraction_count += 1
            self.save()
            return pattern, "created"

        target = existing[0]
        target.add_source(source)
        if pattern_type and not target.pattern_type:
            target.pattern_type = pattern_type
        if evidence_preference and not target.evidence_preference:
            target.evidence_preference = evidence_preference
        target.history.append(HistoryEntry(
            action="updated", reason="补充来源", source=source.title,
        ))
        self.profile.extraction_count += 1
        self.save()
        return target, "updated"

    # ------------------------------------------------------------------
    # 增量更新：价值观排序
    # ------------------------------------------------------------------
    def add_value_ranking(
        self,
        domain: str,
        ranking: list[str],
        content: str,
        source: SourceRef,
        tradeoff_notes: str = "",
        tags: Optional[list[str]] = None,
    ) -> tuple[ValueRanking, str]:
        existing = [
            v for v in self.profile.value_rankings
            if v.status == "active" and v.domain == domain
        ]

        if not existing:
            vr = ValueRanking(
                domain=domain,
                ranking=ranking,
                content=content,
                tradeoff_notes=tradeoff_notes,
                tags=tags or [],
            )
            vr.add_source(source)
            vr.history.append(HistoryEntry(
                action="created", new_value=str(ranking), reason="新价值观提取", source=source.title,
            ))
            self.profile.value_rankings.append(vr)
            self.profile.extraction_count += 1
            self.save()
            return vr, "created"

        target = existing[0]
        # 排序变化检测
        if ranking != target.ranking:
            target.mark_superseded(
                new_item_id="pending",
                reason=f"价值观排序变化：{target.ranking} → {ranking}",
            )
            new_vr = ValueRanking(
                domain=domain,
                ranking=ranking,
                content=content,
                tradeoff_notes=tradeoff_notes,
                tags=tags or [],
            )
            new_vr.add_source(source)
            new_vr.history.append(HistoryEntry(
                action="created", new_value=str(ranking),
                reason=f"价值观变化，取代 {target.id}", source=source.title,
            ))
            target.history[-1].source = f"superseded_by:{new_vr.id}"
            self.profile.value_rankings.append(new_vr)
            self.profile.extraction_count += 1
            self.save()
            return new_vr, "superseded"

        target.add_source(source)
        target.history.append(HistoryEntry(action="updated", reason="补充来源", source=source.title))
        self.profile.extraction_count += 1
        self.save()
        return target, "updated"

    # ------------------------------------------------------------------
    # 增量更新：思维框架
    # ------------------------------------------------------------------
    def add_thinking_framework(
        self,
        domain: str,
        steps: list[str],
        content: str,
        source: SourceRef,
        tags: Optional[list[str]] = None,
    ) -> tuple[ThinkingFramework, str]:
        existing = [
            f for f in self.profile.thinking_frameworks
            if f.status == "active" and f.domain == domain
        ]

        if not existing:
            tf = ThinkingFramework(
                domain=domain,
                steps=steps,
                content=content,
                tags=tags or [],
            )
            tf.add_source(source)
            tf.history.append(HistoryEntry(
                action="created", new_value=str(steps), reason="新思维框架提取", source=source.title,
            ))
            self.profile.thinking_frameworks.append(tf)
            self.profile.extraction_count += 1
            self.save()
            return tf, "created"

        target = existing[0]
        target.add_source(source)
        # 补充步骤
        for s in steps:
            if s not in target.steps:
                target.steps.append(s)
        target.history.append(HistoryEntry(action="updated", reason="补充来源和步骤", source=source.title))
        self.profile.extraction_count += 1
        self.save()
        return target, "updated"

    # ------------------------------------------------------------------
    # 用户校准
    # ------------------------------------------------------------------
    def calibrate(self, item_id: str, corrected_content: str, feedback: str = ""):
        """
        用户校准——纠正认知画像中的错误。

        这是最有价值的认知数据，因为它是用户直接表达的，置信度最高。
        """
        # 在五个维度中查找
        all_items = (
            self.profile.stances
            + self.profile.argument_patterns
            + self.profile.value_rankings
            + self.profile.thinking_frameworks
        )
        target = next((item for item in all_items if item.id == item_id), None)
        if target is None:
            raise ValueError(f"Item not found: {item_id}")

        old_content = target.content
        target.content = corrected_content
        target.confidence = min(0.95, target.confidence + 0.2)  # 用户校准提高置信度
        target.status = "active"
        target.last_updated = _now()
        target.history.append(HistoryEntry(
            action="calibrated",
            old_value=old_content,
            new_value=corrected_content,
            reason=feedback or "用户校准",
        ))
        self.profile.calibration_count += 1

        # 如果相关的 cognitive_gap 是因为这个条目，标记为 resolved
        for gap in self.profile.cognitive_gaps:
            if item_id in gap.related_items and gap.status == "open":
                gap.status = "resolved"

        self.save()

    def add_gap(self, topic: str, reason: str, priority: float = 0.5, related_items: Optional[list[str]] = None):
        """手动添加一个认知盲区（供第二期访谈引擎使用）。"""
        gap = CognitiveGap(
            topic=topic,
            reason=reason,
            priority=priority,
            related_items=related_items or [],
        )
        self.profile.cognitive_gaps.append(gap)
        self.save()
        return gap

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_contradiction(old: str, new: str) -> bool:
        """简单矛盾检测：新内容是否包含对旧内容的否定。"""
        negation_words = ["不", "反对", "拒绝", "并非", "不是", "而非", "不同于", "相反"]
        # 如果新内容包含否定词，且旧内容是肯定陈述，可能是矛盾
        # 这是简化版，真正的矛盾检测需要 LLM 辅助
        has_negation = any(w in new for w in negation_words)
        # 检查新内容是否直接否定旧内容的核心观点
        old_keywords = set(old[:20].replace("，", " ").replace("。", " ").split())
        new_contains_old_keywords = any(kw in new for kw in old_keywords if len(kw) > 1)
        return has_negation and new_contains_old_keywords

    @staticmethod
    def _detect_significant_change(old: str, new: str) -> bool:
        """检测是否是显著变化（内容差异大但不矛盾）。"""
        # 简单的相似度检测：共同字符比例
        old_set = set(old)
        new_set = set(new)
        if not old_set or not new_set:
            return False
        overlap = len(old_set & new_set)
        union = len(old_set | new_set)
        similarity = overlap / union if union > 0 else 0
        return similarity < 0.5  # 相似度低于 50% 视为显著变化


# 全局单例
_default_manager: Optional[CognitiveProfileManager] = None


def get_profile_manager(profile_path: Optional[str | Path] = None) -> CognitiveProfileManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = CognitiveProfileManager(profile_path)
    return _default_manager
