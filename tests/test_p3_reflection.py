"""R9 · 反思层 R1（item_history 裁决 + why 留痕 + 每轮注入）。
R10 · 反思层 R2（裁决落地：revise 下调 confidence / overturn 置 superseded + reflected_apply 痕）。

覆盖：record_reflection 硬断言（空 reason 作废）、reflected 留痕、注入读取
（空库 / 最近优先 + char_cap）、manager 端到端挂线及开关、fail-open；
以及 R2 落地（apply_reflection_verdict）、防自循环、覆盖后反思痕不被吞。
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
    # 建表 + 跑同步迁移（补 profile_items 的 sync 三列）：R2 的 persist 生存性
    # 用例要真跑 persist()，而 persist 的 INSERT 无条件写这三列 → 不迁移会报
    # 「no column named sync_uuid」。与 R1 sync 表同款存量债，此处一次性还清。
    import sqlite3

    from deskmate.memory import sync_store_sqlite as sss

    db = tmp_path / "deskmate.db"
    CognitiveStore(db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        sss.migrate(conn, node="test")
        conn.commit()
    finally:
        conn.close()
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


# ---------------------------------------------------------------------------
# R10 · 反思 R2 落地：apply_reflection_verdict
# ---------------------------------------------------------------------------
def _seed_change(store, item_id, action, content, ts, confidence=0.8, status="active"):
    """造一条「真实的认知条目 + 它的变化痕」（反思素材，非 reflected 自身）。"""
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profile_items"
            "(id, layer, dimension, content, status, confidence) "
            "VALUES(?, 'worldview', 'stance', ?, ?, ?)",
            (item_id, content, status, confidence),
        )
        conn.execute(
            "INSERT OR REPLACE INTO item_history"
            "(item_id, seq, action, old_value, new_value, reason, source, ts) "
            "VALUES(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM item_history WHERE item_id=?), "
            "?, '', ?, '', 'obs', ?)",
            (item_id, item_id, action, content, ts),
        )


def _confidence(store, item_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT confidence FROM profile_items WHERE id=?", (item_id,)
        ).fetchone()[0]


def _status(store, item_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT status FROM profile_items WHERE id=?", (item_id,)
        ).fetchone()[0]


def _apply_rows(store, item_id):
    with store._connect() as conn:
        return conn.execute(
            "SELECT action, new_value, reason FROM item_history "
            "WHERE item_id=? AND action='reflected_apply'", (item_id,)
        ).fetchall()


def test_apply_revise_lowers_confidence(env):
    """revise → confidence ×0.6，且留 reflected_apply 痕（reason 透传）。"""
    store = _store(env)
    _seed_change(store, "wv1", "created", "看法一", "2026-10-01T00:00:00", confidence=0.8)
    assert store.apply_reflection_verdict("wv1", "revise", "新证据出现了") is True
    assert abs(_confidence(store, "wv1") - 0.48) < 1e-6
    rows = _apply_rows(store, "wv1")
    assert rows == [("reflected_apply", "revise", "新证据出现了")]


def test_apply_overturn_deactivates_item(env):
    """overturn → status 置 superseded（不再活跃 → 召回不可见），并留痕。"""
    store = _store(env)
    _seed_change(store, "wv2", "created", "看法二", "2026-10-01T00:00:00")
    assert store.apply_reflection_verdict("wv2", "overturn", "不再信了") is True
    assert _status(store, "wv2") == "superseded"
    assert _apply_rows(store, "wv2") == [("reflected_apply", "overturn", "不再信了")]


def test_apply_maintain_writes_nothing(env):
    """maintain → 条目不动、不写 reflected_apply（R1 的 reflected 痕已足够）。"""
    store = _store(env)
    _seed_change(store, "wv3", "created", "看法三", "2026-10-01T00:00:00", confidence=0.7)
    assert store.apply_reflection_verdict("wv3", "maintain", "依然认") is True
    assert abs(_confidence(store, "wv3") - 0.7) < 1e-6
    assert _status(store, "wv3") == "active"
    assert _apply_rows(store, "wv3") == []


def test_apply_missing_item_fail_open(env):
    """条目不存在也不抛：留痕照写（回指不存在的 id 不崩）。"""
    store = _store(env)
    assert store.apply_reflection_verdict("ghost", "revise", "无此条") is True


def test_reflected_apply_not_a_change_source(env):
    """防自循环：R2 落地写的 reflected_apply 不进反思素材口径。"""
    from deskmate.agency.behaviors import reflection as rf

    store = _store(env)
    _seed_change(store, "wv1", "created", "看法一", "2026-10-01T00:00:00")
    store.record_reflection("wv1", "revise", "理由", "worldview")
    store.apply_reflection_verdict("wv1", "revise", "理由")

    acts = {c["action"] for c in rf._recent_changes(store, 50)}
    assert "reflected_apply" not in acts
    assert "reflected" not in acts
    assert "created" in acts


def test_run_reflection_applies_revise_and_overturn(env, monkeypatch):
    """端到端：run_reflection 解析出 revise/overturn → 真落到条目上。"""
    from deskmate.agency.behaviors import reflection as rf

    store = _store(env)
    _seed_change(store, "wv1", "created", "看法一", "2026-10-01T00:00:00", confidence=0.8)
    _seed_change(store, "wv2", "created", "看法二", "2026-10-02T00:00:00", confidence=0.8)
    _seed_change(store, "wv3", "created", "看法三", "2026-10-03T00:00:00", confidence=0.8)

    monkeypatch.setenv("DESKMATE_REFLECTION_LLM", "1")
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)
    out = (
        "编号：1\n裁决：修正\n理由：新证据\n"
        "编号：2\n裁决：推翻\n理由：不再信"
    )
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda *a, **k: out)

    # importance_threshold=0 → 走旧口径（条数 ≥ min_changes），本用例只验裁决落地
    res = rf.run_reflection(str(env), {"cooldown_hours": 0, "min_changes": 1, "importance_threshold": 0})
    assert res["reflected"] is True
    assert res["verdicts"] == 2
    # batch 顺序 ts DESC → 编号1=wv3（revise），编号2=wv2（overturn）
    assert abs(_confidence(store, "wv3") - 0.48) < 1e-6
    assert _status(store, "wv2") == "superseded"
    assert _apply_rows(store, "wv3") and _apply_rows(store, "wv2")


def test_reflection_survives_layer_overwrite(env):
    """覆盖钉死：整层 persist（世界观回望的落盘方式）不吞掉反思痕。"""
    from deskmate.cognitive.models import CognitiveProfile, Stance

    store = _store(env)
    _seed_change(store, "wv1", "created", "看法一", "2026-10-01T00:00:00")
    store.record_reflection("wv1", "revise", "为什么改的看法", "worldview")

    # 模拟 worldview_reflection 的整层覆盖写：同 id 条目重新 persist 一次
    p = CognitiveProfile()
    p.stances = [Stance(id="wv1", topic="国际冲突", content="看法一（覆盖后）")]
    store.persist(p, layer="worldview")

    out = store.get_reflections_for_injection()
    assert any("为什么改的看法" in t for t in out), "覆盖后反思痕被吞了"