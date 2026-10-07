"""P1 汇流最小版 · 端到端集成（R4）：真实抽取链路 → 自动落锚 → 顺锚召回。

与 `test_p1_anchor_recall.py` 的区别：这里**不手工 upsert 锚**，而是经
`CognitiveExtractor.extract_from_text` 真抽条目（LLM 被打桩），验证 R2 的
`_attach_anchors` 挂线与 R3 的 `_recall_anchor_neighbors` 在真实链路贯通。
"""

from __future__ import annotations

import json

import pytest

from deskmate.cognitive.extractor import CognitiveExtractor
from deskmate.cognitive.profile import CognitiveProfileManager
from deskmate.cognitive.store import CognitiveStore
from deskmate.memory import sync_store_sqlite as sss


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_ANCHOR_RECALL", raising=False)
    monkeypatch.delenv("DESKMATE_PERSON_NAMES", raising=False)
    return tmp_path


def _db(env):
    return env / "deskmate.db"


def _store(env) -> CognitiveStore:
    store = CognitiveStore(db_path=_db(env))
    with store._connect() as conn:
        sss.migrate(conn, "mac", "profile_items")
        sss.migrate(conn, "mac", "memory_anchors")
    return store


def _patch_llm(monkeypatch, payloads: list[dict]) -> None:
    """让提取管线的 LLM 按序返回给定 JSON（不联网）。"""
    import deskmate.memory.llm as llmmod

    it = iter(payloads)
    monkeypatch.setattr(llmmod, "llm_available", lambda: True)
    monkeypatch.setattr(llmmod, "chat", lambda **kw: json.dumps(next(it), ensure_ascii=False))


def test_p1_extract_then_recall_closed_loop(env, monkeypatch):
    """R2（抽取落锚）+ R3（顺锚召回）真实链路闭环。"""
    store = _store(env)
    _patch_llm(monkeypatch, [
        {"stances": [{"topic": "历史真实性", "content": "史料必须交叉比对",
                      "evidence": "e", "confidence": 0.9}]},
        {"thinking_frameworks": [{"domain": "历史真实性", "content": "多方印证后下结论",
                                  "steps": ["查源头", "比对"], "evidence": "e", "confidence": 0.8}]},
    ])
    pm = CognitiveProfileManager(profile_path=str(_db(env)))
    ext = CognitiveExtractor(pm)

    r1 = ext.extract_from_text("万历十五年中，史料必须交叉比对", "万历十五年", "book")
    r2 = ext.extract_from_text("她说，多方印证后再下判断", "某文", "article")
    assert r1.get("stances") == 1 and r2.get("thinking_frameworks") == 1

    # ① 抽取即落锚（R2 真实链路）
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT anchor_kind, anchor_value FROM memory_anchors "
            "WHERE item_kind='profile_item'"
        ).fetchall()
    anchors = set(rows)
    assert ("topic", "历史真实性") in anchors   # 两条不同维度共享同一 topic 锚
    assert ("book", "万历十五年") in anchors     # 来源级 book 锚（r1 是 book）

    # ② 顺锚召回命中邻座（R3 真实链路）
    hits = store.recall_items("交叉比对")
    assert hits[0]["content"] == "史料必须交叉比对"   # seed = FTS 直接命中
    assert hits[0].get("via_anchor") is None

    neighbors = [h for h in hits if h.get("via_anchor")]
    assert len(neighbors) == 1
    assert neighbors[0]["content"] == "多方印证后下结论"   # 靠同 topic 锚被带回
    assert neighbors[0]["anchor_evidence"] == "同主题：历史真实性"


def test_p1_extract_survives_anchor_failure(env, monkeypatch):
    """挂锚抛异常 → 抽取主链路不中断（真实链路 fail-open）。"""
    _store(env)
    _patch_llm(monkeypatch, [
        {"stances": [{"topic": "历史真实性", "content": "史料必须交叉比对",
                      "evidence": "e", "confidence": 0.9}]},
    ])

    def _boom(self, *a, **k):
        raise RuntimeError("anchor boom")

    monkeypatch.setattr(CognitiveStore, "upsert_anchors", _boom)

    pm = CognitiveProfileManager(profile_path=str(_db(env)))
    r = CognitiveExtractor(pm).extract_from_text("史料必须交叉比对", "万历十五年", "book")
    assert r.get("stances") == 1     # 锚失败不影响抽取产出
    assert "error" not in r