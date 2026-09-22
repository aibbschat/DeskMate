"""访谈触发链路的回归锁（2026-09-22）。

## 背景：一个静默的死循环

真实库里 13 个访谈会话，**只有 1 个完成**，其余 12 个全是 active 僵尸；
而萧哥的体感是「遇上过一两次，后面就没遇上过」。两边对不上，根因如下：

```
scheduler 每 30s → 后台 tick → engine.tick() 完整执行访谈分支
                                    ↓
                    建 session + 刷新 last_interview_at（消耗 3 天冷却）
                                    ↓
        _broadcast_deskmate_suggestion() 只认 judgment（interview 无出口）
                                    ↓
                            邀请被丢弃，用户什么也看不到
                                    ↓
        前端 tick 随后到达 → should_interview_now() 撞冷却 → 永远 False
```

于是：**访谈永远不完成 → open gap 永远不消解 → 冷却一到又建一个 →
用户永远看不到**。认知验证卡（Turing test）同构：`last_validation_at`
最后发起 09-19，而 `last_validation_completed_at` 停在 09-07。

## 本文件锁住四件事

1. 后台通道（``allow_interactive=False``）不建 session、不吃冷却
2. 前端通道（默认 ``allow_interactive=True``）照常发起
3. 发起新一轮会结掉旧的 active 会话（防僵尸堆积）
4. 访谈链路按 ``base_dir`` 隔离，不落真实库
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _freeze_desktop(monkeypatch) -> None:
    """把桌面状态固定为「在桌前正常使用、非深专注」。

    ⚠ **不固定的话，本文件的用例会随「跑测试时前台开的是什么窗口」时红时绿。**

    实测（2026-09-22）：萧哥在 Word 里写作时跑全量，`focus` 判成
    ``deep 0.8`` → tick 的访谈块被 ``not (focus.deep)`` 整块挡掉 →
    本文件 3 例全红；把窗口切到别处（``casual``）重跑则全绿。这种测试
    比没有测试更糟：它稳定的失败信号会把人引向产品代码，而真正的原因是
    测试没把环境确定下来。

    固定三处（tick 里前两个是模块级 / 函数级 import，patch 源模块即可）：
    - ``tick.detect_user_activity`` → 稳定的 ACTIVE（否则会议应用标题会判 BUSY）
    - ``window_monitor.get_active_window`` → None（不碰真实窗口，也让 focus 无从计算）
    - ``perception.focus.get_focus_score`` → None（防未来改动把它挪出 ``if _win`` 之外）
    """
    from deskmate.perception import focus as focus_mod
    from deskmate.perception import window_monitor
    from deskmate.proactivity import tick as tick_mod
    from deskmate.proactivity.user_active import ActivityState, UserActivity

    steady = ActivityState(
        level=UserActivity.ACTIVE,
        idle_seconds=0,
        window_title="测试固定窗口",
        app_name="TestHarness",
        reason="测试固定：在桌前正常使用",
    )
    monkeypatch.setattr(tick_mod, "detect_user_activity", lambda *a, **kw: steady)
    monkeypatch.setattr(window_monitor, "get_active_window", lambda *a, **kw: None)
    monkeypatch.setattr(focus_mod, "get_focus_score", lambda *a, **kw: None)


def _pin_environment(monkeypatch) -> None:
    """把 tick 的**非被测变量**全部固定：桌面状态 + 维护类工作。

    本文件只测访谈分支的「发起 / 不发起」语义，其余一切都该是常量：

    - 桌面状态 → ``_freeze_desktop``（见其 docstring：这是时红时绿的元凶）
    - 维护类工作（情感增量提取、表达回灌、冲突扫描、日记灵感、花园结算）
      会联网 / 跑规则 / 变慢，与断言无关，一律静音

    tick 内部是函数级 ``from X import Y``，所以 patch 模块属性即可生效。
    """
    from deskmate.cognitive import conflict_gap, emotion_extract, expression_ingest
    from deskmate.garden import clock as garden_clock
    from deskmate.growth import diary_inspiration

    _freeze_desktop(monkeypatch)
    monkeypatch.setattr(emotion_extract, "maybe_extract_on_tick", lambda **kw: None)
    monkeypatch.setattr(expression_ingest, "maybe_ingest_on_tick", lambda **kw: None)
    monkeypatch.setattr(conflict_gap, "maybe_scan_conflicts_on_tick", lambda **kw: None)
    monkeypatch.setattr(diary_inspiration, "maybe_write_diary", lambda **kw: None)
    monkeypatch.setattr(garden_clock, "maybe_settle_garden", lambda **kw: None)


def _db_path(base_dir) -> Path:
    return Path(base_dir) / "deskmate.db"


def _seed_emotional_gap(base_dir, priority: float = 0.8) -> str:
    """在隔离库里造一个情感层 open gap（访谈的燃料）。"""
    from deskmate.cognitive.profile import resolve_profile_manager

    pm = resolve_profile_manager(layer="emotional", base_dir=base_dir)
    gap = pm.add_gap("情感·测试缺口", "为测试造的缺口（回血方式待澄清）", priority=priority)
    return gap.id


def _sessions(base_dir, layer: str = "emotional") -> list[dict]:
    p = _db_path(base_dir)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, status FROM interview_sessions WHERE layer=? ORDER BY started_at",
            (layer,),
        ).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "status": r[1]} for r in rows]


def _gap_topics(base_dir) -> list[str]:
    p = _db_path(base_dir)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT topic FROM cognitive_gaps")]
    finally:
        con.close()


def _real_db_gap_topics() -> list[str]:
    """真实库的 gap 主题（只读，用于证明隔离生效）。"""
    p = Path.home() / ".deskmate" / "deskmate.db"
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT topic FROM cognitive_gaps")]
    finally:
        con.close()


# ----------------------------------------------------------------------
# 1 + 2：后台不发起 / 前端照常发起
# ----------------------------------------------------------------------
class TestAllowInteractive:
    def test_background_tick_does_not_create_session(self, tmp_path, monkeypatch):
        """后台通道（allow_interactive=False）不建会话——建了也没人看得见。"""
        _pin_environment(monkeypatch)
        _seed_emotional_gap(tmp_path)
        from deskmate.proactivity.tick import ProactiveEngine

        engine = ProactiveEngine(base_dir=tmp_path)
        res = engine.tick(skip_activity_check=True, allow_interactive=False)

        assert res.interview is None
        assert _sessions(tmp_path) == [], "后台通道不该留下任何访谈会话"

    def test_frontend_tick_creates_session(self, tmp_path, monkeypatch):
        """前端通道（默认 allow_interactive=True）照常发起并带回问题。"""
        _pin_environment(monkeypatch)
        _seed_emotional_gap(tmp_path)
        from deskmate.proactivity.tick import ProactiveEngine

        engine = ProactiveEngine(base_dir=tmp_path)
        res = engine.tick(skip_activity_check=True)

        assert res.interview is not None
        assert res.interview["session_id"]
        assert res.interview["questions"]
        sessions = _sessions(tmp_path)
        assert len(sessions) == 1
        assert sessions[0]["id"] == res.interview["session_id"]
        assert sessions[0]["status"] == "active"

    def test_repeated_background_ticks_do_not_starve_frontend(self, tmp_path, monkeypatch):
        """**死循环的正面锁**：后台跑很多次，前端仍必须拿得到邀请。

        这是本文件最重要的一条。修复前，后台第一次 tick 就建了 session 并把
        3 天冷却吃掉，前端此后永远返回 None —— 用户再也收不到访谈邀请。
        """
        _pin_environment(monkeypatch)
        _seed_emotional_gap(tmp_path)
        from deskmate.proactivity.tick import ProactiveEngine

        engine = ProactiveEngine(base_dir=tmp_path)
        for _ in range(3):
            bg = engine.tick(skip_activity_check=True, allow_interactive=False)
            assert bg.interview is None

        front = engine.tick(skip_activity_check=True)
        assert front.interview is not None, "后台跑过多次后，前端仍应拿到访谈邀请"
        assert front.interview["session_id"]

    def test_old_background_behavior_would_starve_frontend(self, tmp_path, monkeypatch):
        """根因复现（对照）：后台若"照常发起"，前端就再也拿不到邀请。

        这条不是在测产品行为，而是把 bug 的**机制**钉进测试：谁将来想把
        allow_interactive 去掉、或让后台恢复默认 True，这里会明确告诉他代价。
        """
        _pin_environment(monkeypatch)
        _seed_emotional_gap(tmp_path)
        from deskmate.proactivity.tick import ProactiveEngine

        engine = ProactiveEngine(base_dir=tmp_path)
        # 修复前的后台行为 == 默认 allow_interactive=True
        old_bg = engine.tick(skip_activity_check=True)
        assert old_bg.interview is not None, "旧行为：后台会建会话（但广播时被丢弃）"

        # 前端随后到达 → 撞上刚被消耗的 3 天冷却 → 永远拿不到
        front = engine.tick(skip_activity_check=True)
        assert front.interview is None, "这正是「再也见不到访谈」的机制"


# ----------------------------------------------------------------------
# 3：防僵尸堆积
# ----------------------------------------------------------------------
class TestExpireActiveSessions:
    def test_new_round_expires_previous_active(self, tmp_path):
        """新一轮发起时，旧的 active 会话被标记 expired（真实库曾积 12 个）。"""
        from deskmate.cognitive.interview import start_interview

        _seed_emotional_gap(tmp_path)
        r1 = start_interview(layer="emotional", base_dir=tmp_path)
        assert r1["session_id"], "第一轮应能发起"

        r2 = start_interview(layer="emotional", base_dir=tmp_path)
        assert r2["session_id"] and r2["session_id"] != r1["session_id"]

        status = {s["id"]: s["status"] for s in _sessions(tmp_path)}
        assert status[r1["session_id"]] == "expired", "旧 active 应被结掉"
        assert status[r2["session_id"]] == "active", "新会话必须活着"

    def test_done_sessions_are_not_touched(self, tmp_path):
        """已完成的会话不该被 expire 改写（历史要能区分「答完」与「没答完」）。"""
        from deskmate.cognitive.interview import start_interview
        from deskmate.cognitive.store import CognitiveStore

        _seed_emotional_gap(tmp_path)
        r1 = start_interview(layer="emotional", base_dir=tmp_path)
        CognitiveStore(db_path=_db_path(tmp_path)).finish_session(
            r1["session_id"], summary="测试：直接标完成"
        )

        start_interview(layer="emotional", base_dir=tmp_path)

        status = {s["id"]: s["status"] for s in _sessions(tmp_path)}
        assert status[r1["session_id"]] == "done"

    def test_expire_returns_count(self, tmp_path):
        from deskmate.cognitive.store import CognitiveStore

        store = CognitiveStore(db_path=_db_path(tmp_path))
        store.create_session(layer="emotional", gap_ids=["q_a"])
        store.create_session(layer="emotional", gap_ids=["q_b"])

        expired = store.expire_active_sessions(layer="emotional")

        assert expired == 2
        assert all(s["status"] == "expired" for s in _sessions(tmp_path))


# ----------------------------------------------------------------------
# 4：base_dir 隔离（此前 tick 的访谈分支无参调用，永远落真实库）
# ----------------------------------------------------------------------
class TestInterviewIsolation:
    def test_gaps_seeded_into_base_dir(self, tmp_path):
        """ensure_emotional_gaps(base_dir=...) 播进隔离库。"""
        from deskmate.cognitive.emotion_profile import ensure_emotional_gaps

        ensure_emotional_gaps(base_dir=tmp_path)

        assert _db_path(tmp_path).exists()
        topics = _gap_topics(tmp_path)
        assert any("关怀偏好" in t for t in topics), f"隔离库应播下情感 gap，实际: {topics}"

    def test_real_db_gap_count_unchanged_by_isolated_calls(self, tmp_path):
        """隔离调用不得动真实库（这是 2026-09-21 那个漏库问题的同族锁）。"""
        before = _real_db_gap_topics()
        if not before:
            pytest.skip("本机无真实库，跳过（隔离已由上面用例覆盖）")

        from deskmate.cognitive.emotion_profile import ensure_emotional_gaps
        from deskmate.cognitive.interview import start_interview

        _seed_emotional_gap(tmp_path)
        ensure_emotional_gaps(base_dir=tmp_path)
        start_interview(layer="emotional", base_dir=tmp_path)

        assert _real_db_gap_topics() == before, "隔离调用污染了真实库"

    def test_cooldown_is_isolated_per_base_dir(self, tmp_path):
        """冷却时间戳按 base_dir 读写：A 库发起过，不该压制 B 库。"""
        from deskmate.cognitive.interview import maybe_trigger_interview

        base_a = tmp_path / "a"
        base_b = tmp_path / "b"
        base_a.mkdir()
        base_b.mkdir()
        _seed_emotional_gap(base_a)
        _seed_emotional_gap(base_b)

        first = maybe_trigger_interview(layer="emotional", base_dir=base_a)
        assert first and first["session_id"]

        # A 库在冷却内 → 不再发起
        assert maybe_trigger_interview(layer="emotional", base_dir=base_a) is None
        # B 库有自己的时间戳 → 不受 A 影响
        second = maybe_trigger_interview(layer="emotional", base_dir=base_b)
        assert second and second["session_id"], "冷却不该跨库共享"


# ----------------------------------------------------------------------
# 冷却本体
# ----------------------------------------------------------------------
class TestCooldown:
    def test_retrigger_blocked_within_window(self, tmp_path):
        from deskmate.cognitive.interview import maybe_trigger_interview

        _seed_emotional_gap(tmp_path)
        assert maybe_trigger_interview(layer="emotional", base_dir=tmp_path)
        assert maybe_trigger_interview(layer="emotional", base_dir=tmp_path) is None

    def test_retrigger_allowed_after_window(self, tmp_path):
        """冷却到期（>3 天）后可以再发起。"""
        from datetime import datetime, timedelta

        from deskmate.cognitive.interview import maybe_trigger_interview
        from deskmate.cognitive.store import CognitiveStore

        _seed_emotional_gap(tmp_path)
        store = CognitiveStore(db_path=_db_path(tmp_path))
        store.set_meta(
            "last_interview_at:emotional",
            (datetime.now() - timedelta(days=4)).isoformat(),
        )

        r = maybe_trigger_interview(layer="emotional", base_dir=tmp_path)
        assert r and r["session_id"], "超过 3 天冷却后应能再次发起"
