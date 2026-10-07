"""P2-R3 · lessons 注入（teachings 表 + manager 每轮注入链）。

覆盖：recent/permanent 限额、字数 cap、空表 fail-open、总开关关闭、
读库异常 fail-open、内容幂等、SYNC 注册 + 端到端挂线。
"""

from __future__ import annotations

import pytest

from deskmate.cognitive.memory_injector import build_lessons_context
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory import sync_store_sqlite as sss


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_LESSONS_INJECT", raising=False)
    monkeypatch.delenv("DESKMATE_AUTO_INJECT", raising=False)
    return tmp_path


def _store(env) -> CognitiveStore:
    """建库 + 补 teachings 的 sync migrate（触发器落 oplog）。"""
    store = CognitiveStore(db_path=env / "deskmate.db")
    with store._connect() as conn:
        sss.migrate(conn, "mac", "teachings")
    return store


# ---------------------------------------------------------------------------
# store：add_lesson / get_lessons_for_injection
# ---------------------------------------------------------------------------
def test_lessons_injection_recent_and_permanent(env):
    """5 条 recent + 2 条 permanent → 返回 2 permanent + 最近 3 recent（共 5）。"""
    store = _store(env)
    for i in range(5):
        store.add_lesson(f"recent-{i}", scope="recent")
    for i in range(2):
        store.add_lesson(f"permanent-{i}", scope="permanent")

    out = store.get_lessons_for_injection()
    assert len(out) == 5, "应为 2 permanent + 最近 3 recent"
    assert "permanent-0" in out and "permanent-1" in out, "永久规则必进"
    recent_got = [t for t in out if t.startswith("recent-")]
    assert len(recent_got) == 3, "recent 限 3 条"


def test_lessons_char_cap(env):
    """大量长文 → 注入总字数 ≤ char_cap（防挤爆上下文）。"""
    store = _store(env)
    for i in range(5):
        store.add_lesson("长" * 400 + str(i), scope="permanent")
    out = store.get_lessons_for_injection(char_cap=1200)
    assert sum(len(t) for t in out) <= 1200
    assert out, "至少应能装下一条"


def test_lessons_empty(env):
    """空表 → 列表为空；build_lessons_context 返回空串。"""
    store = _store(env)
    assert store.get_lessons_for_injection() == []
    assert build_lessons_context(base_dir=env) == ""


def test_add_lesson_idempotent(env):
    """同内容加两次 → 表只有 1 行（lesson_id 内容 md5 去重）。"""
    store = _store(env)
    assert store.add_lesson("反馈：萧哥不要她替用户做主") is True
    assert store.add_lesson("反馈：萧哥不要她替用户做主") is True
    with store._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM teachings").fetchone()[0]
    assert n == 1


def test_add_lesson_empty_content(env):
    """空内容 → 不写、返回 False。"""
    store = _store(env)
    assert store.add_lesson("   ") is False


# ---------------------------------------------------------------------------
# 注入链：build_lessons_context + manager 挂线
# ---------------------------------------------------------------------------
def test_build_lessons_context_content(env):
    """有 lessons 时拼出注入块并含条目正文。"""
    store = _store(env)
    store.add_lesson("反馈：萧哥不要她替用户做主", scope="permanent")
    ctx = build_lessons_context(base_dir=env)
    assert "小桔学到的" in ctx
    assert "不要她替用户做主" in ctx


def test_build_full_context_injects_lessons_block(env):
    """端到端挂线：MemoryManager 每轮上下文含「小桔学到的」块。"""
    store = _store(env)
    store.add_lesson("反馈：萧哥不要她替用户做主", scope="permanent")

    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=env)
    ctx = mgr.build_full_context_for_turn(user_message="帮我写个开头")
    assert "小桔学到的" in ctx
    assert "不要她替用户做主" in ctx


def test_lessons_inject_switch_off(env, monkeypatch):
    """DESKMATE_LESSONS_INJECT=0 → 只关这一块（lessons 不进上下文）。"""
    store = _store(env)
    store.add_lesson("反馈：萧哥不要她替用户做主", scope="permanent")
    monkeypatch.setenv("DESKMATE_LESSONS_INJECT", "0")

    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=env)
    ctx = mgr.build_full_context_for_turn(user_message="帮我写个开头")
    assert "小桔学到的" not in ctx


def test_lessons_auto_inject_off(env, monkeypatch):
    """DESKMATE_AUTO_INJECT=0 → deskmate_auto_inject_context 整体返回空。"""
    store = _store(env)
    store.add_lesson("反馈：萧哥不要她替用户做主", scope="permanent")
    monkeypatch.setenv("DESKMATE_AUTO_INJECT", "0")

    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=env)
    assert mgr.deskmate_auto_inject_context("你好") == ""


def test_lessons_fail_open(env, monkeypatch):
    """读库异常 → build_lessons_context 返回 ""（不抛）。"""
    store = _store(env)

    def _boom(self, recent_limit=3, char_cap=1200):
        raise RuntimeError("db down")

    monkeypatch.setattr(CognitiveStore, "get_lessons_for_injection", _boom)
    assert build_lessons_context(base_dir=env) == ""


# ---------------------------------------------------------------------------
# SYNC 注册 + migrate 补列 + 触发器落 oplog
# ---------------------------------------------------------------------------
def test_teachings_in_sync_tables():
    spec = sss.SYNC_TABLES["teachings"]
    assert spec["pk"] == "lesson_id" and spec["sync_key"] == "lesson_id"
    assert spec["sync_uuid_kind"] == "string"
    sss._validate_sync_tables_no_vectors()   # 无向量列（启动守卫）


def test_teachings_migrate_cols_and_oplog(env):
    store = _store(env)
    with store._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(teachings)").fetchall()}
        assert {"sync_uuid", "sync_hlc", "sync_tombstone"} <= cols

    store.add_lesson("反馈：不要替用户做主", scope="permanent")
    with store._connect() as conn:
        uid = conn.execute(
            "SELECT sync_uuid FROM teachings WHERE content='反馈：不要替用户做主'"
        ).fetchone()[0]
        n_ops = conn.execute(
            "SELECT COUNT(*) FROM _sync_oplog WHERE table_name='teachings'"
        ).fetchone()[0]
    assert uid and uid.startswith("deskmate:teachings:")
    assert n_ops >= 1