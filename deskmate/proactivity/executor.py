"""主动闭环执行桥 — 把闭环决策安全地落地为「真正去做」或「提案等待」。

安全模型（对应 Hermes 主动闭环，但有桌搭式硬边界）：
- 只有决策满足「高置信度 + 明确可逆 + 风险 low/medium + 作用域在 workspace 内」才允许自动执行。
- 自动执行复用 coworker 的 AUTO 任务通路（已被权限引擎约束：写入路径作用域 + CRITICAL 拒杀）。
- 未注册执行器 / 不满足自动门槛 / 防打扰静默 → 降级为「提案」，写入 pending 文件，
  由 GUI 呈现「我注意到 X 还没同步，要我帮你做吗？」，绝不静默替用户做高风险动作。
- 全程受 DisturbManager 约束：静默模式拒绝一切；同类连续忽略降级。

执行器通过 ``register_close_loop_executor`` 由 coworker 启动时注册（提交一次性 AUTO 任务），
deskmate 侧不直接 import coworker，避免循环依赖。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .close_loop import CloseLoopDecision
from .disturb import DisturbManager

logger = logging.getLogger(__name__)

# 自动执行门槛
_AUTO_CONFIDENCE_MIN = 0.85
_AUTO_RISK_ALLOWED = ("low", "medium")

# 防打扰：复用 proactivity 的 DisturbManager（持久化到 ~/.deskmate/memory/disturb_state.json）
_BASE_DIR = Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
_DISTURB_PATH = _BASE_DIR / "memory" / "disturb_state.json"
_disturb = DisturbManager()
_disturb.persist(_DISTURB_PATH)

# 提案落盘（供 GUI 呈现 / 审计）
_PENDING_PATH = _BASE_DIR / "memory" / "close_loop_pending.jsonl"

# 全局执行器（coworker 启动时注册）
_executor_fn: Optional[Callable[[str, str], None]] = None
_auto_mode: bool = os.environ.get("DESKMATE_CLOSE_LOOP_AUTO", "1") != "0"


def register_close_loop_executor(fn: Callable[[str, str], None]) -> None:
    """由 coworker 注册真实执行器：接收 (instruction, workspace)，提交一次性 AUTO 任务。

    Args:
        fn: 签名为 (instruction: str, workspace: str) -> None，内部应异步提交，
            不阻塞调用线程（after_action 在 daemon 线程里调用本桥）。
    """
    global _executor_fn
    _executor_fn = fn
    logger.info("close_loop: 已注册真实执行器（自动闭环可执行）")


def set_auto_mode(on: bool) -> None:
    """运行期开关自动执行（默认开）。关闭后一律降级为提案。"""
    global _auto_mode
    _auto_mode = on


@dataclass
class ExecutionResult:
    executed: bool          # 已自动执行
    proposed: bool          # 已降级为提案（pending）
    skipped: bool           # 未达门槛 / 被防打扰拦截，啥也没做
    reason: str


def _is_auto_eligible(d: CloseLoopDecision) -> bool:
    """决策是否满足自动执行门槛。"""
    return (
        d.should_close_loop
        and d.confidence >= _AUTO_CONFIDENCE_MIN
        and d.reversible
        and d.risk_level in _AUTO_RISK_ALLOWED
    )


def _record_pending(d: CloseLoopDecision, workspace: str | None) -> None:
    """降级为提案：追加到 pending 文件（原子写），供 GUI 呈现。"""
    try:
        _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "action_summary": d.action_summary,
            "rationale": d.rationale,
            "confidence": d.confidence,
            "target_hint": d.target_hint,
            "reversible": d.reversible,
            "risk_level": d.risk_level,
            "workspace": workspace or "",
            "status": "proposed",
        }
        tmp = _PENDING_PATH.with_suffix(".jsonl.tmp")
        # 简单追加：先读现有再写回（pending 文件很小，且只在收尾时写，并发极低）
        lines = []
        if _PENDING_PATH.is_file():
            lines = _PENDING_PATH.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry, ensure_ascii=False))
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, _PENDING_PATH)
    except OSError as e:
        logger.warning("close_loop: 写 pending 失败 %s", e)


def try_execute_close_loop(
    decision: CloseLoopDecision,
    workspace: str | None = None,
) -> ExecutionResult:
    """尝试把闭环决策落地。

    流程：
    1. 防打扰前置检查（静默模式 / 同类冷却 / 连续忽略降级）。
    2. 满足自动门槛 + 已注册执行器 + 自动模式开 → 提交自动执行。
    3. 否则 → 降级为提案（pending）。
    """
    if not decision.should_close_loop or not decision.action_summary.strip():
        return ExecutionResult(False, False, True, "决策未建议闭环")

    # 1. 防打扰：用 "close_loop" 作为建议类型，key 用动作摘要去重
    key = decision.action_summary[:100]
    if not _disturb.should_allow("close_loop", key):
        return ExecutionResult(False, False, True, "防打扰机制拦截")

    # 2. 自动执行门槛
    if _is_auto_eligible(decision) and _executor_fn is not None and _auto_mode:
        try:
            _executor_fn(decision.action_summary, workspace or "")
            _disturb.record_suggestion("close_loop", key, accepted=None)
            logger.info("close_loop: 已自动提交执行 -> %s", decision.action_summary)
            return ExecutionResult(True, False, False, "已自动提交执行")
        except Exception as e:  # 执行器异常不致命，降级为提案
            logger.exception("close_loop: 自动执行失败，降级为提案：%s", e)

    # 3. 降级为提案
    _record_pending(decision, workspace)
    _disturb.record_suggestion("close_loop", key, accepted=None)
    logger.info("close_loop: 降级为提案 -> %s", decision.action_summary)
    return ExecutionResult(False, True, False, "已记录为提案，等待用户确认")



