"""PR14/PR15 guards — unified SQLite connect, scripts are safe by default."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from deskmate.memory._sqlite import connect


def test_connect_sets_busy_timeout_and_wal(tmp_path):
    db = tmp_path / "t.db"
    conn = connect(db)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_connect_readonly(tmp_path):
    db = tmp_path / "ro.db"
    connect(db).close()  # 先把库建出来
    conn = connect(db, readonly=True, isolation_level=None)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
        try:
            conn.execute("CREATE TABLE x (a)")
            created = True
        except Exception:
            created = False
        assert created is False
    finally:
        conn.close()


def test_learned_store_connection_has_busy_timeout(tmp_path):
    from deskmate.memory.learned_store import LearnedStore

    store = LearnedStore(tmp_path)
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    finally:
        conn.close()


def test_session_model_persists(tmp_path):
    """#30：会话级模型落库，且元数据读取立刻可见（引擎按 record.model 走）。"""
    from coworker.conversations import ConversationStore
    from coworker.sessions import SessionRecord

    store = ConversationStore(tmp_path)
    sid = "abc123def456"
    store.save(
        SessionRecord(
            session_id=sid, workspace=str(tmp_path), model="gpt-a",
            mode="interactive", messages=[], title="t", agent="code",
        )
    )
    assert store.load_meta(sid).model == "gpt-a"
    assert store.set_model(sid, "gpt-b") is True
    assert store.load_meta(sid).model == "gpt-b"
    # 空串 = 回到全局默认
    assert store.set_model(sid, "") is True
    assert store.load_meta(sid).model == ""


def test_audit_script_reports_clean():
    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "audit_async_blocking.py"), "--json"],
        capture_output=True, text=True, cwd=repo,
    )
    assert out.returncode == 0
    assert json.loads(out.stdout) == []


def test_doctor_and_prune_are_dry_run(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / "sub").mkdir()
    (tmp_path / "deskmate.db.bak").write_bytes(b"x" * 100)
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "prune_deskmate_backups.py"),
         "--base", str(tmp_path), "--keep", "1"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0 and "dry-run" in out.stdout
    assert (tmp_path / "deskmate.db.bak").is_file()  # 默认不动文件
    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "deskmate_doctor.py"),
         "--base", str(tmp_path), "--json"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert json.loads(out.stdout)["base"].endswith(str(tmp_path))
