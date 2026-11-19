"""PR13 guards — batched behavior log, tail-only reads, retention pruning."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from deskmate.growth import emotion_engine as emo_mod
from deskmate.proactivity import behavior_patterns as bp


def _write_events(path: Path, n: int, *, ts: float | None = None, kind: str = "focus_deep"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(
                json.dumps({"ts": (ts if ts is not None else time.time()) + i,
                            "kind": kind, "payload": {}},
                           ensure_ascii=False)
                + "\n"
            )


def test_behavior_writes_are_batched(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_BEHAVIOR_LOG_SYNC", raising=False)
    fsyncs = {"n": 0}
    real = os.fsync

    def counting(fd):
        fsyncs["n"] += 1
        return real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    for i in range(5):
        bp.log_event("focus_deep", {"i": i}, base_dir=tmp_path)
    assert fsyncs["n"] == 0  # 5 条还在缓冲里，不该碰磁盘
    bp.flush_behavior_log(tmp_path)
    assert fsyncs["n"] == 1
    assert len((tmp_path / "behavior_log.jsonl").read_text().splitlines()) == 5


def test_behavior_buffer_flushes_every_20(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_BEHAVIOR_LOG_SYNC", raising=False)
    monkeypatch.setattr(bp, "_FLUSH_SEC", 3600.0)
    fsyncs = {"n": 0}
    real = os.fsync

    def counting(fd):
        fsyncs["n"] += 1
        return real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    for i in range(40):
        bp.log_event("focus_deep", {"i": i}, base_dir=tmp_path)
    assert fsyncs["n"] == 2  # 20 条一批，两批
    bp.flush_behavior_log(tmp_path)


def test_recent_logged_does_not_read_whole_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_BEHAVIOR_LOG_SYNC", raising=False)
    path = tmp_path / "behavior_log.jsonl"
    _write_events(path, 5000)
    bp.log_event("focus_deep", {}, base_dir=tmp_path)

    def _boom(*_a, **_k):
        raise AssertionError("should not read_text the whole log")

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(bp, "_FLUSH_SEC", 3600.0)
    assert bp._recent_logged("focus_deep", 3600, base_dir=tmp_path) is True


def test_load_events_stops_at_window(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_BEHAVIOR_LOG_SYNC", raising=False)
    path = tmp_path / "behavior_log.jsonl"
    old_ts = time.time() - 200 * 86400
    _write_events(path, 300, ts=old_ts, kind="old")
    _write_events(path, 10, ts=time.time(), kind="new")

    def _boom(*_a, **_k):
        raise AssertionError("should not read_text the whole log")

    monkeypatch.setattr(Path, "read_text", _boom)
    events = bp._load_events(tmp_path, since_ts=time.time() - 7 * 86400)
    assert events and all(e["kind"] == "new" for e in events)


def test_archive_dry_run_then_apply(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_BEHAVIOR_LOG_SYNC", raising=False)
    path = tmp_path / "behavior_log.jsonl"
    _write_events(path, 30, ts=time.time() - 120 * 86400)
    _write_events(path, 10, ts=time.time())
    dry = bp.archive_behavior_log(tmp_path, days=90, dry_run=True)
    assert dry["archived"] == 30 and dry["kept"] == 10
    assert not any(tmp_path.glob("behavior_log.*.gz"))

    res = bp.archive_behavior_log(tmp_path, days=90, dry_run=False)
    assert res["archived"] == 30
    assert any(tmp_path.glob("behavior_log.*.jsonl.gz"))
    assert len(path.read_text().splitlines()) == 10


def test_emotion_read_log_pruning(tmp_path, monkeypatch):
    db = tmp_path / "deskmate.db"
    emo_mod._ensure_schema(db)
    old_ts = (emo_mod.datetime.now() - emo_mod.timedelta(days=30)).isoformat(
        timespec="seconds"
    )
    new_ts = emo_mod.datetime.now().isoformat(timespec="seconds")
    with emo_mod._connect(db) as conn:
        conn.executemany(
            "INSERT INTO emotion_read_log (id, ts, lead, voices, context) VALUES (?,?,?,?,?)",
            [(f"old-{i}", old_ts, "", "[]", "x") for i in range(300)]
            + [(f"new-{i}", new_ts, "", "[]", "x") for i in range(300)],
        )
    monkeypatch.setattr(emo_mod, "_READ_LOG_WRITES", 99)
    monkeypatch.setenv("DESKMATE_EMOTION_READ_RETAIN", "14")
    emo_mod._maybe_prune_read_log(db)
    with emo_mod._connect(db) as conn:
        rows = conn.execute("SELECT COUNT(*), MIN(ts) FROM emotion_read_log").fetchone()
    assert rows[0] == 300  # 超龄行被清掉，且不超过 2000 条上限
    assert rows[1] == new_ts
    # 开关关闭时不清理
    monkeypatch.setenv("DESKMATE_EMOTION_READ_RETAIN", "0")
    monkeypatch.setattr(emo_mod, "_READ_LOG_WRITES", 99)
    emo_mod._maybe_prune_read_log(db)
    with emo_mod._connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM emotion_read_log").fetchone()[0] == 300
