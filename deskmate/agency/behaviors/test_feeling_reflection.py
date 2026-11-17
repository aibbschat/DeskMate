"""情感回顾（R3）的校验与落库契约。

R3 是唯一会「替她写下判断」的入口，护栏必须逐条钉住：证据不足、跨期不够、
情绪总结、旁观口吻、抄事件摘要——全部要拒；写进去的留痕条数必须是**她真正
引用的事件数**，不是池子里的总量。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from deskmate.agency.behaviors.feeling_reflection import (
    _parse_items,
    _validate,
    run_feeling_reflection,
)
from deskmate.cognitive.store import CognitiveStore


def _pool_entry(ref="夜不收", kind="work", n=3):
    evs = [
        {"src": "emotion_log", "id": "e1", "ts": "2026-10-01T10:00:00", "summary": "第七章改到第七遍"},
        {"src": "emotion_log", "id": "e2", "ts": "2026-10-05T10:00:00", "summary": "雨夜那段定稿"},
        {"src": "emotion_log", "id": "e3", "ts": "2026-10-11T10:00:00", "summary": "重读又停在那句上"},
        {"src": "emotion_log", "id": "e4", "ts": "2026-10-02T10:00:00", "summary": "他看后说这章立住了"},
    ]
    return {
        "object_ref": ref, "object_kind": kind, "n": n, "days": 3,
        "summaries": 4, "first_ts": "2026-10-01T10:00:00",
        "last_ts": "2026-10-11T10:00:00", "span_days": 10, "evidence": evs,
    }


GOOD = {
    "object": "夜不收", "kind": "work",
    "stance": "第七章那个雨夜我读了三遍，像他改稿时不肯松手的样子。",
    "work_anchor": {"level": "chapter", "what": "第七章"},
    "real_link": "像他改稿时不肯松手的样子",
    "valence": "negative", "evidence": ["e1", "e2", "e3"], "confidence": 0.5,
}


def _pool_map():
    return {"夜不收": _pool_entry(), "稿子": _pool_entry("稿子", "thing")}


@pytest.mark.parametrize(
    "mutate, expect",
    [
        (lambda d: d.update(evidence=["e1", "e2"]), "evidence_lt3"),
        (lambda d: d.update(evidence=["e1", "e4"]), "evidence_lt3"),
        (lambda d: d.update(evidence=["e1", "e2", "e3", "nope"]), None),
        (lambda d: d.update(object="萧盛"), "object_not_in_pool"),
        (lambda d: d.update(kind="person"), "kind_mismatch"),
        (lambda d: d.update(stance="短"), "stance_len"),
        (lambda d: d.update(stance="它" * 81), "stance_len"),
        (lambda d: d.update(stance="夜不收让我焦虑，一交出去就心慌。"), "emotion_summary"),
        (lambda d: d.update(stance="用户说这稿交得漂亮，我也觉得。"), "banned_voice"),
        (lambda d: d.update(stance="第七章改到第七遍"), "copied_summary"),
        # —— 2026-11-17 判据：work 必须落具体指向 + 现实连接 ——
        (lambda d: d.update(work_anchor=None), "work_no_anchor"),
        (lambda d: d.update(real_link=""), "work_no_real_link"),
        (lambda d: d.update(real_link="交付流程对得上"), "work_no_real_link"),
        (
            lambda d: d.update(
                stance="第九章那个雨夜我读了三遍，像他改稿时不肯松手的样子。",
                work_anchor={"level": "chapter", "what": "第九章"},
            ),
            "anchor_not_grounded",
        ),
        (lambda d: d.update(object="稿子", kind="thing"), "category_not_individual"),
    ],
)
def test_validate_rejects(mutate, expect):
    item = dict(GOOD)
    mutate(item)
    clean, why = _validate(item, _pool_map())
    if expect is None:
        assert clean is not None, why
    else:
        assert clean is None and why.startswith(expect), why


def test_validate_accepts_good_item():
    clean, why = _validate(dict(GOOD), _pool_map())
    assert clean is not None, why
    assert len(clean["evidence"]) == 3
    assert clean["confidence"] == 0.5
    assert clean["work_anchor"] == {"level": "chapter", "what": "第七章"}
    assert clean["real_link"] == "像他改稿时不肯松手的样子"


def test_parse_items_tolerates_fence_and_garbage():
    fenced = "```json\n" + json.dumps({"items": [GOOD]}, ensure_ascii=False) + "\n```"
    assert len(_parse_items(fenced)) == 1
    assert _parse_items("") == []
    assert _parse_items("不是 JSON") == []


def test_prompts_carry_work_individual_criterion():
    """判据必须同时写进 R3 与两份归因 prompt，避免只在代码里拦、模型仍照旧写。"""
    from deskmate.agency.behaviors.feeling_reflection import _SYSTEM as r3_system
    from deskmate.growth.emotion_engine import _EMOTION_SYSTEM as online_system
    from scripts.backfill_emotion_objects import _SYSTEM as backfill_system

    assert "具体指向" in r3_system and "real_link" in r3_system
    for system in (online_system, backfill_system):
        assert "某一章" in system
        assert "不是情感对象" in system
        # 旧的「即使没有书名也算 work」必须被替换掉
        assert "即使没有书名也算 work" not in system


# ----------------------------------------------------------------------
# 集成：一条真实事件池 → 写库 + 留痕条数诚实
# ----------------------------------------------------------------------
def _mk_db(tmp_path):
    db = tmp_path / "deskmate.db"
    CognitiveStore(db_path=db)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS emotion_log (
            id TEXT PRIMARY KEY, ts TEXT, source TEXT, event_summary TEXT,
            object_ref TEXT, object_kind TEXT, description TEXT
        );
        """
    )
    rows = [
        ("e1", "2026-10-01T10:00:00", "chat", "第七章改到第七遍", "夜不收", "work"),
        ("e2", "2026-10-05T10:00:00", "chat", "雨夜那段定稿", "夜不收", "work"),
        ("e3", "2026-10-11T10:00:00", "chat", "重读又停在那句上", "夜不收", "work"),
        ("e4", "2026-10-02T10:00:00", "chat", "他看后说这章立住了", "夜不收", "work"),
        # 类别泛称的稿子：不进池、不耗调用
        ("g1", "2026-10-01T10:00:00", "chat", "稿子没对上", "稿子", "thing"),
        ("g2", "2026-10-05T10:00:00", "chat", "稿子又改了", "稿子", "thing"),
        ("g3", "2026-10-11T10:00:00", "chat", "稿子交了", "稿子", "thing"),
    ]
    conn.executemany(
        "INSERT INTO emotion_log(id, ts, source, event_summary, object_ref, object_kind)"
        " VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_run_writes_and_reports_only_cited_evidence(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)

    def fake_chat(system, user, **kwargs):
        return json.dumps({"items": [GOOD]}, ensure_ascii=False)

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", fake_chat)
    monkeypatch.setattr("deskmate.memory.llm.memory_llm_available", lambda: True)

    res = run_feeling_reflection(str(tmp_path))
    assert res["reflected"] is True
    assert res["written"] == 1, res
    assert res["pool"] == 1

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT reason, new_value FROM item_history WHERE item_id LIKE 'feeling_%'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # 池里 4 条、她只引了 3 条 → 留痕必须写 3，不许写 4
    assert "3 条" in row[0] and "4 条" not in row[0], row[0]
