"""Generic background sub-task delegation for any agent.

Owner ruling 2026-11-22: a long task must be able to hand a *self-contained
chunk of work* to a read-only subagent that runs in the background, then get the
result delivered back into the SAME session as a NEW message so the agent can
keep going. This is the generic form of the proven ``consult_experts`` shape
(dispatch-and-return → background → deliver_to_session), but:

* available to every agent, not only 小桔;
* the unit of work is an arbitrary sub-task (research / verify / draft a chunk),
  not one of six fixed literary experts;
* execution rides the shared :class:`JobHost` (checkpoint + crash recovery +
  global lease) instead of a hand-rolled daemon thread.

Safety: the child engine is a read-only explorer (PLAN mode, no write/shell/
ask_user/save_skill). A delegated chunk can never publish or modify the user's
manuscript — that stays with the main agent.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

SOURCE = "delegation"
_STEP_ID = "subtask"
_MIN_TASK_CHARS = 8

# Callback: (session_id, message, job_id) -> None. Wired by the manager to
# deliver_to_session (切回 loop), by tests to an in-memory recorder.
Deliver = Callable[[str, str, str], None]
# Callback: (task, job_id) -> result. Runs inside the background handler.
RunSubtask = Callable[[str, str], Any]


def _fingerprint(task: str) -> str:
    """Normalize whitespace/case so the same sub-task isn't dispatched twice."""
    norm = re.sub(r"\s+", " ", str(task or "")).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if any(k in result for k in ("report", "conclusion", "text")):
            report = result.get("report") or result.get("conclusion") or result.get("text")
            if isinstance(report, str):
                # A blank report is empty, not content: returning the dict as
                # JSON here would defeat the notifier's empty-result guard.
                return report.strip()
            return "" if report is None else str(report)
        # Structured-but-unknown: keep it readable rather than dumping repr noise.
        try:
            import json

            return json.dumps(result, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return str(result)
    return "" if result is None else str(result)


def _make_result_notifier(store: Any, deliver: Deliver, session_id: str = "") -> Callable:
    """Build an idempotent 'finished' notifier.

    Delivery idempotency uses the orchestration side-effect ledger: the key is
    derived from the job id, so a replayed notification (tick fallback, restart
    reconciliation, duplicate callback) can never deliver the same result twice.

    The target session is read from the job's own ``meta['session_id']`` (not a
    builder closure), so many sessions sharing one host never cross-deliver.
    ``session_id`` is only a fallback for jobs created without that meta.
    """

    def notify(kind: str, payload: dict[str, Any]) -> None:
        if kind != "finished":
            return
        job_id = str(payload.get("id") or "")
        if not job_id:
            return
        key = f"delegate-delivery:{job_id}"
        try:
            claim = store.begin_side_effect(key, job_id=job_id, step_id=_STEP_ID)
        except Exception:  # noqa: BLE001 —— ledger unavailable: still try once
            claim = {"existing": False}
        # Only a record whose status is "ok" means delivered. A "failed"
        # record (deliver raised, job vanished, empty result, no session)
        # must NOT block the retry: the old check skipped on any existing
        # record, so one failed delivery lost the result forever (2026-11-22).
        # A "running" record means a duplicate notification arrived while a
        # delivery is in flight; proceeding would double-deliver, so the
        # runner's pre-check semantics apply here too: only "ok" reuses.
        if claim.get("existing") and claim.get("status") == "ok":
            logger.debug("delegated result %s already delivered", job_id)
            return
        if claim.get("existing") and claim.get("status") == "running":
            # A concurrent duplicate notification while a delivery is in
            # flight: skip, or the result would be delivered twice. But a
            # "running" record older than 10 minutes is a stale leftover of a
            # delivery attempt that died mid-flight (delivery itself is one
            # function call) — retry instead of losing the result forever.
            stale = (time.time() - float(claim.get("updated_at") or 0)) > 600
            if not stale:
                logger.debug("delegated result %s delivery already in flight", job_id)
                return
        try:
            job = store.get_job(job_id)
            if job is None:
                store.finish_side_effect(key, status="failed", result={"error": "job missing"})
                return
            run = job.runs.get(_STEP_ID)
            if run is None or run.status.value != "ok":
                store.finish_side_effect(key, status="failed", result={"error": "step not ok"})
                return
            text = _result_text(run.result)
            if not text.strip():
                store.finish_side_effect(key, status="failed", result={"error": "empty result"})
                return
            target_sid = str(job.meta.get("session_id") or session_id or "")
            if not target_sid:
                store.finish_side_effect(key, status="failed", result={"error": "no session"})
                return
            message = (
                "【后台子任务已完成】\n"
                f"任务：{job.title}\n\n"
                f"{text}\n\n"
                "（这是后台跑完回灌的结果，请用自己的话判断如何向用户汇报。）"
            )
            deliver(target_sid, message, job_id)
            store.finish_side_effect(key, status="ok", result={"delivered": True})
        except Exception:  # noqa: BLE001 —— 通知失败不能拖垮已完成的 job
            logger.warning("delegated result delivery failed for %s", job_id, exc_info=True)
            try:
                store.finish_side_effect(key, status="failed", result={"error": "delivery failed"})
            except Exception:  # noqa: BLE001
                pass

    return notify


def delegate_builder(
    *,
    host: Any,
    run_subtask: RunSubtask,
    session_id: str = "",
    deliver: Optional[Deliver] = None,
    max_inflight: int = 4,
) -> Callable[[], Callable[[str], str]]:
    """Return a factory that builds a ``delegate_task`` tool closure.

    ``run_subtask(task, job_id)`` executes one chunk in the background handler —
    wired by the manager to a fresh read-only child engine. ``deliver`` is the
   回灌 callback (defaults to the host notifier wired at construction time).
    """
    store = host.store
    # Install the result notifier ONCE per host. It routes by each job's own
    # meta['session_id'], so multiple sessions sharing this host are safe. Guard
    # with an attribute so rebuilding engines for the same host doesn't stack
    # duplicate notifiers (which would double-deliver).
    # Assumption: the whole process passes ONE deliver (manager._delegate_deliver,
    # which routes by session_id). If a future caller passes a session-bound
    # deliver, the second install is silently dropped and that session's results
    # would go to the first deliver — register notifiers per session then.
    if deliver is not None and not getattr(host, "_delegate_notify_installed", False):
        result_notify = _make_result_notifier(store, deliver, session_id=session_id)
        base_notifier = getattr(host, "_notifier", None)
        if base_notifier is not None:
            def combined(kind: str, payload: dict[str, Any]) -> None:
                result_notify(kind, payload)
                base_notifier(kind, payload)
            host._notifier = combined
        else:
            host._notifier = result_notify
        host._delegate_notify_installed = True

    def build() -> Callable[[str], str]:
        def delegate_task(task: str) -> str:
            """把一段独立的工作派到后台只读子-agent 去做，立即返回。

            task: 自包含的子任务描述——要查/核/读什么，材料在哪，最终要什么产出。
            适合：多路并行查证、核对引文、读几处材料后给结论、独立成块的分析。
            后台做完会把结果作为一条新消息投回本会话，届时再向用户汇报。
            同一件事不要重复派；后台子-agent 只读，不会改稿或发布。
            """
            clean = re.sub(r"\s+", " ", str(task or "")).strip()
            if len(clean) < _MIN_TASK_CHARS:
                return f"子任务太短（至少 {_MIN_TASK_CHARS} 个字）：写清要查/核什么、材料在哪、要什么产出。"

            # Dedup: same normalized task already dispatched → refuse.
            # The claim lives in the side-effect ledger under the task
            # fingerprint (O(1) lookup), not a scan of the newest 200 jobs:
            # a long-running task used to slip past the scan once 200 newer
            # jobs had piled up behind it. The claim points at the latest
            # dispatched job; only a still pending/running job blocks a
            # re-dispatch — a finished job's claim is re-pointed at the new
            # job (see store.reassign_side_effect).
            fp = _fingerprint(clean)

            from coworker.orchestration import Job, JobStatus, StepKind, StepSpec

            job = Job(
                title=clean[:120],
                source=SOURCE,
                steps=[
                    StepSpec(
                        id=_STEP_ID,
                        title=clean[:120],
                        kind=StepKind.READ,
                        meta={"task": clean, "task_fp": fp, "session_id": session_id},
                    )
                ],
                meta={"task_fp": fp, "session_id": session_id},
            )
            status = job.status.value

            dispatch_key = f"delegate-dispatch:{fp}"
            try:
                claim = store.begin_side_effect(
                    dispatch_key, job_id=job.id, step_id=_STEP_ID
                )
            except Exception:  # noqa: BLE001 —— ledger unavailable: fall back to no dedup
                claim = {"existing": False}
            if claim.get("existing"):
                old = store.get_job(str(claim.get("job_id") or ""))
                if old is not None and old.status.value in ("pending", "running"):
                    return "这件子任务已经在后台跑了，别重复派；等结果回来再说。"
                if old is None:
                    # Claim exists but its job was never persisted: the other
                    # dispatch is between claim and submit (or died there).
                    # Refuse while the claim is fresh; only a stale claim
                    # (older than a minute) is treated as abandoned.
                    age = time.time() - float(claim.get("updated_at") or 0)
                    if age < 60:
                        return "这件子任务已经在后台跑了，别重复派；等结果回来再说。"
                # Previous claim finished or vanished: re-point the key at
                # this job, or the next duplicate dispatch would still read
                # the stale job and double-dispatch.
                try:
                    store.reassign_side_effect(
                        dispatch_key, job_id=job.id, step_id=_STEP_ID
                    )
                except Exception:  # noqa: BLE001 —— re-point failed: dedup may miss once
                    pass

            def handler(spec, _job, _ctx):
                return run_subtask(clean, job.id)

            try:
                job_id = host.submit(job, handler)
            except RuntimeError as exc:
                # Free the dispatch claim: the job never started, so a later
                # identical dispatch must not be refused as "already running".
                try:
                    store.finish_side_effect(
                        dispatch_key, status="failed", result={"error": str(exc)}
                    )
                except Exception:  # noqa: BLE001
                    pass
                return f"现在后台派发槽位满了或重复：{exc}。等一个子任务回来再派。"

            return (
                f"【已派出后台子任务】{job_id}：{clean[:60]}\n"
                "**不要等结果、不要重复派**：先把派了什么告诉用户，继续你手上的事。"
                "结果回来时会作为一条新消息投进本会话，届时再判断如何汇报。"
            )

        return delegate_task

    return build


def collect_subtask_report(engine: Any, task: str) -> Any:
    """Run one delegated sub-task engine to completion and collect its report.

    ``engine.run`` is an async generator; the background handler has no running
    loop, so we drive it with ``asyncio.run`` (same shape as the explore tool).
    The returned dict becomes the delegation job's result.
    """
    import asyncio

    from coworker.engine import EventType as ET

    async def _collect():
        report = ""
        status = "unknown"
        async for event in engine.run(task):
            data = getattr(event, "data", None) or {}
            etype = getattr(event, "type", None)
            if etype == ET.ASSISTANT_MESSAGE and data.get("text"):
                report = data["text"]
            elif etype == ET.TURN_END:
                status = data.get("status", "unknown")
            elif etype == ET.ERROR:
                return "", f"error: {data.get('error', 'unknown error')}"
        return report, status

    report, status = asyncio.run(_collect())
    if status.startswith("error:"):
        return {"error": status.split(":", 1)[1].strip()}
    if not report:
        return {"error": f"子任务没有产出报告（状态：{status}）"}
    out: dict[str, Any] = {"report": report}
    if status != "completed":
        out["note"] = f"提前结束（{status}），报告可能不完整"
    return out


def build_delegate_engine(
    *,
    provider: Any,
    model: str,
    workspace: str,
    max_iterations: int = 12,
    model_settings: Optional[dict[str, Any]] = None,
) -> Any:
    """A read-only child engine that executes one delegated sub-task.

    Reuses the code explorer engine (PLAN mode, read-only toolkit, no approval
    round-trip), then defensively strips any write/ask/skill tool in case the
    toolkit surface changes upstream. The child can research/verify/draft text
    in its report but can never mutate the workspace or publish anything.
    """
    from coworker.tools.subagent import build_explorer_engine

    engine = build_explorer_engine(
        workspace=workspace,
        provider=provider,
        model=model,
        model_settings=model_settings,
        max_iterations=max_iterations,
    )
    for name in ("ask_user", "save_skill", "write_file", "run_shell", "apply_patch", "edit_file"):
        engine.registry._tools.pop(name, None)
    return engine


DELEGATE_GUIDANCE = """
## 后台子任务分派（delegate_task）
你在跑长任务时，如果一块工作可以独立做、需要多路并行核验、或你对结论没把握，
就用 delegate_task 把它派到后台只读子-agent，立即继续手上的事，不要自己空等。
- 适合派：跨多处查证/核引文、读几份材料后给结论、独立成块的分析或草稿思路。
- 任务描述要自包含：说清查什么、材料在哪、要什么产出；子-agent 看不到本会话历史。
- 同一件事不要重复派；子-agent 只读，不改稿、不发布、不替你做决定。
- 结果会作为一条新消息投回本会话，届时再判断如何向用户汇报。
"""
