import asyncio
import time

import pytest

from coworker.orchestration import (
    Artifact,
    ConcurrencyLeases,
    Job,
    JobStatus,
    LeaseConfig,
    OrchestrationRunner,
    OrchestrationStore,
    StepKind,
    StepSpec,
    StepStatus,
)


async def test_resume_from_checkpoint_skips_ok_and_reruns_failed(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-resume",
        title="resume",
        steps=[
            StepSpec(id="a", title="a", kind=StepKind.READ),
            StepSpec(id="b", title="b", kind=StepKind.COMPUTE, deps=["a"]),
        ],
    )
    calls = {"a": 0, "b": 0}

    async def handler(spec, job, ctx):
        calls[spec.id] += 1
        if spec.id == "b" and calls["b"] == 1:
            raise RuntimeError("boom")
        return {"step": spec.id}

    runner = OrchestrationRunner(store, ConcurrencyLeases(LeaseConfig(max_parallel=2)))
    out = await runner.run(job, handler)
    assert out.status == JobStatus.FAILED
    assert out.runs["a"].status == StepStatus.OK
    assert out.runs["b"].status == StepStatus.FAILED
    assert calls == {"a": 1, "b": 1}

    # Resume: a must not rerun, b should rerun and succeed.
    job2 = store.get_job("job-resume")
    assert job2 is not None
    out2 = await runner.run(job2, handler, resume=True)
    assert out2.status == JobStatus.OK
    assert calls == {"a": 1, "b": 2}
    assert out2.runs["b"].result == {"step": "b"}


async def test_parallel_read_only_fanout(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-fanout",
        steps=[
            StepSpec(id="r1", title="r1", kind=StepKind.READ),
            StepSpec(id="r2", title="r2", kind=StepKind.READ),
            StepSpec(id="r3", title="r3", kind=StepKind.READ),
        ],
    )
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(spec, job, ctx):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return spec.id

    runner = OrchestrationRunner(
        store,
        ConcurrencyLeases(LeaseConfig(max_parallel=3, max_read=3)),
    )
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    assert peak >= 2


async def test_write_side_effect_serialized_by_target(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-serial",
        steps=[
            StepSpec(id="w1", title="w1", kind=StepKind.WRITE, target="file:A"),
            StepSpec(id="w2", title="w2", kind=StepKind.WRITE, target="file:A"),
            StepSpec(id="p1", title="p1", kind=StepKind.SIDE_EFFECT, target="publish:A"),
            StepSpec(id="p2", title="p2", kind=StepKind.SIDE_EFFECT, target="publish:A"),
        ],
    )
    active_by_target: dict[str, int] = {}
    peak_by_target: dict[str, int] = {}
    lock = asyncio.Lock()

    async def handler(spec, job, ctx):
        key = spec.target or spec.id
        async with lock:
            active_by_target[key] = active_by_target.get(key, 0) + 1
            peak_by_target[key] = max(peak_by_target.get(key, 0), active_by_target[key])
        await asyncio.sleep(0.03)
        async with lock:
            active_by_target[key] -= 1
        return spec.id

    runner = OrchestrationRunner(
        store, ConcurrencyLeases(LeaseConfig(max_parallel=4))
    )
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    assert peak_by_target.get("file:A", 0) == 1
    assert peak_by_target.get("publish:A", 0) == 1


async def test_idempotent_side_effect_retry(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-idem",
        steps=[
            StepSpec(
                id="publish",
                title="publish",
                kind=StepKind.SIDE_EFFECT,
                idempotency_key="pub:job-idem:v1",
                retries=1,
            )
        ],
    )
    calls = 0

    async def handler(spec, job, ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("network")
        return {"url": "https://example.com/post/1"}

    runner = OrchestrationRunner(store)
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    assert calls == 2

    # Re-run same job/idempotency key should not execute again.
    job2 = store.get_job("job-idem")
    assert job2 is not None
    # Force rerun by marking pending in a fresh job object with same step key.
    job3 = Job(
        id="job-idem-2",
        steps=[
            StepSpec(
                id="publish",
                title="publish",
                kind=StepKind.SIDE_EFFECT,
                idempotency_key="pub:job-idem:v1",
            )
        ],
    )
    out3 = await runner.run(job3, handler)
    assert out3.status == JobStatus.OK
    assert calls == 2
    assert out3.runs["publish"].result == {"url": "https://example.com/post/1"}


async def test_global_concurrency_cap(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-cap",
        steps=[StepSpec(id=f"r{i}", title=f"r{i}", kind=StepKind.READ) for i in range(6)],
    )
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def handler(spec, job, ctx):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return spec.id

    runner = OrchestrationRunner(
        store, ConcurrencyLeases(LeaseConfig(max_parallel=2, max_read=2))
    )
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    assert peak <= 2


async def test_dependency_failure_skips_downstream(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-dep",
        steps=[
            StepSpec(id="a", title="a", kind=StepKind.READ),
            StepSpec(id="b", title="b", kind=StepKind.WRITE, deps=["a"], target="f"),
            StepSpec(id="c", title="c", kind=StepKind.SIDE_EFFECT, deps=["b"], target="p"),
        ],
    )

    async def handler(spec, job, ctx):
        if spec.id == "a":
            raise RuntimeError("fail")
        return spec.id

    runner = OrchestrationRunner(store)
    out = await runner.run(job, handler)
    assert out.status == JobStatus.FAILED
    assert out.runs["a"].status == StepStatus.FAILED
    assert out.runs["b"].status == StepStatus.SKIPPED
    assert out.runs["c"].status == StepStatus.SKIPPED


async def test_artifacts_are_checkpointed(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-art",
        steps=[
            StepSpec(id="extract", title="extract", kind=StepKind.READ),
            StepSpec(id="merge", title="merge", kind=StepKind.COMPUTE, deps=["extract"]),
        ],
    )

    async def handler(spec, job, ctx):
        if spec.id == "extract":
            return Artifact(name="notes", kind="json", value={"n": 1})
        return {"seen": ctx.artifact("notes").value if ctx.artifact("notes") else None}

    runner = OrchestrationRunner(store)
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    reloaded = store.get_job("job-art")
    assert reloaded is not None
    assert reloaded.runs["extract"].artifacts[0].name == "notes"
    assert reloaded.runs["merge"].result == {"seen": {"n": 1}}


async def test_read_check_merge_pattern_parallel_then_merge(tmp_path):
    from coworker.orchestration.automation import build_read_check_merge_pipeline

    store = OrchestrationStore(tmp_path / "orch.db")
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def extract(spec, job, ctx):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.03)
        async with lock:
            active -= 1
        return Artifact(name=f"extract:{spec.id}", kind="json", value={"id": spec.id})

    async def check(spec, job, ctx):
        src = spec.meta["extract_step"]
        return Artifact(name=f"check:{src}", kind="json", value={"ok": True, "src": src})

    async def merge(spec, job, ctx):
        return {
            "extracts": sorted([k for k in ctx.artifacts if k.startswith("extract:")]),
            "checks": sorted([k for k in ctx.artifacts if k.startswith("check:")]),
        }

    job, handlers = build_read_check_merge_pipeline(
        job_id="job-rcm",
        title="read-check-merge",
        source="test",
        items=[{"id": "a"}, {"id": "b"}, {"id": "c"}],
        extract_handler=extract,
        check_handler=check,
        merge_handler=merge,
    )
    runner = OrchestrationRunner(store, ConcurrencyLeases(LeaseConfig(max_parallel=4, max_read=4)))
    out = await runner.run(job, handlers)
    assert out.status == JobStatus.OK
    assert peak >= 2
    assert out.runs["merge"].result["extracts"] == ["extract:a", "extract:b", "extract:c"]
    assert out.runs["merge"].result["checks"] == ["check:a", "check:b", "check:c"]


async def test_checkpointed_publish_failure_preserves_artifacts(tmp_path):
    from coworker.orchestration.automation import build_checkpointed_publish_pipeline

    store = OrchestrationStore(tmp_path / "orch.db")
    calls = {"produce": 0, "publish": 0}

    async def produce(spec, job, ctx):
        calls["produce"] += 1
        return Artifact(name="draft", kind="json", value={"text": "hello"})

    async def publish(spec, job, ctx):
        calls["publish"] += 1
        if calls["publish"] == 1:
            raise RuntimeError("publish timeout")
        return {"published": True, "draft": ctx.artifact("draft").value}

    job, handlers = build_checkpointed_publish_pipeline(
        job_id="job-pub",
        title="publish",
        source="automation",
        produce_handler=produce,
        publish_handler=publish,
        publish_idempotency_key="pub:job-pub:v1",
        publish_retries=0,
    )
    runner = OrchestrationRunner(store)
    out = await runner.run(job, handlers)
    assert out.status == JobStatus.FAILED
    assert calls["produce"] == 1
    assert calls["publish"] == 1
    assert out.runs["produce"].status == StepStatus.OK
    assert out.runs["publish"].status == StepStatus.FAILED
    assert store.get_job("job-pub").runs["produce"].artifacts[0].value == {"text": "hello"}

    # Resume: produce should not rerun, publish should retry and succeed.
    resumed = store.get_job("job-pub")
    out2 = await runner.run(resumed, handlers, resume=True)
    assert out2.status == JobStatus.OK
    assert calls["produce"] == 1
    assert calls["publish"] == 2
    assert out2.runs["publish"].result["published"] is True


def test_process_gate_caps_concurrency_across_event_loops():
    """The provider pool is shared by several loops (manager + consult threads).

    asyncio.Semaphore is loop-bound, so a per-runner cap cannot bound that
    traffic; the cross-loop gate must.
    """
    import threading

    from coworker.orchestration.leases import _CrossLoopLimiter

    limiter = _CrossLoopLimiter(2)
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    async def worker():
        await limiter.acquire()
        try:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.05)
        finally:
            with lock:
                state["active"] -= 1
            limiter.release()

    def run_loop():
        async def main():
            await asyncio.gather(*[worker() for _ in range(6)])

        asyncio.run(main())

    threads = [threading.Thread(target=run_loop) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] <= 2, f"cap leaked across loops: peak={state['peak']}"
    assert limiter.stats()["active"] == 0


def test_process_gate_cancel_does_not_leak_slot():
    from coworker.orchestration.leases import _CrossLoopLimiter

    limiter = _CrossLoopLimiter(1)

    async def scenario():
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        limiter.release()
        # The cancelled waiter must not have consumed the slot permanently.
        await asyncio.wait_for(limiter.acquire(), timeout=1)
        limiter.release()

    asyncio.run(scenario())
    assert limiter.stats() == {"limit": 1, "active": 0, "waiting": 0}


async def test_sync_handler_does_not_serialize_the_job(tmp_path):
    """A sync handler used to run inline and block every other ready step."""
    import threading

    store = OrchestrationStore(tmp_path / "orch.db")
    job = Job(
        id="job-sync",
        steps=[
            StepSpec(id="s1", title="s1", kind=StepKind.READ),
            StepSpec(id="s2", title="s2", kind=StepKind.READ),
        ],
    )
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(spec, job, ctx):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return spec.id

    runner = OrchestrationRunner(
        store,
        ConcurrencyLeases(LeaseConfig(max_parallel=2, max_read=2, share_process_gates=False)),
    )
    out = await runner.run(job, handler)
    assert out.status == JobStatus.OK
    assert peak >= 2, "sync handlers ran serially on the event loop"


def test_prune_drops_old_finished_jobs(tmp_path):
    store = OrchestrationStore(tmp_path / "orch.db")

    async def handler(spec, job, ctx):
        return spec.id

    job = Job(id="job-old", steps=[StepSpec(id="a", title="a")])
    asyncio.run(OrchestrationRunner(store).run(job, handler))
    assert store.get_job("job-old") is not None

    # Backdate the checkpoint, then prune.
    store._conn.execute(
        "UPDATE orchestration_jobs SET updated_at = updated_at - 40*86400 WHERE id='job-old'"
    )
    store._conn.commit()
    assert store.prune(max_age_days=30) == 1
    assert store.get_job("job-old") is None


async def test_cancelled_waiter_does_not_inflate_kind_semaphore():
    """A waiter cancelled before it acquired the kind slot must not release it.

    The release flags must record *successful acquisition*, not the step's kind:
    releasing a semaphore that was never acquired permanently inflates capacity.
    """
    leases = ConcurrencyLeases(
        LeaseConfig(max_parallel=1, max_read=1, share_process_gates=False)
    )
    spec = StepSpec(id="r1", title="r1", kind=StepKind.READ)

    async def hold():
        async with leases.acquire(spec):
            await asyncio.sleep(10)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.01)  # holder owns every slot now

    waiter = asyncio.create_task(hold())
    await asyncio.sleep(0.01)  # waiter parks on the global semaphore
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    assert leases._read._value == 1, "kind semaphore inflated by cancelled waiter"
    assert leases._global._value == 1
    # And the leases still work afterwards.
    async with leases.acquire(spec):
        pass
