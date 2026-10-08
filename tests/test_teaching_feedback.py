"""R10 · 教学回流（江河设计 §四.4「拦必留教」）——裁决提议卡 → 自动写 lesson。

覆盖：resolve_proposal 三种裁决（executed/rejected/never）各留一条 lesson、
scope 映射（never→permanent，其余→recent）、source='adjudication'、
user_note 并入正文、注入读取能取到、以及 fail-open（写 lesson 失败不影响主裁决）。
"""

from __future__ import annotations

import sqlite3

import pytest

from deskmate.agency.engine import AgencyEngine
from deskmate.agency.task_log import TaskStatus
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("DESKMATE_L2_ENABLED", raising=False)
    return tmp_path


def _propose(eng, action_type="write_file", **extra) -> str:
    rec = eng.log.add(
        action_type=action_type,
        risk="high",
        status=TaskStatus.PROPOSED,
        topic="",
        summary="要动外面的东西",
        detail="",
        count=0,
        extra=extra,
    )
    return rec.id


def _lessons(env):
    store = CognitiveStore(db_path=env / "deskmate.db")
    with store._connect() as conn:
        return conn.execute(
            "SELECT content, scope, source FROM teachings ORDER BY created_at"
        ).fetchall()


def test_executed_writes_recent_lesson(env):
    eng = AgencyEngine(base_dir=str(env))
    rid = _propose(eng)
    assert eng.resolve_proposal(rid, TaskStatus.EXECUTED) is True

    rows = _lessons(env)
    assert len(rows) == 1
    content, scope, source = rows[0]
    assert "写文件" in content
    assert "本次放行" in content and "可以继续" in content
    assert scope == "recent"
    assert source == "adjudication"


def test_rejected_writes_recent_lesson(env):
    eng = AgencyEngine(base_dir=str(env))
    rid = _propose(eng)
    assert eng.resolve_proposal(rid, TaskStatus.REJECTED) is True

    content, scope, source = _lessons(env)[0]
    assert "本次拦下" in content and "先问你" in content
    assert scope == "recent"


def test_never_writes_permanent_lesson(env):
    eng = AgencyEngine(base_dir=str(env))
    rid = _propose(eng)
    assert eng.resolve_proposal(rid, TaskStatus.NEVER) is True

    content, scope, source = _lessons(env)[0]
    assert "以后别提" in content and "不要再提议" in content
    assert scope == "permanent"


def test_user_note_merged_into_lesson(env):
    eng = AgencyEngine(base_dir=str(env))
    rid = _propose(eng, user_note="涉及隐私")
    assert eng.resolve_proposal(rid, TaskStatus.REJECTED) is True

    content = _lessons(env)[0][0]
    assert "涉及隐私" in content


def test_lesson_is_injectable(env):
    """写进去要能用得上：get_lessons_for_injection 取得到（permanent 常驻优先）。"""
    eng = AgencyEngine(base_dir=str(env))
    assert eng.resolve_proposal(_propose(eng), TaskStatus.NEVER) is True

    store = CognitiveStore(db_path=env / "deskmate.db")
    out = store.get_lessons_for_injection()
    assert out and any("不要再提议" in t for t in out)


def test_resolve_fail_open_when_lesson_write_breaks(env, monkeypatch):
    """写 lesson 崩了也不能影响主裁决（fail-open）。"""
    eng = AgencyEngine(base_dir=str(env))
    rid = _propose(eng)

    def _boom(*a, **k):
        raise RuntimeError("teachings 表炸了")

    monkeypatch.setattr(CognitiveStore, "add_lesson", _boom)
    assert eng.resolve_proposal(rid, TaskStatus.REJECTED) is True
    # 裁决本身照样落地
    assert all(r.id != rid for r in eng.log.list(limit=50, status=TaskStatus.PROPOSED))


def test_unknown_action_falls_back_to_action_type(env):
    """注册表里没有的动作类型 → 用 action_type 兜底当标题，不崩、照写。"""
    eng = AgencyEngine(base_dir=str(env))
    rec = eng.log.add(
        action_type="mystery_action",
        risk="high",
        status=TaskStatus.PROPOSED,
        topic="",
        summary="",
        detail="",
        count=0,
        extra={},
    )
    assert eng.resolve_proposal(rec.id, TaskStatus.REJECTED) is True
    content = _lessons(env)[0][0]
    assert "mystery_action" in content