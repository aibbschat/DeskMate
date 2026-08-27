"""创作流水线 Web 服务层（进程内注册表 + 操作 + SSE 生成器）。

后端薄桥：把 ``CreativePipeline`` / ``final_review`` 的能力暴露给
``deskmate/roundtable/web.py`` 的 REST / SSE 路由，让前端能创建、查询、
逐步推进创作流水线，并以「知情批准」方式触发圆桌终审（SSE 流式研讨）。

设计要点：
- 流水线实例按 ``pid`` 缓存在进程内 ``_PIPELINES``（sidecar 单进程共享即可）。
- executor 默认 ``stub``（演示/验证不烧 token）；真实创作传 ``llm`` 走 LLMExecutor。
- 终审严格「知情批准」：``final_review_proposal`` 只返回四要素（不拉圆桌）；
  ``stream_final_review`` 在用户选好专家后才跑圆桌并 SSE 转发；synthesis 暂存，
  最后由 ``final_review_apply`` 按用户传入的 verdict 回灌（决定权在用户，不虚）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator, Optional

from .pipeline import CreativePipeline
from .personas_loader import validate_creative_personas
from .executor import StubExecutor, LLMExecutor, ReviewResult
from .final_review import build_final_review_proposal, apply_final_review

logger = logging.getLogger(__name__)


# 进程内流水线注册表（sidecar 单进程，会话级缓存足够）
_PIPELINES: dict[str, CreativePipeline] = {}


def create_pipeline(
    kind: str,
    concept: str,
    *,
    executor_kind: str = "stub",
    base_dir: Optional[str] = None,
    novel_ref: str = "",
) -> dict:
    """校验创作 persona → 建团 → 起项目，返回 {pid, kind, team_id, ids, concept}。"""
    validate_creative_personas()
    if kind not in ("outline", "screenplay"):
        raise ValueError(f"未知 kind: {kind!r}（需 outline/screenplay）")
    if not concept.strip():
        raise ValueError("concept 不能为空")
    executor = StubExecutor() if executor_kind == "stub" else LLMExecutor()
    pid = uuid.uuid4().hex[:12]
    space = f"creation:{pid}"
    pl = CreativePipeline(space=space, executor=executor, base_dir=base_dir)
    if kind == "screenplay":
        ids = pl.start_screenplay_project(concept, novel_ref=novel_ref)
    else:
        ids = pl.start_outline_project(concept)
    _PIPELINES[pid] = pl
    return {
        "pid": pid,
        "kind": kind,
        "team_id": pl._team.team_id,
        "ids": ids,
        "concept": concept,
    }


def get_pipeline(pid: str) -> CreativePipeline:
    pl = _PIPELINES.get(pid)
    if pl is None:
        raise KeyError(pid)
    return pl


def _phase_of(pl: CreativePipeline) -> str:
    """据 board 状态推导当前可推进阶段（前端据此启用按钮）。"""
    items = {it["id"]: it for it in pl.store.list_items(pl.space, pl.lead)}
    oi = items.get(pl.ids.get("outline_item", -1), {}).get("state")
    ori = items.get(pl.ids.get("outline_review_item", -1), {}).get("state")
    si = items.get(pl.ids.get("screen_item", -1), {}).get("state") if "screen_item" in pl.ids else None
    sri = items.get(pl.ids.get("screen_review_item", -1), {}).get("state") if "screen_review_item" in pl.ids else None
    if oi == "done" and si is None:
        return "done"  # 大纲路线定稿
    if oi == "done" and si == "done":
        return "done"  # 剧本路线定稿
    if oi == "done" and sri == "review":
        return "lead_screen"  # 剧本审核完成，待主编拍板
    if oi == "done" and si == "open":
        return "draft_screen"
    if oi == "done" and si == "review":
        return "review_screen"
    if ori == "review":
        return "lead_outline"  # 可单点拍板 或 送终审
    if oi == "review":
        return "review_outline"
    if oi == "open":
        return "draft_outline"
    return "in_progress"


def snapshot(pid: str) -> dict:
    """返回流水线当前全貌：各 item 状态、team_id、concept、阶段、docx 产物。"""
    pl = get_pipeline(pid)
    items = []
    for it in pl.store.list_items(pl.space, pl.lead):
        items.append({
            "id": it["id"],
            "title": it.get("title", ""),
            "state": it.get("state", "open"),
            "criteria": it.get("criteria", ""),
            "comments": len(it.get("comments") or []),
        })
    kind = "screenplay" if "screen_item" in pl.ids else "outline"
    return {
        "pid": pid,
        "kind": kind,
        "concept": pl.concept,
        "team_id": pl._team.team_id,
        "items": items,
        "phase": _phase_of(pl),
        "docx": dict(pl.last_docx),
    }


# -- 逐步推进（包装异常，返回新快照） -------------------------------------
def _step(pid: str, fn_name: str, *args) -> dict:
    pl = get_pipeline(pid)
    getattr(pl, fn_name)(*args)
    return snapshot(pid)


def draft_outline(pid: str) -> dict:
    return _step(pid, "draft_outline")


def review_outline(pid: str) -> dict:
    return _step(pid, "review_outline")


def lead_outline(pid: str, approved: bool) -> dict:
    pl = get_pipeline(pid)
    result = ReviewResult(approved=approved, opinion="（主编手动拍板）" if approved else "（主编打回）")
    pl.lead_verdict_outline(result=result)
    return snapshot(pid)


def draft_screen(pid: str) -> dict:
    return _step(pid, "draft_screenplay")


def review_screen(pid: str) -> dict:
    return _step(pid, "review_screenplay")


def lead_screen(pid: str, approved: bool) -> dict:
    pl = get_pipeline(pid)
    from .executor import ReviewResult

    result = ReviewResult(approved=approved, opinion="（主编手动拍板）" if approved else "（主编打回）")
    pl.lead_verdict_screenplay(result=result)
    return snapshot(pid)


# -- 终审（知情批准）------------------------------------------------------
def final_review_proposal(pid: str, item_kind: str, *, genre_hint: Optional[str] = None) -> dict:
    """返回终审提案四要素（纯数据，不拉圆桌），供前端渲染提案卡。"""
    pl = get_pipeline(pid)
    prop = build_final_review_proposal(pl, item_kind, genre_hint=genre_hint)
    return {
        "item_kind": prop.item_kind,
        "topic": prop.topic,
        "manuscript_text": prop.manuscript_text,
        "criteria": prop.criteria,
        "prior_review": prop.prior_review,
        "all_experts": prop.all_experts,
        "recommended": prop.recommended,
    }


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def stream_final_review(
    pid: str,
    item_kind: str,
    selected_experts: list[str],
    *,
    genre_hint: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """SSE 生成器：用户选定专家后跑圆桌终审，转发协调器事件流。

    末尾发 ``final_review_ready``（带 synthesis）提示前端展示「确认定稿/打回」；
    synthesis 暂存到 pipeline._pending_final，待 ``final_review_apply`` 回灌。
    """
    pl = get_pipeline(pid)
    from deskmate.teams_client import TeamsClient
    from deskmate.roundtable.teams_bridge import RoundtableTeamsBridge
    from deskmate.roundtable.coordinator import Roundtable
    from deskmate.roundtable.personas import get_persona

    proposal = build_final_review_proposal(pl, item_kind, genre_hint=genre_hint)
    client = TeamsClient(base_dir=None)
    team_id = f"final-review:{pl.space}:{proposal.item_id}"
    bridge = RoundtableTeamsBridge(client, team_id=team_id)
    worker_names = [get_persona(p).name if get_persona(p) else p for p in selected_experts]
    try:
        bridge.setup(selected_experts, worker_names, topic=proposal.topic)
    except Exception as exc:  # noqa: BLE001
        logger.warning("终审 teams 基座建团失败(降级纯研讨): %s", exc)
        bridge = None
    personas = [p for pid2 in selected_experts if (p := get_persona(pid2)) is not None]

    synthesis = ""
    try:
        async for ev in Roundtable(
            topic=proposal.topic,
            personas=personas,
            context=proposal.prior_review,
            manuscript_text=proposal.manuscript_text,
            team_bridge=bridge,
        ).run_stream():
            if ev.get("type") == "done":
                synthesis = ev.get("synthesis", "")
                # 暂存，待用户确认 verdict 后回灌
                pl._pending_final = {"proposal": proposal, "synthesis": synthesis}
                yield _sse({
                    "type": "final_review_ready",
                    "item_kind": item_kind,
                    "topic": proposal.topic,
                    "synthesis": synthesis,
                })
                continue
            yield _sse(ev)
    except Exception as exc:  # noqa: BLE001
        logger.exception("终审 SSE 异常")
        yield _sse({"type": "error", "message": str(exc)})
        return
    if not synthesis:
        yield _sse({"type": "error", "message": "圆桌未产出纪要"})


def final_review_apply(pid: str, item_kind: str, verdict: bool) -> dict:
    """用户知情确认后回灌创作项（approve=done / 打回=review→in_progress）。"""
    pl = get_pipeline(pid)
    pending = getattr(pl, "_pending_final", None)
    if pending is None:
        raise ValueError("尚未运行终审研讨，无法回灌")
    proposal = pending["proposal"]
    synthesis = pending["synthesis"]
    status = apply_final_review(pl, proposal, synthesis, verdict)
    return {"status": status, "docx": dict(pl.last_docx), "pid": pid, "item_kind": item_kind}
