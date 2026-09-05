"""
认知层扩展接口——各引擎的抽象基类与契约。

已实现（接入生产）：
- 深度访谈引擎（``InterviewEngine`` → ``interview.SocraticInterviewEngine``，③b 闭环）
- 认知验证引擎（``ValidationEngine`` → ``validation_engine.TuringScenarioEngine``，认知 Turing test）
- 记忆注入器（``MemoryInjector`` → ``memory_injector.CognitiveMemoryInjector``）

这些基类定义了实现类必须遵守的契约，确保同一职责的不同实现可互换。
具体实现的获取走各自模块的工厂函数（如 ``get_interview_engine()``、
``get_validation_engine()``），不经过本模块。
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


# 注（2026-09-05 清理）：原 ``_REGISTRY`` / ``register_engine()`` / ``get_engine()``
# 注册表三件套已删除——它是「只写不读」的空转机制：interview.py 与
# memory_injector.py 各注册一次，全仓却无任何 ``get_engine()`` 取值方，
# 实际调用都直接走 ``SocraticInterviewEngine()`` / ``get_validation_engine()``
# 等工厂函数。留着只会让人误以为存在可插拔引擎。需要时再按真实使用重建。
