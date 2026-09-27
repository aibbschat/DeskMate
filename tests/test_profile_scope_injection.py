"""P1-2 认知画像范围化注入（cognitive/store.recall_items + memory_injector 两级注入）测试。

覆盖：FTS 同事务同步（persist / delete_item）、老库自愈回填、两级注入
（核心层 + 长尾召回）、worldview 恒全量、fail-open 退全量、kill switch。
"""

from __future__ import annotations

import sqlite3

import pytest

from deskmate.cognitive.memory_injector import (
    _SCOPE_BUDGET_CHARS,
    build_cognitive_context,
    build_emotional_context,
)
from deskmate.cognitive.models import CognitiveProfile, Stance
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    return tmp_path


def _store(env) -> CognitiveStore:
    return CognitiveStore(db_path=env / "deskmate.db")


def _profile(layer_items: dict[str, list[Stance]]) -> CognitiveProfile:
    p = CognitiveProfile()
    p.stances = layer_items.get("stances", [])
    return p


def _stance(sid: str, content: str, confidence: float = 0.5,
            status: str = "active") -> Stance:
    return Stance(id=sid, content=content, status=status, confidence=confidence)


# ---------------------------------------------------------------------------
# store：FTS 同步与召回
# ---------------------------------------------------------------------------


def test_persist_syncs_fts_and_recall_hits(env):
    store = _store(env)
    store.persist(_profile({"stances": [
        _stance("s1", "写作时必须交叉比对史料", confidence=0.9),
        _stance("s2", "偏好深夜写作", confidence=0.8),
    ]}), layer="cognitive")
    hits = store.recall_items("史料要交叉比对吗", layers=("cognitive",))
    assert [h["id"] for h in hits] == ["s1"]


def test_recall_respects_layer_and_excludes(env):
    store = _store(env)
    store.persist(_profile({"stances": [
        _stance("s1", "写作时必须交叉比对史料"),
    ]}), layer="cognitive")
    store.persist(_profile({"stances": [
        _stance("w1", "史料只是一种视角"),
    ]}), layer="worldview")
    assert store.recall_items("史料", layers=("cognitive",))[0]["id"] == "s1"
    assert store.recall_items("史料", layers=("worldview",))[0]["id"] == "w1"
    assert store.recall_items("史料", exclude_ids={"s1"})[0]["id"] == "w1"


def test_delete_item_removes_from_fts(env):
    store = _store(env)
    store.persist(_profile({"stances": [_stance("s1", "写作时必须交叉比对史料")]}),
                  layer="cognitive")
    assert store.delete_item("s1")
    assert store.recall_items("交叉比对史料") == []


def test_legacy_library_backfill(env):
    """老库升级：条目存在而 FTS 为空 → 初始化时自愈回填。"""
    db = env / "deskmate.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE profile_items (id TEXT PRIMARY KEY, layer TEXT, dimension TEXT,"
        " topic TEXT, content TEXT, category TEXT, status TEXT, confidence REAL,"
        " extra_json TEXT, first_extracted TEXT, last_updated TEXT)"
    )
    con.execute(
        "INSERT INTO profile_items VALUES ('s1','cognitive','stance','','"
        "写作时必须交叉比对史料','','active',0.9,'{}',NULL,NULL)"
    )
    con.commit()
    con.close()
    hits = _store(env).recall_items("交叉比对史料")
    assert [h["id"] for h in hits] == ["s1"]


def test_fts_self_heals_on_count_mismatch(env):
    """质检补（2026-09-26）：FTS **非空**但数量与条目不符 → 初始化即自愈重建。

    真实场景：脚本（如 scripts/restore_cognitive_layer.py）直插 profile_items
    绕过 persist；或某次 persist 的 _rebuild_fts 抛错被 catch。旧的「FTS 为空
    才算失步」判据在这种情况不重建 → 新条目**永久搜不到**（范围化注入静默
    退化为只剩核心层）。
    """
    store = _store(env)
    store.persist(_profile({"stances": [_stance("s1", "写作时必须交叉比对史料")]}),
                  layer="cognitive")
    # 直插一条绕过 persist：FTS 仍 1 行，条目变 2 行 → 数量不符
    con = sqlite3.connect(str(env / "deskmate.db"))
    con.execute(
        "INSERT INTO profile_items(id, layer, dimension, topic, content,"
        " category, status, confidence, extra_json) VALUES"
        " ('s2','cognitive','stance','','深夜写东西时思路最顺','','active',0.8,'{}')"
    )
    con.commit()
    con.close()
    # 重新初始化 → 自愈
    store2 = _store(env)
    con = sqlite3.connect(str(env / "deskmate.db"))
    n_fts = con.execute("SELECT COUNT(*) FROM profile_items_fts").fetchone()[0]
    con.close()
    assert n_fts == 2
    assert [h["id"] for h in store2.recall_items("深夜写东西思路")] == ["s2"]


def test_recall_fail_open(env):
    """FTS 表坏了 → 返回空（调用方退全量），绝不抛异常。"""
    store = _store(env)
    con = sqlite3.connect(str(env / "deskmate.db"))
    con.execute("DROP TABLE IF EXISTS profile_items_fts")
    con.commit()
    con.close()
    assert store.recall_items("史料") == []


# ---------------------------------------------------------------------------
# injector：两级注入
# ---------------------------------------------------------------------------


def _many_items(n: int, start: int = 0) -> list[Stance]:
    """n 条与「史料」无关的条目（置信度递减），用于撑爆预算。"""
    return [
        _stance(f"pad{i}", f"无关条目{i}号讲的是别的事情，跟检索词完全不相干" * 8,
                confidence=0.9 - i * 0.01)
        for i in range(start, start + n)
    ]


def test_two_tier_injection_core_plus_recalled(env):
    budget_items = max(4, _SCOPE_BUDGET_CHARS // 160)
    items = _many_items(budget_items)
    items.append(_stance("s_tail", "写作时必须交叉比对史料", confidence=0.3))
    store = _store(env)
    store.persist(_profile({"stances": items}), layer="cognitive")

    text = build_cognitive_context(query="史料要交叉比对吗")
    assert "写作时必须交叉比对史料" in text  # 长尾召回进来了
    assert "无关条目0号" in text  # 高置信核心层还在
    assert "无关条目{}号".format(budget_items - 1) not in text  # 低价值长尾被裁
    assert "她眼中的" not in text


def test_empty_query_keeps_full_behavior(env):
    items = _many_items(30)
    store = _store(env)
    store.persist(_profile({"stances": items}), layer="cognitive")
    text = build_cognitive_context()  # 不传 query = 历史行为
    for i in range(30):
        assert f"无关条目{i}号" in text


def test_kill_switch_keeps_full_behavior(env, monkeypatch):
    monkeypatch.setenv("DESKMATE_PROFILE_SCOPE", "0")
    items = _many_items(30)
    items.append(_stance("s_tail", "写作时必须交叉比对史料", confidence=0.3))
    _store(env).persist(_profile({"stances": items}), layer="cognitive")
    text = build_cognitive_context(query="史料")
    assert "写作时必须交叉比对史料" in text
    assert "无关条目29号" in text


def test_worldview_always_full(env):
    """worldview 层恒全量——「她是谁」不因召回缺席。"""
    items = _many_items(30)
    store = _store(env)
    store.persist(_profile({"stances": items}), layer="worldview")
    text = build_cognitive_context(query="史料")
    for i in range(30):
        assert f"无关条目{i}号" in text


def test_fts_broken_falls_back_to_full(env):
    items = _many_items(30)
    store = _store(env)
    store.persist(_profile({"stances": items}), layer="cognitive")
    con = sqlite3.connect(str(env / "deskmate.db"))
    con.execute("DROP TABLE profile_items_fts")
    con.commit()
    con.close()
    text = build_cognitive_context(query="史料")
    assert "无关条目0号" in text  # 没崩，退回全量


def test_emotional_context_scoped(env):
    items = _many_items(max(4, _SCOPE_BUDGET_CHARS // 160))
    items.append(_stance("e_tail", "他低落时靠整理史料缓过来", confidence=0.3))
    _store(env).persist(_profile({"stances": items}), layer="emotional")
    text = build_emotional_context(query="他最近状态低落怎么办")
    assert "他低落时靠整理史料缓过来" in text
