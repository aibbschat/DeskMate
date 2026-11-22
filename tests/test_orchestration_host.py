"""后台 Job 宿主：派发即返回 + 后台并行 + 崩溃接管 + 主路回收。

先红后绿：这些测试锁定「长任务断了能自愈」的真实行为，而不是
只验证 helper。宿主把 consult 的 daemon 线程模式泛化，并补上
进程重启后扫描 RUNNING job 自动 resume 的能力。
"""

from __future__ import annotations

import threading
import time

import pytest

from coworker.orchestration.host import JobHost
from coworker.orchestration.models import (
    Artifact,
    Job,
    JobStatus,
    StepKind,
    StepRun,
    StepSpec,
    StepStatus,
)
from coworker.orchestration.store import OrchestrationStore


def _job(jid: str, n: int = 3) -> Job:
    return Job(
        id=jid,
        title=f"job {jid}",
        source="test",
        steps=[
            StepSpec(id=f"s{i}", title=f"step {i}", kind=StepKind.READ)
            for i in range(1, n + 1)
        ],
    )


def test_submit_returns_immediately_and_runs_in_background(tmp_path):
    """submit 派发即返回，任务在后台线程跑完并落库。"""
    store = OrchestrationStore(tmp_path / "h.db")
    events: list[str] = []

    def notifier(kind: str, run: dict) -> None:
        events.append(kind)

    host = JobHost(store, notifier=notifier)
    started = threading.Event()

    def handler(spec, job, ctx):
        started.set()
        return {"step": spec.id}

    t0 = time.time()
    jid = host.submit(_job("bg"), handler)
    assert jid == "bg"
    # 派发返回必须远快于任务真正跑完——证明是后台而非同步。
    assert time.time() - t0 < 1.0
    assert started.wait(timeout=5)
    assert host.wait(timeout=10)
    assert store.get_job("bg").status == JobStatus.OK
    assert "started" in events and "finished" in events


def test_recover_resumes_crashed_job_and_skips_completed_steps(tmp_path):
    """进程崩溃现场（s1 已完成、s2 RUNNING、s3 PENDING）重启后只重跑未完成步。"""
    store = OrchestrationStore(tmp_path / "h.db")
    job = _job("crash")
    job.runs["s1"] = StepRun(
        step_id="s1",
        status=StepStatus.OK,
        result={"v": 1},
        artifacts=[Artifact(name="s1_out", value={"v": 1})],
    )
    job.runs["s2"] = StepRun(step_id="s2", status=StepStatus.RUNNING, attempt=1)
    job.status = JobStatus.RUNNING
    store.save_job(job)

    # 新宿主实例 = 模拟进程重启后重建
    host = JobHost(store)
    calls: list[str] = []

    def handler(spec, j, ctx):
        calls.append(spec.id)
        return {"step": spec.id}

    recovered = host.recover(lambda j: handler)
    assert recovered == ["crash"]
    assert host.wait(timeout=10)

    reloaded = store.get_job("crash")
    assert reloaded.status == JobStatus.OK
    assert calls == ["s2", "s3"], f"已完成步被重跑：{calls}"
    assert reloaded.runs["s1"].result == {"v": 1}
    assert reloaded.runs["s1"].artifacts[0].name == "s1_out"


def test_recover_skips_completed_and_dedups_running(tmp_path):
    """已 OK 的 job 不接管；同一 job 重复 recover 不重复起线程。"""
    store = OrchestrationStore(tmp_path / "h.db")
    done = _job("done")
    done.status = JobStatus.OK
    store.save_job(done)

    live = _job("live")
    live.status = JobStatus.RUNNING
    store.save_job(live)
    started = threading.Event()
    release = threading.Event()
    count = {"n": 0}
    lock = threading.Lock()

    def handler(spec, j, ctx):
        with lock:
            count["n"] += 1
        started.set()
        release.wait(timeout=5)
        return {"step": spec.id}

    host = JobHost(store, max_concurrent=4)
    assert host.recover(lambda j: handler) == ["live"]
    assert started.wait(timeout=5)
    # 任务仍在跑时再 recover 一次：不得重复拉起
    assert host.recover(lambda j: handler) == []
    release.set()
    assert host.wait(timeout=10)
    with lock:
        assert count["n"] == 3, f"步被重复执行：{count['n']}"


def test_submit_respects_concurrency_cap(tmp_path):
    """全局并发闸：超过上限的派发被拒，避免拖垮 provider。"""
    store = OrchestrationStore(tmp_path / "h.db")
    entered = threading.Event()
    release = threading.Event()

    def handler(spec, j, ctx):
        entered.set()
        release.wait(timeout=5)
        return {"step": spec.id}

    host = JobHost(store, max_concurrent=1)
    host.submit(_job("one"), handler)
    assert entered.wait(timeout=5)
    with pytest.raises(RuntimeError):
        host.submit(_job("two"), handler)
    release.set()
    assert host.wait(timeout=10)


def test_recover_reports_failed_job_without_handler(tmp_path):
    """拿不到 handler 的未完成 job 不静默悬挂：标记 failed 并通知。"""
    store = OrchestrationStore(tmp_path / "h.db")
    job = _job("orphan")
    job.status = JobStatus.RUNNING
    store.save_job(job)

    events: list[tuple] = []
    host = JobHost(store, notifier=lambda k, r: events.append((k, r)))
    assert host.recover(lambda j: None, fail_unresolved=True) == []
    reloaded = store.get_job("orphan")
    assert reloaded.status == JobStatus.FAILED
    assert "no handler" in (reloaded.meta.get("recover_error") or "")


def test_read_check_merge_fans_out_and_merges_all(tmp_path):
    """读稿/调研/检查类任务：N 路只读扇出真并行，主路 merge 汇总全部。"""
    from coworker.orchestration.automation import submit_read_check_merge
    from coworker.orchestration.leases import LeaseConfig

    store = OrchestrationStore(tmp_path / "h.db")
    # 提高本 runner 的 fan-out 预算以验证 5 路并行；默认 max_parallel=4
    # 会把同时在场压到 4——那是正确的进程级预算，不是 fan-out 失效。
    host = JobHost(
        store,
        max_concurrent=8,
        lease_config=LeaseConfig(
            max_parallel=8, max_read=8, share_process_gates=False
        ),
    )
    n = 5
    active = {"n": 0, "peak": 0}
    lock = threading.Lock()
    release = threading.Barrier(n)

    def extract(spec, job, ctx):
        with lock:
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
        release.wait(timeout=5)  # 所有 extract 必须同时在场才能通过
        with lock:
            active["n"] -= 1
        return {"id": spec.meta["item"]["key"], "n": spec.meta["item"]["key"]}

    def check(spec, job, ctx):
        prior = ctx.result(spec.meta["extract_step"])
        return {"checked": prior["id"]}

    def merge(spec, job, ctx):
        return {"merged": sorted(v["n"] for v in ctx.results.values() if isinstance(v, dict) and "n" in v)}

    jid = submit_read_check_merge(
        host,
        job_id="rcm",
        title="read-check-merge",
        source="test",
        # 纯只读扇出：uses_llm=False 避开全局 LLM 配额，验证 fan-out 本身。
        # 真实 LLM 步受进程级 gate 约束（正确行为），不该无脑全并行。
        items=[{"id": f"r{i}", "key": i, "uses_llm": False} for i in range(n)],
        extract_handler=extract,
        check_handler=check,
        merge_handler=merge,
    )
    assert jid == "rcm"
    assert host.wait(timeout=15)
    done = store.get_job("rcm")
    assert done.status == JobStatus.OK, done.status
    assert active["peak"] == n, f"扇出没有真并行：peak={active['peak']}"
    merge_run = done.runs["merge"]
    assert merge_run.result["merged"] == list(range(n)), merge_run.result
    # 每一路 check 都拿到了自己那路的 extract 结果
    for i in range(n):
        assert done.runs[f"check-r{i}"].result["checked"] == i


def test_manager_recovery_is_conservative_and_respects_registration(tmp_path):
    """manager 启动自愈：未登记 source 不接管，登记后可重建的才接管。"""
    from coworker.orchestration import Job, StepKind, StepSpec, StepStatus
    from coworker.server.manager import SessionManager

    store = OrchestrationStore(tmp_path / "h.db")
    host = JobHost(store, max_concurrent=4)

    # 两类未完成的 job：automation（未登记，不该接管）与 rcm（可重建）
    auto = Job(id="auto", title="a", source="automation", steps=[
        StepSpec(id="s1", title="s1", kind=StepKind.READ)
    ])
    auto.status = JobStatus.RUNNING
    store.save_job(auto)
    rcm = Job(id="rcm", title="r", source="read_check_merge", steps=[
        StepSpec(id="s1", title="s1", kind=StepKind.READ)
    ])
    rcm.status = JobStatus.RUNNING
    store.save_job(rcm)

    mgr = SessionManager.__new__(SessionManager)
    mgr.orchestration_store = store
    mgr.orchestration_host = host
    mgr._orch_recover_resolvers = {}
    mgr._loop = None

    # 未登记任何 source：自愈空跑，automation 仍是 RUNNING（没被误接管）
    assert mgr.recover_orchestration_jobs() == 0
    assert store.get_job("auto").status == JobStatus.RUNNING

    # 只登记 read_check_merge：只接管它，automation 依然不碰
    ran = threading.Event()

    def handler(spec, j, ctx):
        ran.set()
        return {"ok": 1}

    mgr.register_orch_recovery("read_check_merge", lambda j: handler)
    assert mgr.recover_orchestration_jobs() == 1
    assert ran.wait(timeout=5)
    assert host.wait(timeout=10)
    assert store.get_job("rcm").status == JobStatus.OK
    assert store.get_job("auto").status == JobStatus.RUNNING


def test_manager_runner_is_shared_with_host_and_falls_back(tmp_path):
    """automation 走宿主共享 runner（同一租约预算），host 不可用时兜底。"""
    from coworker.orchestration import ConcurrencyLeases, LeaseConfig, OrchestrationRunner
    from coworker.server.manager import SessionManager

    store = OrchestrationStore(tmp_path / "h.db")
    mgr = SessionManager.__new__(SessionManager)
    mgr.orchestration_store = store

    # 无 host：兜底自建 runner
    mgr.orchestration_host = None
    fallback = mgr.orchestration_runner()
    assert isinstance(fallback, OrchestrationRunner)

    # 有 host：返回宿主的同一个 runner 与同一租约对象（并发预算统一）
    host = JobHost(store, lease_config=LeaseConfig(max_parallel=3))
    mgr.orchestration_host = host
    shared = mgr.orchestration_runner()
    assert shared is host.runner
    assert shared.leases is host.runner.leases
    assert host.runner.leases.config.max_parallel == 3


def test_read_check_merge_recovery_skips_completed_extracts(tmp_path):
    """崩溃接管：已完成的 extract 不重跑，只补跑未完成的 check/merge。"""
    from coworker.orchestration.automation import build_read_check_merge_pipeline

    store = OrchestrationStore(tmp_path / "h.db")
    job, handlers = build_read_check_merge_pipeline(
        job_id="rcm-recover",
        title="rcm",
        source="test",
        items=[{"id": "r0", "key": 0}, {"id": "r1", "key": 1}],
        extract_handler=lambda spec, j, c: {"n": spec.meta["item"]["key"]},
        check_handler=lambda spec, j, c: {"checked": 1},
        merge_handler=lambda spec, j, c: {"merged": 1},
    )
    # 模拟崩溃现场：两路 extract 已完成并带 artifact，check/merge 未完成
    job.runs["r0"] = StepRun(
        step_id="r0", status=StepStatus.OK, result={"n": 0},
        artifacts=[Artifact(name="r0_out", value=0)],
    )
    job.runs["r1"] = StepRun(
        step_id="r1", status=StepStatus.OK, result={"n": 1},
        artifacts=[Artifact(name="r1_out", value=1)],
    )
    job.status = JobStatus.RUNNING
    store.save_job(job)

    host = JobHost(store)
    extract_calls: list[str] = []
    real_extract = handlers["r0"]

    def spy(spec, j, c):
        extract_calls.append(spec.id)
        return real_extract(spec, j, c)

    handlers["r0"] = spy
    handlers["r1"] = spy
    assert host.recover(lambda j: handlers) == ["rcm-recover"]
    assert host.wait(timeout=10)
    done = store.get_job("rcm-recover")
    assert done.status == JobStatus.OK
    assert extract_calls == [], f"已完成的 extract 被重跑：{extract_calls}"
    assert done.runs["r0"].artifacts[0].name == "r0_out"
