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
    ) -> TickResult:
        """执行一次 TICK。

        Args:
            user_message: 当前用户消息（如有）
            skip_activity_check: 跳过用户活跃度检查（调试用）

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

        # 组装感知上下文
        activity_desc = f"状态: {activity.level.value}, {activity.reason}"
        if focus is not None:
            activity_desc += (
                f"; 专注度: {focus.level} ({focus.score:.2f}，{focus.reason})"
            )
        ctx = build_proactive_context(
            workspace=self.workspace,
            memory_context=self._memory_context,
            user_activity=activity_desc,
            project_context=self._project_context,
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
        )

        if decision.action == ActionType.NO_ACTION:
            return TickResult(
                action=ActionType.NO_ACTION,
                judgment=judgment,
                activity=activity,
                decision=decision,
                skipped_reason=decision.reason,
                elapsed_ms=int((time.time() - start) * 1000),
                heartbeat_interval_ms=self._heartbeat_interval_ms,
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

        self.last_tick_time = time.time()

        return TickResult(
            action=decision.action,
            judgment=judgment,
            activity=activity,
            decision=decision,
            elapsed_ms=int((time.time() - start) * 1000),
            heartbeat_interval_ms=self._heartbeat_interval_ms,
        )

    def record_feedback(self, suggestion_key: str, accepted: bool) -> None:
        """记录用户反馈。

        同时上报成长系统：接受 → 信任/默契/心情上升；忽略 → 下降。
        """
        self.disturb.record_feedback(suggestion_key, accepted)
        try:
            from deskmate.growth.engine import GrowthEngine

            GrowthEngine(base_dir=self.base_dir).record_suggestion_feedback(accepted)
        except Exception as e:
            logger.debug("growth engine suggestion feedback record failed: %s", e)

    def set_silent(self, silent: bool) -> None:
        """设置静默模式。"""
        self.disturb.set_silent_mode(silent)
