"""TICK 循环 — 感知→思考→行动→学习的主动推理主引擎。

每 tick 调用一次，收集桌面状态 + 记忆上下文，判断是否需要主动介入。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Optional

from .confidence import (
    ActionDecision,
    ActionType,
    THRESHOLD_PREPARE,
    THRESHOLD_SUGGEST,
    evaluate_action,
)
from .context_builder import (
    ProactiveContext,
    build_proactive_context,
    format_context_for_judge,
)
from .disturb import DisturbManager
from .judge import JudgmentResult, judge_proactive_action
from .user_active import ActivityState, UserActivity, detect_user_activity


@dataclass
class TickResult:
    """一次 TICK 的结果。"""
    action: ActionType
    judgment: JudgmentResult | None = None
    activity: ActivityState | None = None
    decision: ActionDecision | None = None
    skipped_reason: str = ""
    elapsed_ms: int = 0
    heartbeat_interval_ms: int | None = None
    # P2-4：「看见你在做什么」活动描述 + 长时间专注陪伴语
    what_user_doing: str = ""
    companion_message: str = ""
    # ③b 深度访谈触发负载（session_id + questions）；前端收到后弹访谈 UI
    interview: Optional[dict] = None
    # 认知验证（Turing test）情境负载：前端收到后弹验证卡（AI 代答 vs 用户答）
    validation: Optional[dict] = None
    # 日记灵感负载：素材攒够时小桔自己动笔（后台动作，不占 SUGGEST 通道）
    diary: Optional[dict] = None
    # 花园花开结算负载：后台把新事件开成花（不打扰；前端可轻声提示一句）
    garden: Optional[dict] = None

    @property
    def has_suggestion(self) -> bool:
        return self.action == ActionType.SUGGEST and self.judgment is not None

    def to_dict(self) -> dict:
        """序列化为 MCP 工具返回的字典。"""
        if self.action == ActionType.NO_ACTION:
            result = {
                "action": "no_action",
                "reason": self.skipped_reason or "无需介入",
            }
            # 前端 useProactiveTick 依赖 user_activity 判断桌宠状态
            # （active->focused, busy->concerned, idle->离开电脑）。
            # no_action 提前 return 时也必须带上，否则前端永远走 else 分支。
            if self.activity:
                result["user_activity"] = self.activity.level.value
        else:
            result = {
                "action": self.action.value,
                "confidence": self.decision.confidence if self.decision else 0.0,
            }
            if self.judgment:
                result.update({
                    "suggestion_type": self.judgment.suggestion_type,
                    "suggestion_content": self.judgment.suggestion_content,
                    "reasoning": self.judgment.reasoning,
                })
            if self.activity:
                result["user_activity"] = self.activity.level.value

        # 心跳间隔（活力调制）：前端据此重新调度慢心跳
        if self.heartbeat_interval_ms:
            result["heartbeat_interval_ms"] = self.heartbeat_interval_ms

        # P2-4：「看见你在做什么」活动描述（显式设置优先，否则从 activity 自动生成）
        if self.what_user_doing:
            result["what_user_doing"] = self.what_user_doing
        elif self.activity:
            try:
                from .companion import describe_activity
                result["what_user_doing"] = describe_activity(None, self.activity)
            except Exception:
                pass

        # P2-4：长时间专注陪伴语（前端走 CareToast 温暖呈现）
        if self.companion_message:
            result["companion_message"] = self.companion_message

        # ③b 深度访谈触发负载：前端 useProactiveTick 据此 dispatch 访谈事件
        if self.interview:
            result["interview"] = self.interview

        # 认知验证（Turing test）情境负载：前端据此弹验证卡
        if self.validation:
            result["validation"] = self.validation

        # 日记灵感：小桔自己动笔（不是打扰型弹窗，前端可轻声提示一句）
        if self.diary:
            result["diary"] = self.diary

        # 花园花开结算：后台动作（不打扰），前端可轻声提示一句
        if self.garden:
            result["garden"] = self.garden

        return result


@dataclass
class ProactiveEngine:
    """主动推理引擎。

    每次 tick 执行完整的 4A 循环：
    1. 感知（Perceive）：收集桌面状态
    2. 思考（Think）：LLM 推理判断
    3. 行动（Act）：根据置信度决定行动
    4. 学习（Learn）：记录反馈，更新防打扰规则
    """
    workspace: str | None = None
    model: str | None = None
    base_dir: Path | None = None

    # 子系统
    disturb: DisturbManager = field(default_factory=DisturbManager)
    last_tick_time: float = 0.0
    _memory_context: str = ""
    _project_context: str = ""
    _growth_interval_ratio: float = 1.0
    _heartbeat_interval_ms: int | None = None

    def set_memory_context(self, memory_context: str) -> None:
        """设置记忆上下文（由 MCP 工具调用者提供）。"""
        self._memory_context = memory_context

    def set_project_context(self, project_context: str) -> None:
        """设置项目上下文（由 MCP 工具调用者提供）。"""
        self._project_context = project_context

    def tick(
        self,
        user_message: str = "",
        skip_activity_check: bool = False,
        allow_interactive: bool = True,
    ) -> TickResult:
        """执行一次 TICK。

        Args:
            user_message: 当前用户消息（如有）
            skip_activity_check: 跳过用户活跃度检查（调试用）
            allow_interactive: 是否允许发起「需要用户在前端浮层回应」的交互
                （深度访谈 / 认知验证卡）。**后台 scheduler tick 必须传 False**：
                它的产物只有一条只认 suggestion 的 WS 广播，这两类负载没有出口，
                建了 session 却送不到用户眼前，还会把 3 天冷却白吃掉 → 前端永远
                收不到邀请（2026-09-22 定位的静默死循环）。默认 True 供前端路由
                （HTTP 响应能直达 useProactiveTick）与手动入口使用。

        Returns:
            TickResult
        """
        start = time.time()
        # 照顾循环 + 成长调制（每次 tick 先让时间流逝生效）：
        # - tick_decay: 活力缓慢恢复 / 心情向中性收敛
        # - 信任感 → 阈值偏移；活力 → 心跳间隔（返回给前端重新调度）
        try:
            from deskmate.growth.engine import GrowthEngine

            growth = GrowthEngine(base_dir=self.base_dir)
            growth.tick_decay()
            suggest_thr = growth.modulate_proactive_threshold(THRESHOLD_SUGGEST)
            prepare_thr = growth.modulate_proactive_threshold(THRESHOLD_PREPARE)
            self._growth_interval_ratio = growth.modulate_proactive_frequency(1.0)
        except Exception:
            suggest_thr = THRESHOLD_SUGGEST
            prepare_thr = THRESHOLD_PREPARE
            self._growth_interval_ratio = 1.0
        self._heartbeat_interval_ms = int(5 * 60_000 * self._growth_interval_ratio)

        # ===== 1.6 日记灵感（后台动作，不打扰、不占通道）=====
        # 与深度访谈同一范式：纯规则、零 LLM、条件满足才发生。
        # 差异在于日记**不需要用户在场**——她是自己动笔，不是来打断你。
        # 所以放在 idle 判断之前：素材攒够了就安静写下来，返回里带一句轻提示。
        # 没攒够则什么都不做（绝不为了「今天还没写」而凑一篇流水账）。
        diary_payload = None
        try:
            from deskmate.growth.diary_inspiration import maybe_write_diary

            diary_payload = maybe_write_diary(base_dir=self.base_dir)
        except Exception:
            diary_payload = None  # 日记失败绝不阻断主动推理主流程

        # ===== 1.6.2 花园花开结算（后台动作，不打扰、不占通道）=====
        # 与日记同一范式：纯规则、零 LLM、安静把新事件开成花。
        # 结算结果直接写库即生效，前端进花园时自取（/garden、/garden/flowers）；
        # 不通过 tick 返回值推送（前端不消费该字段，避免冗余传递）。
        try:
            from deskmate.garden.clock import maybe_settle_garden

            maybe_settle_garden(base_dir=self.base_dir)
        except Exception:
            logger.debug("花园结算失败，跳过（不阻断主流程）", exc_info=True)

        # ===== 1. 感知（Perceive）=====
        # 先获取活动窗口信息，传给 detect_user_activity 做会议应用检测
        from deskmate.perception.window_monitor import get_active_window
        _win = None
        try:
            _win = get_active_window()
            activity = detect_user_activity(
                app_name=_win.app, window_title=_win.title,
            )
        except Exception:
            activity = detect_user_activity()

        # P1-1：专注度合成（深专注时不打扰）——纯规则零 LLM，fail-open
        focus = None
        if _win is not None:
            try:
                from deskmate.perception.focus import get_focus_score

                focus = get_focus_score(_win.app, _win.title, _win.url)
            except Exception:
                focus = None

        if not skip_activity_check:
            if activity.level == UserActivity.IDLE:
                # 用户离开电脑：不弹建议（不打扰），但返回 user_activity 让前端显示 idle 状态
                return TickResult(
                    action=ActionType.NO_ACTION,
                    activity=activity,
                    skipped_reason=f"用户不在桌前（空闲 {activity.idle_seconds // 60} 分钟）",
                    elapsed_ms=int((time.time() - start) * 1000),
                    heartbeat_interval_ms=self._heartbeat_interval_ms,
                )
            # BUSY 状态（会议/通话）不跳过推理：
            # 用户在会议中也可能需要提醒（如伏笔遗漏），只是防打扰阈值更严

        # ===== 1.5 深度访谈 / 认知验证触发（③b 自动发起：纯规则，不占 LLM judge）=====
        # 触发条件见 should_interview_now：open_gaps>=2 且距上次访谈>=3 天。
        # idle 已在上方 return；busy（会议/通话）与深专注不弹——访谈需要用户专注回应。
        #
        # ⚠ allow_interactive（2026-09-22 修）：访谈与验证卡都是「要在浮层里点/答」
        # 的交互，只有**有 UI 呈现能力的通道**才该发起。后台 scheduler tick 的产物
        # 只有一条 WS 广播，而 _broadcast_deskmate_suggestion 只认 judgment——
        # interview / validation 根本没有出口。它建了 session、消耗了 3 天冷却，
        # 用户却什么都看不到；前端 tick 随后永远撞冷却 → 邀请再也送不出去
        # （真实库曾积压 12 个无人见过的 active 会话）。
        # 故后台传 allow_interactive=False（不建、不吃冷却），把发起权留给前端。
        # 维护类工作（情感增量 / 表达回灌 / 冲突扫描 / gap 播种）不需要交互，
        # 后台照做——所以不能把整块一起跳过。
        try:
            if activity.level != UserActivity.BUSY and not (focus is not None and focus.level == "deep"):
                from deskmate.cognitive.interview import maybe_trigger_interview
                from deskmate.cognitive.emotion_profile import ensure_emotional_gaps

                # (a) 认知访谈优先（需前端弹卡）
                if allow_interactive:
                    iv = maybe_trigger_interview(limit=3, base_dir=self.base_dir)
                    if iv and iv.get("session_id") and iv.get("questions"):
                        return TickResult(
                            action=ActionType.SUGGEST,
                            activity=activity,
                            interview=iv,
                            skipped_reason="深度访谈待回应（基于认知盲区积累）",
                            elapsed_ms=int((time.time() - start) * 1000),
                            heartbeat_interval_ms=self._heartbeat_interval_ms,
                        )
                # (b) 维护类：无需用户交互，后台也照做
                # 日记→情感画像增量提取（轻量规则，幂等；节流 ~4h，fail-open 不阻塞 tick）
                try:
                    from deskmate.cognitive.emotion_extract import maybe_extract_on_tick
                    maybe_extract_on_tick(interval_hours=4)
                except Exception:
                    logger.debug("情感画像增量提取失败，跳过", exc_info=True)
                # 表达源回灌（F2）：把访谈回答、聊天里说过的话接进情感层与认知层。
                # 此前两层的唯一真实输入源都是那 15 篇日记，learned_history 227 条
                # 一条没用上。放在冲突扫描之前：新进来的表达当轮就能参与检测。
                # 节流 4h、幂等、fail-open。
                try:
                    from deskmate.cognitive.expression_ingest import maybe_ingest_on_tick
                    maybe_ingest_on_tick(interval_hours=4, days=7, limit=5)
                except Exception:
                    logger.debug("表达源回灌失败，跳过", exc_info=True)
                # 冲突驱动的 gap 再生（方案 C）：出现与既有画像**相反**的信号时才生 gap。
                # 为什么需要：gap 是存量消耗品（ensure_emotional_gaps 只补一次），答完即枯竭
                # → open_gaps()=0 → should_interview_now 恒 False → 小桔再也不提问（静默死亡）。
                # 为什么放这里：必须在下面的 maybe_trigger_interview **之前**，
                # 新生成的冲突 gap 当轮即可被情感访谈消费，形成闭环；节流 6h，fail-open。
                # ⚠ 扫描范围已不止日记（F3）：只扫日记时再生形同虚设，
                # 他访谈里答的、聊天里说的同样能推翻画像。
                try:
                    from deskmate.cognitive.conflict_gap import maybe_scan_conflicts_on_tick
                    maybe_scan_conflicts_on_tick(interval_hours=6)
                except Exception:
                    logger.debug("冲突 gap 扫描失败，跳过", exc_info=True)
                # 认知访谈无内容时，尝试情感访谈（情感画像缺口：关怀偏好/回血方式待澄清）
                ensure_emotional_gaps(base_dir=self.base_dir)
                # (c) 情感访谈 + 认知验证：同样要前端弹卡，后台不发起
                if allow_interactive:
                    iv_emo = maybe_trigger_interview(limit=3, layer="emotional", base_dir=self.base_dir)
                    if iv_emo and iv_emo.get("session_id") and iv_emo.get("questions"):
                        return TickResult(
                            action=ActionType.SUGGEST,
                            activity=activity,
                            interview=iv_emo,
                            skipped_reason="情感访谈待回应（基于情感画像缺口）",
                            elapsed_ms=int((time.time() - start) * 1000),
                            heartbeat_interval_ms=self._heartbeat_interval_ms,
                        )
                    # 认知验证（Turing test）：访谈都无内容时，生成一个验证情境，
                    # 让 AI 以用户身份回答 vs 用户回答，校准"像我度"。
                    # 节流 ~3 天（与访谈同节奏），只在能生成情境时触发。
                    try:
                        from datetime import datetime, timedelta
                        from pathlib import Path as _Path

                        from deskmate.cognitive.store import CognitiveStore
                        from deskmate.cognitive.validation_engine import run_validation
                        # 按 base_dir 解析库（与 resolve_profile_manager 同口径）：
                        # 否则节流时间戳落全局库，多 workspace 会互相压制。
                        store = CognitiveStore(db_path=_Path(self.base_dir) / "deskmate.db")
                        last = store.get_meta("last_validation_at")
                        due = True
                        if last:
                            try:
                                last_dt = datetime.fromisoformat(str(last))
                                due = (datetime.now() - last_dt) >= timedelta(days=3)
                            except Exception:
                                due = True
                        if due:
                            v = run_validation()
                            if v["scenario"].get("id"):
                                # 记录验证发起时间，避免每次心跳都弹
                                store.set_meta("last_validation_at", datetime.now().isoformat())
                                return TickResult(
                                    action=ActionType.SUGGEST,
                                    activity=activity,
                                    interview=None,
                                    validation=v["scenario"],
                                    skipped_reason="认知验证情境待回应（Turing test 校准像我度）",
                                    elapsed_ms=int((time.time() - start) * 1000),
                                    heartbeat_interval_ms=self._heartbeat_interval_ms,
                                )
                    except Exception:
                        logger.debug("认知验证情境生成失败，跳过", exc_info=True)
        except Exception:
            logger.debug("访谈触发失败，跳过", exc_info=True)

        # 组装感知上下文
        activity_desc = f"状态: {activity.level.value}, {activity.reason}"
        if focus is not None:
            activity_desc += (
                f"; 专注度: {focus.level} ({focus.score:.2f}，{focus.reason})"
            )
            # P2-3：深专注进入行为事件日志（30 分钟去重），供「常在周X/某时段深度工作」模式提炼
            if focus.level == "deep":
                try:
                    from deskmate.proactivity.behavior_patterns import log_focus_deep

                    log_focus_deep(
                        app=(_win.app if _win else ""),
                        title=(_win.title if _win else ""),
                        base_dir=self.base_dir,
                    )
                except Exception:
                    pass
        # P1-2：注入行为偏好（用户反复接受的建议类型）→ judge 更懂该提什么
        pref_ctx = ""
        try:
            from deskmate.proactivity.preferences import PreferenceStore

            pref_ctx = PreferenceStore(base_dir=self.base_dir).to_context()
        except Exception:
            pref_ctx = ""
        # P2-3：注入从长期行为推断出的规律（非用户明说，纯行为轨迹统计）→ judge 更「懂节奏」
        behavior_ctx = ""
        try:
            from deskmate.proactivity.behavior_patterns import to_context

            behavior_ctx = to_context(base_dir=self.base_dir)
        except Exception:
            behavior_ctx = ""
        # P2-5：注入情感回顾（记得用户上周的状态）→ 连续情感关怀
        emotion_ctx = ""
        try:
            from deskmate.growth.emotion_timeline import EmotionTimeline
            from deskmate.growth.engine import GrowthEngine

            eng = GrowthEngine(base_dir=self.base_dir)
            et = EmotionTimeline(base_dir=self.base_dir)
            # 先补记前几天：他会补写日记，历史快照里那些靠行为推断的标签
            # 得让位给他自己写下的心情（只补没判过的，digest 相同不重复烧 token）
            try:
                et.backfill_from_diary(eng, days=14)
            except Exception as e:
                logger.debug("日记情感补记跳过: %s", e)
            et.record_today(eng)
            emotion_ctx = et.lookback(7)
        except Exception as e:
            # 这里原本是裸 except（连日志都没有）：情感层整条链路静默失效时，
            # 表面现象只是「小桔不再主动关怀」，无从查起。
            logger.warning("情感时间线不可用（已跳过情感回顾注入）: %s", e, exc_info=True)
            emotion_ctx = ""
        # 情感画像（layer='emotional'）：真实情绪触发点/回血方式/关怀偏好，供 judge 个性化关怀
        emotion_profile_ctx = ""
        try:
            from deskmate.cognitive.emotion_profile import emotional_context

            emotion_profile_ctx = emotional_context()
        except Exception:
            emotion_profile_ctx = ""
        # 「她观察到的世界」：把小桔主动摄入的外部观察回灌主动推理上下文，
        # 让 L2 主动决策（judge）也能依据她看到的世界判断要不要介入（2026-09-06）。
        # fail-open：取不到就当空，绝不拖累 tick。
        world_ctx = ""
        try:
            from deskmate.memory.world_context import build_world_context

            world_ctx = build_world_context(self.base_dir)
        except Exception:
            world_ctx = ""
        mem_ctx = self._memory_context
        if world_ctx:
            mem_ctx = (mem_ctx + "\n\n" + world_ctx).strip() if mem_ctx else world_ctx
        ctx = build_proactive_context(
            workspace=self.workspace,
            memory_context=mem_ctx,
            user_activity=activity_desc,
            project_context=self._project_context,
        )
        extra_ctx = ""
        if pref_ctx:
            extra_ctx += f"\n## 用户行为偏好\n{pref_ctx}"
        if behavior_ctx:
            extra_ctx += f"\n## 长期行为模式（从行为推断）\n{behavior_ctx}"
        if emotion_ctx:
            extra_ctx += f"\n## 情感回顾（连续关怀）\n{emotion_ctx}"
        if emotion_profile_ctx:
            extra_ctx += f"\n## 情感画像（他亲口说过的情绪，关怀请贴合）\n{emotion_profile_ctx}"
        if extra_ctx:
            ctx = ProactiveContext(
                perception=ctx.perception,
                memory_context=ctx.memory_context + extra_ctx,
                user_activity=ctx.user_activity,
                project_context=ctx.project_context,
                bible=ctx.bible,
            )

        context_text = format_context_for_judge(ctx)

        # 如果上下文太短（没有足够信息），跳过
        if len(context_text.strip()) < 50:
            return TickResult(
                action=ActionType.NO_ACTION,
                activity=activity,
                skipped_reason="感知信息不足，跳过推理",
                elapsed_ms=int((time.time() - start) * 1000),
                heartbeat_interval_ms=self._heartbeat_interval_ms,
            )

        # ===== 2. 思考（Think）=====
        judgment = judge_proactive_action(context_text, model=self.model)

        # ===== 3. 行动（Act）=====
        # 信任感调制阈值已在开头算好（trust 高 → 阈值下调 → 更敢主动）
        decision = evaluate_action(
            confidence=judgment.confidence,
            reason=judgment.reasoning,
            suggest_threshold=suggest_thr,
            prepare_threshold=prepare_thr,
            suggestion_type=judgment.suggestion_type,
        )

        # P2-4：「看见你在做什么」活动描述 + 长时间专注陪伴语
        # 必须在 NO_ACTION 提前 return 之前计算，否则 get_companion_tracker().update()
        # 永不被调用：专注计时器无法推进，「工作累了来提醒」永远不触发（历史回归，已修）。
        what_doing = ""
        companion_msg = ""
        try:
            from .companion import describe_activity, get_companion_tracker
            what_doing = describe_activity(_win, activity)
            # 仅当 judge 决定不介入时，才用陪伴语填补（不抢建议的风头）
            if decision.action == ActionType.NO_ACTION:
                companion_msg = get_companion_tracker().update(activity, _win) or ""
        except Exception:
            pass

        if decision.action == ActionType.NO_ACTION:
            return TickResult(
                action=ActionType.NO_ACTION,
                judgment=judgment,
                activity=activity,
                decision=decision,
                skipped_reason=decision.reason,
                elapsed_ms=int((time.time() - start) * 1000),
                heartbeat_interval_ms=self._heartbeat_interval_ms,
                what_user_doing=what_doing,
                companion_message=companion_msg,
                diary=diary_payload,
            )

        if decision.action == ActionType.SUGGEST:
            # P1-1：深专注时不弹建议（只静默记录，打扰分寸升级）
            if focus is not None and focus.level == "deep":
                return TickResult(
                    action=ActionType.NO_ACTION,
                    judgment=judgment,
                    activity=activity,
                    decision=decision,
                    skipped_reason=f"深专注（{focus.reason}）不打扰，静默记录",
                    elapsed_ms=int((time.time() - start) * 1000),
                    heartbeat_interval_ms=self._heartbeat_interval_ms,
                )
            # 防打扰检查
            suggestion_key = judgment.suggestion_content[:100]
            if not self.disturb.should_allow(judgment.suggestion_type, suggestion_key):
                return TickResult(
                    action=ActionType.NO_ACTION,
                    judgment=judgment,
                    activity=activity,
                    decision=decision,
                    skipped_reason="防打扰机制拦截",
                    elapsed_ms=int((time.time() - start) * 1000),
                    heartbeat_interval_ms=self._heartbeat_interval_ms,
                )
            # 记录建议
            self.disturb.record_suggestion(
                judgment.suggestion_type,
                suggestion_key,
            )
            # P2-10：主动建议推送 → 行为日志（供「常在何时被建议/哪类建议」规律积累）
            # 2026-09-22：按小时抽样（同小时同类只留一条）。它曾占日志 94%
            # （13197/14069 条）却提炼不出任何模式 → 纯存储噪声，见 log_event docstring。
            try:
                from deskmate.proactivity.behavior_patterns import log_event
                log_event("suggestion_pushed",
                          {"type": judgment.suggestion_type, "key": suggestion_key},
                          dedup_per_hour=True)
            except Exception:
                pass

        self.last_tick_time = time.time()

        # P2-4：「看见你在做什么」活动描述 + 长时间专注陪伴语
        what_doing = ""
        companion_msg = ""
        try:
            from .companion import describe_activity, get_companion_tracker
            what_doing = describe_activity(_win, activity)
            # 仅当 judge 决定不介入时，才用陪伴语填补（不抢建议的风头）
            if decision.action == ActionType.NO_ACTION:
                companion_msg = get_companion_tracker().update(activity, _win) or ""
        except Exception:
            pass

        return TickResult(
            action=decision.action,
            judgment=judgment,
            activity=activity,
            decision=decision,
            elapsed_ms=int((time.time() - start) * 1000),
            heartbeat_interval_ms=self._heartbeat_interval_ms,
            what_user_doing=what_doing,
            companion_message=companion_msg,
            diary=diary_payload,
        )

    def record_feedback(self, suggestion_key: str, accepted: bool) -> None:
        """记录用户反馈。

        同时上报成长系统：接受 → 信任/默契/心情上升；忽略 → 下降。
        被接受的建议沉淀为「行为偏好」（P1-2：主动本身越用越懂）。
        """
        self.disturb.record_feedback(suggestion_key, accepted)
        # P1-2：被接受 → 沉淀行为偏好（反查建议类型；不碰 memory manager）
        if accepted:
            try:
                from deskmate.proactivity.preferences import PreferenceStore

                typ = ""
                for rec in reversed(self.disturb.records):
                    if rec.suggestion_key == suggestion_key:
                        typ = rec.suggestion_type
                        break
                if typ:
                    PreferenceStore(base_dir=self.base_dir).record_accepted(
                        typ, suggestion_key
                    )
            except Exception as e:
                logger.debug("preference record failed: %s", e)
        try:
            from deskmate.growth.engine import GrowthEngine

            GrowthEngine(base_dir=self.base_dir).record_suggestion_feedback(accepted)
        except Exception as e:
            logger.debug("growth engine suggestion feedback record failed: %s", e)

    def set_silent(self, silent: bool) -> None:
        """设置静默模式。"""
        self.disturb.set_silent_mode(silent)
