"""「开聊即递」回归锁（2026-10-19 修复首页主动开场白断链）。

场景：桌搭空会话首页（DeskHome）挂载时她先开口——此刻服务端**还没有会话记录**
（会话只在首轮用户消息后才建）。修复前 POST /handoff/speak 走到
append_session_messages 返 False → 404，她的话落不进聊天，「开聊即递」整条链断。

本文件锁两条：
A. 空会话 speak：端点先兜底建 deskmate 会话记录，200 且消息落库；
B. 已有会话 speak：行为不变（消息追加到既有记录）。
"""

from __future__ import annotations

import pytest

from deskmate.proactivity.ledger import record_proactive


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """隔离的 DESKMATE_DIR + 一条 pending 的主动产出。"""
    base = tmp_path / "deskmate"
    base.mkdir()
    monkeypatch.setenv("DESKMATE_DIR", str(base))
    rid = record_proactive(
        base_dir=base,
        source="cloud_audit",
        kind="plot_contradiction",
        content="你《国子监秘闻录》第二章的伏笔和第五章对不上",
        evidence="第二章：……；第五章：……",
        understanding="她在核对双线伏笔时发现的",
        confidence=0.9,
        anchor="cloud_audit:plot_contradiction:test1",
    )
    assert rid, "测试前提：台账行必须落库"
    return base, rid


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    return TestClient(create_app(manager))


def test_speak_creates_session_when_missing(desk, tmp_path, monkeypatch):
    """A. 空会话（服务端无记录）speak → 兜底建记录 + 200，她的话落进会话。"""
    from coworker.sessions import SessionRecord
    from coworker.server.manager import SessionManager

    base, rid = desk
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    client = _client(tmp_path)
    session_id = "fresh-home-session-1"

    r = client.post(
        "/v1/deskmate/proactive/handoff/speak",
        json={"id": rid, "session_id": session_id},
    )
    assert r.status_code == 200, f"空会话 speak 应成功，实得 {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True and body["id"] == rid
    # 文案由服务端 compose（第一人称复述 + #id 自报）
    assert "第二章的伏笔" in body["text"]

    # 会话记录已兜底建成，且她的助手消息在里面
    mgr = SessionManager(data_dir=tmp_path / "data")
    rec = mgr.session_store.load(session_id)
    assert isinstance(rec, SessionRecord), "空会话应被兜底建成 deskmate 会话记录"
    assert rec.agent == "deskmate"
    assert len(rec.messages) == 1
    assert rec.messages[0]["role"] == "assistant"
    assert "第二章的伏笔" in rec.messages[0]["content"]


def test_speak_appends_to_existing_session(desk, tmp_path, monkeypatch):
    """B. 已有会话 speak → 消息追加到既有记录（不新建、不覆盖）。"""
    from coworker.sessions import SessionRecord
    from coworker.server.manager import SessionManager

    base, rid = desk
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    client = _client(tmp_path)
    session_id = "existing-session-1"

    mgr = SessionManager(data_dir=tmp_path / "data")
    mgr.session_store.save(
        SessionRecord(
            session_id=session_id,
            workspace="",
            model=mgr.model,
            mode=mgr.mode.value,
            messages=[{"role": "user", "content": "在吗", "ts": 1}],
            agent="deskmate",
        )
    )

    r = client.post(
        "/v1/deskmate/proactive/handoff/speak",
        json={"id": rid, "session_id": session_id},
    )
    assert r.status_code == 200, r.text

    rec = mgr.session_store.load(session_id)
    assert len(rec.messages) == 2
    assert rec.messages[0]["role"] == "user"  # 原有消息不被覆盖
    assert rec.messages[1]["role"] == "assistant"
