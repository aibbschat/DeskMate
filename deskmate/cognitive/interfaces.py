"""
认知层扩展接口——第二期功能的抽象基类，留好接入点。

当前 MVP 只实现：认知画像管理 + 认知提取管线 + 冷启动。
第二期将实现：深度访谈引擎、认知验证引擎、隐性知识提取器。

这些接口定义了第二期功能必须实现的契约，确保架构系统化、可扩展。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import CognitiveProfile, CognitiveGap


class InterviewEngine(ABC):
    """
    深度访谈引擎（第二期）。

    基于认知画像的薄弱点（cognitive_gaps + questioned 状态的条目），
    生成有结构的深度访谈问题，通过苏格拉底式追问挖掘用户的深层认知。

    接入方式：挂在 auto_tasks 框架上，作为一个新的自动任务类型
    （task_type="cognitive_interview"），定期发起访谈。
    """

    @abstractmethod
    def generate_questions(self, profile: CognitiveProfile, limit: int = 3) -> list[dict]:
        """
        基于认知画像生成访谈问题。

        优先级：
        1. status=questioned 的条目（检测到矛盾，需要澄清）
        2. priority 高的 cognitive_gaps（重要但未澄清的领域）
        3. confidence 低的 active 条目（需要更多证据）

        返回：[{"id", "topic", "question", "context", "related_item_id", "priority"}]
        """
        ...

    @abstractmethod
    def process_response(
        self, profile: CognitiveProfile, question_id: str, response: str
    ) -> CognitiveProfile:
        """
        处理用户的访谈回复，提取认知特征并更新画像。

        访谈回复是最有价值的认知数据，因为它是用户针对特定问题的直接表达，
        比从作品中推断的置信度更高。
        """
        ...

    @abstractmethod
    def should_interview_now(self, profile: CognitiveProfile) -> bool:
        """判断现在是否应该发起访谈（基于盲区数量、距上次访谈时间等）。"""
        ...


class ValidationEngine(ABC):
    """
    认知验证引擎（第二期）——"认知 Turing test"。

    生成用户没遇到过的具体情境，让 AI 以用户身份回答，
    然后和用户的实际回答对比，评估"像我"的程度。

    验证结果反过来更新认知画像：不像的地方就是认知薄弱点。
    """

    @abstractmethod
    def generate_scenario(self, profile: CognitiveProfile, domain: Optional[str] = None) -> dict:
        """
        生成一个验证场景。

        场景应该是：
        - 用户没遇到过的（不能是作品里写过的）
        - 涉及用户的核心认知维度（立场/价值观/思维框架）
        - 有明确的决策或表达要求

        返回：{"id", "domain", "scenario", "question", "related_items": [...]}
        """
        ...

    @abstractmethod
    def evaluate_similarity(
        self, profile: CognitiveProfile, scenario_id: str, ai_response: str, user_response: str
    ) -> dict:
        """
        评估 AI 回答和用户回答的相似度。

        评估维度：
        - 立场一致性（核心观点是否一致）
        - 论证模式相似度（分析路径是否相似）
        - 价值观排序一致性（优先级是否一致）
        - 表达习惯相似度（用词/语气是否相似）

        返回：{"overall_score", "dimension_scores": {...}, "divergence_points": [...], "feedback": "..."}
        """
        ...

    @abstractmethod
    def apply_feedback(self, profile: CognitiveProfile, evaluation: dict) -> CognitiveProfile:
        """
        将验证反馈应用到认知画像。

        - 不像的维度 → 降低相关条目的置信度
        - 分歧点 → 生成新的 cognitive_gap（待访谈澄清）
        - 像的维度 → 提高相关条目的置信度
        """
        ...


class TacitKnowledgeExtractor(ABC):
    """
    隐性知识提取器（第二期）。

    认知画像提取的是显性知识（立场/论证/价值观——可以用语言表达的）。
    但用户作为写了二十本书的作者，还有很多隐性知识：
    - 语感（什么句子读起来顺、什么不顺）
    - 叙事直觉（什么时候该转折、什么时候该留白）
    - 节奏把控（情节的快慢、张弛）
    - 人物塑造的微妙手法

    这些东西在文字里，但很难用结构化的方式提取。
    隐性知识提取器是另一条技术路径，和认知提取并行。
    """

    @abstractmethod
    def extract_tacit(self, text: str, metadata: Optional[dict] = None) -> list[dict]:
        """
        从文本中提取隐性知识特征。

        返回：[{"type", "description", "evidence", "confidence", "tags"}]
        type 可以是：linguistic_feel / narrative_intuition / pacing / character_subtlety / ...
        """
        ...

    @abstractmethod
    def merge_tacit(self, profile: CognitiveProfile, tacit_features: list[dict]) -> CognitiveProfile:
        """
        将隐性知识特征合并到认知画像。

        隐性知识可能需要单独的存储维度（tacit_knowledge 字段），
        或者作为现有维度的补充（如论证模式的"语感偏好"子字段）。
        """
        ...


class MemoryInjector(ABC):
    """
    认知画像注入器（第二期）。

    当前 MVP 阶段，认知画像只做展示和后台积累，不注入对话上下文。
    第二期实现注入器，在合适的时机将认知画像注入 LLM 上下文，
    让 AI 在对话中真正"像你一样思考"。

    注入策略：
    - 不是全量注入（太占 token），而是基于当前话题检索相关认知条目
    - 高置信度的条目优先注入
    - 注入时标注来源（"基于你对 X 问题的立场..."），让 AI 知道这是用户的认知，不是通用知识
    """

    @abstractmethod
    def select_for_context(self, profile: CognitiveProfile, context: str, limit: int = 5) -> list[dict]:
        """
        基于当前对话上下文，选择相关的认知条目注入。

        选择策略：语义相关性 × 置信度 × 时效性。
        """
        ...

    @abstractmethod
    def format_for_injection(self, items: list[dict]) -> str:
        """
        将认知条目格式化为可注入 LLM 上下文的文本。

        格式应该清晰标注"这是用户的认知"，避免 AI 混淆为通用知识。
        """
        ...


# 注册表——第二期实现后注册到这里，MVP 阶段为空
_REGISTRY: dict[str, type] = {
    "interview_engine": None,
    "validation_engine": None,
    "tacit_extractor": None,
    "memory_injector": None,
}


def register_engine(name: str, cls: type):
    """注册一个引擎实现（第二期用）。"""
    if name not in _REGISTRY:
        raise ValueError(f"Unknown engine name: {name}. Available: {list(_REGISTRY.keys())}")
    _REGISTRY[name] = cls


def get_engine(name: str):
    """获取一个已注册的引擎实现。未注册返回 None。"""
    return _REGISTRY.get(name)
