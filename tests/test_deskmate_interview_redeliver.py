"""访谈邀请「补送」的回归锁（2026-09-22 二次定位）。

## 背景：死循环之外，还藏着「送达」这一半

上一轮（``test_deskmate_interview_trigger.py``）修掉了后半段——后台 tick 抢发、
把 3 天冷却白吃掉。但萧哥重启后说「还是没弹」，把链条往前推才看到另一半：

```
前端 tick（用户交互驱动）→ HTTP /v1/deskmate/proactive/tick
        ↓
_create_session → 立刻写 last_interview_at（3 天冷却当场扣掉）
        ↓
返回 interview 负载 → 前端 dispatch → InterviewCard 气泡渲染
        ↓
气泡只存在 60 秒（60s 后自动 setPhase("exit") → onClose → setInterview(null)）
        ↓
用户当时不在窗口前（他常在 Word 里写作）→ 气泡无声消失
        ↓
冷却已扣 → 之后每次 tick 都被冷却挡死 → 3 天内不再邀请
```

证据：真实库 09-17 以来 6 个会话**全部 0 答题**，且反复是同一批 gap
（``gap_0b65fa4a``/``gap_6eeb060b`` 从 09-17 问到 09-22）。这不是"她不想问"，
是"问了但从没送到"。

## 补送的语义

``create_session`` 扣的那 3 天冷却，节流的是「问过他这件事」；而这里的事
**他压根没看见**。所以补送是一次**重放**：不新建会话、不刷新冷却，因此不受
冷却约束。用户切回窗口 → focus → 前端 tick → 命中补送 → 气泡再次出现。

## 本文件锁住七件事

1. 冷却被吃掉后，补送仍能把那张已发出的邀请送出来（**核心用例**）
2. 答过至少一题就不再补送（他见过它了）
3. 节流：同层 30 分钟内不重复补送（前端 tick 最快 15s 一次）
4. 次数上限：单个会话最多补送若干次，避免无限重放
5. 时长兜底：挂满 48 小时无人回应 → 收敛为 expired
6. 有挂起邀请时**不另开新会话**（否则继续堆僵尸，同一批 gap 被反复问）
7. 关联对象全失效、问题再也重建不出来的挂起邀请 → **立刻收敛**，
   不许白占 48h 把这一层的新邀请挡在门外
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
LAYER = "emotional"


def _db_path(base_dir) -> Path:
    return Path(base_dir) / "deskmate.db"


def _pm(base_dir):
    from deskmate.cognitive.profile import resolve_profile_manager

    return resolve_profile_manager(layer=LAYER, base_dir=base_dir)


def _seed_gap(base_dir, priority: float = 0.8) -> str:
    """造一个情感层 open gap（访谈的燃料）。priority>=0.8 → 一个就够触发。"""
    pm = _pm(base_dir)
    gap = pm.add_gap("情感·测试缺口", "为测试造的缺口（回血方式待澄清）", priority=priority)
    return gap.id


def _sessions(base_dir) -> list[dict]:
    p = _db_path(base_dir)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, status, started_at, gap_ids_json FROM interview_sessions "
            "WHERE layer=? ORDER BY started_at",
            (LAYER,),
        ).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "status": r[1], "started_at": r[2], "gap_ids": r[3]} for r in rows]


def _set_session_started_at(base_dir, session_id: str, when: datetime) -> None:
    con = sqlite3.connect(_db_path(base_dir))
    try:
        con.execute(
            "UPDATE interview_sessions SET started_at=? WHERE id=?",
            (when.isoformat(), session_id),
        )
        con.commit()
    finally:
        con.close()


def _pin_environment(monkeypatch) -> None:
    """固定 tick 的非被测变量：桌面状态 + 维护类工作。

    桌面状态**必须**固定：tick 的访谈块被 ``activity != BUSY and not focus.deep``
    门控，跑测试时如果前台正开着 Word（``focus=deep``），本文件会整体变红，
    而根因只是环境不确定（2026-09-22 实测踩过）。
    """
    from deskmate.cognitive import conflict_gap, emotion_extract, expression_ingest
    from deskmate.garden import clock as garden_clock
    from deskmate.growth import diary_inspiration
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
    monkeypatch.setattr(emotion_extract, "maybe_extract_on_tick", lambda **kw: None)
    monkeypatch.setattr(expression_ingest, "maybe_ingest_on_tick", lambda **kw: None)
    monkeypatch.setattr(conflict_gap, "maybe_scan_conflicts_on_tick", lambda **kw: None)
    monkeypatch.setattr(diary_inspiration, "maybe_write_diary", lambda **kw: None)
    monkeypatch.setattr(garden_clock, "maybe_settle_garden", lambda **kw: None)


def _trigger(base_dir, **kw):
    from deskmate.cognitive.interview import maybe_trigger_interview

    return maybe_trigger_interview(layer=LAYER, base_dir=base_dir, **kw)


# ----------------------------------------------------------------------
# 1：核心 —— 冷却吃掉之后，邀请仍必须送得到
# ----------------------------------------------------------------------
class TestRedeliverBeatsCooldown:
    def test_first_call_creates_then_redelivers_same_session(self, tmp_path):
        """第一次是「发起」，第二次（冷却期内）是「补送同一张邀请」。

        这正是萧哥的场景：邀请发出去了，他没看见（气泡 60 秒后自消），
        再切回窗口时不应该什么都不给。
        """
        _seed_gap(tmp_path)

        first = _trigger(tmp_path)
        assert first is not None and first["session_id"]
        assert not first.get("redelivered")
        sid = first["session_id"]

        # 冷却已被扣：这一层 3 天内 should_interview_now 应为 False
        from deskmate.cognitive.interview import get_interview_engine

        pm = _pm(tmp_path)
        assert get_interview_engine().should_interview_now(
            pm.profile, layer=LAYER, store=pm.store
        ) is False, "前提：create_session 必须已经扣掉 3 天冷却"

        # 绕过节流：把上次补送时间推远
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        second = _trigger(tmp_path)
        assert second is not None, "冷却期内也必须能把没送达的邀请补出来"
        assert second["session_id"] == sid, "补送应重放同一张邀请，而不是新建"
        assert second.get("redelivered") is True
        assert [q["id"] for q in second["questions"]] == [q["id"] for q in first["questions"]]

    def test_redeliver_does_not_touch_cooldown(self, tmp_path):
        """补送是重放，不该重置 last_interview_at（否则冷却被无限刷新）。"""
        _seed_gap(tmp_path)
        _trigger(tmp_path)
        pm = _pm(tmp_path)
        before = pm.store.get_meta(f"last_interview_at:{LAYER}")

        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )
        _trigger(tmp_path)

        assert pm.store.get_meta(f"last_interview_at:{LAYER}") == before

    def test_rebuild_skips_when_subject_gone(self, tmp_path):
        """关联对象不存在了 → 宁可不问，也不编一个无据的问题。"""
        from deskmate.cognitive.interview import rebuild_questions_from_qids

        pm = _pm(tmp_path)
        assert rebuild_questions_from_qids(["q_deadbeef00__g=gap_notexist__m=explore"], pm) == []

    def test_rebuild_skips_dangling_reference(self, tmp_path):
        """gap 还在，但 related_items 指向画像里不存在的条目 → 无据，不问。

        真实库教训：两个 pytest 夹具 gap 的 related_items 指向只存在于测试
        进程的 stance，它们照样被拿去提问（问出「关于『矛盾待澄清』…」）。
        """
        from deskmate.cognitive.interview import rebuild_questions_from_qids
        from deskmate.cognitive.models import CognitiveGap

        pm = _pm(tmp_path)
        g = CognitiveGap(topic="孤立话题", reason="关联条目已不存在",
                         related_items=["stance_ghost"])
        pm.profile.cognitive_gaps.append(g)
        pm.save()

        qid = f"q_deadbeef01__i=stance_ghost__g={g.id}__m=clarify"
        assert rebuild_questions_from_qids([qid], pm) == []

    def test_rebuild_skips_resolved_gap(self, tmp_path):
        """已结案的 gap 不再问——qid 仍指向它，但 status 说明这事已经了结。"""
        from deskmate.cognitive.interview import rebuild_questions_from_qids

        gid = _seed_gap(tmp_path)
        pm = _pm(tmp_path)
        for g in pm.profile.cognitive_gaps:
            if g.id == gid:
                g.status = "resolved"
        pm.save()

        qid = f"q_deadbeef02__g={gid}__m=explore"
        assert rebuild_questions_from_qids([qid], pm) == []


# ----------------------------------------------------------------------
# 2 / 3 / 4 / 5：不该补送的情形
# ----------------------------------------------------------------------
class TestRedeliverGuards:
    def test_no_redeliver_after_any_answer(self, tmp_path):
        """答过至少一题 → 他见过这轮邀请，不再补送。"""
        _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first
        pm = _pm(tmp_path)
        pm.store.add_qa(
            session_id=first["session_id"],
            question="测试问题",
            answer="测试回答",
            question_id=first["questions"][0]["id"],
        )
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        assert _trigger(tmp_path) is None

    def test_throttled_within_interval(self, tmp_path):
        """同层 30 分钟内不重复补送（否则气泡每 15 秒被顶一次）。"""
        _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first
        pm = _pm(tmp_path)
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )
        assert _trigger(tmp_path) is not None, "前置：推远时间后应能补送"

        # 刚补送过 → 立刻再问应为 None
        assert _trigger(tmp_path) is None

    def test_gives_up_after_max_times(self, tmp_path):
        """达到补送上限后不再重放（避免变成无限骚扰）。"""
        from deskmate.cognitive.interview import _REDELIVER_MAX_TIMES

        _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first
        pm = _pm(tmp_path)
        pm.store.set_meta(f"interview_redeliver_count:{first['session_id']}", _REDELIVER_MAX_TIMES)
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        assert _trigger(tmp_path) is None

    def test_expires_when_too_old(self, tmp_path):
        """挂满 48 小时仍无人回应 → 收敛为 expired，不再补送。"""
        from deskmate.cognitive.interview import _REDELIVER_MAX_AGE_HOURS

        _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first
        _set_session_started_at(
            tmp_path,
            first["session_id"],
            datetime.now() - timedelta(hours=_REDELIVER_MAX_AGE_HOURS + 1),
        )
        pm = _pm(tmp_path)
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        assert _trigger(tmp_path) is None
        assert pm.store.latest_active_session(LAYER) is None, "超时应把会话收敛为 expired"


# ----------------------------------------------------------------------
# 6：有挂起邀请时不另开新会话（防僵尸堆积）
# ----------------------------------------------------------------------
class TestPendingBlocksNewSession:
    def test_pending_prevents_new_session_even_after_cooldown(self, tmp_path):
        """冷却过期了，但上一张邀请仍没人回应 → 只补送，不新建。

        修复前：冷却一到就新建，旧的被 ``expire_active_sessions`` 标掉，
        同一批 gap 换个 qid 再问一遍——真实库里那 6 个 0 答题会话就是这么来的。
        """
        _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first
        sid = first["session_id"]

        pm = _pm(tmp_path)
        # 把冷却与节流都推远：模拟「3 天已过、上一张邀请依然没人理」
        pm.store.set_meta(f"last_interview_at:{LAYER}", (datetime.now() - timedelta(days=5)).isoformat())
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        again = _trigger(tmp_path)
        assert again is not None
        assert again["session_id"] == sid, "应补送挂起的那张，而不是另开一轮"
        assert again.get("redelivered") is True
        assert len(_sessions(tmp_path)) == 1, "不该多出一个会话"


# ----------------------------------------------------------------------
# 7：挂起邀请「问不出来」时不能白占 48 小时（僵尸收敛）
# ----------------------------------------------------------------------
class TestUnrebuildablePendingIsConverged:
    def _make_unrebuildable_zombie(self, tmp_path) -> str:
        """先正常发起一次，再把它的问题对象全部结案 → 这轮问题再也重建不出来。"""
        gid = _seed_gap(tmp_path)
        first = _trigger(tmp_path)
        assert first, "前提：先要有一次正常发起"
        pm = _pm(tmp_path)
        for g in pm.profile.cognitive_gaps:
            if g.id == gid:
                g.status = "resolved"
        pm.save()
        return first["session_id"]

    def test_zombie_is_converged_immediately(self, tmp_path):
        """问题不可重建 → 立刻收敛，而不是静静挂到 48h 时长兜底。

        真实库教训（09-22）：夹具 gap 清理后 cognitive 层还挂着 ``iv_a03eeaec9e``，
        它引用的 gap 都已 resolved → 每次 tick 只走补送、补送又无产出，
        这一层等于被一张「问不出来的邀请」挡住 48 小时。
        """
        sid = self._make_unrebuildable_zombie(tmp_path)
        assert _trigger(tmp_path) is None, "关联对象已失效 → 不编问题，不补送"
        sess = {s["id"]: s for s in _sessions(tmp_path)}
        assert sess[sid]["status"] == "expired", "失效的邀请应立刻收敛"

    def test_layer_recovers_once_cooldown_expires(self, tmp_path):
        """收敛后，冷却一到就能重新发起——不会被僵尸一直挡到 48h 后。"""
        self._make_unrebuildable_zombie(tmp_path)
        _trigger(tmp_path)                      # 这一趟把僵尸收敛掉
        pm = _pm(tmp_path)
        pm.store.set_meta(
            f"last_interview_at:{LAYER}",
            (datetime.now() - timedelta(days=4)).isoformat(),
        )
        _seed_gap(tmp_path)                     # 重新备好燃料
        out = _trigger(tmp_path)
        assert out is not None and out.get("redelivered") is not True, "应变回正常发起"
        assert len(_sessions(tmp_path)) == 2, "应新建第二轮会话"


# ----------------------------------------------------------------------
# 端到端：tick 走完整链路也能补送
# ----------------------------------------------------------------------
class TestTickEndToEnd:
    def test_tick_redelivers_pending_interview(self, tmp_path, monkeypatch):
        """走真实 tick：冷却期内、有挂起邀请时，前端通道仍拿到邀请。"""
        _pin_environment(monkeypatch)
        _seed_gap(tmp_path)
        from deskmate.proactivity.tick import ProactiveEngine

        engine = ProactiveEngine(base_dir=tmp_path)
        first = engine.tick(skip_activity_check=True)
        assert first.interview is not None
        sid = first.interview["session_id"]

        pm = _pm(tmp_path)
        pm.store.set_meta(
            f"last_interview_redeliver_at:{LAYER}",
            (datetime.now() - timedelta(hours=2)).isoformat(),
        )

        again = engine.tick(skip_activity_check=True)
        assert again.interview is not None, "后端通道必须能把没送达的邀请补出来"
        assert again.interview["session_id"] == sid
        assert again.interview["questions"]
