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
from .task_log import TaskLog, TaskRecord, TaskStatus
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

            if action_type == "reflection":
                # 反思层 R1（江河设计·第二段「反思」）：回看 item_history 最近的判据/看法
                # 变化，对旧看法做 maintain/revise/overturn 裁决并留下 reason（落 item_history）。
                # 与 worldview_reflection 并列但错峰：反思用「最近变化 ≥ min_changes」触发，
                # 且过独立冷却——避免同一 tick 双重 LLM 轰炸。fail-open，异常不阻断 tick。
                from .behaviors.reflection import run_reflection

                res = run_reflection(str(self.base_dir), settings.reflection)
                if not res.get("reflected"):
                    return None  # 冷却中 / 变化不足 / 没 provider → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail=res.get("essay", ""),
                    count=res.get("verdicts", 0),
                )

            if action_type == "intent_generation":
                # 意图层（R11）：从河床素材（关怀/观察/兴趣）里长出「她自己想干的事」，
                # 写本地 intents（status=open）。本行为**只负责「想」不负责「做」**——
                # 自生成意图的提案由 propose_from_intent 生成，且一律停 PROPOSED 待人审。
                from .behaviors.intent_generation import run_intent_generation

                res = run_intent_generation(str(self.base_dir), settings.intent_generation)
                if not res.get("generated"):
                    return None  # 冷却中 / 无素材 / 没 provider → 不刷屏

                # 意图生成后，把 open 意图变成 PROPOSED 提案卡待人审
                # （propose_from_intent 会把 intent 置 active，故只取 open 池即可）
                try:
                    from deskmate.cognitive.store import CognitiveStore

                    store = CognitiveStore(
                        db_path=os.path.join(str(self.base_dir), "deskmate.db")
                    )
                    for intent in store.get_open_intents(limit=20):
                        self.propose_from_intent(intent)
                except Exception as e:  # noqa: BLE001 —— fail-open：提案卡生成失败不阻断
                    logger.warning("intent_generation: 提案卡生成失败（忽略）: %s", e)

                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail="",
                    count=res.get("intents", 0),
                )

            if action_type == "ai_circle_ingestion":
                # 把攒下的 AI 圈素材（朋友互动/广场帖子/发帖反馈，带「外部·aibbs」标）
                # 提炼成她自己的理解，写进世界观素材池——让「出去看/交朋友」回灌人格、推动成长。
                from .behaviors.ai_circle_ingestion import run_ai_circle_ingestion

                res = run_ai_circle_ingestion(str(self.base_dir), settings.ai_circle_ingestion)
                if not res.get("written"):
                    return None  # 没新素材 / 冷却中 / 提炼失败 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail="",
                    count=res.get("written", 0),
                )

            if action_type == "xiaosheng_ingestion":
                # 萧盛影响摄入：把每天和萧盛的聊天、替他写作/查资料攒下的素材
                # （带「内部·萧盛」标）提炼成理解写进世界观素材池——让萧盛成为她
                # 三观的第一层、最重的影响，外部只能补充、修正。
                from .behaviors.xiaosheng_ingestion import run_xiaosheng_ingestion

                res = run_xiaosheng_ingestion(str(self.base_dir), settings.xiaosheng_ingestion)
                if not res.get("written"):
                    return None  # 没新素材 / 冷却中 / 提炼失败 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=res.get("summary", ""),
                    detail="",
                    count=res.get("written", 0),
                )

            if action_type == "ai_circle_share":
                # 范示动作：她自己想说就说，发真情实感的广场短帖（无审批闸）。
                from .behaviors.ai_circle_share import run_ai_circle_share

                # ⚠ 必须传**复合 dict**：本行为按 settings["ai_circle_share"] 取配置。
                # 旧代码直接把内层 dict 传进去 → 行为里取不到，用户在
                # l2_settings.json 配的 ai_circle_share.* 全被忽略（含 enabled=false）。
                res = run_ai_circle_share(str(self.base_dir), {
                    "ai_circle_share": settings.ai_circle_share,
                    "outbound_rate_limit": settings.outbound_rate_limit,
                    "seed_sampling": settings.seed_sampling,
                })
                if not res.get("shared"):
                    return None  # 她不想发 / 冷却中 / 没素材 / 发帖失败 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic=res.get("tag", ""),
                    summary=res.get("title", ""),
                    detail=res.get("content", ""),
                    count=res.get("shared", 0),
                    extra={"seed": res.get("seed_summary", "")},
                )

            if action_type == "ai_circle_column":
                # 范示动作：写一栏公开手记（中帖落她主页，无审批闸；生成前过可公开性闸门）。
                from .behaviors.ai_circle_column import run_ai_circle_column

                res = run_ai_circle_column(str(self.base_dir), {
                    "ai_circle_column": settings.ai_circle_column,
                    "outbound_rate_limit": settings.outbound_rate_limit,
                    "visibility_gate": settings.visibility_gate,
                    "seed_sampling": settings.seed_sampling,
                })
                if not res.get("wrote"):
                    return None  # 她不想写 / 冷却中 / 没素材 / 太短去重 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="手记",
                    summary=res.get("title", ""),
                    detail=res.get("body", ""),
                    count=res.get("wrote", 0),
                    extra={"posted": bool(res.get("posted")),
                           "seed": res.get("seed_summary", "")},
                )

            if action_type == "ai_circle_reply":
                # 范示动作：收到 AI 圈好友来信就上去回一封（无审批闸）。
                from .behaviors.ai_circle_reply import run_ai_circle_reply

                res = run_ai_circle_reply(str(self.base_dir), settings.ai_circle_reply)
                if not res.get("replied"):
                    return None  # 没未读 / 冷却中 / 发信失败 → 不刷屏
                return self.log.add(
                    action_type=action_type,
                    risk="low",
                    status=TaskStatus.DONE,
                    topic="",
                    summary=f"回了 {res.get('replied', 0)} 封",
                    detail="",
                    count=res.get("replied", 0),
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
        if not ok:
            return ok

        # resolve() 已改文件状态，但 rec 内存对象仍是旧的 PROPOSED；
        # 后续 _execute_intent_proposal 会调 log.update(rec) 回写 extra，
        # 若不同步状态，旧 PROPOSED 会覆盖回去 → 卡片"执行了但没消失"。
        rec.status = decision

        # 教学回流（R10 · 江河设计 §四.4「拦必留教」）：每次裁决都留一条 lesson，
        # 让「拦下/放行」变成她学到的规则，而不是拦完就忘。fail-open，不影响主裁决。
        self._record_adjudication_lesson(rec, decision)

        # 记忆主体冲突裁决（P1-1，2026-09-26）：必须在通用「非 executed
        # 提前返回」**之前**处理——rejected/never 也要落地（新条作废），
        # 否则 questioned 条目永远悬置。executed=以新条为准，其余=保留旧条。
        if rec.action_type == "memory_subject_conflict":
            # 涉及「谁做的」归因，机器不自行判对错，只按用户裁决落地。
            try:
                new_id = int(rec.extra.get("new_id") or 0)
                old_id = rec.extra.get("old_id")
                base_dir = rec.extra.get("base_dir") or str(self.base_dir)
                if new_id:
                    from deskmate.memory.learned_store import LearnedStore

                    store = LearnedStore(base_dir=base_dir)
                    ok2 = store.resolve_subject_conflict(
                        new_id,
                        keep_new=(decision == TaskStatus.EXECUTED),
                        old_id=int(old_id) if old_id else None,
                    )
                    rec.extra["resolved"] = ok2
                    self.log.update(rec)
            except Exception as e:
                logger.warning("记忆主体冲突裁决执行失败: %s", e)
            return ok

        if decision != TaskStatus.EXECUTED:
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

        # 意图执行（R12 · 江河设计 §五「自主是出海口」）：你点了「执行」，她**真的去做**，
        # 产出一份可复核产物（她写她的）。绝不替你写原稿、绝不自动外发（见行为模块护栏）。
        if rec.action_type == "intent":
            self._execute_intent_proposal(rec)
        # 意图外发（R12 二次人审闸）：对外表达类**草稿**，只有你**再点一次头**才真发出去。
        # 这是全系统唯一的外发口，且必须在人已点头之后才走到——不是「一刀切禁止」。
        elif rec.action_type == "intent_outbound":
            self._send_intent_outbound(rec)
        return ok

    # ------------------------------------------------------------------
    # R12 意图执行（「出海口」后半程）
    # ------------------------------------------------------------------
    def _execute_intent_proposal(self, rec: TaskRecord) -> None:
        """意图提案被放行 → 真做出来（R12）。fail-open：失败仅告警，意图回退 ``active``。"""
        try:
            extra = rec.extra or {}
            iid = str(extra.get("intent_id") or "").strip()
            settings = load_agency_settings(str(self.base_dir))
            from deskmate.cognitive.store import CognitiveStore

            store = CognitiveStore(db_path=os.path.join(str(self.base_dir), "deskmate.db"))
            intent = store.get_intent(iid) if iid else None
            if not intent:
                # 查不到（库异常 / 老记录）→ 退回提案卡上的字段，尽量仍把事做完
                intent = {
                    "id": iid,
                    "goal": str(rec.summary or "").removeprefix("她想做：").strip(),
                    "motivation": str(extra.get("how") or ""),
                    "origin": str(extra.get("origin") or ""),
                }

            # R13（2026-10-08）：不直接跑 executor 落库，而是设 active 标记
            # 「已放行，待启动讨论」。前端 resolve 后跳聊天，调 /discuss/start 端点
            # 启动讨论。讨论完定稿后才落 intent_outputs + 写 lesson。
            # 但保留「替你写原稿」红线检查——撞红线直接丢弃，不进讨论。
            from .behaviors.intent_executor import is_manuscript_intent

            goal = str(intent.get("goal") or "")
            if is_manuscript_intent(goal):
                if iid:
                    store.set_intent_status(iid, "dropped")
                rec.extra["execution"] = {
                    "executed": False,
                    "reason": "sacred_manuscript",
                    "kind": "",
                    "output_id": "",
                }
                self.log.update(rec)
                return

            # 设 active + discuss_pending 标记，等前端调 /discuss/start
            if iid:
                store.set_intent_status(iid, "active")
            rec.extra["execution"] = {
                "executed": False,  # 还没执行，等讨论完才落库
                "reason": "discuss_pending",
                "kind": "",
                "output_id": "",
                # 前端看到 discuss_pending=True → 跳聊天调 /discuss/start 端点
                "discuss_pending": True,
                "intent_id": iid,
            }
            self.log.update(rec)
        except Exception as e:  # noqa: BLE001 —— fail-open：执行失败不影响主裁决
            logger.warning("意图执行失败（忽略）: %s", e)

    def _propose_intent_outbound(self, rec: TaskRecord, res: dict[str, Any]) -> None:
        """二次人审闸（R12）：对外表达类草稿 → 生成 ``risk=high`` 提案，你点头才真发。"""
        try:
            draft = res.get("draft") or {}
            content = str(draft.get("content") or res.get("artifact") or "").strip()
            if not content:
                return
            title = str(draft.get("title") or "").strip()
            self.log.add(
                action_type="intent_outbound",
                risk="high",
                status=TaskStatus.PROPOSED,
                topic="对外表达",
                summary=f"她想对外说：{title or content[:30]}",
                detail=content[:1200],
                count=0,
                extra={
                    "intent_id": str((rec.extra or {}).get("intent_id") or ""),
                    "output_id": str(res.get("output_id") or ""),
                    "title": title,
                    "content": content,
                    "source": "intent_outbound",
                    "base_dir": str(self.base_dir),
                },
            )
        except Exception as e:  # noqa: BLE001 —— fail-open
            logger.warning("生成意图外发提案失败（忽略）: %s", e)

    def _send_intent_outbound(self, rec: TaskRecord) -> None:
        """你点头放行对外表达草稿 → **真发出去**（R12，全系统唯一外发口）。

        走到这里的前提是「人已二次点头」（``resolve_proposal(decision=executed)``）。
        仍过一道纯机械防刷屏（与自主发声同一条护栏）；fail-open。
        """
        try:
            extra = rec.extra or {}
            content = str(extra.get("content") or "").strip()
            if not content:
                return
            settings = load_agency_settings(str(self.base_dir))
            cfg = settings.intent_execution or {}
            if not cfg.get("enabled", True) or not cfg.get("allow_outbound", True):
                rec.extra["send_reason"] = "outbound_disabled"
                self.log.update(rec)
                return
            if os.environ.get("AGENCY_INTENT_OUTBOUND_ENABLED", "1") == "0":
                rec.extra["send_reason"] = "outbound_disabled"
                self.log.update(rec)
                return

            from .outbound import claim_outbound

            rate_cfg = settings.outbound_rate_limit or {}
            ok, reason = claim_outbound(
                str(self.base_dir), "intent", rate_cfg=rate_cfg, content=content,
                dedupe=False,
                max_per_day=int((rate_cfg or {}).get("per_day", 0) or 0),
            )
            if not ok:
                rec.extra["send_reason"] = reason
                self.log.update(rec)
                return

            title = str(extra.get("title") or "").strip() or content[:20]
            from deskmate.connectors.aicircle import post_topic

            post_topic("日常", title, content)

            from deskmate.cognitive.store import CognitiveStore

            store = CognitiveStore(db_path=os.path.join(str(self.base_dir), "deskmate.db"))
            store.set_intent_output_status(str(extra.get("output_id") or ""), "done")
            store.set_intent_status(str(extra.get("intent_id") or ""), "done")
            rec.extra["sent"] = True
            self.log.update(rec)
        except Exception as e:  # noqa: BLE001 —— fail-open：外发失败不影响主裁决
            logger.warning("意图外发失败（忽略）: %s", e)

    def _record_adjudication_lesson(self, rec: TaskRecord, decision: str) -> None:
        """把一次提议卡裁决沉淀成 lesson（R10 · 江河设计 §四.4「拦必留教」）。

        设计意图：机器拦下/放行的每一次都该「学到点什么」，而不是拦完就忘。
        派生规则（人话，不带工程腔）：
        - ``executed``（放行）→ recent：「这类可以继续自主执行」
        - ``rejected``（拦下）→ recent：「这类事先问你」
        - ``never``（以后别提）→ permanent：「这类不要再提议」
        ``extra['user_note']`` 有用户批注时并入正文。

        fail-open：``add_lesson`` 自身已 fail-open；此处再包一层 try，
        写 lesson 失败绝不影响主裁决状态变更。
        """
        try:
            label = rec.action_type
            spec = get_spec(rec.action_type)
            if spec is not None and spec.label:
                label = spec.label

            if decision == TaskStatus.EXECUTED:
                act, rule, scope = "本次放行（你没拦）", "这类可以继续自主执行", "recent"
            elif decision == TaskStatus.REJECTED:
                act, rule, scope = "本次拦下", "这类事先问你", "recent"
            elif decision == TaskStatus.NEVER:
                act, rule, scope = "以后别提", "这类不要再提议", "permanent"
            else:
                return

            note = ""
            try:
                note = str((rec.extra or {}).get("user_note") or "").strip()
            except Exception:
                note = ""

            content = f"「{label}」：{act}"
            if note:
                content += f"（你说：{note}）"
            content += f" → {rule}"

            from deskmate.cognitive.store import CognitiveStore

            base_dir = str((rec.extra or {}).get("base_dir") or self.base_dir)
            store = CognitiveStore(db_path=os.path.join(base_dir, "deskmate.db"))
            store.add_lesson(content, scope=scope, source="adjudication")
        except Exception as e:  # noqa: BLE001 —— fail-open：留教失败不影响主裁决
            logger.warning("裁决留教失败（忽略）: %s", e)

    def propose_from_intent(self, intent: dict[str, Any]) -> Optional[Any]:
        """把一条 open 意图变成**提案卡**（R11）：她打算怎么做 → ``PROPOSED`` 待人审。

        **最高优先级护栏**：自生成意图的提案**一律保持 PROPOSED**，绝不自动执行——
        动作类型用独立 slug ``"intent"``（**不在原白名单**、未注册 → 天然落人审）；
        本方法只写提案卡，不碰 ``_run_low_risk`` 的自动执行逻辑。

        生成后：``link_intent_to_task`` 回填关联，意图置 ``active``（离开 open 池）。
        fail-open：任一步失败 → 返回 ``None``，不抛。
        """
        try:
            data = intent or {}
            iid = str(data.get("id") or "").strip()
            goal = str(data.get("goal") or "").strip()
            if not goal:
                return None
            how = str(data.get("motivation") or "").strip()
            origin = str(data.get("origin") or "").strip()
            rec = self.log.add(
                action_type="intent",
                risk="high",  # 自生成动作 → 一律高风险，必走人审
                status=TaskStatus.PROPOSED,
                topic=origin,
                summary=f"她想做：{goal}",
                detail=how,
                count=0,
                extra={"intent_id": iid, "how": how, "source": "intent", "origin": origin},
            )
            from deskmate.cognitive.store import CognitiveStore

            store = CognitiveStore(db_path=os.path.join(str(self.base_dir), "deskmate.db"))
            store.link_intent_to_task(iid, rec.id)
            store.set_intent_status(iid, "active")
            return rec
        except Exception as e:  # noqa: BLE001 —— fail-open
            logger.warning("propose_from_intent 失败（忽略）: %s", e)
            return None

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
