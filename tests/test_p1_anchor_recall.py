"""P1 汇流最小版 · 顺锚走一跳召回（R3）。

覆盖：同锚邻座被 seed 带出（带 via_anchor/evidence）、开关即时回退纯 FTS、
邻座封顶、无锚数据保持纯 FTS、顺锚异常 fail-open 不空结果。
"""

from __future__ import annotations

import pytest

from deskmate.cognitive.models import CognitiveProfile, Stance
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory import sync_store_sqlite as sss


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_ANCHOR_RECALL", raising=False)
    return tmp_path


def _store(env) -> CognitiveStore:
    """建库并补 sync migrate（否则 persist 写 sync_uuid 列报错，recall 不可达）。"""
    store = CognitiveStore(db_path=env / "deskmate.db")
    with store._connect() as conn:
        sss.migrate(conn, "mac", "profile_items")
        sss.migrate(conn, "mac", "memory_anchors")
    return store


def _stance(sid: str, content: str, topic: str = "") -> Stance:
    return Stance(id=sid, content=content, topic=topic, confidence=0.5)


def _persist(store: CognitiveStore, stances: list[Stance]) -> None:
    p = CognitiveProfile()
    p.stances = stances
    store.persist(p, layer="cognitive")


def _anchor(store: CognitiveStore, item_id: str, kind: str, value: str) -> None:
    store.upsert_anchors("profile_item", item_id, [(kind, value)])


def test_anchor_recall_hops_to_neighbor(env):
    """seed 命中 A，A 与 B 同锚 → 召回含 B，且带 via_anchor + evidence。

    注意：A/B 的 topic 都留空、只有锚里有主题，以此证明 B 是**顺锚**带回，
    而非 FTS 直接命中（避免脆弱断言：只断言含某 id，不断言完整列表）。
    """
    store = _store(env)
    _persist(store, [
        _stance("s_A", "史料必须交叉比对"),
        _stance("s_B", "孤证不能单独成立"),
    ])
    _anchor(store, "s_A", "topic", "历史真实性")
    _anchor(store, "s_B", "topic", "历史真实性")

    hits = store.recall_items("史料")
    ids = [h["id"] for h in hits]
    assert ids[0] == "s_A" and "s_B" in ids

    neighbor = next(h for h in hits if h["id"] == "s_B")
    assert neighbor["via_anchor"] is True
    assert "历史真实性" in neighbor["anchor_evidence"]


def test_anchor_recall_off_returns_pure_fts(env, monkeypatch):
    """DESKMATE_ANCHOR_RECALL=0 → 即时退回纯 FTS，无邻座。"""
    monkeypatch.setenv("DESKMATE_ANCHOR_RECALL", "0")
    store = _store(env)
    _persist(store, [
        _stance("s_A", "史料必须交叉比对"),
        _stance("s_B", "孤证不能单独成立"),
    ])
    _anchor(store, "s_A", "topic", "历史真实性")
    _anchor(store, "s_B", "topic", "历史真实性")

    assert [h["id"] for h in store.recall_items("史料")] == ["s_A"]


def test_anchor_recall_no_anchors_keeps_pure_fts(env):
    """无锚数据（表空）→ 行为 = 今天纯 FTS，结果不带 via_anchor 键。"""
    store = _store(env)
    _persist(store, [
        _stance("s_A", "史料必须交叉比对"),
        _stance("s_B", "孤证不能单独成立"),
    ])
    hits = store.recall_items("史料")
    assert [h["id"] for h in hits] == ["s_A"]
    assert "via_anchor" not in hits[0]


def test_anchor_recall_cap(env):
    """同锚邻座 > 上限 → 邻座数 ≤ _ANCHOR_NEIGHBOR_CAP（防爆炸）。"""
    from deskmate.cognitive.store import _ANCHOR_NEIGHBOR_CAP

    store = _store(env)
    _persist(store, [_stance("s_A", "史料必须交叉比对")]
             + [_stance(f"n{i}", f"另一件事{i}") for i in range(9)])
    _anchor(store, "s_A", "topic", "历史真实性")
    for i in range(9):
        _anchor(store, f"n{i}", "topic", "历史真实性")

    hits = store.recall_items("史料")
    neighbors = [h for h in hits if h.get("via_anchor")]
    assert 0 < len(neighbors) <= _ANCHOR_NEIGHBOR_CAP


def test_anchor_recall_fail_open(env, monkeypatch):
    """顺锚环节抛异常 → 只 warning，仍返回 seed（绝不空结果、绝不抛）。"""
    store = _store(env)
    _persist(store, [_stance("s_A", "史料必须交叉比对")])

    def _boom(self, seeds, excl):
        raise RuntimeError("boom")

    monkeypatch.setattr(CognitiveStore, "_recall_anchor_neighbors", _boom)
    assert [h["id"] for h in store.recall_items("史料")] == ["s_A"]


# ---------------------------------------------------------------------------
# 工单 §七：抽锚 / 去重 / 同步明文
# ---------------------------------------------------------------------------


def test_anchor_extract_topic_book():
    """样例文本 → 抽出期望的 topic/book 锚；非 book 来源不产 book 锚。"""
    from deskmate.cognitive.extractor import extract_anchors

    anchors = extract_anchors(
        "《万历十五年》里讲，史料要交叉比对", "万历十五年", "book",
        [{"topic": "历史真实性"}],
    )
    assert ("topic", "历史真实性") in anchors
    assert ("book", "万历十五年") in anchors
    # 非 book 来源 → 只有 topic，无 book
    assert extract_anchors("随便一段", "某文章", "article", [{"topic": "创作观"}]) == [
        ("topic", "创作观")
    ]


def test_anchor_dedup(env):
    """邻座不与 FTS 命中 / exclude_ids 重复。"""
    store = _store(env)
    _persist(store, [
        _stance("s_A", "史料必须交叉比对"),
        _stance("s_B", "孤证不能单独成立"),
        _stance("s_C", "第三条同锚但不该出现"),
    ])
    for i in ("s_A", "s_B", "s_C"):
        _anchor(store, i, "topic", "历史真实性")

    hits = store.recall_items("史料", exclude_ids={"s_C"})
    ids = [h["id"] for h in hits]
    assert "s_C" not in ids          # exclude_ids 生效（邻座也不越过它）
    assert len(ids) == len(set(ids))  # seed 与邻座之间无重复
    assert ids[0] == "s_A"


def test_anchor_sync_plaintext():
    """memory_anchors 已在 SYNC_TABLES 注册，且无向量列（启动守卫通过）。"""
    from deskmate.memory import sync_store_sqlite as sss

    spec = sss.SYNC_TABLES["memory_anchors"]
    assert spec["pk"] == "row_key" and spec["sync_key"] == "row_key"
    assert spec["sync_uuid_kind"] == "string"
    # 启动守卫：任何向量列都会启动即报错——调用通过即证明纯文本
    sss._validate_sync_tables_no_vectors()