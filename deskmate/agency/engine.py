"""L2 调度引擎 —— 一次 tick 跑完「该自主干的事」。

流程：
1. 读配置；kill switch 关 → 整块跳过（返回 skipped）。
2. 遍历白名单动作类型（排除被用户「以后别提」的）。
3. 风险闸分流：
   - low  → 直接执行对应行为，写 task_log(done)
   - high → 不执行，写 task_log(proposed) 生成提议卡，等用户裁决
   - unknown → 同 high（fail-safe，绝不默认放开）
4. 全部 fail-open，任何异常都不阻断主流程、不影响 proactivity tick。

入口：tick()（后台周期调用）/ run_now()（强制一次）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import logging

logger = logging.getLogger(__name__)

from .settings import AgencySettings, load_agency_settings
from .task_log import TaskLog, TaskStatus
from .risk import classify_risk, requires_approval
from .registry import get_spec
from .router import route_task


class AgencyEngine:
    """L2 自主层引擎。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base_dir = Path(
            base_dir
            or os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
        )
        self.log = TaskLog(self.base_dir)

    # ------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        """执行一次 L2 tick。返回本轮摘要（供日志/面板）。"""
        settings = load_agency_settings(str(self.base_dir))
        if not settings.enabled:
            return {"skipped": True, "reason": "L2 自主层已关闭（kill switch）"}

        never = set(self.log.never_list())
        actions = [a for a in settings.whitelist_actions if a not in never]

        # 过期归档：外部信息必须有过期机制，否则「去年的信息今年还用来决定」。
        # 与白名单无关——只要自主层开着就该跑，且失败不影响后续。
        archived = self._archive_expired_external()

        executed: list[dict[str, Any]] = []
        proposed: list[dict[str, Any]] = []
        skipped_unknown: list[str] = []

        for action_type in actions:
            spec = get_spec(action_type)
            if spec is None:
                skipped_unknown.append(action_type)
                continue
            risk = classify_risk(action_type)
            # 代理决策路由（当前全 self；接缝已留）
            handler = route_task(action_type)
            if handler != "self":
                # 未来：委派给 persona 执行/决策。当前无此分支。
                pass

            if risk == "low":
                rec = self._run_low_risk(action_type, settings)
                if rec is not None:
                    executed.append(rec.to_dict())
            else:
                # high / unknown → 走提议卡
                rec = self.log.add(
                    action_type=action_type,
                    risk=risk,
                    status=TaskStatus.PROPOSED,
                    topic="",
                    summary=f"提议：{spec.label}",
                    detail=spec.description,
                )
                proposed.append(rec.to_dict())

        return {
            "skipped": False,
            "enabled": True,
            "executed": executed,
            "proposed": proposed,
            "skipped_unknown": skipped_unknown,
            "archived_expired": archived,
            "task_count": len(executed) + len(proposed),
        }

    # ------------------------------------------------------------------
    def _archive_expired_external(self) -> int:
        """归档过期的外部信息（external_items）。fail-open，失败返回 0。"""
        try:
            from deskmate.memory.external_store import ExternalStore

            return int(ExternalStore(base_dir=self.base_dir).archive_expired() or 0)
        except Exception as e:
            logger.debug("L2 外部信息过期归档失败: %s", e)
            return 0

    # ------------------------------------------------------------------
    def _run_low_risk(self, action_type: str, settings: AgencySettings) -> Optional[Any]:
        """执行低风险动作。返回写入的 TaskRecord（或 None）。fail-open。"""
        try:
            if action_type == "world_search":
                from .behaviors.world_ingestion import run_world_ingestion

                res = run_world_ingestion(
                    str(self.base_dir),
                    settings.topics,
                    settings.world_search,
                    topic_specs=settings.topic_specs,
                )
                # 本轮没实际搜（都在冷却）→ 不写 done 记录，免得面板刷屏
                if not res.get("searched"):
                    return None
                # 可信度不足的外部信息：不自动内化，转成提议卡等萧哥裁决
                pipe = res.get("pipeline") or {}
                awaiting_ids = list(pipe.get("awaiting_ids") or [])
                if awaiting_ids:
                    self._propose_external_review(awaiting_ids, str(self.base_dir))
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="、".join(res.get("searched", [])),
                    summary=res.get("summary", ""),
                    detail="\n".join(res.get("facts", [])),
                    count=res.get("stored", 0),
                    extra={"awaiting_review": len(awaiting_ids)},
                )

            if action_type == "external_digest":
                # 消化不可信区：筛选/摘要 → 验证 → 达标者写入内部记忆
                from deskmate.memory.ingestion_pipeline import run_ingestion_pipeline

                pipe = run_ingestion_pipeline(str(self.base_dir))
                promoted = int(pipe.get("promoted") or 0)
                awaiting = int(pipe.get("awaiting_review") or 0)
                if not promoted and not awaiting:
                    return None  # 本轮没东西可消化，不刷屏
                if awaiting:
                    self._propose_external_review(
                        list(pipe.get("awaiting_ids") or []), str(self.base_dir)
                    )
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=pipe.get("summary", ""),
                    detail="",
                    count=promoted,
                    extra={"awaiting_review": awaiting},
                )

            if action_type == "observation_diary":
                # 把世界观察写成小桔的观察日记（落本地日记，低风险自主）
                from .behaviors.observation_diary import run_observation_diary

                res = run_observation_diary(str(self.base_dir), settings.observation_diary)
                if not res.get("written"):
                    return None  # 冷却中 / 没新观察 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail="\n".join(res.get("observations", [])),
                    count=res.get("written", 0),
                )

            if action_type == "worldview_reflection":
                # 把攒下的世界观察理解沉淀成她自己的看法（写认知库 worldview 层，低风险自主）
                # 半自动：攒够 min_observations 条才回望一次，且过 7 天冷却。
                # 她弃权（"我还说不好"）也算跑过一次——照样标记已消化，不反复问她。
                from .behaviors.worldview_reflection import run_worldview_reflection

                res = run_worldview_reflection(
                    str(self.base_dir), settings.worldview_reflection
                )
                if not res.get("reflected"):
                    return None  # 攒不够 / 冷却中 / 没 provider → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail=res.get("essay", ""),
                    count=res.get("written", 0),
                )

            # 未来低风险动作（rss_digest / note_organize）在此接上
            return None
        except Exception as e:
            logger.warning("L2 低风险动作 %s 执行失败: %s", action_type, e)
            return None

    # ------------------------------------------------------------------
    def _propose_external_review(self, item_ids: list[str], base_dir: str) -> None:
        """把「可信度不足、待裁决」的外部信息转成一张提议卡。

        同一时间只保留一张 pending 的 external_review 卡——攒批而不是每 tick 一张，
        否则主动摄入一频繁就刷屏。已有一张时只更新待裁决列表。
        """
        if not item_ids:
            return
        try:
            pending = self.log.list(limit=50, status=TaskStatus.PROPOSED)
            for rec in pending:
                if rec.action_type != "external_review":
                    continue
                # 已有一张：合并待裁决 id 后直接返回，不再新建
                merged = list(dict.fromkeys(list(rec.extra.get("item_ids") or []) + item_ids))
                rec.extra["item_ids"] = merged
                rec.count = len(merged)
                rec.summary = f"{len(merged)} 条外部信息可信度不足，要不要收进内部记忆？"
                self.log.update(rec)
                return
            self.log.add(
                action_type="external_review",
                risk="high",
                status=TaskStatus.PROPOSED,
                topic="",
                summary=f"{len(item_ids)} 条外部信息可信度不足，要不要收进内部记忆？",
                detail="这些信息来自可信度较低的外部来源，未经人工确认不会写入内部记忆。",
                count=len(item_ids),
                extra={"item_ids": item_ids, "base_dir": base_dir},
            )
        except Exception as e:
            logger.warning("L2 生成外部信息提议卡失败: %s", e)

    # ------------------------------------------------------------------
    def run_now(self) -> dict[str, Any]:
        """强制一次 tick（MCP / 端点用）。"""
        return self.tick()

    def resolve_proposal(self, record_id: str, decision: str) -> bool:
        """裁决高风险提议卡。decision ∈ executed/rejected/never。"""
        rec = self.log.get(record_id)
        if rec is None or rec.status != TaskStatus.PROPOSED:
            return False
        ok = self.log.resolve(record_id, decision)
        if not ok or decision != TaskStatus.EXECUTED:
            return ok

        # 用户同意执行：按动作类型补跑
        if rec.action_type == "external_review":
            # 把待裁决的外部信息写入内部记忆——人工确认是唯一放行口
            item_ids = list(rec.extra.get("item_ids") or [])
            base_dir = rec.extra.get("base_dir") or str(self.base_dir)
            if item_ids:
                try:
                    from deskmate.memory.ingestion_pipeline import promote_approved

                    res = promote_approved(item_ids, base_dir=base_dir)
                    logger.info(
                        "L2 外部信息裁决：批准 %s 条，写入内部记忆 %s 条",
                        res.get("approved", 0), res.get("written", 0),
                    )
                    # 回填结果，便于任务面板回溯
                    rec.extra["approved"] = res.get("approved", 0)
                    rec.extra["written"] = res.get("written", 0)
                    self.log.update(rec)
                except Exception as e:
                    logger.warning("L2 外部信息裁决执行失败: %s", e)
        # 未来：write_file / send_message / delete 在此接上真实执行
        return ok

    def status(self) -> dict[str, Any]:
        """面板/状态用：开关、白名单、最近任务。"""
        settings = load_agency_settings(str(self.base_dir))
        recent = [r.to_dict() for r in self.log.list(limit=20)]
        return {
            "enabled": settings.enabled,
            "kill_switch_env": "DESKMATE_L2_ENABLED",
            "whitelist_actions": settings.whitelist_actions,
            "topics": settings.topics,
            "recent_tasks": recent,
            "pending_proposals": [r.to_dict() for r in self.log.list(limit=50, status=TaskStatus.PROPOSED)],
        }
