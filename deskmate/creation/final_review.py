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
import re
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
    item_id: int  # 创作产出项（大纲v1 / 全剧本 / 第X集）
    review_item_id: int  # 单点审核项
    topic: str  # 圆桌议题标题
    manuscript_text: str  # 送审片段（四要素①）
    criteria: str  # 验收标准（四要素③）
    prior_review: str  # 前序单点审核意见（四要素④）
    all_experts: list[dict] = field(default_factory=list)  # 7 专家 [{id,name,role}]
    recommended: list[str] = field(default_factory=list)  # 推荐子集 id（四要素②）
    novel_excerpt: str = ""  # 原著圣经摘要（novel_text 前 2000 字，pipeline 懒加载构建），供圆桌专家对照改编
    episode: Optional[int] = None  # 分集模式下被终审的集号（None=全本/大纲）


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
    episode: Optional[int] = None,
) -> FinalReviewProposal:
    """主编在 review 阶段生成「终审提案」（纯数据，不拉圆桌）。

    episode：分集模式下的集号（剧本终审可针对某一集）；None=大纲/全本。
    """
    if item_kind == "outline":
        item_id = pipeline.ids["outline_item"]
        review_item_id = pipeline.ids["outline_review_item"]
    elif item_kind == "screenplay":
        item_id = pipeline._screen_item_id(episode)
        review_item_id = pipeline._screen_review_item_id(episode)
        if episode is None and pipeline._is_episodic():
            episode = pipeline._episode_of(item_id)
    else:
        raise ValueError(f"未知 item_kind: {item_kind}")
    manuscript_text = pipeline._drafts.get(item_id, "")
    criteria = pipeline._criteria(review_item_id)
    prior = pipeline._last_review_result(review_item_id).opinion
    all_experts = [{"id": p.id, "name": p.name, "role": p.role} for p in load_personas()]
    recommended = recommend_experts(genre_hint, criteria)
    label = "剧本" if item_kind == "screenplay" else "大纲"
    if episode:
        label = f"第{episode}集"
    topic = f"《{pipeline.concept[:14]}…》{label}终审"
    return FinalReviewProposal(
        item_kind=item_kind, space=pipeline.space, item_id=item_id,
        review_item_id=review_item_id, topic=topic, manuscript_text=manuscript_text,
        criteria=criteria, prior_review=prior, all_experts=all_experts,
        recommended=recommended, novel_excerpt=getattr(pipeline, "novel_text", "")[:2000],
        episode=episode,
    )


def build_roundtable_context(proposal: "FinalReviewProposal") -> str:
    """构造圆桌终审注入上下文：前序审核意见 + 原著圣经摘要（如有）。

    **唯一权威入口**——同步路径（``run_final_review``）与 SSE 路径
    （``service.stream_final_review``）必须都走这里，防止两处漂移
    （曾出现 SSE 路径漏传原著、专家对着空中楼阁讨论的回归）。
    """
    context = proposal.prior_review
    if getattr(proposal, "novel_excerpt", ""):
        context = (
            f"{proposal.prior_review}\n\n"
            f"【原著参考（圣经摘要节选，供对照改编）】\n{proposal.novel_excerpt}"
        )
    return context


def parse_verdict(synthesis: str) -> bool:
    """从《综合研判报告》解析 verdict：True=通过定稿，False=打回修改。

    解析顺序（两道防线）：
    1. **结构化结论行**：报告含 ``VERDICT: APPROVE`` / ``VERDICT: REJECT``
       （主持人 synthesis prompt 强制输出）→ 直接采信，不猜措辞；
    2. **关键词启发式兜底**（旧报告/外部调用方无 VERDICT 行时）：
       正向优先——含明确通过词（建议定稿/通过终审等）即 approve，避免
       「本次不建议打回」这类含反向词的句子被裸「打回」误杀；反向判前先剔除
       否定式（不建议打回/不予打回/无需修改等），默认倾向定稿（不误杀）。

    注意：启发式对自然语言否定不完美，新报告一律走 VERDICT 行，这是根治。
    """
    s = synthesis or ""
    m = re.search(r"VERDICT:\s*(APPROVE|REJECT)", s, re.IGNORECASE)
    if m:
        return m.group(1).upper() == "APPROVE"
    approve_kw = ("通过终审", "准予通过", "建议定稿", "准予定稿", "同意定稿", "予以定稿")
    if any(k in s for k in approve_kw):
        return True
    # 否定式清洗（双重否定=正面：「不建议打回」→中性），避免反向词误杀
    for phrase in (
        "不建议打回", "不予打回", "无需打回", "不用打回", "不予以打回", "不必打回",
        "不建议修改", "不予修改", "无需修改", "不用修改",
    ):
        s = s.replace(phrase, "（中性）")
    # 打回只认「强否定」词：打回/驳回/重写/未达定稿/不合格/返工/退回。
    # 「建议修改」不在此列——「建议修改三处台词，不涉及结构」是弱建议而非打回，
    # 若列入会误杀（Hermes 巡检 P2-6 同款误判）；明确打回靠强词兜住。
    reject_kw = ("打回", "驳回", "重写", "未达定稿", "不予定稿", "不可定稿", "不合格", "需返工", "退回")
    if any(k in s for k in reject_kw):
        return False
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

    注意：本函数是**同步**入口（内部 ``asyncio.run`` 跑圆桌）。若在 async 代码
    （如 web 路由的协程）里调用会因「不能从运行中的事件循环调 asyncio.run」崩溃——
    async 场景请改用 ``service.stream_final_review``（SSE 流式）或先 ``await`` 化。
    """
    try:
        # 防嵌套：已在事件循环中（async 调用方）时给出清晰错误而非崩溃。
        # 放在最前：避免建团等副作用发生在报错之前。
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "run_final_review 是同步入口，不能从运行中的事件循环调用；"
            "async 场景请用 service.stream_final_review（SSE 流式终审）。"
        )
    client = TeamsClient(base_dir=base_dir)
    team_id = f"final-review:{proposal.space}:{proposal.item_id}"
    bridge = RoundtableTeamsBridge(client, team_id=team_id)
    worker_ids = selected_experts
    worker_names = [
        (get_persona(pid).name if get_persona(pid) else pid) for pid in worker_ids
    ]
    bridge.setup(worker_ids, worker_names, topic=proposal.topic)
    personas = [p for pid in worker_ids if (p := get_persona(pid)) is not None]
    context = build_roundtable_context(proposal)
    result = asyncio.run(
        Roundtable(
            topic=proposal.topic,
            personas=personas,
            context=context,
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
