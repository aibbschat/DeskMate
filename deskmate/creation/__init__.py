"""创作流水线（线路 B · 原生 teams 用法）。

把「编剧闷头干、没人审」改造成「主编组队派活 → 写 → 审核 → 打回 → 定稿」的
带验收门生产流水线。详见 docs/teams-integration-design.md §4。
"""
from .pipeline import CreativePipeline
from .guard import PipelineGuard, PipelineGuardError
from .personas_loader import (
    LEAD_ID,
    OUTLINE_WORKER,
    SCREENPLAY_WORKER,
    REVIEW_WORKER,
    build_team_workers,
    validate_creative_personas,
)
from .executor import CreativeExecutor, StubExecutor, LLMExecutor, ReviewResult
from .final_review import (
    FinalReviewProposal,
    build_final_review_proposal,
    run_final_review,
    apply_final_review,
    parse_verdict,
    recommend_experts,
)

__all__ = [
    "CreativePipeline",
    "PipelineGuard",
    "PipelineGuardError",
    "LEAD_ID",
    "OUTLINE_WORKER",
    "SCREENPLAY_WORKER",
    "REVIEW_WORKER",
    "build_team_workers",
    "validate_creative_personas",
    "CreativeExecutor",
    "StubExecutor",
    "LLMExecutor",
    "ReviewResult",
    "FinalReviewProposal",
    "build_final_review_proposal",
    "run_final_review",
    "apply_final_review",
    "parse_verdict",
    "recommend_experts",
]
