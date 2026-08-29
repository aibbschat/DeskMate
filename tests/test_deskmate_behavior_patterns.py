"""Tests for P2-3 长期模式学习——从行为推断偏好（deskmate/proactivity/behavior_patterns.py）。

核心理念：不只记「用户说过的话」，更从「行为轨迹」推断偏好。
零 LLM、fail-open、可观测、可单测。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from deskmate.proactivity import behavior_patterns as BP


def _write_event(desk: Path, kind: str, ts: float, payload: dict | None = None) -> None:
    line = json.dumps({"ts": ts, "kind": kind, "payload": payload or {}}, ensure_ascii=False)
    (desk / "behavior_log.jsonl").open("a", encoding="utf-8").write(line + "\n")


def _weekday_ts(y: int, m: int, d: int, h: int = 10) -> float:
    """构造某年月日小时的 unix 时间戳。"""
    import datetime

    return datetime.datetime(y, m, d, h, 0, 0).timestamp()


class TestLogEvent:
    def test_log_event_writes_jsonl(self, tmp_path):
        BP.log_event("suggestion_accepted", {"type": "x"}, base_dir=tmp_path)
        p = tmp_path / "behavior_log.jsonl"
        assert p.is_file()
        rec = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert rec["kind"] == "suggestion_accepted"
        assert rec["payload"]["type"] == "x"
        assert isinstance(rec["ts"], float)

    def test_log_reschedule_shape(self, tmp_path):
        BP.log_reschedule("审稿", from_weekday=3, to_weekday=4, base_dir=tmp_path)
        rec = json.loads((tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["kind"] == "reschedule"
        assert rec["payload"] == {"task_type": "审稿", "from_weekday": 3, "to_weekday": 4}

    def test_fail_open_on_bad_dir(self):
        # 不应抛异常（base_dir 指向不可写位置时静默）
        BP.log_event("x", {"a": 1}, base_dir="/nonexistent/deep/path/xyz")
        BP.mine_patterns(base_dir="/nonexistent/deep/path/xyz")


class TestWeekdayCadence:
    def test_detects_dominant_weekday(self, tmp_path):
        # 2026-08-03 是周一；连续 5 个周一都「接受提醒」
        base = _weekday_ts(2026, 8, 3)  # 周一
        for w in range(5):
            ts = base + w * 7 * 86400
            _write_event(tmp_path, "suggestion_accepted", ts, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "周一" in joined
        assert "接受我的提醒" in joined

    def test_no_false_weekday_when_spread(self, tmp_path):
        # 均匀分布在各星期几 → 不应提炼出「常在周X」
        start = _weekday_ts(2026, 8, 3)  # 周一
        for wd in range(7):
            _write_event(tmp_path, "suggestion_accepted", start + wd * 86400, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert not any("常在周" in p for p in res["patterns"])


class TestHourCadence:
    def test_detects_active_hour(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 9)  # 周一 09:00
        for i in range(10):
            ts = base + i * 86400  # 每天 09:00 接受提醒
            _write_event(tmp_path, "suggestion_accepted", ts, {"type": "plot"})
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert any("09:00" in p for p in res["patterns"])


class TestRecurrence:
    def test_detects_stable_interval(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        # 每 7 天一次，连续 5 次（稳定节奏）
        for i in range(5):
            _write_event(tmp_path, "creation_approved", base + i * 7 * 86400)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert any("每 7 天" in p for p in res["patterns"])

    def test_no_recurrence_when_irregular(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for off in [0, 1, 30, 200]:
            _write_event(tmp_path, "creation_approved", base + off * 86400)
        res = BP.mine_patterns(base_dir=tmp_path, days=400)
        assert not any("每" in p and "天一次" in p for p in res["patterns"])


class TestDeferral:
    def test_detects_thursday_to_friday(self, tmp_path):
        # 连续 3 次把「审稿」从周四(3)推到周五(4)
        for _ in range(3):
            BP.log_reschedule("审稿", from_weekday=3, to_weekday=4, ts=time.time(),
                              base_dir=tmp_path)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        joined = " ".join(res["patterns"])
        assert "周四" in joined and "周五" in joined and "审稿" in joined
        assert "避开周四" in joined

    def test_no_deferral_on_inconsistent(self, tmp_path):
        BP.log_reschedule("审稿", 3, 4, ts=time.time(), base_dir=tmp_path)
        BP.log_reschedule("审稿", 1, 2, ts=time.time(), base_dir=tmp_path)
        res = BP.mine_patterns(base_dir=tmp_path, days=120)
        assert not any("避开" in p for p in res["patterns"])


class TestToContext:
    def test_empty_when_no_data(self, tmp_path):
        assert BP.to_context(base_dir=tmp_path) == ""
        res = BP.mine_patterns(base_dir=tmp_path)
        assert res["event_count"] == 0
        assert res["patterns"] == []

    def test_context_formats_patterns(self, tmp_path):
        base = _weekday_ts(2026, 8, 3, 10)
        for w in range(5):
            _write_event(tmp_path, "suggestion_accepted", base + w * 7 * 86400, {"type": "plot"})
        ctx = BP.to_context(base_dir=tmp_path, days=120)
        assert "从你长期行为推断" in ctx
        assert "周一" in ctx

    def test_focus_deep_dedup(self, tmp_path):
        now = time.time()
        BP.log_focus_deep(app="Code", base_dir=tmp_path)  # 记一次
        BP.log_focus_deep(app="Code", base_dir=tmp_path)  # 30s 内应被去重
        lines = (tmp_path / "behavior_log.jsonl").read_text(encoding="utf-8").splitlines()
        assert sum(1 for l in lines if json.loads(l)["kind"] == "focus_deep") == 1
