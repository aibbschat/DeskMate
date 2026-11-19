"""PR12 performance guards — schema-once, bounded reflection pool, per-room eviction.

Each test asserts the new *cost* property (zero repeat DDL, no thread pile-up, no
permanent engine leak), plus the behaviour it must not break.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from deskmate.bots import runtime as runtime_mod
from deskmate.bots.runtime import BotRuntime
from deskmate.growth import emotion_engine as emo_mod
from deskmate.memory import after_action as aa_mod
from deskmate.memory.learned_store import LearnedStore


@pytest.mark.skipif(
    os.environ.get("DESKMATE_SCHEMA_CACHE", "1") == "0",
    reason="schema 缓存被回滚开关关闭，本用例的前提不成立",
)
def test_learned_schema_runs_once_per_db(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = LearnedStore._ensure_schema_locked

    def counting(self):
        calls["n"] += 1
        real(self)

    monkeypatch.setattr(LearnedStore, "_ensure_schema_locked", counting)
    LearnedStore(tmp_path)
    LearnedStore(tmp_path)
    LearnedStore(tmp_path)
    assert calls["n"] == 1

    # 库被删掉重建 → 缓存失效，schema 必须重跑（否则新库没有表）。
    (tmp_path / "deskmate.db").unlink()
    LearnedStore(tmp_path)
    assert calls["n"] == 2


@pytest.mark.skipif(
    os.environ.get("DESKMATE_EMOTION_SCHEMA_CACHE", "1") == "0",
    reason="情绪 schema 缓存被回滚开关关闭，本用例的前提不成立",
)
def test_emotion_schema_runs_once_per_db(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = emo_mod._ensure_schema_locked

    def counting(db):
        calls["n"] += 1
        real(db)

    monkeypatch.setattr(emo_mod, "_ensure_schema_locked", counting)
    db = tmp_path / "deskmate.db"
    emo_mod._ensure_schema(db)
    emo_mod._ensure_schema(db)
    emo_mod._ensure_schema(db)
    assert calls["n"] == 1

    db.unlink()
    emo_mod._ensure_schema(db)
    assert calls["n"] == 2


def test_after_action_single_flight_skips_overlap(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKMATE_AFTER_ACTION_POOL", raising=False)
    monkeypatch.setenv("DESKMATE_AFTER_ACTION_WORKERS", "1")
    started = threading.Event()
    release = threading.Event()
    runs = {"n": 0}

    def slow():
        runs["n"] += 1
        started.set()
        release.wait(timeout=5)

    aa_mod._submit_after_action(tmp_path, slow)
    assert started.wait(timeout=5)
    # 同一 base_dir 上一轮链还在跑 → 本轮直接跳过，不再堆线程。
    aa_mod._submit_after_action(tmp_path, slow)
    time.sleep(0.05)
    assert runs["n"] == 1
    release.set()

    # 链跑完后可以再提交。
    deadline = time.time() + 5
    while time.time() < deadline and str(tmp_path) in aa_mod._AA_INFLIGHT:
        time.sleep(0.02)
    aa_mod._submit_after_action(tmp_path, slow)
    deadline = time.time() + 5
    while time.time() < deadline and runs["n"] < 2:
        time.sleep(0.02)
    release.set()
    assert runs["n"] == 2


def test_after_action_pool_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_AFTER_ACTION_POOL", "0")
    ran = threading.Event()

    def quick():
        ran.set()

    aa_mod._submit_after_action(tmp_path, quick)
    assert ran.wait(timeout=2)


def test_bot_runtime_evict_room_drops_engines_and_locks(tmp_path):
    rt = BotRuntime(books_root=tmp_path)
    rt._engines[("r1", "a")] = object()
    rt._engines[("r1", "b")] = object()
    rt._engines[("r1:solo", "a")] = object()
    rt._engines[("r2", "a")] = object()
    rt._turn_locks[("r1", "a")] = threading.Lock()

    removed = rt.evict_room("r1")
    assert removed == 3  # ("r1", a/b) + ("r1:solo", a) 都属于这个群
    assert ("r1", "a") not in rt._engines
    assert ("r1", "a") not in rt._turn_locks
    assert ("r2", "a") in rt._engines


def test_bot_runtime_evict_room_handles_solo_keys(tmp_path):
    rt = BotRuntime(books_root=tmp_path)
    rt._engines[("r9:solo", "a")] = object()
    assert rt.evict_room("r9:solo") == 1
    assert not rt._engines
