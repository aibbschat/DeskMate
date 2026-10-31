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
import queue
import threading
import uuid
import time
from collections import OrderedDict
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Optional

from .pipeline import CreativePipeline
from .personas_loader import validate_creative_personas
from .executor import StubExecutor, LLMExecutor, ReviewResult
from .final_review import build_final_review_proposal, apply_final_review, build_roundtable_context

logger = logging.getLogger(__name__)


# 进程内流水线注册表（sidecar 单进程，会话级缓存足够）。
# 用 OrderedDict 做 LRU：超出上限淘汰最久未访问者；访问/创建都 move_to_end，
# 淘汰后下次 get_pipeline 会按持久化注册表懒恢复（board/草稿在磁盘，不丢）。
# 修复 B4①：原先无界 dict，长进程无限增长泄漏内存。
_PIPELINES: "OrderedDict[str, CreativePipeline]" = OrderedDict()
_PIPELINES_CAP = 128


def _cache_pipeline(pid: str, pl: "CreativePipeline") -> None:
    """放入注册表并维持 LRU 上限，避免长进程无限增长泄漏内存（B4①）。"""
    _PIPELINES[pid] = pl
    _PIPELINES.move_to_end(pid)
    while len(_PIPELINES) > _PIPELINES_CAP:
        _PIPELINES.popitem(last=False)

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
    style: Optional[list[str]] = None,
    pacing: str = "",
    episodes: int = 1,
    episode_range: Optional[list[int]] = None,
) -> dict:
    """校验创作 persona → 建团 → 起项目 → 预建原著索引，返回摘要。

    透明化 · 可提要求：style（风格标签）/ pacing（节奏）/ episodes（集数）/
    episode_range（仅产出集号）为结构化制作要求，透传给 executor 强约束。
    返回体含 novel_index 状态（ready/failed/none），供前端创建后直接展示
    「建立原著索引…」的落点：stub 模式强制降级圣经（零 LLM 不烧 token），
    llm 模式生成精炼圣经；索引失败不阻断创建（按无原著继续，与 pipeline 一致）。
    """
    validate_creative_personas()
    if kind not in ("outline", "screenplay"):
        raise ValueError(f"未知 kind: {kind!r}（需 outline/screenplay）")
    if not concept.strip():
        raise ValueError("concept 不能为空")
    episodes = max(1, int(episodes or 1))
    if kind == "outline":
        episodes = 1  # 大纲路线不产剧本，集数无意义
    executor = StubExecutor() if executor_kind == "stub" else LLMExecutor()
    pid = uuid.uuid4().hex[:12]
    space = f"creation:{pid}"
    pl = CreativePipeline(
        space=space, executor=executor, base_dir=base_dir, novel_cache_root=novel_cache_root
    )
    if kind == "screenplay":
        ids = pl.start_screenplay_project(
            concept, novel_ref=novel_ref, episodes=episodes,
            episode_range=episode_range, style=style, pacing=pacing,
        )
    else:
        ids = pl.start_outline_project(concept, novel_ref=novel_ref, style=style, pacing=pacing)
    _cache_pipeline(pid, pl)
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
        "created_at": time.time(),
        # 透明化 · 结构化制作要求（重启后恢复）
        "style": list(pl.style or []),
        "pacing": pl.pacing or "",
        "episodes": int(pl.episodes or 1),
        "episode_range": list(pl.episode_range or []),
        "prior_context": getattr(pl, "prior_context", "summary_recent"),
        "include_review_notes": bool(getattr(pl, "include_review_notes", True)),
    }
    _save_registry()

    # 预建原著索引。stub 模式零 LLM、秒级完成，同步建；llm 模式要为整本书生成
    # 改编圣经，一本书可能几分钟——放到后台线程，创建接口立即返回，
    # 前端据 novel_index.status == "building" 轮询 novel_index_status 等完成。
    if executor_kind == "stub":
        novel_index = _build_index_for(pl, degrade=True)
    elif not getattr(pl, "novel_ref", ""):
        novel_index = {"status": "none", "novel_ref": ""}
    else:
        novel_index = _start_index_build(pid, pl)
    return {
        "pid": pid,
        "kind": kind,
        "team_id": pl._team.team_id,
        "ids": ids,
        "concept": concept,
        "novel_index": novel_index,
        "style": list(pl.style or []),
        "pacing": pl.pacing or "",
        "episodes": int(pl.episodes or 1),
        "episode_range": list(pl.episode_range or []),
    }


# 原著索引后台构建中的 pid 集合（llm 模式整书精炼圣经耗时可达数分钟，
# 不能卡住创建接口；draft 若先到会同步等待，build 命中缓存即秒回）。
_INDEX_BUILDING: set[str] = set()
_INDEX_BUILD_LOCK = threading.Lock()


def _start_index_build(pid: str, pl: "CreativePipeline") -> dict:
    """后台线程建索引，立即返回 building 状态；完成后回写失败标记到注册表。"""
    with _INDEX_BUILD_LOCK:
        _INDEX_BUILDING.add(pid)

    def _run() -> None:
        try:
            _build_index_for(pl, degrade=False)
        finally:
            with _INDEX_BUILD_LOCK:
                _INDEX_BUILDING.discard(pid)
            meta = _REGISTRY_META.get(pid)
            if meta is not None:
                meta["novel_index_failed"] = bool(getattr(pl, "_novel_index_failed", False))
                _save_registry()

    threading.Thread(target=_run, name=f"novel-index-{pid}", daemon=True).start()
    return {"status": "building", "novel_ref": getattr(pl, "novel_ref", "")}


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
    raw_ids = dict(meta.get("ids", {}) or {})
    # JSON 落盘会把分集 dict 的 int 集号键变字符串（{"1": 5}），恢复时归一化回 int
    for _k in ("screen_items", "screen_review_items"):
        if isinstance(raw_ids.get(_k), dict):
            raw_ids[_k] = {int(k): v for k, v in raw_ids[_k].items()}
    pl.ids = raw_ids
    pl.style = list(meta.get("style", []) or [])
    pl.pacing = meta.get("pacing", "") or ""
    pl.episodes = int(meta.get("episodes", 1) or 1)
    pl.episode_range = [int(e) for e in (meta.get("episode_range", []) or [])]
    pl.prior_context = meta.get("prior_context") or "summary_recent"
    pl.include_review_notes = bool(meta.get("include_review_notes", True))
    pl._related_override = list(meta.get("related_override", []) or [])
    pl._novel_index_failed = bool(meta.get("novel_index_failed", False))
    pl.last_docx = dict(meta.get("last_docx", {}) or {})
    # 找回团队（board 数据在 teams.db，registry 按 lead_session 定位）
    pl._team = pl.reg.for_lead_session(f"{pl.space}:lead")
    # 恢复跨集前情摘要（pipeline.__init__ 已调 _load_drafts 读草稿，但若用不同
    # base_dir 构造，草稿路径可能与落盘路径不一致，此处强制重新加载确保摘要恢复）
    pl._load_drafts()
    # 重建原著索引句柄（bible 缓存文件仍在，懒加载直接命中）
    if pl.novel_ref:
        from .novel_index import NovelIndex

        cache_root = pl._novel_cache_root
        pl.novel_index = (
            NovelIndex(pl.novel_ref, cache_root=Path(cache_root))
            if cache_root
            else NovelIndex(pl.novel_ref)
        )
    _cache_pipeline(pid, pl)
    logger.info("已从持久化注册表恢复流水线 %s（space=%s）", pid, pl.space)
    return pl


def get_pipeline(pid: str) -> CreativePipeline:
    pl = _PIPELINES.get(pid)
    if pl is None:
        pl = _restore_pipeline(pid)
        if pl is None:
            raise KeyError(pid)
    else:
        _PIPELINES.move_to_end(pid)  # LRU 命中刷新
    return pl


def _phase_of(pl: CreativePipeline) -> str:
    """据 board 状态推导当前可推进阶段（前端据此启用按钮）。

    分集模式（screen_items 存在）：剧本阶段聚合为「任一集可推进」——
    未定稿的最小集处于 open→写；review→审；审核项 review→拍板。
    """
    items = {it["id"]: it for it in pl.store.list_items(pl.space, pl.lead)}
    oi = items.get(pl.ids.get("outline_item", -1), {}).get("state")
    ori = items.get(pl.ids.get("outline_review_item", -1), {}).get("state")
    episodic = bool(pl.ids.get("screen_items"))
    if episodic:
        # 分集：任一集未 done 即处于剧本推进期
        states = [
            items.get(sid, {}).get("state", "open")
            for sid in pl.ids.get("screen_items", {}).values()
        ]
        rev_states = [
            items.get(rid, {}).get("state", "open")
            for rid in pl.ids.get("screen_review_items", {}).values()
        ]
        any_pending = any(s != "done" for s in states)
        if oi == "done" and not any_pending:
            return "done"  # 所有集定稿
        if oi == "done" and any(r == "review" for r in rev_states):
            return "lead_screen"  # 有集审核完成，待拍板（优先于被打回集，用户可先拍板已审完的集）
        if oi == "done" and any(s == "in_progress" for s in states):
            return "draft_screen"  # 有集被打回重写中（或正在撰写），仍需"写"
        if oi == "done" and any(s == "review" for s in states):
            return "review_screen"  # 有集待审核
        if oi == "done":
            return "draft_screen"  # 有集待写（open/in_progress）
    else:
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
            "episode": pl._episode_of(it["id"]),  # 分集模式下为集号；单集/大纲为 None
        })
    kind = "screenplay" if ("screen_item" in pl.ids or "screen_items" in pl.ids) else "outline"
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
        # 透明化 · 结构化制作要求与分集信息
        "style": list(getattr(pl, "style", []) or []),
        "pacing": getattr(pl, "pacing", "") or "",
        "episodes": int(getattr(pl, "episodes", 1) or 1),
        "episode_range": list(getattr(pl, "episode_range", []) or []),
        # 前情上下文档位（加集续写衔接）
        "prior_context": getattr(pl, "prior_context", "summary_recent"),
        "include_review_notes": bool(getattr(pl, "include_review_notes", True)),
        # 用户打回意见（重写时注入 prompt，前端展示「上次你提的修改要求」）
        "user_notes": dict(getattr(pl, "_user_notes", {}) or {}),
        # 人物小传（防漂移锚点：前端可预览/编辑，每集剧本重注入）
        "char_profiles": getattr(pl, "_char_profiles", "") or "",
        "char_profiles_ready": bool(getattr(pl, "_char_profiles", "") or ""),
    }


# -- 加集续写（同一本书做完前 N 集，后续接着做） ---------------------------
def extend_episodes(
    pid: str,
    episode_range: list[int],
    *,
    prior_context: Optional[str] = None,
    include_review_notes: Optional[bool] = None,
) -> dict:
    """加集续写：为新增集号建项，风格/节奏/前情自动继承本流水线。

    prior_context：None=不改（沿用当前档位）；"summary"=仅摘要；
    "summary_recent"=摘要+紧邻上集定稿全文（默认档）。
    注册表同步更新（ids/episodes/episode_range/档位），重启可恢复。
    """
    pl = get_pipeline(pid)
    if prior_context in ("summary", "summary_recent"):
        pl.prior_context = prior_context
    if include_review_notes is not None:
        pl.include_review_notes = bool(include_review_notes)
    out = pl.extend_episodes(list(episode_range or []))
    # 注册表元数据同步（进程重启后 _REGISTRY_META 可能为空，先从磁盘回填）
    if pid not in _REGISTRY_META:
        _REGISTRY_META[pid] = _load_registry().get(pid, {})
    meta = _REGISTRY_META.get(pid)
    if meta is not None:
        meta["ids"] = dict(pl.ids)
        meta["episodes"] = int(pl.episodes or 1)
        meta["episode_range"] = list(pl.episode_range or [])
        meta["prior_context"] = pl.prior_context
        meta["include_review_notes"] = bool(pl.include_review_notes)
        _save_registry()
    pl._save_drafts()  # _cfg 档位随草稿落盘（双保险）
    return {"pid": pid, **out, "prior_context": pl.prior_context,
            "include_review_notes": pl.include_review_notes}


# -- 产出预览（透明化：用户要看得见每个 item 产出的文本） -----------------
def item_content(pid: str, item_id: int) -> dict:
    """返回指定 item 的草稿内容（产出预览）。无草稿返回空串。"""
    pl = get_pipeline(pid)
    return {
        "pid": pid,
        "item_id": item_id,
        "content": pl._drafts.get(int(item_id), ""),
        "episode": pl._episode_of(int(item_id)),
    }


# -- 原著索引（阶段 5：创建时预建 + 进行中可指定关联章节） -----------------
def novel_index_status(pid: str) -> dict:
    """查询原著索引状态：none（未关联）/ building（后台建索引中）/ pending（未建）/ ready / failed。"""
    pl = get_pipeline(pid)
    ni = getattr(pl, "novel_index", None)
    if ni is None:
        return {"status": "none", "novel_ref": getattr(pl, "novel_ref", "")}
    with _INDEX_BUILD_LOCK:
        building = pid in _INDEX_BUILDING
    if getattr(pl, "_novel_index_failed", False):
        return {"status": "failed", "novel_ref": ni.ref}
    meta = ni.get_meta()
    if building and not meta:
        return {"status": "building", "novel_ref": ni.ref}
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


# -- 人物小传（防漂移锚点：独立持久化文档，每集剧本重注入） -----------------
def extract_characters(pid: str, *, force: bool = False) -> dict:
    """生成/刷新「人物小传」锚点（从圣经或大纲抽取，幂等，落盘）。

    返回 ``char_profiles``（markdown 文本）与 ``ready``（非空标记）。
    """
    pl = get_pipeline(pid)
    doc = pl.extract_characters(force=force)
    return {"pid": pid, "char_profiles": doc, "ready": bool(doc.strip())}


def set_char_profiles(pid: str, text: str) -> dict:
    """用户手改人物小传（全剧后续每集生效，因为是独立持久锚点）。落盘并返回。"""
    pl = get_pipeline(pid)
    pl._char_profiles = (text or "").strip()
    pl._save_drafts()
    return {"pid": pid, "char_profiles": pl._char_profiles, "ready": bool(pl._char_profiles)}


# -- 逐步推进（包装异常，返回新快照） -------------------------------------
def _step(pid: str, fn_name: str, *args) -> dict:
    pl = get_pipeline(pid)
    getattr(pl, fn_name)(*args)
    return snapshot(pid)


# -- SSE 流式推进（生成型 step：executor 阶段事件实时推给前端） ------------
async def _stream_step(pid: str, fn_name: str, *, disconnect=None, **kwargs) -> AsyncGenerator[str, None]:
    """把同步 pipeline step 跑在独立线程，阶段事件经队列桥接为 SSE 流。

    executor 在生成过程中经 on_event 回调上报 {type,message,progress} 事件；
    done 时统一在事件循环线程取快照（避免线程并发读 store）。

    disconnect: 可选的可等待 Callable（如 ``request.is_disconnected``），用于
    检测 SSE 客户端是否已断开。若检测到断开，置 ``abort`` 事件并取消后台任务——
    底层生成循环（executor 分段写剧本）会在当前场景返回后停止后续场景，不再
    烧 token；已发出的事件被丢弃，前端瞬间收尾（修复 B4②：原先 task.cancel 只
    取消 asyncio 包装任务，底层 LLM 线程照跑完，断开后仍持续烧 token）。
    """
    pl = get_pipeline(pid)
    q: queue.Queue = queue.Queue()
    # 中止信号：断开时置位，executor 分段循环在场景间检查并提前结束
    abort = threading.Event()

    def on_event(ev: dict) -> None:
        q.put(("ev", ev))

    def run() -> None:
        try:
            getattr(pl, fn_name)(on_event=on_event, abort=abort, **kwargs)
            q.put(("done", None))
        except Exception as exc:  # noqa: BLE001
            q.put(("error", str(exc)))

    task = asyncio.create_task(asyncio.to_thread(run))
    while True:
        # 客户端断开检测：优先于队列轮询，断开即中止生成并退出
        if disconnect is not None:
            try:
                if await disconnect():
                    abort.set()  # 通知生成循环停止后续阶段（省 token）
                    task.cancel()  # 取消 asyncio 包装任务
                    logger.info("SSE 客户端已断开，中止创作 step %s (pid=%s)", fn_name, pid)
                    return
            except Exception:  # noqa: BLE001
                pass
        try:
            kind, payload = q.get_nowait()
        except queue.Empty:
            if task.done():
                break
            await asyncio.sleep(0.03)
            continue
        if kind == "ev":
            yield _sse(payload)
        elif kind == "done":
            yield _sse({"type": "done", "snapshot": snapshot(pid)})
            break
        elif kind == "error":
            yield _sse({"type": "error", "message": payload})
            break


def stream_draft_outline(pid: str, disconnect=None) -> AsyncGenerator[str, None]:
    return _stream_step(pid, "draft_outline", disconnect=disconnect)


def stream_draft_screen(pid: str, episode: Optional[int] = None, disconnect=None) -> AsyncGenerator[str, None]:
    return _stream_step(pid, "draft_screenplay", episode=episode, disconnect=disconnect)


def stream_review_outline(pid: str, disconnect=None) -> AsyncGenerator[str, None]:
    return _stream_step(pid, "review_outline", disconnect=disconnect)


def stream_review_screen(pid: str, episode: Optional[int] = None, disconnect=None) -> AsyncGenerator[str, None]:
    return _stream_step(pid, "review_screenplay", episode=episode, disconnect=disconnect)


def draft_outline(pid: str) -> dict:
    return _step(pid, "draft_outline")


def review_outline(pid: str) -> dict:
    return _step(pid, "review_outline")


def lead_outline(pid: str, approved: bool, user_note: Optional[str] = None) -> dict:
    pl = get_pipeline(pid)
    result = ReviewResult(approved=approved, opinion="（主编手动拍板）" if approved else "（主编打回）")
    pl.lead_verdict_outline(result=result, user_note=user_note)
    return snapshot(pid)


def draft_screen(pid: str, episode: Optional[int] = None) -> dict:
    pl = get_pipeline(pid)
    if episode is not None:
        pl.draft_screenplay(episode=episode)
    else:
        pl.draft_screenplay()
    return snapshot(pid)


def review_screen(pid: str, episode: Optional[int] = None) -> dict:
    pl = get_pipeline(pid)
    if episode is not None:
        pl.review_screenplay(episode=episode)
    else:
        pl.review_screenplay()
    return snapshot(pid)


def lead_screen(pid: str, approved: bool, episode: Optional[int] = None,
                user_note: Optional[str] = None) -> dict:
    pl = get_pipeline(pid)
    from .executor import ReviewResult

    result = ReviewResult(approved=approved, opinion="（主编手动拍板）" if approved else "（主编打回）")
    if episode is not None:
        pl.lead_verdict_screenplay(episode=episode, result=result, user_note=user_note)
    else:
        pl.lead_verdict_screenplay(result=result, user_note=user_note)
    return snapshot(pid)


# -- 终审（知情批准）------------------------------------------------------
def final_review_proposal(pid: str, item_kind: str, *, genre_hint: Optional[str] = None,
                          episode: Optional[int] = None) -> dict:
    """返回终审提案四要素（纯数据，不拉圆桌），供前端渲染提案卡。

    episode：分集模式下指定被终审的集号（剧本终审可针对某一集）。
    """
    pl = get_pipeline(pid)
    prop = build_final_review_proposal(pl, item_kind, genre_hint=genre_hint, episode=episode)
    return {
        "item_kind": prop.item_kind,
        "topic": prop.topic,
        "manuscript_text": prop.manuscript_text,
        "criteria": prop.criteria,
        "prior_review": prop.prior_review,
        "all_experts": prop.all_experts,
        "recommended": prop.recommended,
        "episode": prop.episode,
    }


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def stream_final_review(
    pid: str,
    item_kind: str,
    selected_experts: list[str],
    *,
    genre_hint: Optional[str] = None,
    episode: Optional[int] = None,
    disconnect: "Optional[Callable[[], Awaitable[bool]]]" = None,
) -> AsyncGenerator[str, None]:
    """SSE 生成器：用户选定专家后跑圆桌终审，转发协调器事件流。

    开头立刻 yield 一个 ``info`` 事件，使 HTTP 响应头立即发出（SSE 响应头在首个
    yield 时才推给客户端），避免前端在「初始化 + 邀请专家 + 探测在线状态」这一长串
    同步/await 阶段里完全收不到任何反馈而误以为卡死。

    末尾发 ``final_review_ready``（带 synthesis）提示前端展示「确认定稿/打回」；
    synthesis 暂存到 pipeline._pending_final，待 ``final_review_apply`` 回灌。

    disconnect: 可选可等待 Callable（如 ``request.is_disconnected``），客户端
    断开时立即结束流，避免无人接收还继续烧 token 跑圆桌。
    """
    try:
        pl = get_pipeline(pid)
    except KeyError:
        yield _sse({"type": "error", "message": "流水线不存在或已失效，请重新创建创作。"})
        return
    from deskmate.teams_client import TeamsClient
    from deskmate.roundtable.teams_bridge import RoundtableTeamsBridge
    from deskmate.roundtable.coordinator import Roundtable
    from deskmate.roundtable.personas import get_persona

    proposal = build_final_review_proposal(pl, item_kind, genre_hint=genre_hint, episode=episode)
    # 立刻发首个事件：SSE 响应头在首个 yield 时才推给客户端，这里先发一条 info，
    # 让前端「进行中」面板立即出现，不再在后面「初始化 + 邀请专家 + 探测在线状态」
    # 这一长串同步/await 阶段里死寂无反馈而误以为卡死。
    yield _sse({"type": "info", "text": f"已就绪，正在组建圆桌终审（邀请 {len(selected_experts)} 位专家）…"})

    # 用 pipeline 自身的 base（与 board 数据同根），避免自定义数据目录下 bridge 建团分家。
    # 终审团队另起 space（final-review:…），只登记花名册不抢流水线 worker。
    # 整段初始化套 wait_for 超时降级：TeamsClient 构造或建团若卡住/失败，不阻断纯内存研讨终审。
    bridge = None
    try:
        client = await asyncio.wait_for(
            asyncio.to_thread(TeamsClient, base_dir=pl.client.base), timeout=20
        )
        team_id = f"final-review:{pl.space}:{proposal.item_id}"
        bridge = RoundtableTeamsBridge(client, team_id=team_id)
        worker_names = [get_persona(p).name if get_persona(p) else p for p in selected_experts]
        await asyncio.wait_for(
            asyncio.to_thread(bridge.setup, selected_experts, worker_names, topic=proposal.topic),
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("终审 teams 基座建团失败(降级纯研讨): %s", exc)
        bridge = None
    personas = [p for pid2 in selected_experts if (p := get_persona(pid2)) is not None]
    yield _sse({"type": "info", "text": f"圆桌已就位，{len(personas)} 位专家开始研讨…"})

    synthesis = ""
    try:
        async for ev in Roundtable(
            topic=proposal.topic,
            personas=personas,
            context=build_roundtable_context(proposal),
            manuscript_text=proposal.manuscript_text,
            team_bridge=bridge,
        ).run_stream():
            # 客户端断开检测：断开即结束流（结果丢弃，不再烧 token 跑圆桌）
            if disconnect is not None:
                try:
                    if await disconnect():
                        logger.info("SSE 客户端已断开，取消终审研讨 (pid=%s)", pid)
                        return
                except Exception:  # noqa: BLE001
                    pass
            if ev.get("type") == "done":
                synthesis = ev.get("synthesis", "")
                # 暂存，待用户确认 verdict 后回灌（按 (item_kind, episode) 分槽，避免多集连续终审互相覆盖）
                if not isinstance(getattr(pl, "_pending_final", None), dict):
                    pl._pending_final = {}
                pl._pending_final[(item_kind, episode)] = {"proposal": proposal, "synthesis": synthesis}
                # 落盘（B4③）：synthesis 是圆桌产出，原先只存内存，进程重启/流水线
                # 被 LRU 淘汰再恢复后会丢失，导致 final_review_apply 报「尚未运行终审研讨」。
                # 随草稿原子写盘，恢复时一并读回。
                try:
                    pl._save_drafts()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("终审结果暂存落盘失败（仅内存态，重启可能丢）: %s", exc)
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


def final_review_apply(pid: str, item_kind: str, verdict: bool, episode: Optional[int] = None) -> dict:
    """用户知情确认后回灌创作项（approve=done / 打回=review→in_progress）。

    episode 用于从分槽的 _pending_final 中精确取回对应集的终审结果，
    避免多集连续终审时后一次覆盖前一次（单槽竞态）。
    """
    pl = get_pipeline(pid)
    pending = getattr(pl, "_pending_final", None)
    if not isinstance(pending, dict) or not pending:
        raise ValueError("尚未运行终审研讨，无法回灌")
    entry = pending.get((item_kind, episode))
    if entry is None:
        raise ValueError(
            f"未找到{'第'+str(episode)+'集' if episode else '该'}的终审结果，请先运行对应集的终审研讨"
        )
    proposal = entry["proposal"]
    synthesis = entry["synthesis"]
    status = apply_final_review(pl, proposal, synthesis, verdict)
    return {"status": status, "docx": dict(pl.last_docx), "pid": pid, "item_kind": item_kind}


# -- 创作对话流（chat_flow：把流水线放入原生对话流） ----------------------
def get_chat_messages(pid: str) -> list[dict]:
    """获取创作对话历史（按时间顺序）。"""
    pl = get_pipeline(pid)
    from .chat_flow import get_chat
    chat = get_chat(pid, pl)
    return chat.messages


async def stream_chat(pid: str, text: str, disconnect: "Optional[Callable[[], Awaitable[bool]]]" = None) -> AsyncGenerator[str, None]:
    """用户在创作对话流里发一条消息 → 意图识别 → 执行动作 → SSE 逐消息返回。

    返回的每条 SSE 事件为 {"type":"chat_message","message":{...}}，
    message 含 type/user/system/progress/artifact/review/milestone/expert/help/error。
    """
    pl = get_pipeline(pid)
    from .chat_flow import get_chat
    chat = get_chat(pid, pl)
    try:
        async for msg in chat.post(text):
            yield _sse({"type": "chat_message", "message": msg})
            if disconnect is not None:
                try:
                    if await disconnect():
                        logger.info("SSE 客户端已断开，结束创作对话流 (pid=%s)", pid)
                        return
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("创作对话 SSE 异常")
        yield _sse({"type": "error", "message": str(exc)})
