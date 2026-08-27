"""圆桌终审桥接（线路 B → 线路 A 的桥）。

混合式 + 知情批准（见 docs/teams-integration-design.md §5.2）：
- build_final_review_proposal：主编(lead) 自主判断时机 + 预选推荐专家，
  但只产出「终审提案」对象（含四要素），**绝不**拉圆桌烧 token。
- run_final_review：用户知情批准后，才实例化圆桌（TeamsBridge + Roundtable），
  基于送审片段 / 前序意见研讨，返回《综合研判报告》synthesis + verdict。
- apply_final_review：主编以 lead 把 verdict 回灌创作项
  （approve=done / 打回=review→in_progress）。

设计铁律：提案必须先把四要素（送审片段 / 推荐专家 / 验收标准 / 前序意见）呈现给用户，
用户看清才批准——否则「决定权」是虚的。本模块后端只生成提案 + 执行批准后的研讨 +
回灌；前端（TeamRequestCard 渲染四要素 + 专家勾选）留阶段 4。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from deskmate.teams_client import TeamsClient
from deskmate.roundtable.teams_bridge import REVIEW_DIMENSIONS, RoundtableTeamsBridge
from deskmate.roundtable.coordinator import Roundtable
from deskmate.roundtable.personas import get_persona, load_personas

logger = logging.getLogger(__name__)


@dataclass
class FinalReviewProposal:
    """终审提案（知情批准的四要素载体）。纯数据，生成时不拉圆桌。"""

    item_kind: str  # "outline" | "screenplay"
    space: str
    item_id: int  # 创作产出项（大纲v1 / 全剧本）
    review_item_id: int  # 单点审核项
    topic: str  # 圆桌议题标题
    manuscript_text: str  # 送审片段（四要素①）
    criteria: str  # 验收标准（四要素③）
    prior_review: str  # 前序单点审核意见（四要素④）
    all_experts: list[dict] = field(default_factory=list)  # 7 专家 [{id,name,role}]
    recommended: list[str] = field(default_factory=list)  # 推荐子集 id（四要素②）


def recommend_experts(genre_hint: Optional[str], criteria: str = "") -> list[str]:
    """基于稿子主题预选圆桌专家子集（主编判断时机时调用）。

    默认覆盖 4 个审稿维度（史实/文笔/结构/市场）；genre_hint 命中时加权追加
    adaptation（改编适配）/ genre（品类受众）/ historian（史实，避免漏）。
    """
    base = [owner for _title, owner, _crit in REVIEW_DIMENSIONS]
    if not genre_hint:
        return base
    rec = list(base)
    h = genre_hint
    if any(k in h for k in ("改编", "影视", "剧本", "编剧")) and "adaptation" not in rec:
        rec.append("adaptation")
    if any(k in h for k in ("品类", "受众", "市场", "读者")) and "genre" not in rec:
        rec.append("genre")
    if any(k in h for k in ("史实", "历史", "古装")) and "historian" not in rec:
        rec.append("historian")
    return rec


def build_final_review_proposal(
    pipeline, item_kind: str, *, genre_hint: Optional[str] = None,
) -> FinalReviewProposal:
    """主编在 review 阶段生成「终审提案」（纯数据，不拉圆桌）。"""
    if item_kind == "outline":
        item_id = pipeline.ids["outline_item"]
        review_item_id = pipeline.ids["outline_review_item"]
    elif item_kind == "screenplay":
        item_id = pipeline.ids["screen_item"]
        review_item_id = pipeline.ids["screen_review_item"]
    else:
        raise ValueError(f"未知 item_kind: {item_kind}")
    manuscript_text = pipeline._drafts.get(item_id, "")
    criteria = pipeline._criteria(review_item_id)
    prior = pipeline._last_review_result(review_item_id).opinion
    all_experts = [{"id": p.id, "name": p.name, "role": p.role} for p in load_personas()]
    recommended = recommend_experts(genre_hint, criteria)
    label = "剧本" if item_kind == "screenplay" else "大纲"
    topic = f"《{pipeline.concept[:14]}…》{label}终审"
    return FinalReviewProposal(
        item_kind=item_kind, space=pipeline.space, item_id=item_id,
        review_item_id=review_item_id, topic=topic, manuscript_text=manuscript_text,
        criteria=criteria, prior_review=prior, all_experts=all_experts,
        recommended=recommended,
    )


def parse_verdict(synthesis: str) -> bool:
    """从《综合研判报告》解析 verdict：True=通过定稿，False=打回修改。"""
    s = synthesis or ""
    reject_kw = ("打回", "建议修改", "未达定稿", "不予定稿", "不合格", "需返工", "退回")
    approve_kw = ("通过终审", "建议定稿", "可定稿", "准予定稿", "同意定稿")
    if any(k in s for k in reject_kw):
        return False
    if any(k in s for k in approve_kw):
        return True
    # 默认倾向定稿，除非报告明确打回（避免误杀）
    return True


def run_final_review(
    proposal: FinalReviewProposal,
    selected_experts: list[str],
    *,
    base_dir: Optional[str] = None,
    verdict_override: Optional[bool] = None,
) -> tuple[str, bool]:
    """用户知情批准后执行圆桌终审，返回 (synthesis, verdict)。

    selected_experts：用户批准后的专家 id 列表（可在 recommended 基础上增减）。
    verdict_override：仅验证用，强制 verdict；生产路径靠 parse_verdict。
    """
    client = TeamsClient(base_dir=base_dir)
    team_id = f"final-review:{proposal.space}:{proposal.item_id}"
    bridge = RoundtableTeamsBridge(client, team_id=team_id)
    worker_ids = selected_experts
    worker_names = [
        (get_persona(pid).name if get_persona(pid) else pid) for pid in worker_ids
    ]
    bridge.setup(worker_ids, worker_names, topic=proposal.topic)
    personas = [p for pid in worker_ids if (p := get_persona(pid)) is not None]
    result = asyncio.run(
        Roundtable(
            topic=proposal.topic,
            personas=personas,
            context=proposal.prior_review,
            manuscript_text=proposal.manuscript_text,
            team_bridge=bridge,
        ).run()
    )
    synthesis = result.synthesis
    verdict = verdict_override if verdict_override is not None else parse_verdict(synthesis)
    return synthesis, verdict


def apply_final_review(
    pipeline, proposal: FinalReviewProposal, synthesis: str, verdict: bool,
) -> str:
    """主编以 lead 把终审结论回灌创作项：approve=done / 打回=review→in_progress。"""
    item_id = proposal.item_id
    review_item_id = proposal.review_item_id
    lead = pipeline.lead
    space = pipeline.space
    if verdict:
        pipeline.guard.transition(space, lead, review_item_id, to="done", comment="圆桌终审通过")
        pipeline.guard.transition(space, lead, item_id, to="done", comment="圆桌终审定稿")
        pipeline._export_docx(proposal.item_kind, proposal.manuscript_text)
        return "approved"
    # 打回：产出项 review→in_progress（worker 无权做，主编 lead 裁决）；审核项保持 review 留待再审
    pipeline.guard.transition(
        space, lead, item_id, to="in_progress",
        comment=f"圆桌终审打回：{synthesis[:200]}",
    )
    return "rejected"
