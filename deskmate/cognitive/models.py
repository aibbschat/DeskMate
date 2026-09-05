"""
认知画像数据模型——系统化、可扩展、留好接口。

五个维度：
1. stances           立场图谱（你对各类问题的明确立场）
2. argument_patterns 论证模式（你习惯怎么分析、怎么说服）
3. value_ranking     价值观排序（冲突时你保什么、舍什么）
4. thinking_frameworks 思维框架（你分析问题的习惯性路径）
5. cognitive_gaps    认知盲区/待澄清（你还没明确表达过的领域）

每个维度的条目都有：唯一ID、内容、来源、置信度、变化历史、状态、标签。
扩展接口：第二期的深度访谈/认知验证/隐性知识提取都基于这些模型。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


def _now() -> str:
    return datetime.now().isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class SourceRef:
    """来源引用——每条认知特征都可追溯到具体来源。"""
    type: str                        # book / conversation / vault / memory / interview / calibration
    title: str                       # 来源标题（书名/对话日期/vault条目名）
    excerpt: str = ""                # 来源中的关键片段（支持溯源）
    date: str = ""                   # 来源日期
    confidence: float = 0.5          # 该来源对这条认知的支持强度 0-1


@dataclass
class HistoryEntry:
    """变化历史——每次更新都留痕，支持回溯。"""
    action: str                      # created / updated / superseded / questioned / calibrated
    timestamp: str = field(default_factory=_now)
    old_value: str = ""
    new_value: str = ""
    reason: str = ""                 # 变化原因（新来源/用户校准/矛盾检测）
    source: str = ""                 # 触发变化的来源


@dataclass
class CognitiveItem:
    """认知条目的基类——五个维度的条目都继承这个结构。"""
    id: str = ""
    content: str = ""                # 条目内容（立场/模式/价值观/框架的具体描述）
    category: str = ""               # 分类（历史观/创作观/人物观/...）
    sources: list[SourceRef] = field(default_factory=list)
    confidence: float = 0.5          # 置信度 0-1，多来源支持会提高
    tags: list[str] = field(default_factory=list)
    status: str = "active"           # active / superseded / questioned / merged
    first_extracted: str = field(default_factory=_now)
    last_updated: str = field(default_factory=_now)
    history: list[HistoryEntry] = field(default_factory=list)
    notes: str = ""                  # 备注（如"待访谈澄清"）

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CognitiveItem":
        sources = [SourceRef(**s) for s in d.pop("sources", [])]
        history = [HistoryEntry(**h) for h in d.pop("history", [])]
        return cls(sources=sources, history=history, **d)

    def add_source(self, source: SourceRef):
        """添加来源，自动提高置信度。"""
        self.sources.append(source)
        # 多来源支持提高置信度（上限0.95）
        avg_conf = sum(s.confidence for s in self.sources) / len(self.sources)
        self.confidence = min(0.95, avg_conf + 0.05 * (len(self.sources) - 1))
        self.last_updated = _now()

    def mark_questioned(self, reason: str = ""):
        """标记为存疑（检测到矛盾时），待访谈澄清。"""
        old_status = self.status  # 必须先存旧值，否则历史会记成 questioned→questioned
        self.status = "questioned"
        self.history.append(HistoryEntry(
            action="questioned",
            old_value=old_status,
            new_value="questioned",
            reason=reason,
        ))
        self.last_updated = _now()

    def mark_superseded(self, new_item_id: str, reason: str = ""):
        """标记为被取代（立场变化时，旧条目保留历史）。"""
        old_status = self.status  # 不能写死 "active"：被取代的也可能是存疑条目
        self.status = "superseded"
        self.history.append(HistoryEntry(
            action="superseded",
            old_value=old_status,
            new_value="superseded",
            reason=reason,
            source=f"superseded_by:{new_item_id}",
        ))
        self.last_updated = _now()


@dataclass
class Stance(CognitiveItem):
    """立场——你对某类问题的明确观点。"""
    topic: str = ""                  # 议题（历史真实性/创作主题/人物塑造/...）

    def __post_init__(self):
        if not self.id:
            self.id = _new_id("stance")
        if not self.category:
            self.category = "立场"


@dataclass
class ArgumentPattern(CognitiveItem):
    """论证模式——你习惯怎么分析问题、怎么说服。"""
    pattern_type: str = ""           # 论证方式类型（历史类比/制度推演/反证/归纳/...）
    evidence_preference: str = ""    # 证据偏好（正史>笔记>演义，拒绝自媒体单源...）

    def __post_init__(self):
        if not self.id:
            self.id = _new_id("arg")
        if not self.category:
            self.category = "论证模式"


@dataclass
class ValueRanking(CognitiveItem):
    """价值观排序——冲突情境下你的优先级。"""
    domain: str = ""                 # 领域（创作/历史/技术/生活...）
    ranking: list[str] = field(default_factory=list)  # 优先级排序（["真实","好看","创新"]）
    tradeoff_notes: str = ""         # 权衡说明（什么情况下可以妥协、什么不行）

    def __post_init__(self):
        if not self.id:
            self.id = _new_id("value")
        if not self.category:
            self.category = "价值观"


@dataclass
class ThinkingFramework(CognitiveItem):
    """思维框架——你分析问题的习惯性路径。"""
    domain: str = ""                 # 适用领域（历史问题/创作问题/技术问题...）
    steps: list[str] = field(default_factory=list)  # 分析步骤（["制度背景","权力结构","人物选择","后果"]）

    def __post_init__(self):
        if not self.id:
            self.id = _new_id("think")
        if not self.category:
            self.category = "思维框架"


@dataclass
class CognitiveGap:
    """认知盲区/待澄清——你还没明确表达过的领域。
    这是第二期深度访谈引擎的输入：基于盲区生成访谈问题。"""
    id: str = field(default_factory=lambda: _new_id("gap"))
    topic: str = ""                  # 待澄清的议题
    reason: str = ""                 # 为什么是盲区（从未提及/表达矛盾/数据不足）
    priority: float = 0.5            # 澄清优先级 0-1（影响访谈排序）
    related_items: list[str] = field(default_factory=list)  # 相关的已有认知条目ID
    created_at: str = field(default_factory=_now)
    status: str = "open"             # open / in_interview / resolved / dismissed
    resolved_at: str = ""            # 澄清完成时间（status 转为 resolved 时写入）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CognitiveProfile:
    """认知画像——五个维度的聚合。

    这是认知层的核心数据结构，持久化到 ~/.deskmate/cognitive_profile.json。
    增量更新、变化留痕、可扩展。
    """
    version: str = "1.0"
    created_at: str = field(default_factory=_now)
    last_updated: str = field(default_factory=_now)

    # 五个维度
    stances: list[Stance] = field(default_factory=list)
    argument_patterns: list[ArgumentPattern] = field(default_factory=list)
    value_rankings: list[ValueRanking] = field(default_factory=list)
    thinking_frameworks: list[ThinkingFramework] = field(default_factory=list)
    cognitive_gaps: list[CognitiveGap] = field(default_factory=list)

    # 元数据
    extraction_count: int = 0        # 累计提取次数
    calibration_count: int = 0       # 累计用户校准次数
    summary: str = ""                 # 一句话总结（自动生成）

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "CognitiveProfile":
        stances = [Stance.from_dict(s) for s in d.pop("stances", [])]
        args = [ArgumentPattern.from_dict(s) for s in d.pop("argument_patterns", [])]
        vals = [ValueRanking.from_dict(s) for s in d.pop("value_rankings", [])]
        thinks = [ThinkingFramework.from_dict(s) for s in d.pop("thinking_frameworks", [])]
        gaps = [CognitiveGap(**g) for g in d.pop("cognitive_gaps", [])]
        return cls(
            stances=stances,
            argument_patterns=args,
            value_rankings=vals,
            thinking_frameworks=thinks,
            cognitive_gaps=gaps,
            **d,
        )

    @classmethod
    def from_json(cls, s: str) -> "CognitiveProfile":
        return cls.from_dict(json.loads(s))

    def active_stances(self) -> list[Stance]:
        return [s for s in self.stances if s.status == "active"]

    def active_patterns(self) -> list[ArgumentPattern]:
        return [p for p in self.argument_patterns if p.status == "active"]

    def active_values(self) -> list[ValueRanking]:
        return [v for v in self.value_rankings if v.status == "active"]

    def active_frameworks(self) -> list[ThinkingFramework]:
        return [f for f in self.thinking_frameworks if f.status == "active"]

    def open_gaps(self) -> list[CognitiveGap]:
        return [g for g in self.cognitive_gaps if g.status == "open"]

    def stats(self) -> dict:
        """统计摘要——用于前端展示。"""
        return {
            "total_stances": len(self.stances),
            "active_stances": len(self.active_stances()),
            "total_patterns": len(self.argument_patterns),
            "active_patterns": len(self.active_patterns()),
            "total_values": len(self.value_rankings),
            "active_values": len(self.active_values()),
            "total_frameworks": len(self.thinking_frameworks),
            "active_frameworks": len(self.active_frameworks()),
            "open_gaps": len(self.open_gaps()),
            "extraction_count": self.extraction_count,
            "calibration_count": self.calibration_count,
            "last_updated": self.last_updated,
            "summary": self.summary,
        }
