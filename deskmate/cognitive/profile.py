"""
认知画像管理——加载、保存、增量更新、变化检测、历史留痕。

核心设计：
- 增量更新：新提取的特征和已有画像对比，判断是新增/补充/变化/矛盾
- 变化留痕：每次更新都记录历史，支持回溯
- 矛盾检测：新特征和已有特征矛盾时，标记为 questioned，生成 cognitive_gap
- 可扩展：五个维度的更新逻辑统一，新增维度只需加一个方法
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from deskmate.paths import deskmate_db_path, deskmate_legacy_profile_path
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

from .store import CognitiveStore

logger = logging.getLogger(__name__)


def _default_profile_path() -> Path:
    """兼容旧语义：返回 legacy JSON 路径（仅作为首次迁移源）。"""
    return deskmate_legacy_profile_path()


def _default_db_path() -> Path:
    return deskmate_db_path()


class CognitiveProfileManager:
    """认知画像管理器——加载、保存、增量更新（SQLite 持久化，可拓展）。

    profile_path 兼容两种语义：
    - 以 .db 结尾 → 直接作为统一库路径
    - 以 .json 结尾 / 省略 → 当作 legacy 迁移源，库落到默认 deskmate.db
    """

    def __init__(self, profile_path: Optional[str | Path] = None, layer: str = "cognitive"):
        self.layer = layer
        if profile_path and str(profile_path).endswith(".db"):
            db_path = profile_path
            legacy = _default_profile_path()
        else:
            db_path = _default_db_path()
            legacy = profile_path if (profile_path and str(profile_path).endswith(".json")) else _default_profile_path()
        self.db_path = Path(db_path)
        self.store = CognitiveStore(db_path=db_path, legacy_json=legacy)
        # 仅对默认统一库自动迁移 legacy JSON（自定义库如测试库不自动迁）
        if self.db_path.resolve() == _default_db_path().resolve():
            self.store.migrate_from_legacy(legacy)
        self.profile: Optional[CognitiveProfile] = None
        self._load()

    def _load(self):
        self.profile = self.store.load_profile(layer=self.layer)

    def save(self, force: bool = False):
        """持久化画像。变更后顺手失效 will digest 缓存，让下游调制器下次重建。

        force: 透传给 store.persist 的防清空开关。默认 False —— 库里本层有数据
               而要写入空画像时会拒绝（见 store.persist 的护栏说明）。


        接线说明（2026-09-05）：
        - **必须 lazy import** —— `deskmate.will.digest` 反向导入 `cognitive.profile`，
          在 profile.py 顶层 import will 会形成循环依赖。
        - **try/except fail-open** —— will 未安装 / kill switch / 缓存层异常时
          不影响画像保存主流程；意志层只是消费者，挂掉不能让出生产端。
        - **覆盖全部 save 调用点**（diary_extract / emotion_extract / interview /
          validation_engine / expression_ingest / repair_fake_duplicate_gaps 等），
          等同于在唯一保存路径统一插桩，散点接不会漏。
        """
        self.profile.last_updated = _now()
        self.store.persist(self.profile, layer=self.layer, force=force)
        # 挂锚下沉（2026-11-11）：任何写入路径（add_stance / add_value_ranking /
        # 世界观回望 / 日记提取 / 情绪提取 …）最终都会走 save()，故在此统一补锚。
        # 此前挂锚只长在 extractor 的显式调用点上，生产写路径全部绕过 → 锚表恒 0 行。
        self._sync_anchors()
        # 失效 will digest 模块级缓存（30min TTL，画像改了就别再吃旧数据）
        try:
            from deskmate.will.digest import invalidate_digest
            invalidate_digest()
        except Exception:
            # will 未安装 / kill switch / 缓存模块异常 → 主流程不动
            pass

    def _sync_anchors(self) -> None:
        """把本层画像条目的共享锚同步进 ``memory_anchors``（纯增、幂等、fail-open）。

        锚的派生规则复用 ``extractor.extract_anchors``（topic / book / person），
        不另造一套口径。person 名单一次取全、逐条传参，避免每条都扫一遍注册表。
        任何异常只告警，绝不影响画像保存主链路。
        """
        try:
            from .extractor import _active_person_names, extract_anchors

            items = (
                list(self.profile.stances)
                + list(self.profile.argument_patterns)
                + list(self.profile.value_rankings)
                + list(self.profile.thinking_frameworks)
            )
            if not items:
                return
            names = _active_person_names(self.store)
            seen: set[str] = set()
            for it in items:
                iid = getattr(it, "id", "") or ""
                if not iid or iid in seen:
                    continue
                seen.add(iid)
                topic = getattr(it, "topic", "") or getattr(it, "domain", "") or ""
                sources = getattr(it, "sources", None) or []
                src = sources[0] if sources else None
                text = (it.content or "") + ("\n" + src.excerpt if src and src.excerpt else "")
                anchors = extract_anchors(
                    text,
                    (src.title if src else ""),
                    (src.type if src else ""),
                    [{"topic": topic, "domain": topic}],
                    store=self.store,
                    person_names=names,
                )
                if anchors:
                    self.store.upsert_anchors("profile_item", iid, anchors)
        except Exception as e:  # noqa: BLE001 —— fail-open：挂锚绝不阻断保存
            logger.warning("挂锚同步失败（忽略，不影响保存）: %s", e)

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
        contexts: Optional[list[str]] = None,
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
                contexts=contexts or [],
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

        # 前置：文本高度相似（同模板/近重复重复提取）→ 直接补来源，不判矛盾/变化，
        # 否则模板里自带否定词（如「不愿对外暴露心事」）会误生成伪 gap。
        if self._texts_similar(target.content, content):
            target.add_source(source)
            if tags:
                for t in tags:
                    if t not in target.tags:
                        target.tags.append(t)
            target.history.append(HistoryEntry(
                action="updated",
                old_value=target.content,
                new_value=target.content,
                reason="重复提取（高度相似内容），补充来源，置信度提高",
                source=source.title,
            ))
            self.profile.extraction_count += 1
            self.save()
            return target, "updated"

        # 简单矛盾检测：内容是否明显相反（包含"不"/"反对"/"拒绝"等否定词）
        is_contradiction = self._detect_contradiction(target.content, content)

        if is_contradiction:
            # 矛盾分级：先看情境是否相交（容纳矛盾、标注条件）
            if not self._contexts_conflict(target.contexts, contexts or []):
                # 合法并存：不同情境下相反 → 小桔理解你的多面，不置疑、不堆 gap
                if target.contexts:
                    target.notes = (
                        (target.notes + "；" if target.notes else "")
                        + f"适用情境：{', '.join(target.contexts)}"
                    )
                new_stance = Stance(
                    topic=topic,
                    content=content,
                    category=category or "立场",
                    tags=tags or [],
                    contexts=contexts or [],
                    status="active",
                )
                new_stance.add_source(source)
                new_stance.history.append(HistoryEntry(
                    action="created",
                    new_value=content,
                    reason=f"与既有立场情境性并存（适用：{', '.join(contexts or [])}），未置疑",
                    source=source.title,
                ))
                self.profile.stances.append(new_stance)
                self.profile.extraction_count += 1
                self.save()
                return new_stance, "coexist"

            # 真矛盾（同情境下相反）→ 两者都标记 questioned，生成 cognitive_gap
            target.mark_questioned(reason=f"与新提取的立场矛盾：{content[:50]}")
            new_stance = Stance(
                topic=topic,
                content=content,
                category=category or "立场",
                tags=tags or [],
                contexts=contexts or [],
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
                contexts=contexts or [],
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
        contexts: Optional[list[str]] = None,
    ) -> tuple[ArgumentPattern, str]:
        # pattern_type 为两边都空时不能算「同类」：LLM 提取常常不给这个字段，
        # 一旦 "" == "" 成立，任何无类型的模式都会被并进第一条，内容直接丢失。
        # 类型缺失时交给下面的内容前缀比对兜底（内容确实相同才合并）。
        existing = [
            p for p in self.profile.argument_patterns
            if p.status == "active"
            and (
                (p.pattern_type and pattern_type and p.pattern_type == pattern_type)
                or p.content[:20] == content[:20]
            )
        ]

        if not existing:
            pattern = ArgumentPattern(
                content=content,
                pattern_type=pattern_type,
                evidence_preference=evidence_preference,
                tags=tags or [],
                contexts=contexts or [],
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
        if contexts:
            for c in contexts:
                if c not in (target.contexts or []):
                    target.contexts.append(c)
        target.history.append(HistoryEntry(
            # 补 generate_id/content：as-of 回放要靠 new_value 还原「当时的正文」，
            # 早期这里只写 action 不写值 → 回放成空串（2026-09-23 巡检发现）。
            action="updated", old_value=target.content, new_value=target.content,
            reason="补充来源", source=source.title,
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
        contexts: Optional[list[str]] = None,
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
                contexts=contexts or [],
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
                contexts=contexts or [],
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
        if contexts:
            for c in contexts:
                if c not in (target.contexts or []):
                    target.contexts.append(c)
        # 补 new_value：as-of 回放靠它还原当时正文（2026-09-23 巡检发现此前漏写）
        target.history.append(HistoryEntry(
            action="updated", old_value=target.content, new_value=target.content,
            reason="补充来源", source=source.title,
        ))
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
        contexts: Optional[list[str]] = None,
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
                contexts=contexts or [],
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
        if contexts:
            for c in contexts:
                if c not in (target.contexts or []):
                    target.contexts.append(c)
        # 补充步骤
        for s in steps:
            if s not in target.steps:
                target.steps.append(s)
        # 补 new_value：as-of 回放靠它还原当时正文（2026-09-23 巡检发现此前漏写）
        target.history.append(HistoryEntry(
            action="updated", old_value=target.content, new_value=target.content,
            reason="补充来源和步骤", source=source.title,
        ))
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
    def _normalize_text(text: str) -> str:
        """归一化：去空白与标点，只保留语义字符（用于相似度比较）。"""
        import re

        return re.sub(r"[\s\W]+", "", text or "").lower()

    @staticmethod
    def _texts_similar(a: str, b: str, ratio: float = 0.8) -> bool:
        """两段文本是否高度相似（同模板/近重复）。高度相似绝不可能是矛盾。

        解决「日记规则提取用固定模板 → 同模板重复提取被误判为矛盾」的伪 gap 问题：
        模板文本本身含否定词（如「不愿对外暴露心事」），若只按否定词判矛盾，
        同一条模板重复出现就会被误标 questioned + spawn 假 gap。
        """
        if not a or not b:
            return False
        na = CognitiveProfileManager._normalize_text(a)
        nb = CognitiveProfileManager._normalize_text(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        import difflib

        return difflib.SequenceMatcher(None, na, nb).ratio() >= ratio

    @staticmethod
    def _detect_contradiction(old: str, new: str) -> bool:
        """简单矛盾检测：新内容是否包含对旧内容的否定。"""
        negation_words = ["不", "反对", "拒绝", "并非", "不是", "而非", "不同于", "相反"]
        # 前置拦截：新旧文本高度相似（同模板/近重复）→ 绝不判矛盾，
        # 否则「不愿对外暴露心事」这类含否定词的模板文本重复提取会误生成伪 gap
        if CognitiveProfileManager._texts_similar(old, new):
            return False
        # 如果新内容包含否定词，且旧内容是肯定陈述，可能是矛盾
        # 这是简化版，真正的矛盾检测需要 LLM 辅助
        has_negation = any(w in new for w in negation_words)
        # 检查新内容是否直接否定旧内容的核心观点
        old_keywords = set(old[:20].replace("，", " ").replace("。", " ").split())
        new_contains_old_keywords = any(kw in new for kw in old_keywords if len(kw) > 1)
        return has_negation and new_contains_old_keywords

    @staticmethod
    def _contexts_conflict(existing: list[str], new: list[str]) -> bool:
        """两条内容相反时，判断是否「真矛盾」（需置疑）还是「情境性并存」。

        容纳矛盾、标注条件：
        - 任一方 contexts 为空（全局适用）→ 与任何情境相交 → 真矛盾，需置疑；
        - 双方 contexts 都不空且有交集 → 同情境下相反 → 真矛盾，需置疑；
        - 双方 contexts 都不空且无交集 → 不同情境下相反 → 合法并存，不置疑。
        """
        ex = set(existing or [])
        nw = set(new or [])
        if not ex or not nw:
            return True
        return bool(ex & nw)

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

    # ------------------------------------------------------------------
    # 存量清理：合并「同模板重复提取」产生的伪 gap / 重复 questioned 立场
    # ------------------------------------------------------------------
    def repair_fake_duplicate_gaps(self) -> dict:
        """幂等清理历史伪矛盾数据（仅限内容高度相似的重复立场）。

        背景：旧版矛盾检测把「含否定词的模板文本」的重复提取误判为矛盾，
        产生多条同内容 stance + 一个指向它们的僵尸 gap（真实案例：隐私与安全感，
        3 条同款 stance + gap_c56a4090）。

        本方法只合并**同 topic 且内容高度相似**（_texts_similar）的重复立场：
        - 保留其中 active 的一条作为主条目，吸收其余重复条目的来源；
        - 其余重复条目标记 superseded（保留历史，不删除）；
        - 若某 open gap 的 related_items 全部指向已被合并/已 superseded 的重复条目
          且同 topic 下已存在 active 主条目 → 将该 gap 标记 resolved。

        幂等：重复执行不产生新副作用（已合并过则无变化）。
        绝不触碰真实矛盾：内容差异大的 questioned 条目原样保留。
        """
        changes = {"merged_stances": [], "resolved_gaps": [], "note": ""}
        profile = self.profile

        # 按 topic 分组，找「同 topic 且内容高度相似」的 stance 簇
        from collections import defaultdict

        by_topic: dict[str, list] = defaultdict(list)
        for s in profile.stances:
            by_topic[s.topic].append(s)

        for topic, items in by_topic.items():
            if len(items) < 2:
                continue
            # 在簇内做两两相似判断，聚出「同一内容」的子簇
            visited: set[str] = set()
            for base in items:
                if base.id in visited:
                    continue
                cluster = [base]
                for other in items:
                    if other.id == base.id or other.id in visited:
                        continue
                    if self._texts_similar(base.content, other.content):
                        cluster.append(other)
                if len(cluster) < 2:
                    for c in cluster:
                        visited.add(c.id)
                    continue
                # 簇内保留一条 active（无 active 则保留第一条），其余合并
                primary = next((c for c in cluster if c.status == "active"), cluster[0])
                for dup in cluster:
                    if dup.id == primary.id:
                        continue
                    # 已合并过的 superseded 条目跳过（幂等：不重复合并）
                    if dup.status == "superseded":
                        continue
                    if dup.status in ("questioned", "active"):
                        # 合并来源到主条目（若主条目尚未含这些来源）
                        for src in dup.sources:
                            if src not in primary.sources:
                                primary.sources.append(src)
                        # 重算主条目置信度（复刻 add_source 逻辑）
                        if primary.sources:
                            avg_conf = sum(x.confidence for x in primary.sources) / len(primary.sources)
                            primary.confidence = min(0.95, avg_conf + 0.05 * (len(primary.sources) - 1))
                        primary.last_updated = _now()
                        dup.mark_superseded(
                            new_item_id=primary.id,
                            reason=f"清理：与主条目内容高度相似（历史伪矛盾），合并来源",
                        )
                        changes["merged_stances"].append({
                            "topic": topic, "dup_id": dup.id, "primary_id": primary.id,
                            "dup_status": "superseded",
                        })
                    visited.add(dup.id)
                visited.add(primary.id)
                # 主条目恢复 active（若曾因伪矛盾被误标 questioned）
                if primary.status == "questioned" and len(cluster) > 1:
                    primary.status = "active"
                    primary.history.append(HistoryEntry(
                        # 2026-09-23：这里改的是**状态**（questioned→active），不是正文。
                        # 原名 action="restored" 属于内容变更白名单，as-of 回放会把正文
                        # 错当成 "active"（new_value 写的是状态字面量）；改用具名动作，
                        # 让「状态变更」与「内容变更」在语义上彻底分开。
                        action="status_restored",
                        old_value="questioned", new_value="active",
                        reason="清理：内容高度相似的重复立场，非真实矛盾", source="repair",
                    ))
                    primary.last_updated = _now()

        # 消解指向「已无真实矛盾」的伪 gap：
        # 若 gap 的 related 条目同 topic 下已有 active 主条目，且 related 内不存在
        # 「内容不同的互相 questioned」真矛盾对 → 该 gap 是伪矛盾产物，可消解
        for g in profile.cognitive_gaps:
            if g.status != "open" or not g.related_items:
                continue
            related = [s for s in profile.stances if s.id in g.related_items]
            if not related:
                continue
            topic = g.topic.split("：", 1)[-1].strip()
            topic_items = [s for s in profile.stances if s.topic == topic]
            has_active = any(s.status == "active" for s in topic_items)
            # 真矛盾 = related 里存在两条 content 不同的 questioned 条目
            real_conflict = any(
                a.status == "questioned" and b.status == "questioned"
                and not self._texts_similar(a.content, b.content)
                for i, a in enumerate(related) for b in related[i + 1:]
            )
            if has_active and not real_conflict:
                g.status = "resolved"
                g.resolved_at = _now()
                changes["resolved_gaps"].append({
                    "gap_id": g.id, "topic": g.topic, "reason": "清理：related 均为同内容重复立场，非真实矛盾",
                })

        if changes["merged_stances"] or changes["resolved_gaps"]:
            self.save()
        else:
            changes["note"] = "无需清理"
        return changes


# 全局单例（按 layer 隔离：认知层 / 情感层各自一个 manager）
_default_managers: dict[str, CognitiveProfileManager] = {}


def get_profile_manager(
    profile_path: Optional[str | Path] = None,
    layer: str = "cognitive",
) -> CognitiveProfileManager:
    """按 layer 获取画像管理器（全局缓存，同 layer 复用实例）。

    兼容旧调用：get_profile_manager() → 认知层。
    情感层：get_profile_manager(layer='emotional')。
    测试库场景：传入 profile_path 时不缓存（每次新建，避免污染全局）。
    """
    if profile_path is not None:
        return CognitiveProfileManager(profile_path, layer=layer)
    key = layer or "cognitive"
    if key not in _default_managers:
        _default_managers[key] = CognitiveProfileManager(layer=key)
    return _default_managers[key]


def resolve_profile_manager(
    layer: str = "cognitive",
    base_dir: Optional[str | Path] = None,
) -> CognitiveProfileManager:
    """按 base_dir 解析某层画像管理器——**调用时**解析，保证多 workspace 不串库。

    与 ``get_profile_manager`` 的唯一区别：``base_dir`` 非空时按
    ``<base_dir>/deskmate.db`` 打开，且**不进全局缓存**（与
    ``get_profile_manager(profile_path=...)`` 同一契约——自定义库不缓存）。

    为什么需要它（2026-09-22）：``proactivity/tick`` 的访谈分支原先无参调用
    ``get_profile_manager()``，于是**永远落到全局库**——tick 明明持有
    ``self.base_dir``（可能是测试库 / 别的 workspace），访谈却读写真实库。
    多 workspace 场景会互相串库；测试里表现为「时红时绿」。
    """
    if base_dir is None:
        return get_profile_manager(layer=layer)
    db_path = Path(base_dir) / "deskmate.db"
    return get_profile_manager(profile_path=str(db_path), layer=layer)
