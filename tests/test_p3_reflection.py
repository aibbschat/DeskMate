"""R9 · 反思层 R1（item_history 裁决 + why 留痕 + 每轮注入）。

覆盖：record_reflection 硬断言（空 reason 作废）、reflected 留痕、注入读取
（空库 / 最近优先 + char_cap）、manager 端到端挂线及开关、fail-open。
"""

from __future__ import annotations

import pytest

from deskmate.cognitive.memory_injector import build_reflection_context
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_REFLECTION_INJECT", raising=False)
    monkeypatch.delenv("DESKMATE_AUTO_INJECT", raising=False)
    monkeypatch.delenv("DESKMATE_REFLECTION_LLM", raising=False)
    return tmp_path


def _store(env) -> CognitiveStore:
    return CognitiveStore(db_path=env / "deskmate.db")


def _seed_reflected(store, item_id, verdict, reason, ts, content=""):
    """直接往 item_history 落一条 reflected 痕（可控 ts，便于验证「最近优先」）。"""
    with store._connect() as conn:
        if content:
            conn.execute(
                "INSERT OR REPLACE INTO profile_items"
                "(id, layer, dimension, content, status) VALUES(?, 'worldview', 'stance', ?, 'active')",
                (item_id, content),
            )
        conn.execute(
            "INSERT OR REPLACE INTO item_history"
            "(item_id, seq, action, old_value, new_value, reason, source, ts) "
            "VALUES(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM item_history WHERE item_id=?), "
            "'reflected', ?, '', ?, 'worldview', ?)",
            (item_id, item_id, verdict, reason, ts),
        )


# ---------------------------------------------------------------------------
# store：record_reflection 硬断言 + 留痕
# ---------------------------------------------------------------------------
def test_record_reflection_requires_reason(env):
    """空 reason = 本次反思作废：返回 False，且 item_history 不写反射痕。"""
    store = _store(env)
    assert store.record_reflection("item-1", "revise", "   ") is False
    with store._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM item_history WHERE action='reflected'"
        ).fetchone()[0]
    assert n == 0, "空理由不得落痕（硬断言）"


def test_record_reflection_writes_item_history(env):
    """非空 reason → 落 action='reflected' 一行，old_value=verdict、reason 保留。"""
    store = _store(env)
    assert store.record_reflection("item-1", "overturn", "旧看法被新证据推翻") is True
    with store._connect() as conn:
        row = conn.execute(
            "SELECT action, old_value, reason, source FROM item_history "
            "WHERE item_id='item-1' AND action='reflected'"
        ).fetchone()
    assert row is not None
    assert row[0] == "reflected"
    assert row[1] == "overturn"
    assert row[2] == "旧看法被新证据推翻"
    assert row[3] == "profile_item"  # subject_kind 默认


# ---------------------------------------------------------------------------
# 注入读取：空库 / 最近优先 + char_cap
# ---------------------------------------------------------------------------
def test_get_reflections_for_injection_empty(env):
    store = _store(env)
    assert store.get_reflections_for_injection() == []
    assert build_reflection_context(base_dir=env) == ""


def test_get_reflections_for_injection_recent(env):
    """最近优先：限额 limit 取最新的 N 条；总字数受 char_cap 约束。"""
    store = _store(env)
    _seed_reflected(store, "i1", "maintain", "理由1", "2026-10-01T00:00:00", "看法一")
    _seed_reflected(store, "i2", "revise", "理由2", "2026-10-02T00:00:00", "看法二")
    _seed_reflected(store, "i3", "overturn", "理由3", "2026-10-03T00:00:00", "看法三")

    out = store.get_reflections_for_injection(limit=2)
    assert len(out) == 2
    assert "理由3" in out[0] and "理由2" in out[1]  # 最新在前
    assert "理由1" not in "".join(out)

    # char_cap 约束：cap 很小 → 至少空/截断，不超字
    capped = store.get_reflections_for_injection(limit=3, char_cap=10)
    assert sum(len(t) for t in capped) <= 10


# ---------------------------------------------------------------------------
# 注入链：manager 端到端挂线 + 开关
# ---------------------------------------------------------------------------
def test_build_full_context_injects_reflection_block(env, monkeypatch):
    store = _store(env)
    _seed_reflected(store, "i1", "revise", "因为新证据出现了", "2026-10-03T00:00:00", "看法一")

    from deskmate.memory.manager import DeskMateMemoryManager

    mgr = DeskMateMemoryManager(base_dir=env)
    ctx = mgr.build_full_context_for_turn(user_message="我们接着聊")
    assert "小桔最近的自我反思" in ctx
    assert "因为新证据出现了" in ctx

    # 单独开关关闭 → 反思块不进上下文（总开关 DESKMATE_AUTO_INJECT 另测于 lessons）
    monkeypatch.setenv("DESKMATE_REFLECTION_INJECT", "0")
    ctx_off = mgr.build_full_context_for_turn(user_message="我们接着聊")
    assert "小桔最近的自我反思" not in ctx_off


# ---------------------------------------------------------------------------
# fail-open
# ---------------------------------------------------------------------------
def test_reflection_fail_open(env, monkeypatch):
    """写库异常 / 读库异常 → 返回 False / ""，不抛、不阻断主记忆注入。"""
    store = _store(env)

    def _boom(self):
        raise RuntimeError("db down")

    monkeypatch.setattr(CognitiveStore, "_connect", _boom)
    assert store.record_reflection("i1", "revise", "有理由") is False
    assert build_reflection_context(base_dir=env) == ""