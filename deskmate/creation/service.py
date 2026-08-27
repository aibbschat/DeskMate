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
import os
import uuid
from pathlib import Path
from typing import AsyncGenerator, Optional

from .pipeline import CreativePipeline
from .personas_loader import validate_creative_personas
from .executor import StubExecutor, LLMExecutor, ReviewResult
from .final_review import build_final_review_proposal, apply_final_review, build_roundtable_context

logger = logging.getLogger(__name__)


# 进程内流水线注册表（sidecar 单进程，会话级缓存足够）
_PIPELINES: dict[str, CreativePipeline] = {}

# 注册表持久化路径（P1-4：sidecar 重启后流水线可恢复）。None=默认
# ``resolve_base()/creation_pipelines.json``；测试可注入临时目录避免污染真实环境。
_REGISTRY_PATH: Optional[Path] = None


def _registry_file() -> Path:
    if _REGISTRY_PATH is not None:
        return Path(_REGISTRY_PATH)
    from deskmate.teams_client import resolve_base

    return resolve_base() / "creation_pipelines.json"


def _load_registry() -> dict:
    """读持久化注册表 {pid: 恢复元数据}。文件缺失/损坏返回空表（不阻断）。"""
    try:
        path = _registry_file()
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("流水线注册表读取失败（按空表继续）: %s", exc)
    return {}


def _save_registry() -> None:
    """把注册表元数据原子写盘（供重启后按 pid 恢复流水线）。"""
    try:
        path = _registry_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(_REGISTRY_META, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("流水线注册表落盘失败（不影响本次运行）: %s", exc)


# 注册表元数据：pid -> 恢复所需字段（草稿文本在 pipeline 自己的 drafts 文件，不入这里）
_REGISTRY_META: dict = {}


def create_pipeline(
    kind: str,
    concept: str,
    *,
    executor_kind: str = "stub",
    base_dir: Optional[str] = None,
    novel_ref: str = "",
    novel_cache_root: Optional[str] = None,
) -> dict:
    """校验创作 persona → 建团 → 起项目 → 预建原著索引，返回摘要。

    返回体含 novel_index 状态（ready/failed/none），供前端创建后直接展示
    「建立原著索引…」的落点：stub 模式强制降级圣经（零 LLM 不烧 token），
    llm 模式生成精炼圣经；索引失败不阻断创建（按无原著继续，与 pipeline 一致）。
    """
    validate_creative_personas()
    if kind not in ("outline", "screenplay"):
        raise ValueError(f"未知 kind: {kind!r}（需 outline/screenplay）")
    if not concept.strip():
        raise ValueError("concept 不能为空")
    executor = StubExecutor() if executor_kind == "stub" else LLMExecutor()
    pid = uuid.uuid4().hex[:12]
    space = f"creation:{pid}"
    pl = CreativePipeline(
        space=space, executor=executor, base_dir=base_dir, novel_cache_root=novel_cache_root
    )
    if kind == "screenplay":
        ids = pl.start_screenplay_project(concept, novel_ref=novel_ref)
    else:
        ids = pl.start_outline_project(concept, novel_ref=novel_ref)
    _PIPELINES[pid] = pl
    # 登记恢复元数据（P1-4）：sidecar 重启后按 pid 可从磁盘恢复流水线
    _REGISTRY_META[pid] = {
        "space": space,
        "kind": kind,
        "concept": concept,
        "executor_kind": executor_kind,
        "base_dir": str(base_dir) if base_dir else None,
        "novel_ref": novel_ref,
        "novel_cache_root": str(novel_cache_root) if novel_cache_root else None,
        "ids": dict(ids),
        "related_override": list(getattr(pl, "_related_override", []) or []),
        "novel_index_failed": bool(getattr(pl, "_novel_index_failed", False)),
        "last_docx": dict(getattr(pl, "last_docx", {}) or {}),
        "created_at": uuid.uuid4().hex[:8],
    }
    _save_registry()

    # 预建原著索引（创建时同步，后续 draft 直接命中缓存；失败降级不阻断）
    novel_index = _build_index_for(pl, degrade=(executor_kind == "stub"))
    return {
        "pid": pid,
        "kind": kind,
        "team_id": pl._team.team_id,
        "ids": ids,
        "concept": concept,
        "novel_index": novel_index,
    }


def _build_index_for(pl: "CreativePipeline", *, degrade: bool = False) -> dict:
    """为流水线预建原著索引。返回状态摘要；无原著/失败都返回可用摘要。"""
    ni = getattr(pl, "novel_index", None)
    if ni is None:
        return {"status": "none", "novel_ref": getattr(pl, "novel_ref", "")}
    try:
        meta = ni.build(degrade=degrade)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预建原著索引失败（按无原著继续）: %s", exc)
        pl._novel_index_failed = True  # 快照据此返回 failed（而非 pending）
        return {"status": "failed", "novel_ref": ni.ref, "error": str(exc)}
    return {
        "status": "ready",
        "novel_ref": ni.ref,
        "total_chars": meta["total_chars"],
        "n": meta["n"],
        "degraded": meta["degraded"],
        "bible_chars": len(meta["bible"]),
    }


def _restore_pipeline(pid: str) -> Optional[CreativePipeline]:
    """从持久化注册表重建流水线实例（board/草稿在磁盘，进程重启不丢）。

    不重新 start_*_project（避免重复建团）；手工恢复业务字段，团队经
    ``for_lead_session(f"{space}:lead")`` 从 teams 注册表找回。
    """
    meta = _load_registry().get(pid)
    if not meta:
        return None
    from .executor import LLMExecutor, StubExecutor

    executor = StubExecutor() if meta.get("executor_kind") == "stub" else LLMExecutor()
    pl = CreativePipeline(
        space=meta["space"],
        executor=executor,
        base_dir=meta.get("base_dir"),
        novel_cache_root=meta.get("novel_cache_root"),
    )
    # 恢复业务字段
    pl.concept = meta.get("concept", "")
    pl.novel_ref = meta.get("novel_ref", "")
    pl.ids = dict(meta.get("ids", {}) or {})
    pl._related_override = list(meta.get("related_override", []) or [])
    pl._novel_index_failed = bool(meta.get("novel_index_failed", False))
    pl.last_docx = dict(meta.get("last_docx", {}) or {})
    # 找回团队（board 数据在 teams.db，registry 按 lead_session 定位）
    pl._team = pl.reg.for_lead_session(f"{pl.space}:lead")
    # 重建原著索引句柄（bible 缓存文件仍在，懒加载直接命中）
    if pl.novel_ref:
        from .novel_index import NovelIndex

        cache_root = pl._novel_cache_root
        pl.novel_index = (
            NovelIndex(pl.novel_ref, cache_root=Path(cache_root))
            if cache_root
            else NovelIndex(pl.novel_ref)
        )
    _PIPELINES[pid] = pl
    logger.info("已从持久化注册表恢复流水线 %s（space=%s）", pid, pl.space)
    return pl


def get_pipeline(pid: str) -> CreativePipeline:
    pl = _PIPELINES.get(pid)
    if pl is None:
        pl = _restore_pipeline(pid)
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
    idx_summary = novel_index_status(pid)
    idx_summary.pop("chapters", None)  # 章节目录留给 GET novel-index 单独取，snapshot 只带轻量摘要
    return {
        "pid": pid,
        "kind": kind,
        "concept": pl.concept,
        "team_id": pl._team.team_id,
        "novel_ref": getattr(pl, "novel_ref", ""),
        "items": items,
        "phase": _phase_of(pl),
        "docx": dict(pl.last_docx),
        "novel_index": idx_summary,
        "related_chapters": list(getattr(pl, "_related_override", []) or []),
    }


# -- 原著索引（阶段 5：创建时预建 + 进行中可指定关联章节） -----------------
def novel_index_status(pid: str) -> dict:
    """查询原著索引状态：none（未关联）/ pending（未建）/ ready / failed。"""
    pl = get_pipeline(pid)
    ni = getattr(pl, "novel_index", None)
    if ni is None:
        return {"status": "none", "novel_ref": getattr(pl, "novel_ref", "")}
    if getattr(pl, "_novel_index_failed", False):
        return {"status": "failed", "novel_ref": ni.ref}
    meta = ni.get_meta()
    if not meta:
        return {"status": "pending", "novel_ref": ni.ref}
    return {
        "status": "ready",
        "novel_ref": ni.ref,
        "total_chars": meta["total_chars"],
        "n": meta["n"],
        "degraded": meta["degraded"],
        "bible_chars": len(meta["bible"]),
        "chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"]} for c in meta["chapters"]
        ],
    }


def pick_novel_chapters(pid: str, topic: str = "", k: int = 3) -> dict:
    """按主题挑候选相关章节（bigram 匹配，返回标题+开头预览供前端勾选）。"""
    pl = get_pipeline(pid)
    ni = getattr(pl, "novel_index", None)
    if ni is None:
        return {"chapters": [], "topic": topic, "error": "该流水线未关联原著"}
    if ni.get_meta() is None:
        _build_index_for(pl, degrade=True)  # 未建先建（零 LLM 索引兜底）
    try:
        picks = ni.pick_chapters(topic.strip() or (pl.concept or "")[:800], k=k)
    except Exception as exc:  # noqa: BLE001
        return {"chapters": [], "topic": topic, "error": str(exc)}
    return {
        "chapters": [
            {"idx": c["idx"], "title": c["title"], "chars": c["chars"], "head": (c["body"] or "")[:120]}
            for c in picks
        ],
        "topic": topic.strip() or pl.concept,
    }


def set_related_chapters(pid: str, indices: list[int]) -> dict:
    """设定创作参考章节（1..n）；空列表恢复自动匹配。返回更新后的引用清单。"""
    pl = get_pipeline(pid)
    pl.set_related_chapters([int(i) for i in (indices or [])])
    return {"related": list(pl._related_override)}


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
    # 用 pipeline 自身的 base（与 board 数据同根），避免自定义数据目录下 bridge 建团分家。
    # 终审团队另起 space（final-review:…），只登记花名册不抢流水线 worker。
    client = TeamsClient(base_dir=pl.client.base)
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
            context=build_roundtable_context(proposal),
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
