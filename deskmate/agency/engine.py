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
            "task_count": len(executed) + len(proposed),
        }

    # ------------------------------------------------------------------
    def _run_low_risk(self, action_type: str, settings: AgencySettings) -> Optional[Any]:
        """执行低风险动作。返回写入的 TaskRecord（或 None）。fail-open。"""
        try:
            if action_type == "world_search":
                from .behaviors.world_ingestion import run_world_ingestion

                res = run_world_ingestion(
                    str(self.base_dir), settings.topics, settings.world_search
                )
                # 本轮没实际搜（都在冷却）→ 不写 done 记录，免得面板刷屏
                if not res.get("searched"):
                    return None
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="、".join(res.get("searched", [])),
                    summary=res.get("summary", ""),
                    detail="\n".join(res.get("facts", [])),
                    count=res.get("stored", 0),
                )
            # 未来低风险动作（rss_digest / note_organize）在此接上
            return None
        except Exception as e:
            logger.warning("L2 低风险动作 %s 执行失败: %s", action_type, e)
            return None

    # ------------------------------------------------------------------
    def run_now(self) -> dict[str, Any]:
        """强制一次 tick（MCP / 端点用）。"""
        return self.tick()

    def resolve_proposal(self, record_id: str, decision: str) -> bool:
        """裁决高风险提议卡。decision ∈ executed/rejected/never。"""
        ok = self.log.resolve(record_id, decision)
        if ok and decision == TaskStatus.EXECUTED:
            # 用户同意执行：补跑该高风险动作（当前无高风险内置行为，留接缝）
            rec = self.log.get(record_id)
            if rec is not None and get_spec(rec.action_type) is not None:
                # 未来：在此真实执行 write_file / send_message / delete
                pass
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
