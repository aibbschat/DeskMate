"""
认知层（Cognitive Layer）——从"懂我"到"像我"的关键跃迁。

五个维度：
1. stances           立场图谱
2. argument_patterns 论证模式
3. value_ranking     价值观排序
4. thinking_frameworks 思维框架
5. cognitive_gaps    认知盲区/待澄清

架构设计（系统化、可扩展、留好接口）：
- models.py      数据模型（五个维度，每个条目都有来源/置信度/变化历史/状态）
- profile.py     画像管理（加载/保存/增量更新/变化检测/矛盾检测/用户校准）
- extractor.py   认知提取管线（从文本/对话中多层解析认知特征，LLM 驱动）
- cold_start.py  冷启动（从已有 vault/MEMORY/style_profile 提取初始画像）
- diary_extract.py 日记→认知提取（③a 焊点：把 MomentFeed 日记增量提取进画像，按 post id 幂等）
- interview.py   深度访谈引擎（③b 闭环：SocraticInterviewEngine 基于薄弱点生成苏格拉底式问题，回收回答回灌画像消解 gap；start_interview/answer_interview/maybe_trigger_interview 高层驱动）
- interfaces.py  扩展接口（第二期：深度访谈引擎/认知验证引擎/隐性知识提取器/记忆注入器）

MVP 范围：
- 认知画像数据结构 + 增量更新
- 认知提取管线（LLM 驱动，多层解析）
- 冷启动（从已有数据快速生成第一版）
- 后端 API（profile/extract/calibrate/gaps）
- 前端展示（「它眼中的我」第 8 栏目「它理解的我」）
- 与 auto_tasks 集成（读书任务自动触发认知提取）

第二期（接口已留好）：
- 深度访谈引擎（InterviewEngine）
- 认知验证引擎（ValidationEngine，认知 Turing test）
- 隐性知识提取器（TacitKnowledgeExtractor，语感/节奏/叙事直觉）
- 记忆注入器（MemoryInjector，认知画像注入对话上下文）
"""

from .models import (
    CognitiveProfile,
    Stance,
    ArgumentPattern,
    ValueRanking,
    ThinkingFramework,
    CognitiveGap,
    SourceRef,
    HistoryEntry,
)
from .profile import CognitiveProfileManager, get_profile_manager
from .store import CognitiveStore
from .extractor import CognitiveExtractor, get_extractor
from .cold_start import cold_start
from .diary_extract import extract_diary_into_cognitive
from .interview import (
    start_interview,
    answer_interview,
    maybe_trigger_interview,
    get_interview_engine,
)
from .validation_engine import (
    get_validation_engine,
    run_validation,
)
from .interfaces import (
    InterviewEngine,
    ValidationEngine,
    TacitKnowledgeExtractor,
    MemoryInjector,
    register_engine,
    get_engine,
)

__all__ = [
    # 数据模型
    "CognitiveProfile",
    "Stance",
    "ArgumentPattern",
    "ValueRanking",
    "ThinkingFramework",
    "CognitiveGap",
    "SourceRef",
    "HistoryEntry",
    # 画像管理
    "CognitiveProfileManager",
    "get_profile_manager",
    "CognitiveStore",
    # 提取管线
    "CognitiveExtractor",
    "get_extractor",
    # 冷启动
    "cold_start",
    # 日记→认知（③a 焊点）
    "extract_diary_into_cognitive",
    # 深度访谈引擎（③b 闭环）
    "start_interview",
    "answer_interview",
    "maybe_trigger_interview",
    "get_interview_engine",
    # 认知验证引擎（Turing test）
    "get_validation_engine",
    "run_validation",
    # 扩展接口（第二期）
    "InterviewEngine",
    "ValidationEngine",
    "TacitKnowledgeExtractor",
    "MemoryInjector",
    "register_engine",
    "get_engine",
]
