"""TICK 循环 — 感知→思考→行动→学习的主动推理主引擎。

每 tick 调用一次，收集桌面状态 + 记忆上下文，判断是否需要主动介入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .confidence import ActionDecision, ActionType, evaluate_action
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
            return result

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
        # ===== 1. 感知（Perceive）=====
        # 先获取活动窗口信息，传给 detect_user_activity 做会议应用检测
        from deskmate.perception.window_monitor import get_active_window
        try:
            _win = get_active_window()
            activity = detect_user_activity(
                app_name=_win.app, window_title=_win.title,
            )
        except Exception:
            activity = detect_user_activity()

        if not skip_activity_check:
            if activity.level == UserActivity.IDLE:
                # 用户离开电脑：不弹建议（不打扰），但返回 user_activity 让前端显示 idle 状态
                return TickResult(
                    action=ActionType.NO_ACTION,
                    activity=activity,
                    skipped_reason=f"用户不在桌前（空闲 {activity.idle_seconds // 60} 分钟）",
                    elapsed_ms=int((time.time() - start) * 1000),
                )
            # BUSY 状态（会议/通话）不跳过推理：
            # 用户在会议中也可能需要提醒（如伏笔遗漏），只是防打扰阈值更严

        # 组装感知上下文
        ctx = build_proactive_context(
            workspace=self.workspace,
            memory_context=self._memory_context,
            user_activity=f"状态: {activity.level.value}, {activity.reason}",
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
            )

        # ===== 2. 思考（Think）=====
        judgment = judge_proactive_action(context_text, model=self.model)

        # ===== 3. 行动（Act）=====
        decision = evaluate_action(
            confidence=judgment.confidence,
            reason=judgment.reasoning,
        )

        if decision.action == ActionType.NO_ACTION:
            return TickResult(
                action=ActionType.NO_ACTION,
                judgment=judgment,
                activity=activity,
                decision=decision,
                skipped_reason=decision.reason,
                elapsed_ms=int((time.time() - start) * 1000),
            )

        if decision.action == ActionType.SUGGEST:
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
        )

    def record_feedback(self, suggestion_key: str, accepted: bool) -> None:
        """记录用户反馈。"""
        self.disturb.record_feedback(suggestion_key, accepted)

    def set_silent(self, silent: bool) -> None:
        """设置静默模式。"""
        self.disturb.set_silent_mode(silent)
