"""性能回归护栏（2026-11-19 PR10）。

只锁数量级，不锁精确毫秒（CI 机器差异大）。目的是让「哪天又变慢 10 倍」直接红，
而不是靠人肉感知。四条护栏对应本轮 PR1 / PR4 / PR6 / PR7 的核心收益。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from coworker.skills.base import SkillLoader


def test_context_build_is_bounded(tmp_path, monkeypatch):
    """PR1：温缓存下每轮上下文组装不能退化到秒级。"""
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setenv("DESKMATE_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("DESKMATE_EMBEDDING_BACKEND", "off")
    monkeypatch.setenv("DESKMATE_WEATHER", "0")
    monkeypatch.setenv("DESKMATE_CONTEXT_PARALLEL", "1")
    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=tmp_path)
    mgr.build_full_context_for_turn("warmup")  # 预热缓存
    t0 = time.perf_counter()
    for _ in range(5):
        mgr.build_full_context_for_turn("第四章写得怎么样了")
    per_call_ms = (time.perf_counter() - t0) * 1000 / 5
    assert per_call_ms < 1000, f"上下文组装退化到 {per_call_ms:.0f}ms/轮"


def test_skill_rescan_reuses_parsed_skills(tmp_path, monkeypatch):
    """PR6：目录未变时 rescan 零文件读取，远快于全量重读。"""
    d = tmp_path / "skills"
    for i in range(20):
        s = d / f"skill-{i}"
        s.mkdir(parents=True)
        (s / "SKILL.md").write_text(
            f"---\nname: skill-{i}\ndescription: test skill {i}\n---\n\nbody {'x' * 200}\n"
        )
    loader = SkillLoader([d])
    t0 = time.perf_counter()
    for _ in range(50):
        loader.rescan()
    per_call_ms = (time.perf_counter() - t0) * 1000 / 50
    assert per_call_ms < 10, f"缓存失效，rescan 退化到 {per_call_ms:.1f}ms"


def test_persist_noop_produces_no_oplog(tmp_path, monkeypatch):
    """PR7：同内容 persist 不产生 oplog 行。"""
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    from deskmate.cognitive.store import CognitiveStore
    from deskmate.cognitive.profile import CognitiveProfile
    from deskmate.cognitive.models import Stance
    from deskmate.memory.sync_store_sqlite import migrate
    from coworker.providers.openai_provider import OpenAIProvider  # noqa: F401 - 确保记账模块可导入

    db = tmp_path / "deskmate.db"
    p = CognitiveProfile()
    p.stances.append(Stance(content="史料优先", topic="历史真实性"))
    store = CognitiveStore(db_path=db)
    store.persist(p)
    con = sqlite3.connect(str(db))
    migrate(con, "guard-node")
    con.commit()
    con.close()

    def n_ops() -> int:
        con = sqlite3.connect(str(db))
        try:
            return con.execute(
                "SELECT COUNT(*) FROM _sync_oplog WHERE table_name='profile_items'"
            ).fetchone()[0]
        finally:
            con.close()

    store.persist(p)
    assert n_ops() == 0, "同内容 persist 不应产生 oplog"
    p.stances[0].content = "史料与正文互证"
    store.persist(p)
    assert n_ops() > 0, "内容变化必须照常落 oplog"


def test_watchdog_fires_before_sdk_timeout(monkeypatch):
    """PR2：首字看门狗必须在 SDK 600s 读超时之前触发（复用 provider 用例即可）。"""
    # 真实断言在 tests/test_provider_watchdog.py；这里只保证该文件存在且可收集，
    # 防止后续有人误删看门狗测试。
    path = Path(__file__).parent / "test_provider_watchdog.py"
    assert path.is_file()
