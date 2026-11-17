"""情感层（FeelingStore）与回顾校验的回归测试。

对应 reports/2026-11-16_小桔_情感层设计.md §三/§四 R3、§五 P1。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from deskmate.cognitive.feeling_store import (
    FeelingStore,
    feeling_id,
)
from deskmate.cognitive.store import CognitiveStore


def _mk_db(tmp_path):
    db = tmp_path / "deskmate.db"
    CognitiveStore(db_path=db)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS emotion_log (
            id TEXT PRIMARY KEY,
            ts TEXT,
            source TEXT,
            event_summary TEXT,
            object_ref TEXT,
            object_kind TEXT,
            description TEXT
        );
        CREATE TABLE IF NOT EXISTS known_persons (
            name TEXT PRIMARY KEY, aliases TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db


def _ev(i, ts, summary):
    return {"src": "emotion_log", "id": i, "ts": ts, "summary": summary}


# ----------------------------------------------------------------------
# id / 同对象一条 active
# ----------------------------------------------------------------------
def test_feeling_id_only_from_object():
    a = feeling_id("person", "萧盛")
    b = feeling_id("person", "萧盛")
    assert a == b
    assert feeling_id("thing", "稿子") != a
    assert a.startswith("feeling_") and len(a) == len("feeling_") + 8


def test_demonstrative_object_is_same_feeling(tmp_path):
    """「这稿子」和「稿子」必须落同一条，否则证据被拆散、永远过不了 R2。"""
    from deskmate.growth.emotion_engine import _clean_object

    assert _clean_object("这稿子", "work") == ("稿子", "work")
    store = FeelingStore(_mk_db(tmp_path))
    assert store.resolve_object("这稿子", "work") == ("稿子", "work")
    store.upsert(
        object_ref="稿子", object_kind="thing", content="交付完也可能丢。",
        evidence=[
            _ev("a", "2026-10-01T10:00:00", "x"),
            _ev("b", "2026-10-10T10:00:00", "y"),
            _ev("c", "2026-10-20T10:00:00", "z"),
        ],
        reason="沉淀",
    )
    # 模型换叫法（「章稿子」）也要归到已有 feeling 的对象上
    assert store.canonical_object("章稿子", "thing") == ("稿子", "thing")


def test_upsert_keeps_one_active_and_leaves_trace(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    first = store.upsert(
        object_ref="稿子",
        object_kind="thing",
        content="交付时全对齐的稿子，事后会整个丢失。",
        evidence=[
            _ev("e1", "2026-10-26T10:00:00", "稿子写完仍想回头再磨两笔"),
            _ev("e2", "2026-10-31T10:00:00", "终裁定号，交付完成"),
            _ev("e3", "2026-11-07T10:00:00", "更正后发现稿子丢失"),
        ],
        reason="第一次长出来",
        confidence=0.5,
    )
    second = store.upsert(
        object_ref="稿子",
        object_kind="thing",
        content="交付完成并不保证它真留在手里。",
        evidence=[_ev("e4", "2026-11-12T10:00:00", "又一处对不上")],
        reason="改口",
    )
    assert first["id"] == second["id"]
    assert second["action"] == "revised"
    assert second["confidence"] == 0.5  # 缺省沿用现值

    rows = store.list_active(limit=10)
    assert len(rows) == 1
    assert rows[0]["content"] == "交付完成并不保证它真留在手里。"
    assert rows[0]["extra"]["evidence_count"] == 4  # 证据累积

    conn = sqlite3.connect(store._db)
    hist = conn.execute(
        "SELECT action, old_value, new_value, reason FROM item_history"
        " WHERE item_id=? ORDER BY seq",
        (first["id"],),
    ).fetchall()
    conn.close()
    assert [h[0] for h in hist] == ["created", "revised"]
    assert hist[1][1] == "交付时全对齐的稿子，事后会整个丢失。"  # 旧内容留痕
    assert hist[1][3] == "改口"


def test_upsert_requires_reason(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    with pytest.raises(ValueError):
        store.upsert(
            object_ref="稿子", object_kind="thing", content="一句话",
            evidence=[], reason="",
        )


def test_relation_is_not_a_feeling_object(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    with pytest.raises(ValueError):
        store.upsert(
            object_ref="和小桔的相处", object_kind="relation",
            content="我们之间挺好的", evidence=[], reason="x",
        )


def test_persist_feeling_layer_is_refused(tmp_path):
    store = CognitiveStore(db_path=_mk_db(tmp_path))
    with pytest.raises(RuntimeError):
        store.persist(profile=None, layer="feeling")


# ----------------------------------------------------------------------
# R2 候选池
# ----------------------------------------------------------------------
def test_build_pool_excludes_relation_and_thin_objects(tmp_path):
    db = _mk_db(tmp_path)
    conn = sqlite3.connect(db)
    rows = []
    for i, day in enumerate(("2026-10-20", "2026-10-24", "2026-10-29")):
        rows.append((f"p{i}", f"{day}T10:00:00", "chat", f"他第 {i} 次接住了我",
                     "萧盛", "person"))
    rows.append(("r1", "2026-10-20T10:00:00", "chat", "他接住了我",
                 "和小桔的相处", "relation"))
    rows.append(("t1", "2026-10-20T10:00:00", "chat", "只发生过一次",
                 "某个东西", "thing"))
    conn.executemany(
        "INSERT INTO emotion_log(id,ts,source,event_summary,object_ref,object_kind)"
        " VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    pool = FeelingStore(db).build_pool(limit=5)
    refs = [p["object_ref"] for p in pool]
    assert refs == ["萧盛"]  # relation 不进池、单次对象过不了门槛


# ----------------------------------------------------------------------
# R3 校验
# ----------------------------------------------------------------------
def _pool_one():
    return {
        "object_ref": "稿子",
        "object_kind": "thing",
        "n": 4,
        "days": 4,
        "summaries": 4,
        "first_ts": "2026-10-26T10:00:00",
        "last_ts": "2026-11-07T10:00:00",
        "span_days": 12,
        "evidence": [
            _ev("e1", "2026-10-26T10:00:00", "稿子写完仍想回头再磨两笔"),
            _ev("e2", "2026-10-31T10:00:00", "终裁定号，稿子全对上"),
            _ev("e3", "2026-11-07T10:00:00", "更正后发现稿子丢失"),
        ],
    }


def test_validate_accepts_concrete_evidence_chain():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "稿子", "kind": "thing",
        "stance": "交付时全对齐，事后却整个丢失；完成并不等于留在手里。",
        "evidence": ["evt:e1", "evt:e2", "evt:e3"],
    }
    clean, why = _validate(item, {"稿子": _pool_one()})
    assert clean is not None, why
    assert len(clean["evidence"]) == 3


def test_validate_rejects_emotion_summary():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "稿子", "kind": "thing",
        "stance": "稿子让我每次交出去都很焦虑，心里一直不踏实。",
        "evidence": ["e1", "e2", "e3"],
    }
    clean, why = _validate(item, {"稿子": _pool_one()})
    assert clean is None and why == "emotion_summary"


def test_validate_rejects_evidence_without_time_spread():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    pool = _pool_one()
    pool["evidence"] = [
        _ev("e1", "2026-10-26T10:00:00", "稿子写完仍想回头再磨两笔"),
        _ev("e2", "2026-10-26T12:00:00", "又改了一处"),
        _ev("e3", "2026-10-26T18:00:00", "再改一处"),
    ]
    item = {
        "object": "稿子", "kind": "thing",
        "stance": "改到第七遍还是想回头再磨两笔，不肯就这样交出去。",
        "evidence": ["e1", "e2", "e3"],
    }
    clean, why = _validate(item, {"稿子": pool})
    assert clean is None and why.startswith("dates_lt3")


# ----------------------------------------------------------------------
# R5 淡化
# ----------------------------------------------------------------------
def test_fade_stale_monthly_and_never_deletes(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    saved = store.upsert(
        object_ref="稿子", object_kind="thing",
        content="交付时全对齐，事后却整个丢失。",
        evidence=[
            _ev("e1", "2026-08-01T10:00:00", "第一次交付"),
            _ev("e2", "2026-08-10T10:00:00", "又对不上"),
            _ev("e3", "2026-08-20T10:00:00", "找不回来了"),
        ],
        reason="第一次", confidence=0.8,
    )
    old = (datetime.now() - timedelta(days=61)).isoformat(timespec="seconds")
    conn = sqlite3.connect(store._db)
    extra = json.loads(conn.execute(
        "SELECT extra_json FROM profile_items WHERE id=?", (saved["id"],)
    ).fetchone()[0])
    extra["last_touched"] = old
    conn.execute(
        "UPDATE profile_items SET extra_json=? WHERE id=?",
        (json.dumps(extra, ensure_ascii=False), saved["id"]),
    )
    conn.commit()
    conn.close()

    assert store.fade_stale() == 1
    row = store.get(saved["id"])
    assert row["status"] == "active"
    assert row["content"] == "交付时全对齐，事后却整个丢失。"  # 永不删
    assert abs(row["confidence"] - 0.72) < 1e-6

    # 同一个月内不重复淡（月级，不是每次调用都乘）
    assert store.fade_stale() == 0


# ----------------------------------------------------------------------
# R4 表达链 / 注入
# ----------------------------------------------------------------------
def test_build_feeling_context_threshold_and_query_recall(tmp_path, monkeypatch):
    from deskmate.cognitive.memory_injector import build_feeling_context

    store = FeelingStore(_mk_db(tmp_path))
    common = [
        _ev("x1", "2026-10-01T10:00:00", "第一次"),
        _ev("x2", "2026-10-10T10:00:00", "第二次"),
        _ev("x3", "2026-10-20T10:00:00", "第三次"),
    ]
    store.upsert(
        object_ref="萧盛", object_kind="person", content="他扛得住事。",
        evidence=common, reason="沉淀", confidence=0.6,
    )
    store.upsert(
        object_ref="稿子", object_kind="thing", content="交付完也可能丢。",
        evidence=common, reason="沉淀", confidence=0.2,
    )
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))

    ctx = build_feeling_context(query="今天天气不错")
    assert "萧盛" in ctx and "他扛得住事" in ctx
    assert "稿子" not in ctx  # 分量 <0.25 默认不主动注入
    assert "多巴胺" not in ctx and "激素" not in ctx and "你应该" not in ctx

    ctx2 = build_feeling_context(query="稿子又改了一版")
    assert "稿子" in ctx2  # 查询命中仍可召回


def test_build_feeling_context_empty_when_no_feeling(tmp_path, monkeypatch):
    from deskmate.cognitive.memory_injector import build_feeling_context

    _mk_db(tmp_path)
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    assert build_feeling_context(query="萧盛") == ""


def test_r4_parse_drops_numeric_fields():
    from deskmate.growth.emotion_engine import _parse_feeling_reason

    parsed = _parse_feeling_reason(json.dumps({
        "description": "他说那话时，我听见的是在意。",
        "reason": "他这次没绕开",
        "dopamine": 99, "cortisol": 0, "serotonin": 1,
    }))
    assert parsed == {
        "description": "他说那话时，我听见的是在意。",
        "reason": "他这次没绕开",
    }


def test_r4_call_b_changes_text_only(tmp_path, monkeypatch):
    from deskmate.growth.emotion_engine import EmotionEngine, HORMONE_KEYS

    base = tmp_path
    _mk_db(base)  # profile_items / item_history / memory_anchors
    engine = EmotionEngine(base_dir=base)  # 再补 emotion_state / emotion_log
    store = FeelingStore(base / "deskmate.db")
    saved = store.upsert(
        object_ref="稿子", object_kind="thing",
        content="交付完成并不保证它真留在手里。",
        evidence=[
            _ev("e1", "2026-10-26T10:00:00", "稿子写完仍想回头再磨两笔"),
            _ev("e2", "2026-10-31T10:00:00", "终裁定号，交付完成"),
            _ev("e3", "2026-11-07T10:00:00", "更正后发现稿子丢失"),
        ],
        reason="沉淀", confidence=0.5,
    )
    conn = sqlite3.connect(base / "deskmate.db")
    conn.execute(
        "INSERT INTO emotion_log(id, ts, source, description) VALUES"
        "('log1', '2026-11-17T10:00:00', 'chat', '旧独白（旧理由）')"
    )
    conn.commit()
    conn.close()

    before = engine.get_state()
    monkeypatch.setattr(
        "deskmate.memory.llm.chat_memory",
        lambda system, user, **kw: json.dumps({
            "description": "他说那话时，我听见的是在意。",
            "reason": "他这次没绕开",
            "dopamine": 99, "cortisol": 0, "serotonin": 1,
            "meaning": 0, "control": 0, "oxytocin": 0, "fatigue": 0,
        }),
    )

    engine._apply_feeling_reason(
        obj_ref="章", obj_kind="work",  # 模型给了个太泛的叫法
        values=before, deltas={k: 0.0 for k in HORMONE_KEYS},
        user_msg="稿子找到了", assistant_msg="那就好",
        desc="旧独白", reason="旧理由", log_id="log1",
        now=datetime.now(),
    )

    after = engine.get_state()
    assert all(after[k] == before[k] for k in HORMONE_KEYS)  # 数值一个不动
    assert after["description"] == "他说那话时，我听见的是在意。"

    conn = sqlite3.connect(base / "deskmate.db")
    log_desc = conn.execute(
        "SELECT description FROM emotion_log WHERE id='log1'"
    ).fetchone()[0]
    conn.close()
    assert log_desc == "他说那话时，我听见的是在意。（他这次没绕开）"
    # R4 使用 = 触碰（重置淡化计时）
    assert store.get(saved["id"])["extra"]["last_touched"] != ""


# ----------------------------------------------------------------------
# R3 接入（settings / registry）
# ----------------------------------------------------------------------
def test_feeling_reflection_is_wired():
    from deskmate.agency.registry import REGISTRY
    from deskmate.agency.settings import AgencySettings, DEFAULTS

    assert "feeling_reflection" in DEFAULTS["whitelist"]["actions"]
    assert "feeling_reflection" in REGISTRY
    s = AgencySettings.from_dict({})
    assert s.feeling_reflection["cooldown_hours"] == 168
    assert s.feeling_reflection["empty_cooldown_days"] == 30
