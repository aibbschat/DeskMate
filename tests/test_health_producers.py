"""P2 三健康生产者单元测试（2026-10-07）。

这些生产者 fail-open、依赖真实文件（automation.db / sync_cursor.json / docx），
故在 /tmp 构造输入验证：注册、产出正确记录、节流、确定性 anchor。

**不碰 ~/.deskmate 活库**：base_dir 一律指向 tmp。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "deskmate_base"
    d.mkdir()
    return d


def test_producers_registered():
    from deskmate.proactivity import health_producers  # noqa: F401
    from deskmate.proactivity.ledger import PRODUCERS

    for src in ("automation", "sync_health", "writing"):
        assert src in PRODUCERS, f"{src} 未注册"
        assert callable(PRODUCERS[src])


# ---- 纯函数 ----
def test_hit_fail_words():
    from deskmate.proactivity.health_producers import _hit_fail_words

    assert _hit_fail_words("今天又被火山限额挡住了，跑空") is True
    assert _hit_fail_words("HTTP 429 太多，无法完成") is True
    assert _hit_fail_words("一切正常，产出已落盘") is False


def test_timeline_has_today(base):
    from deskmate.proactivity.health_producers import _timeline_has_today

    tl = base / "auto_tasks" / "timeline.jsonl"
    tl.parent.mkdir()
    today = time.strftime("%Y-%m-%d", time.localtime())
    tl.write_text(
        json.dumps({"task_type": "daily_reading", "date": today}) + "\n"
        + json.dumps({"task_type": "progress_tracking", "date": "2000-01-01"}) + "\n",
        encoding="utf-8",
    )
    assert _timeline_has_today(base, "daily_reading") is True
    assert _timeline_has_today(base, "progress_tracking") is False  # 只有旧日期
    assert _timeline_has_today(base, "missing") is False


def test_cron_missed():
    from deskmate.proactivity.health_producers import _cron_missed

    now = time.time()
    # 每小时整点跑：2 小时前就过了上一次应跑点、且 last_run 更早 → miss
    assert _cron_missed("0 * * * *", now - 7200, now) is True
    # last_run 就在最近一次应跑点之后 → 跑过了
    assert _cron_missed("0 * * * *", now - 60, now) is False
    # 无 cron → 不报
    assert _cron_missed(None, None, now) is False


# ---- automation 生产者 ----
def _make_automation_db(base, *, last_run=None, run_status="ok", task_type="daily_reading"):
    adb = base / "automation.db"
    conn = sqlite3.connect(str(adb))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scheduled_tasks(id TEXT PRIMARY KEY, enabled INTEGER, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_runs(task_id TEXT, started_at TEXT, data TEXT)"
    )
    tid = "t_reading"
    task = {
        "title": "读书账本",
        "task_type": task_type,
        "last_run": last_run if last_run is not None else time.time(),
        "last_status": run_status,
        "schedule": {"cron": "0 8 * * *"},
    }
    conn.execute(
        "INSERT INTO scheduled_tasks(id, enabled, data) VALUES(?,1,?)",
        (tid, json.dumps(task)),
    )
    conn.execute(
        "INSERT INTO task_runs(task_id, started_at, data) VALUES(?,?,?)",
        (tid, "x", json.dumps({"status": run_status})),
    )
    conn.commit()
    conn.close()
    # 构造「今日有 progress_tracking、但无 daily_reading」的时间线，
    # 使 _timeline_has_today('daily_reading') 返回 False → 命中 zero 分支。
    tl = base / "auto_tasks" / "timeline.jsonl"
    tl.parent.mkdir(exist_ok=True)
    today = time.strftime("%Y-%m-%d", time.localtime())
    tl.write_text(
        json.dumps({"task_type": "progress_tracking", "date": today}) + "\n",
        encoding="utf-8",
    )
    return tid


def test_automation_producer_zero_output(base):
    """status=ok 但 timeline 今日无 daily_reading 记录 → 产出 task_anomaly。

    注意：_timeline_has_today 在无 timeline 文件时**保守返回 True**（不误报），
    故必须存在 timeline.jsonl 且今日缺 daily_reading，才命中 zero 分支。
    """
    from deskmate.proactivity.health_producers import _automation_producer

    _make_automation_db(base)
    items = _automation_producer(base_dir=base)
    assert any(it["kind"] == "task_anomaly" for it in items), f"应产出 task_anomaly，实得 {items}"
    # 确定性 anchor（含 today，跨调用合并）
    assert any("zero" in it["anchor"] for it in items)


def test_automation_producer_throttled(base):
    """每小时至多扫一次：第二次调用（同秒）被节流返回 []。"""
    from deskmate.proactivity.health_producers import _automation_producer

    _make_automation_db(base)
    first = _automation_producer(base_dir=base)
    assert first, "首次调用应产出"
    second = _automation_producer(base_dir=base)
    assert second == [], f"节流后应返回空，实得 {second}"


def test_automation_producer_no_db(base):
    """无 automation.db → 静默返回 []（fail-open，不崩）。"""
    from deskmate.proactivity.health_producers import _automation_producer

    items = _automation_producer(base_dir=base)
    assert items == []


# ---- sync_health 生产者 ----
def test_sync_health_drift_from_ctx(base):
    """有 drift 上下文（来自同步层）→ 产出 drift 条目，不依赖本地游标。"""
    from deskmate.proactivity.health_producers import _sync_health_producer

    items = _sync_health_producer(base_dir=base, drift={"profile_items": 5})
    assert any(it["kind"] == "drift" for it in items), f"应产出 drift，实得 {items}"


def test_sync_health_window_failed(base):
    """本地游标超 2h 未更新 → 产出 window_failed。"""
    from deskmate.proactivity.health_producers import _sync_health_producer

    db = base / "deskmate.db"
    cursor = {
        "pull_after_seq": 10,
        "push_after_seq": 10,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 3 * 3600)),
    }
    Path(str(db) + ".sync_cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
    items = _sync_health_producer(base_dir=base)
    assert any(it["kind"] == "window_failed" for it in items), f"应产出 window_failed，实得 {items}"


def test_sync_health_window_ok(base):
    """游标刚更新 → 不报 window_failed。"""
    from deskmate.proactivity.health_producers import _sync_health_producer

    db = base / "deskmate.db"
    cursor = {
        "pull_after_seq": 10,
        "push_after_seq": 10,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60)),
    }
    Path(str(db) + ".sync_cursor.json").write_text(json.dumps(cursor), encoding="utf-8")
    items = _sync_health_producer(base_dir=base)
    assert not any(it["kind"] == "window_failed" for it in items)


# ---- writing 生产者 ----
def test_writing_producer_stall(base, monkeypatch):
    """docx mtime 停笔超阈值 → 产出 stall_flag；新鲜则不报。"""
    from deskmate.proactivity.health_producers import _writing_producer

    docx = base / "manuscript.docx"
    docx.write_bytes(b"PK\x03\x04 fake docx")
    old = time.time() - 10 * 86400  # 10 天前
    os.utime(docx, (old, old))
    monkeypatch.setenv("SCRIBE_DOCX", str(docx))

    items = _writing_producer(base_dir=base)
    assert any(it["kind"] == "stall_flag" for it in items), f"应产出 stall_flag，实得 {items}"

    # 新鲜文档分支：用独立 base（无节流记录）验证「新鲜则不触发」
    base2 = base.parent / "deskmate_base2"
    base2.mkdir()
    docx2 = base2 / "manuscript.docx"
    docx2.write_bytes(b"PK fake")
    fresh = time.time() - 3600  # 1 小时前 = 新鲜
    os.utime(docx2, (fresh, fresh))
    monkeypatch.setenv("SCRIBE_DOCX", str(docx2))
    items_fresh = _writing_producer(base_dir=base2)
    assert not any(it["kind"] == "stall_flag" for it in items_fresh), (
        f"新鲜文档不应报 stall_flag，实得 {items_fresh}"
    )


def test_writing_producer_throttled(base, monkeypatch):
    """每日至多扫一次：第二次调用被节流返回 []。"""
    from deskmate.proactivity.health_producers import _writing_producer

    docx = base / "manuscript.docx"
    docx.write_bytes(b"PK fake")
    old = time.time() - 10 * 86400
    os.utime(docx, (old, old))
    monkeypatch.setenv("SCRIBE_DOCX", str(docx))

    first = _writing_producer(base_dir=base)
    assert first, "首次应产出 stall_flag"
    second = _writing_producer(base_dir=base)
    assert second == [], f"节流后应返回空，实得 {second}"
