"""consult → orchestration 集成契约（2026-11-22）。

这条分支在线上有两条硬要求，且都是踩过的坑：

1. 专家超时必须**保留已产出的部分**（`_collect` 的契约），不能被 runner 的
   同额 `timeout_s` 抢先抛 TimeoutError 换成「调用失败」；
2. 编排层出错时不能把整轮专家重跑一遍——每位专家都是一次完整 LLM 调用。

这里用替身把 `_run_one` 换掉，只验这两条与结果排序/落库。
"""

from __future__ import annotations

import deskmate.consult as consult
from coworker.orchestration import JobStatus, OrchestrationStore


def _patch_side_effects(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(consult, "_read_state", lambda: {})
    monkeypatch.setattr(consult, "_mutate", lambda fn: None)
    monkeypatch.setattr(consult, "_write_audit", lambda *a, **k: None)
    monkeypatch.setattr(
        consult, "_NOTIFIER", lambda kind, run: captured.append(run) if kind == "finished" else None
    )
    return captured


def test_consult_orchestration_preserves_partial_output_on_timeout(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_run_one(expert_id, brief, book, provider, model, shelf, timeout, run_id=""):
        calls.append(expert_id)
        return "timeout", f"partial-{expert_id}", 1.0, {}

    monkeypatch.setattr(consult, "_run_one", fake_run_one)
    captured = _patch_side_effects(monkeypatch)
    store = OrchestrationStore(tmp_path / "orch.db")

    consult._job_thread(
        "c-timeout",
        ["plot", "outline"],
        "brief",
        "book",
        None,
        "model",
        None,
        5,
        None,
        store,
    )

    snap = captured[-1]
    assert [r["status"] for r in snap["results"]] == ["timeout", "timeout"]
    assert [r["text"] for r in snap["results"]] == ["partial-plot", "partial-outline"]
    assert sorted(calls) == ["outline", "plot"]

    job = store.get_job("consult-c-timeout")
    assert job is not None
    assert job.status is JobStatus.OK


def test_consult_orchestration_does_not_rerun_finished_experts(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_run_one(expert_id, brief, book, provider, model, shelf, timeout, run_id=""):
        calls.append(expert_id)
        if expert_id == "plot":
            raise RuntimeError("boom")
        return "ok", f"text-{expert_id}", 0.5, {}

    monkeypatch.setattr(consult, "_run_one", fake_run_one)
    captured = _patch_side_effects(monkeypatch)
    store = OrchestrationStore(tmp_path / "orch.db")

    consult._job_thread(
        "c-fail",
        ["plot", "outline"],
        "brief",
        "book",
        None,
        "model",
        None,
        5,
        None,
        store,
    )

    # 关键：每位专家只调用一次。旧实现会整体回退到 legacy 路径再跑一遍。
    assert sorted(calls) == ["outline", "plot"]
    snap = captured[-1]
    by_expert = {r["expert"]: r for r in snap["results"]}
    assert by_expert["plot"]["status"] == "error"
    assert "boom" in by_expert["plot"]["text"]
    assert by_expert["outline"]["status"] == "ok"
    assert by_expert["outline"]["text"] == "text-outline"
    assert snap["status"] == "failed"

    job = store.get_job("consult-c-fail")
    assert job is not None
    assert job.status is JobStatus.FAILED
