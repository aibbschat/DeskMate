"""回填脚本的批写契约：整批一个事务。

文档与 docstring 都写「整批失败不写」。2026-11-17 复核发现实现是逐条 autocommit
——写库中途出错会留下半批。这两条测试把「不部分写入」钉成行为：
① 正常批：全部落库；② 写到第二条炸：一条都不留。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_emotion_objects", ROOT / "scripts" / "backfill_emotion_objects.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load_backfill()

BATCH = [
    ("e1", "2026-11-01T10:00:00", "chat", "他说这段不合史实，把稿子退回来了"),
    ("e2", "2026-11-02T10:00:00", "chat", "我今天核对完三个版本的稿子"),
]


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "deskmate.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE emotion_log ("
        " id TEXT PRIMARY KEY, ts TEXT, source TEXT, description TEXT,"
        " object_ref TEXT NOT NULL DEFAULT '',"
        " object_kind TEXT NOT NULL DEFAULT 'none',"
        " event_summary TEXT NOT NULL DEFAULT '',"
        " attributed INTEGER NOT NULL DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO emotion_log(id, ts, source, description) VALUES(?,?,?,?)",
        [(rid, ts, src, desc) for rid, ts, src, desc in BATCH],
    )
    conn.commit()
    conn.close()
    return p


def _reply():
    return json.dumps(
        [
            {"id": "e1", "object_ref": "稿子", "object_kind": "work",
             "event_summary": "他退回稿件"},
            {"id": "e2", "object_ref": "稿子", "object_kind": "work",
             "event_summary": "核完三个版本"},
        ],
        ensure_ascii=False,
    )


def _rows(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT id, object_ref, object_kind, event_summary, attributed"
            " FROM emotion_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_batch_writes_all_rows(db, monkeypatch):
    monkeypatch.setattr(bf, "_llm_complete", lambda *a, **k: _reply())
    wrote, failed = bf._run_batch(db, BATCH, 1, 5.0)
    assert (wrote, failed) == (2, 0)
    rows = _rows(db)
    assert [r[4] for r in rows] == [bf.BACKFILLED, bf.BACKFILLED]
    assert rows[0][1] == "稿子" and rows[0][2] == "work"


def test_batch_rolls_back_whole_batch_on_write_error(db, monkeypatch):
    """第二条 UPDATE 抛错：第一条必须回滚，不留半批。"""
    monkeypatch.setattr(bf, "_llm_complete", lambda *a, **k: _reply())

    real_connect = bf._connect

    class _Boom(Exception):
        pass

    def flaky_connect(path):
        real = real_connect(path)
        state = {"updates": 0}

        class _Wrapper:
            def execute(self, sql, params=()):
                if sql.startswith("UPDATE emotion_log"):
                    state["updates"] += 1
                    if state["updates"] == 2:
                        raise _Boom()
                return real.execute(sql, params)

            def close(self):
                real.close()

        return _Wrapper()

    monkeypatch.setattr(bf, "_connect", flaky_connect)
    wrote, failed = bf._run_batch(db, BATCH, 1, 5.0)
    assert (wrote, failed) == (0, 1)
    assert [r[4] for r in _rows(db)] == [0, 0], "半批写入未回滚"


def test_backfill_prompt_bans_observer_voice():
    """回填 prompt 也要管视角：摘要写「用户/对方」会把旁观口吻带进情感证据。"""
    assert "不许写「用户」" in bf._SYSTEM
    assert "她的口吻" in bf._SYSTEM
