"""P2 三健康生产者单元测试（2026-10-07）。

这些生产者 fail-open、依赖真实文件（automation.db / sync_cursor.json / docx），
故在 /tmp 构造输入验证：注册、产出正确记录、节流、确定性 anchor。

**不碰活库**：base_dir 一律指向 tmp；automation 生产者还会按候选顺序找 Coworker 的
automation.db（可能落到 `$COWORKER_STATE_DIR`），故相关用例显式把该环境变量也指向 tmp，
免得读到本机/云端的真实库。
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
def test_hit_fail_words_deprecated_always_false():
    """**2026-10-09 改**：关键词失败判据已整条废弃，恒返回 False。

    原断言（命中「火山/限额/429」为 True）已作废——该判据双向都错：
    - 误伤：**成功**汇报原文就含「不擅自删除」「没有超时」等措辞；
    - 漏报：假设「产出必进 artifacts」，而读书任务 artifacts 恒为 []。
    零产出判据改为只认 timeline.jsonl 硬锚点。
    """
    from deskmate.proactivity.health_producers import _hit_fail_words

    assert _hit_fail_words("今天又被火山限额挡住了，跑空") is False
    assert _hit_fail_words("HTTP 429 太多，无法完成") is False
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
    # 已跑过：last_run 在最近一次应跑点之后（hourly ⇒ 最近应跑点在 1 小时内）
    assert _cron_missed("0 * * * *", now - 60, now) is False
    # 没跑过：last_run=None，应跑点已过 30 分钟宽限 → miss
    assert _cron_missed("0 * * * *", None, now) is True
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


def test_automation_db_path_finds_coworker_state_dir(monkeypatch, tmp_path):
    """回归：automation.db 的真实落点是 Coworker 的 state_dir，不是 deskmate 目录。

    历史 bug：误写成 `<deskmate_base>/automation.db`（两端都不存在）→ 生产者永远空转。
    本用例把 COWORKER_STATE_DIR 指向 tmp 并放一个 automation.db，断言解析器能找到它。
    """
    from deskmate.proactivity.health_producers import _automation_db_path

    sd = tmp_path / "coworker_state"
    sd.mkdir()
    (sd / "automation.db").write_bytes(b"")
    monkeypatch.setenv("COWORKER_STATE_DIR", str(sd))

    # 不传 base_dir → 应落到 COWORKER_STATE_DIR
    assert _automation_db_path() == sd / "automation.db"
    # 传一个不含 automation.db 的 base_dir → 仍应回落到 state_dir（不再死看 deskmate 目录）
    empty_base = tmp_path / "deskmate_base"
    empty_base.mkdir()
    assert _automation_db_path(empty_base) == sd / "automation.db"


def test_automation_producer_no_db(base, monkeypatch, tmp_path):
    """无 automation.db → 静默返回 []（fail-open，不崩）。"""
    from deskmate.proactivity.health_producers import _automation_producer

    # 隔离 Coworker state_dir（否则会读到本机真实的 ~/.config/coworker/automation.db）
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "coworker_state"))
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


# ---- 2026-10-09：automation 误报止血（方案 A）----


def test_disabled_producers_defaults_to_automation():
    """默认停用 automation（误报止血），其余两个健康生产者保留。"""
    from deskmate.proactivity.disabled_producers import (
        MISREPORTING,
        disabled_producers,
        exclude_for_producers_all,
    )

    assert MISREPORTING == ("automation",)
    assert disabled_producers() == ("automation",)
    # sync_health / writing 实测产出真实且有价值（同步漂移、停笔位点），必须保留
    assert "sync_health" not in disabled_producers()
    assert "writing" not in disabled_producers()


def test_exclude_keeps_cloud_audit_default():
    """exclude 会**整体替换** ledger 的默认 exclude，必须把 cloud_audit 带上。

    run_producers_all 的签名默认 exclude=("cloud_audit",)（它由 tick 的 SUGGEST 分支负责）。
    若只传停用名单，cloud_audit 会在每个周期窗口被多跑一遍 —— 白烧 token。
    """
    from deskmate.proactivity.disabled_producers import exclude_for_producers_all

    exc = exclude_for_producers_all()
    assert "cloud_audit" in exc
    assert "automation" in exc


def test_disabled_producers_env_override(monkeypatch):
    """环境变量可覆盖/一键回滚：显式设值完全覆盖默认（用于止血与对拍）。"""
    from deskmate.proactivity import disabled_producers as D

    monkeypatch.setenv(D._ENV_KEY, "sync_health")
    assert D.disabled_producers() == ("sync_health",)

    monkeypatch.setenv(D._ENV_KEY, "")  # 空串 = 一个都不停（回滚）
    assert D.disabled_producers() == ()
    assert D.exclude_for_producers_all() == ("cloud_audit",)

    monkeypatch.setenv(D._ENV_KEY, " automation , writing ")  # 去空白去重
    assert D.disabled_producers() == ("automation", "writing")


def test_run_producers_all_actually_skips_automation(monkeypatch, tmp_path):
    """端到端：run_producers_all 接到 exclude 后，automation 生产者**不被调用**。

    这条锁的是真回归风险——exclude 只是参数，若调用方忘了传，误报会立刻回来，
    而台账里不会留下任何痕迹（它安静地又开始报）。
    """
    from deskmate.proactivity import disabled_producers as D
    from deskmate.proactivity import health_producers as HP
    from deskmate.proactivity import ledger as L

    called = []
    monkeypatch.setitem(L.PRODUCERS, "automation", lambda **k: called.append("automation") or [])
    monkeypatch.setitem(L.PRODUCERS, "writing", lambda **k: called.append("writing") or [])

    monkeypatch.delenv(D._ENV_KEY, raising=False)
    L.run_producers_all(base_dir=tmp_path, exclude=D.exclude_for_producers_all())
    assert "automation" not in called
    assert "writing" in called, "保留的生产者仍应被调用"

    # 对拍：清空停用清单 → automation 恢复（证明是 exclude 在起作用，不是别的）
    called.clear()
    monkeypatch.setenv(D._ENV_KEY, "")
    L.run_producers_all(base_dir=tmp_path, exclude=D.exclude_for_producers_all())
    assert "automation" in called



# ---- 2026-10-09 方案B：automation 误报三处修复的回归锁 ----


def test_title_stem_splits_on_colon():
    """缺陷②依据：title 与 timeline task_name 是冒号前缀关系（实测）。"""
    from deskmate.proactivity.health_producers import _title_stem

    assert _title_stem("每日读书：萧盛已出版作品") == "每日读书"
    assert _title_stem("进度跟踪：国子监秘闻录") == "进度跟踪"
    assert _title_stem("无冒号标题") == "无冒号标题"


def test_timeline_matches_by_task_name_not_task_type(tmp_path):
    """**缺陷②回归**：判据必须按 task_name 匹配。

    实测 5 个启用任务的 task_type 全是 None → 依赖它会让该分支永不触发。
    """
    from deskmate.proactivity.health_producers import _timeline_has_today

    bd = tmp_path / "bd_tl2"
    bd.mkdir()
    tl = bd / "auto_tasks" / "timeline.jsonl"
    tl.parent.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d", time.localtime())
    # 只有 task_name，没有 task_type —— 真实数据即如此
    tl.write_text(
        json.dumps({"task_name": "每日读书", "date": today}, ensure_ascii=False) + chr(10),
        encoding="utf-8",
    )
    assert _timeline_has_today(bd, "每日读书") is True
    assert _timeline_has_today(bd, "写作营") is False


def test_running_task_not_reported_as_missed(tmp_path, monkeypatch):
    """**缺陷①回归（真实故障）**：任务运行期间不得报「该跑却没跑成」。

    现场：读书任务 10:00:57 开始、10:37:16 结束，误报发生在 10:34:13 ——
    跑了 36 分钟，它在第 34 分钟报警。根因：last_run 只在**结束**时更新。
    """
    import sqlite3

    from deskmate.proactivity.health_producers import _automation_producer

    base = tmp_path / "bd_running"
    # 隔离：不读本机/云端真实 automation.db（沿用文件头约定）
    monkeypatch.setenv("COWORKER_STATE_DIR", str(base))
    base.mkdir(parents=True, exist_ok=True)
    adb = base / "automation.db"
    c = sqlite3.connect(str(adb))
    c.execute("CREATE TABLE scheduled_tasks (id TEXT PRIMARY KEY, enabled INTEGER, data TEXT)")
    c.execute(
        "CREATE TABLE task_runs (run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL, data TEXT)"
    )
    tid = "t_running"
    c.execute(
        "INSERT INTO scheduled_tasks VALUES (?,1,?)",
        (tid, json.dumps({"title": "每日读书：萧盛已出版作品", "schedule": {"cron": "0 * * * *"}})),
    )
    # 关键：task_runs 有本次 started_at（刚刚开始），但 last_run 仍是 None（**未结束**）
    c.execute(
        "INSERT INTO task_runs VALUES (?,?,?,?)",
        ("r1", tid, time.time() - 60, json.dumps({"status": "running"})),
    )
    c.commit()
    c.close()

    items = _automation_producer(base_dir=base)
    kinds = [i["anchor"].rsplit(":", 1)[-1] for i in items]
    assert "miss" not in kinds, f"运行中的任务被误报为 miss: {items}"


def test_stale_task_still_reported_as_missed(tmp_path, monkeypatch):
    """反向锁：修缺陷①**不能**把真 miss 也一起放过。"""
    import sqlite3

    from deskmate.proactivity.health_producers import _automation_producer

    base = tmp_path / "bd_stale"
    # 隔离：不读本机/云端真实 automation.db（沿用文件头约定）
    monkeypatch.setenv("COWORKER_STATE_DIR", str(base))
    base.mkdir(parents=True, exist_ok=True)
    adb = base / "automation.db"
    c = sqlite3.connect(str(adb))
    c.execute("CREATE TABLE scheduled_tasks (id TEXT PRIMARY KEY, enabled INTEGER, data TEXT)")
    c.execute("CREATE TABLE task_runs (run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL, data TEXT)")
    tid = "t_stale"
    c.execute(
        "INSERT INTO scheduled_tasks VALUES (?,1,?)",
        (tid, json.dumps({"title": "该跑没跑", "schedule": {"cron": "0 * * * *"}})),
    )
    # 没有任何 run 记录、last_run 也为 None → 真 miss
    c.commit()
    c.close()

    items = _automation_producer(base_dir=base)
    kinds = [i["anchor"].rsplit(":", 1)[-1] for i in items]
    assert "miss" in kinds, f"真 miss 被漏报了: {items}"
