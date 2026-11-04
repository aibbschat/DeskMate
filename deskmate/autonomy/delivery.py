"""自主任务结果送达 —— 把 ``task_runs`` 的完成结果接进 ``proactive_ledger``。

背景：定时任务跑完只在 ``broadcast_session`` 发一个 ``task_done``，而前端
没有任何消费者；窗口没开就彻底丢失，也没有 OS 通知。「做完了没人知道」是
历史「回执 ok 无实产」的镜像问题。本模块负责送达闭合四件套里的
「承载通道 + 凭证」，出口复用台账已有的开场念 / 信箱，不另造通道。

设计约束：
* 纯函数 + 显式 ``base_dir``，测试库放 /tmp，绝不写生产库；
* 成功与失败都投递，失败要说清卡在哪，不静默；
* 确定性 anchor 按 run_id 去重，``delivered_at`` 幂等，绝不重复打扰；
* 投递失败一律 fail-open（记日志返回空串），绝不拖垮任务本身。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from deskmate.proactivity.ledger import record_proactive

logger = logging.getLogger(__name__)

SOURCE = "autonomy"
KIND = "task_result"
_MAX_CONTENT = 280


def delivery_anchor(task_id: str, run_id: str) -> str:
    """一次任务运行对应一个稳定锚点。同 run 重复投递合并，不同 run 不串。"""
    return f"run:{run_id}"


def _summary(run: Any) -> str:
    text = (getattr(run, "result_text", "") or "").strip()
    err = (getattr(run, "error", "") or "").strip()
    status = getattr(run, "status", "") or ""
    if status == "error":
        return f"任务未完成：{err or text or '未知错误'}"[:_MAX_CONTENT]
    return text[:_MAX_CONTENT]


def build_completion_event(
    task: Any, run: Any, *, status: str, ledger_id: str = ""
) -> dict:
    """全局 ``/ws/events`` 的完成事件（与 ``automation_run_started`` 并列）。

    前端消费它做即时提示；即使窗口当时没开，结果仍已在台账里，开 App 后
    由开场念 / 信箱补达。
    """
    return {
        "type": "automation_run_finished",
        "data": {
            "task_id": getattr(task, "id", ""),
            "task_title": getattr(task, "title", ""),
            "run_id": getattr(run, "run_id", ""),
            "session_id": getattr(run, "session_id", ""),
            "status": status,
            "text": _summary(run),
            "ledger_id": ledger_id,
            "workspace": getattr(task, "workspace", ""),
            "agent": getattr(task, "agent", ""),
            "finished_at": getattr(run, "finished_at", None) or time.time(),
        },
    }


def build_ledger_payload(task: Any, run: Any) -> dict:
    """交给 ``record_proactive`` 的参数。confidence=1.0 且 bypass_threshold：
    任务结果是「实产」，不该被 0.75 的主动度门槛误拦；anchor 保证幂等。
    payload 只放结构化小字段，避免 oplog 每份复制全文放大同步成本。
    """
    status = getattr(run, "status", "") or ""
    content = _summary(run)
    evidence = (
        f"task={getattr(task, 'id', '')} run={getattr(run, 'run_id', '')} "
        f"status={status} session={getattr(run, 'session_id', '')}"
    )[:500]
    return {
        "source": SOURCE,
        "kind": KIND,
        "content": content,
        "evidence": evidence,
        "understanding": "",
        "anchor": delivery_anchor(
            getattr(task, "id", ""), getattr(run, "run_id", "")
        ),
        "payload": {
            "task_id": getattr(task, "id", ""),
            "run_id": getattr(run, "run_id", ""),
            "session_id": getattr(run, "session_id", ""),
            "status": status,
            "error": (getattr(run, "error", "") or "")[:200],
            "artifacts": list(getattr(run, "artifacts", []) or [])[:10],
        },
        "confidence": 1.0,
        "bypass_threshold": True,
    }


def deliver_run_result(
    task: Any, run: Any, *, base_dir: Optional[Path] = None
) -> str:
    """把一次 run 的结果落进 ``proactive_ledger``，返回台账 id。

    ``base_dir=None`` 时走真实 ``~/.deskmate``（生产）；测试必须显式传
    ``base_dir=tmp_path``。已投递过（``delivered_at`` 非空）直接短路。
    """
    anchor = delivery_anchor(
        getattr(task, "id", ""), getattr(run, "run_id", "")
    )
    checkpoint = getattr(run, "checkpoint", None)
    if getattr(run, "delivered_at", None) is not None:
        # Return the SAME ledger id as the first call — an anchor here would look
        # like a different row and make tracing a delivered run needlessly hard.
        if isinstance(checkpoint, dict):
            return checkpoint.get("delivery_id") or anchor
        return anchor
    kwargs = build_ledger_payload(task, run)
    if not kwargs["content"].strip():
        # 三层空内容守卫的源头：没有实产就不要制造一张空卡。
        return ""
    try:
        rid = record_proactive(base_dir=base_dir, **kwargs)
    except Exception:
        logger.warning("自主任务结果投递台账失败（fail-open）", exc_info=True)
        return ""
    if rid:
        try:
            run.delivered_at = time.time()
            if isinstance(checkpoint, dict):
                checkpoint["delivery_id"] = rid
        except Exception:
            pass
    return rid or ""
