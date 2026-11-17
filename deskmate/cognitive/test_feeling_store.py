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
    """「这本夜不收」和「夜不收」必须落同一条，否则证据被拆散、永远过不了 R2。"""
    from deskmate.growth.emotion_engine import _clean_object

    assert _clean_object("这本夜不收", "work") == ("夜不收", "work")
    store = FeelingStore(_mk_db(tmp_path))
    assert store.resolve_object("这本夜不收", "work") == ("夜不收", "work")
    store.upsert(
        object_ref="夜不收", object_kind="work", content="第七章那个雨夜写得太狠。",
        evidence=[
            _ev("a", "2026-10-01T10:00:00", "x"),
            _ev("b", "2026-10-10T10:00:00", "y"),
            _ev("c", "2026-10-20T10:00:00", "z"),
        ],
        reason="沉淀",
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手的样子",
    )
    # 模型换叫法（「夜不收那本」）也要归到已有 feeling 的对象上
    assert store.canonical_object("夜不收那本", "work") == ("夜不收", "work")
    # 类别泛称不是情感对象：直接退 none
    assert _clean_object("稿子", "work") == ("", "none")
    assert store.resolve_object("稿子", "thing") == ("", "")


def test_upsert_keeps_one_active_and_leaves_trace(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    first = store.upsert(
        object_ref="夜不收",
        object_kind="work",
        content="第七章那个雨夜，他回头那一下写得我发紧。",
        evidence=[
            _ev("e1", "2026-10-26T10:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-10-31T10:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-11-07T10:00:00", "重读时又停在那句上"),
        ],
        reason="第一次长出来",
        confidence=0.5,
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手",
    )
    second = store.upsert(
        object_ref="夜不收",
        object_kind="work",
        content="那个雨夜我记住的不是情节，是他回头那一下的迟疑。",
        evidence=[_ev("e4", "2026-11-12T10:00:00", "又翻回第七章")],
        reason="改口",
    )
    assert first["id"] == second["id"]
    assert second["action"] == "revised"
    assert second["confidence"] == 0.5  # 缺省沿用现值

    rows = store.list_active(limit=10)
    assert len(rows) == 1
    assert rows[0]["content"] == "那个雨夜我记住的不是情节，是他回头那一下的迟疑。"
    assert rows[0]["extra"]["evidence_count"] == 4  # 证据累积

    conn = sqlite3.connect(store._db)
    hist = conn.execute(
        "SELECT action, old_value, new_value, reason FROM item_history"
        " WHERE item_id=? ORDER BY seq",
        (first["id"],),
    ).fetchall()
    conn.close()
    assert [h[0] for h in hist] == ["created", "revised"]
    assert hist[1][1] == "第七章那个雨夜，他回头那一下写得我发紧。"  # 旧内容留痕
    assert hist[1][3] == "改口"


def test_upsert_requires_reason(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    with pytest.raises(ValueError):
        store.upsert(
            object_ref="夜不收", object_kind="work", content="一句话",
            evidence=[], reason="",
        )


def test_upsert_refuses_category_object(tmp_path):
    """「稿子」是类别，不是个体：不许立情感。"""
    store = FeelingStore(_mk_db(tmp_path))
    with pytest.raises(ValueError):
        store.upsert(
            object_ref="稿子", object_kind="thing", content="交付总对不上。",
            evidence=[], reason="x",
        )


def test_retire_hides_but_keeps_content_and_trace(tmp_path):
    """撤下不是删除：status 退 active、内容与历史都在，注入/R4/回池看不到。"""
    store = FeelingStore(_mk_db(tmp_path))
    saved = store.upsert(
        object_ref="夜不收", object_kind="work",
        content="第七章那个雨夜写得我发紧。",
        evidence=[
            _ev("e1", "2026-10-01T10:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-10-10T10:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-10-20T10:00:00", "又翻回第七章"),
        ],
        reason="沉淀", confidence=0.5,
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手",
    )
    assert store.retire("夜不收", "work", reason="对象不成立：撤下留痕") is True
    row = store.get(saved["id"])
    assert row["status"] == "retired"
    assert row["content"] == "第七章那个雨夜写得我发紧。"  # 内容不删
    assert store.list_active() == []
    assert store.context_for("夜不收") == []

    conn = sqlite3.connect(store._db)
    hist = conn.execute(
        "SELECT action, reason FROM item_history WHERE item_id=? ORDER BY seq",
        (saved["id"],),
    ).fetchall()
    conn.close()
    assert [h[0] for h in hist] == ["created", "retired"]
    assert hist[1][1] == "对象不成立：撤下留痕"

    # 撤下是幂等的，不会重复写历史
    assert store.retire("夜不收", "work", reason="再说一次") is False


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


def test_build_pool_skips_category_and_retired(tmp_path):
    """类别泛称不耗调用；已撤下的对象不再回池。"""
    db = _mk_db(tmp_path)
    store = FeelingStore(db)
    rows = []
    for i, day in enumerate(("2026-10-20", "2026-10-24", "2026-10-29")):
        rows.append((f"c{i}", f"{day}T10:00:00", "chat", f"稿子第 {i} 次没对上",
                     "稿子", "thing"))
        rows.append((f"w{i}", f"{day}T11:00:00", "chat", f"第七章第 {i} 次改",
                     "夜不收", "work"))
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO emotion_log(id,ts,source,event_summary,object_ref,object_kind)"
        " VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    saved = store.upsert(
        object_ref="夜不收", object_kind="work",
        content="第七章那个雨夜写得我发紧。",
        evidence=[
            _ev("e1", "2026-10-20T11:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-10-24T11:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-10-29T11:00:00", "又翻回第七章"),
        ],
        reason="沉淀", confidence=0.5,
    )
    refs = [p["object_ref"] for p in store.build_pool(limit=5)]
    assert refs == ["夜不收"]  # 「稿子」是类别，不进池

    assert store.retire("夜不收", "work", reason="暂时撤下") is True
    assert store.build_pool(limit=5) == []  # retired 不回池
    assert store.get(saved["id"])["status"] == "retired"


# ----------------------------------------------------------------------
# R3 校验
# ----------------------------------------------------------------------
def _pool_one():
    return {
        "object_ref": "夜不收",
        "object_kind": "work",
        "n": 4,
        "days": 4,
        "summaries": 4,
        "first_ts": "2026-10-26T10:00:00",
        "last_ts": "2026-11-07T10:00:00",
        "span_days": 12,
        "evidence": [
            _ev("e1", "2026-10-26T10:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-10-31T10:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-11-07T10:00:00", "重读又停在那句上"),
        ],
    }


def _pool_person():
    return {
        "object_ref": "萧盛",
        "object_kind": "person",
        "n": 4,
        "days": 4,
        "summaries": 4,
        "first_ts": "2026-10-26T10:00:00",
        "last_ts": "2026-11-07T10:00:00",
        "span_days": 12,
        "evidence": [
            _ev("e1", "2026-10-26T10:00:00", "他把密码交给我"),
            _ev("e2", "2026-10-31T10:00:00", "他认了我的判断"),
            _ev("e3", "2026-11-07T10:00:00", "他拖着病也没停笔"),
        ],
    }


def test_validate_accepts_work_with_anchor_and_real_link():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "夜不收", "kind": "work",
        "stance": "第七章那个雨夜我读了三遍，像他改稿时不肯松手的样子。",
        "evidence": ["evt:e1", "evt:e2", "evt:e3"],
        "work_anchor": {"level": "chapter", "what": "第七章"},
        "real_link": "像他改稿时不肯松手的样子",
    }
    clean, why = _validate(item, {"夜不收": _pool_one()})
    assert clean is not None, why
    assert clean["work_anchor"]["what"] == "第七章"
    assert len(clean["evidence"]) == 3


def test_validate_rejects_category_object():
    """「稿子」是类别不是个体——即使证据链很完整也拒。"""
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "稿子", "kind": "thing",
        "stance": "交付时全对齐，事后却整个丢失；完成并不等于留在手里。",
        "evidence": ["e1", "e2", "e3"],
    }
    pool = dict(_pool_one(), object_ref="稿子", object_kind="thing")
    clean, why = _validate(item, {"稿子": pool})
    assert clean is None and why == "category_not_individual"


def test_validate_rejects_work_without_anchor():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "夜不收", "kind": "work",
        "stance": "交付时全对齐，事后却整个丢失；完成并不等于留在手里。",
        "evidence": ["e1", "e2", "e3"],
        "real_link": "像他改稿时不肯松手的样子",
    }
    clean, why = _validate(item, {"夜不收": _pool_one()})
    assert clean is None and why == "work_no_anchor"


def test_validate_rejects_work_without_real_link():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "夜不收", "kind": "work",
        "stance": "第七章那个雨夜我读了三遍，还是停在那句上。",
        "evidence": ["e1", "e2", "e3"],
        "work_anchor": {"level": "chapter", "what": "第七章"},
        "real_link": "",
    }
    clean, why = _validate(item, {"夜不收": _pool_one()})
    assert clean is None and why == "work_no_real_link"


def test_validate_rejects_anchor_not_in_evidence():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "夜不收", "kind": "work",
        "stance": "第九章那个雨夜我读了三遍，像他改稿时不肯松手的样子。",
        "evidence": ["e1", "e2", "e3"],
        "work_anchor": {"level": "chapter", "what": "第九章"},
        "real_link": "像他改稿时不肯松手的样子",
    }
    clean, why = _validate(item, {"夜不收": _pool_one()})
    assert clean is None and why.startswith("anchor_not_grounded")


def test_validate_rejects_emotion_summary():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    item = {
        "object": "萧盛", "kind": "person",
        "stance": "他让我每次交出去都很焦虑，心里一直不踏实。",
        "evidence": ["e1", "e2", "e3"],
    }
    clean, why = _validate(item, {"萧盛": _pool_person()})
    assert clean is None and why == "emotion_summary"


def test_validate_rejects_evidence_without_time_spread():
    from deskmate.agency.behaviors.feeling_reflection import _validate

    pool = _pool_person()
    pool["evidence"] = [
        _ev("e1", "2026-10-26T10:00:00", "他改主意了"),
        _ev("e2", "2026-10-26T12:00:00", "他又改了一次"),
        _ev("e3", "2026-10-26T18:00:00", "他最后认了"),
    ]
    item = {
        "object": "萧盛", "kind": "person",
        "stance": "他改主意又回头，最后还是认了；这一路不算轻省。",
        "evidence": ["e1", "e2", "e3"],
    }
    clean, why = _validate(item, {"萧盛": pool})
    assert clean is None and why.startswith("dates_lt3")


# ----------------------------------------------------------------------
# R5 淡化
# ----------------------------------------------------------------------
def test_fade_stale_monthly_and_never_deletes(tmp_path):
    store = FeelingStore(_mk_db(tmp_path))
    saved = store.upsert(
        object_ref="夜不收", object_kind="work",
        content="第七章那个雨夜，他回头那一下写得我发紧。",
        evidence=[
            _ev("e1", "2026-08-01T10:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-08-10T10:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-08-20T10:00:00", "重读时又停在那句上"),
        ],
        reason="第一次", confidence=0.8,
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手",
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
    assert row["content"] == "第七章那个雨夜，他回头那一下写得我发紧。"  # 永不删
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
        object_ref="夜不收", object_kind="work", content="第七章那个雨夜写得我发紧。",
        evidence=common, reason="沉淀", confidence=0.2,
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手",
    )
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))

    ctx = build_feeling_context(query="今天天气不错")
    assert "萧盛" in ctx and "他扛得住事" in ctx
    assert "夜不收" not in ctx  # 分量 <0.25 默认不主动注入
    assert "多巴胺" not in ctx and "激素" not in ctx and "你应该" not in ctx

    ctx2 = build_feeling_context(query="夜不收又改了一版")
    assert "夜不收" in ctx2  # 查询命中仍可召回


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
        object_ref="夜不收", object_kind="work",
        content="第七章那个雨夜，他回头那一下写得我发紧。",
        evidence=[
            _ev("e1", "2026-10-26T10:00:00", "第七章改到第七遍"),
            _ev("e2", "2026-10-31T10:00:00", "雨夜那段定稿"),
            _ev("e3", "2026-11-07T10:00:00", "重读时又停在那句上"),
        ],
        reason="沉淀", confidence=0.5,
        work_anchor={"level": "chapter", "what": "第七章"},
        real_link="像他改稿时不肯松手",
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
        obj_ref="夜不收那本", obj_kind="work",  # 模型给了个别名写法
        values=before, deltas={k: 0.0 for k in HORMONE_KEYS},
        user_msg="夜不收第七章又改了一版", assistant_msg="那就好",
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

    # Call B 触发计数可查：candidates / fired
    conn = sqlite3.connect(base / "deskmate.db")
    stats = json.loads(conn.execute(
        "SELECT data FROM json_blobs WHERE key='feeling_call_b'"
    ).fetchone()[0])
    conn.close()
    assert stats["candidates"] == 1
    assert stats["fired"] == 1


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
