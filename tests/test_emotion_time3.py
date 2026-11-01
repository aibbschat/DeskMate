"""独立通道二期 · 时间三态（回潮/等待/脾气）单测。

覆盖：回潮剂量/冷却/开关、等待立项/爬坡/落差释放/lapsed/对账、
脾气分档/刺许可/黑名单/开关。LLM 侧（编故事/spoken 侦探）走 fail-open 路径。
"""
import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from deskmate.growth.emotion_engine import (
    EmotionEngine, BASELINES, HORMONE_KEYS, _connect,
    ECHO_MAX_DELTA, PENDING_MAX_OPEN, PENDING_CLIMB_CAP,
    _is_abusive,
)


@pytest.fixture()
def eng():
    d = tempfile.mkdtemp()
    e = EmotionEngine(base_dir=Path(d))
    e.get_state()
    yield e


def _state(eng):
    return eng.get_state()


def _set(eng, **vals):
    with _connect(eng._db) as conn:
        for k, v in vals.items():
            conn.execute(f"UPDATE emotion_state SET {k}=? WHERE key='self'", (v,))


def _insert_old_narrative(eng, hours_ago=48.0, deltas=None, salience=0.9,
                          narrative="那天他否了我递出去的那句，我心里刺了一下"):
    """造一条 hours_ago 前的旧叙事（回潮候选）。返回 row id。"""
    rid = str(uuid.uuid4())
    ts = (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    with _connect(eng._db) as conn:
        conn.execute(
            "INSERT INTO emotion_log (id, ts, source, deltas, description,"
            " conversation_id, event_type, event_summary, narrative, salience, origin)"
            " VALUES (?, ?, 'experience', ?, '旧独白', NULL, 'experience', '旧事', ?, ?, 'system')",
            (rid, ts, json.dumps(deltas or {"cortisol": 8.0, "dopamine": -6.0},
                                 ensure_ascii=False), narrative, salience),
        )
    return rid


# ----------------------------------------------------------------------
# 回潮
# ----------------------------------------------------------------------
def test_echo_ripples_and_logs(eng):
    """旧事 ≥24h → 微澜泛起（≤±2）+ echo 留痕 + 注入文本返回叙事。"""
    _insert_old_narrative(eng)
    before = _state(eng)
    echoed = eng.maybe_echo()
    assert echoed, "应泛出旧叙事文本"
    after = _state(eng)
    # cortisol 旧 delta +8 × 0.9 × 0.15 = 1.08 → 微澜向上
    assert 0.2 < after["cortisol"] - before["cortisol"] <= ECHO_MAX_DELTA
    assert after["dopamine"] - before["dopamine"] < 0
    with _connect(eng._db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM emotion_log WHERE source='echo'").fetchone()[0]
    assert n == 1


def test_echo_cooldown_24h(eng):
    """同一件旧事 24h 内只泛一次（不泡旧缸）。"""
    _insert_old_narrative(eng)
    assert eng.maybe_echo() is not None
    assert eng.maybe_echo() is None, "冷却期内不应再泛同一件"


def test_echo_skips_too_old_and_too_fresh(eng):
    """<24h 的新事和 >7d 的旧事都不泛。"""
    _insert_old_narrative(eng, hours_ago=5.0)
    assert eng.maybe_echo() is None
    _insert_old_narrative(eng, hours_ago=24.0 * 10)
    assert eng.maybe_echo() is None


def test_echo_switch_off(eng, monkeypatch):
    """DESKMATE_EMOTION_ECHO=0 → 不泛。"""
    monkeypatch.setenv("DESKMATE_EMOTION_ECHO", "0")
    _insert_old_narrative(eng)
    assert eng.maybe_echo() is None


# ----------------------------------------------------------------------
# 等待：立项 / 爬坡 / 落地 / lapsed / 对账
# ----------------------------------------------------------------------
def test_pending_create_and_dedup(eng):
    """立项成功；同名 open 不重复立。"""
    pid = eng.pending_create("spoken", "等他回我消息", expectation="大概会回", valence="good")
    assert pid
    assert eng.pending_create("spoken", "等他回我消息", expectation="大概会回") is None
    assert len(eng.open_pendings()) == 1


def test_pending_fuzzy_dedup_same_affair(eng):
    """同一摊事换个说法（「等 review 核稿结果」↔「等主编的 review」）不再立项。"""
    assert eng.pending_create(
        "spoken", "等 review 核稿结果", expectation="核完要么确认改对了，要么返工",
        valence="unknown")
    assert eng.pending_create(
        "spoken", "等主编的 review", expectation="他大概会给一条意见",
        valence="mixed") is None
    assert len(eng.open_pendings()) == 1, "同一摊事只该悬一件"


def test_pending_fuzzy_keeps_short_titles_apart(eng):
    """短名不参与模糊合并（撞字概率高，如「悬事0」「悬事1」）。"""
    assert eng.pending_create("spoken", "悬事0", expectation="预期一二三")
    assert eng.pending_create("spoken", "悬事1", expectation="预期一二三")
    assert len(eng.open_pendings()) == 2


def test_due_bucket_maps_by_rule(eng):
    """due_bucket 由规则映射成 due_ts；认不出的桶一律不设（不硬猜）。"""
    from deskmate.growth.emotion_engine import _due_ts_from_bucket
    assert _due_ts_from_bucket("tomorrow") is not None
    assert _due_ts_from_bucket("this_week") is not None
    assert _due_ts_from_bucket("unknown") is None
    assert _due_ts_from_bucket("") is None
    assert _due_ts_from_bucket("随便吧") is None


def test_pending_due_near_boosts_climb(eng):
    """有 due_ts 且剩余 <1/4 → 当次爬坡 ×1.5（临近效应真正可触发）。"""
    now = datetime.now()
    item = {
        "expected_valence": "good",
        "created_ts": (now - timedelta(hours=20)).isoformat(timespec="seconds"),
        "due_ts": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
    }
    assert eng._pending_climb_deltas(item, now)["dopamine"] == 1.5
    item["due_ts"] = None
    assert eng._pending_climb_deltas(item, now)["dopamine"] == 1.0


def test_pending_spoken_requires_expectation(eng):
    """问题 A：spoken 类必须有 ≥5 字预期故事，否则不立项（挡客套话/即时任务）。"""
    assert eng.pending_create("spoken", "下次再见") is None
    assert eng.pending_create("spoken", "等你缓过来", expectation="") is None
    assert eng.pending_create("spoken", "下一题", expectation="嗯") is None  # <5 字
    # 有预期就能立
    assert eng.pending_create("spoken", "等编辑回信", expectation="他大概下周回")
    # system 类不受此限（对账来的可能没预期）
    assert eng.pending_create("system", "某提案待审", source_ref="x")


def test_pending_max_open_lapse_oldest(eng):
    """open 满 3 件 → 新来一件，最旧的自动 lapsed。"""
    ids = [eng.pending_create("spoken", f"悬事{i}", expectation=f"这件事的预期{i}") for i in range(PENDING_MAX_OPEN)]
    assert all(ids)
    eng.pending_create("spoken", "新来的悬事", expectation="新来的预期很长")
    opens = eng.open_pendings()
    assert len(opens) == PENDING_MAX_OPEN
    assert "悬事0" not in [p["title"] for p in opens], "最旧的应被挤成 lapsed"
    with _connect(eng._db) as conn:
        st = conn.execute("SELECT status FROM pending_stories WHERE id=?", (ids[0],)).fetchone()[0]
    assert st == "lapsed"


def test_pending_lapse_reason_carries_title(eng):
    """问题 B：挤兑 lapsed 的留痕要带上被挤掉的 title，不能全是「装不下新的了」。"""
    eng.pending_create("spoken", "悬事A", expectation="悬事A的预期")
    eng.pending_create("spoken", "悬事B", expectation="悬事B的预期")
    eng.pending_create("spoken", "悬事C", expectation="悬事C的预期")
    eng.pending_create("spoken", "悬事D", expectation="悬事D的预期")  # 挤掉悬事A
    with _connect(eng._db) as conn:
        row = conn.execute(
            "SELECT event_summary FROM emotion_log WHERE source='lapsed' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert row and "悬事A" in row[0], f"lapsed 留痕应带被挤掉的 title，got: {row}"


def test_pending_climb_once_per_day(eng):
    """爬坡每日每件一次：同日两次 tick 只爬一次；good→dopamine 升。"""
    eng.pending_create("spoken", "盼着他的回音", expectation="他会回我", valence="good")
    before = _state(eng)
    eng.pending_tick()
    after1 = _state(eng)
    d1 = after1["dopamine"] - before["dopamine"]
    assert d1 > 0, "good 预期应爬 dopamine"
    eng.pending_tick()
    after2 = _state(eng)
    assert abs(after2["dopamine"] - after1["dopamine"]) < 0.01, "同日不应重复爬坡"


def test_pending_climb_valence_direction(eng):
    """bad→cortisol 升；mixed→两股同起（忐忑）。"""
    eng.pending_create("spoken", "担心被否", expectation="可能被否", valence="bad")
    before = _state(eng)
    eng.pending_tick()
    after = _state(eng)
    assert after["cortisol"] - before["cortisol"] > 0
    assert abs(after["dopamine"] - before["dopamine"]) < 0.01

    d = tempfile.mkdtemp()
    e2 = EmotionEngine(base_dir=Path(d))
    e2.get_state()
    e2.pending_create("spoken", "七上八下的事", expectation="好坏参半", valence="mixed")
    b2 = e2.get_state()
    e2.pending_tick()
    a2 = e2.get_state()
    assert a2["cortisol"] - b2["cortisol"] > 0 and a2["dopamine"] - b2["dopamine"] > 0


def test_pending_land_better_worse(eng):
    """落地落差：better→dopamine+serotonin 升；worse→dopamine 跌+cortisol 升。"""
    pid = eng.pending_create("spoken", "等一个结果", expectation="可能有结果", valence="bad")
    before = _state(eng)
    eng.pending_land(pid, "better")
    after = _state(eng)
    assert after["dopamine"] - before["dopamine"] > 0
    assert after["serotonin"] - before["serotonin"] > 0
    with _connect(eng._db) as conn:
        row = conn.execute(
            "SELECT status, landed_outcome FROM pending_stories WHERE id=?", (pid,)).fetchone()
    assert row[0] == "landed" and row[1] == "better"

    pid2 = eng.pending_create("spoken", "另一件事", expectation="会有结果", valence="good")
    before2 = _state(eng)
    eng.pending_land(pid2, "worse")
    after2 = _state(eng)
    assert after2["dopamine"] - before2["dopamine"] < 0
    assert after2["cortisol"] - before2["cortisol"] > 0


def test_pending_land_spoken_fuzzy(eng):
    """spoken 落地按 title 模糊匹配。"""
    eng.pending_create("spoken", "等编辑的回信", expectation="他大概会回信", valence="good")
    eng.pending_land_spoken("编辑的回信有结果了", "as_expected")
    with _connect(eng._db) as conn:
        st = conn.execute(
            "SELECT status FROM pending_stories WHERE title='等编辑的回信'").fetchone()[0]
    assert st == "landed"


def test_pending_lapse_after_7_days(eng):
    """7 天无结果 → lapsed + dopamine 小跌。"""
    eng.pending_create("spoken", "久等无音的事", expectation="不知道会不会有结果")
    old_ts = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
    with _connect(eng._db) as conn:
        conn.execute("UPDATE pending_stories SET created_ts=? WHERE status='open'", (old_ts,))
    before = _state(eng)
    eng.pending_tick()
    after = _state(eng)
    assert after["dopamine"] - before["dopamine"] < 0
    assert eng.open_pendings() == []


def test_pending_switch_off(eng, monkeypatch):
    """DESKMATE_EMOTION_PENDING=0 → 不立项不爬坡。"""
    monkeypatch.setenv("DESKMATE_EMOTION_PENDING", "0")
    assert eng.pending_create("spoken", "一件事") is None
    eng.pending_tick()  # 不崩即可


def test_pending_reconcile_system(eng):
    """agency 提案对账：新 PROPOSED 立项；被裁决后落地（executed→better）。"""
    from deskmate.agency.task_log import TaskLog, TaskStatus
    log = TaskLog(eng._db.parent)
    rec = log.add("intent", "high", TaskStatus.PROPOSED, summary="外发草稿等点头")
    eng.pending_tick()  # 对账 → 立项
    opens = eng.open_pendings()
    assert any("外发草稿" in p["title"] for p in opens), "新提案应立项"
    # 裁决 executed → 再对账 → 落地 better（dopamine 升）
    rec.status = TaskStatus.EXECUTED
    assert log.update(rec)
    before = _state(eng)
    eng.pending_tick()
    after = _state(eng)
    assert after["dopamine"] - before["dopamine"] > 0, "executed 应按 better 释放"
    assert eng.open_pendings() == []


def test_pending_climb_cap(eng):
    """累计爬坡封顶 PENDING_CLIMB_CAP。"""
    eng.pending_create("spoken", "长线悬着的事", expectation="悬很久才有结果", valence="bad")
    with _connect(eng._db) as conn:
        conn.execute("UPDATE pending_stories SET climbed_total=? WHERE status='open'",
                     (PENDING_CLIMB_CAP,))
    before = _state(eng)
    eng.pending_tick()
    after = _state(eng)
    assert abs(after["cortisol"] - before["cortisol"]) < 0.01, "封顶后不再爬"


# ----------------------------------------------------------------------
# 脾气
# ----------------------------------------------------------------------
def test_temper_tiers(eng):
    """cortisol/fatigue 高 → 耐性薄/耐性尽。"""
    assert eng.temper_state()["tier"] == "ok"
    _set(eng, cortisol=BASELINES["cortisol"] + 16.0)  # patience=68
    assert eng.temper_state()["tier"] == "thin"
    _set(eng, cortisol=BASELINES["cortisol"] + 25.0, fatigue=BASELINES["fatigue"] + 30.0)
    assert eng.temper_state()["tier"] == "spent"


def test_temper_sting_requires_safety(eng):
    """刺的底气 = 安全感：cortisol 有声部 + oxytocin 足才放许可。"""
    _set(eng, cortisol=BASELINES["cortisol"] + 8.0, oxytocin=BASELINES["oxytocin"] + 5.0)
    assert eng.temper_state()["sting_allowed"] is True
    _set(eng, oxytocin=BASELINES["oxytocin"] - 20.0)
    assert eng.temper_state()["sting_allowed"] is False, "底气不足不放刺"


def test_temper_injection_shows_permission(eng):
    """耐性薄时注入块出现脾气段；回退开关关掉后整段消失。"""
    _set(eng, cortisol=BASELINES["cortisol"] + 16.0)
    ctx = eng.describe_turn_context()
    assert "耐性有点薄" in ctx


def test_temper_switch_off(eng, monkeypatch):
    """DESKMATE_EMOTION_TEMPER=0 → 注入块无脾气段。"""
    monkeypatch.setenv("DESKMATE_EMOTION_TEMPER", "0")
    _set(eng, cortisol=BASELINES["cortisol"] + 12.0)
    ctx = eng.describe_turn_context()
    assert "耐性" not in ctx


def test_abusive_blacklist_and_foul(eng):
    """黑名单命中 → temper_foul 留痕 → 下轮注入点破。"""
    assert _is_abusive("你这个傻逼") is True
    assert _is_abusive("今天天气不错") is False
    eng.note_reply_foul("你就是个傻逼，滚蛋")
    assert eng.temper_state()["recent_foul"] is True
    ctx = eng.describe_turn_context()
    assert "说过线了" in ctx
    eng2 = EmotionEngine(base_dir=Path(tempfile.mkdtemp()))
    eng2.get_state()
    eng2.note_reply_foul("正常说话而已")
    assert eng2.temper_state()["recent_foul"] is False


# ----------------------------------------------------------------------
# 日志卫生（2026-10-16 巡审 P0）
# ----------------------------------------------------------------------
def test_prune_emotion_log_only_old_time_decay(eng):
    """只裁 14 天前的 time_decay；新 time_decay 与所有 experience 行保留。"""
    from deskmate.growth.emotion_engine import EMOTION_LOG_DECAY_KEEP_DAYS
    old_ts = (datetime.now()
              - timedelta(days=EMOTION_LOG_DECAY_KEEP_DAYS + 3)).isoformat(timespec="seconds")
    fresh_ts = datetime.now().isoformat(timespec="seconds")
    with _connect(eng._db) as conn:
        def _ins(source: str, ts: str) -> None:
            conn.execute(
                "INSERT INTO emotion_log (id, ts, source, deltas, description, conversation_id) "
                "VALUES (?, ?, ?, '{}', '', NULL)",
                (str(uuid.uuid4()), ts, source))
        _ins("time_decay", old_ts)     # 过期 → 该裁
        _ins("time_decay", fresh_ts)   # 未过期 → 保留
        _ins("experience", old_ts)     # 情绪来路 → 绝不裁
        # 让 decay 真跑（_do_decay_step 在 elapsed<1s 时早退）
        conn.execute(
            "UPDATE emotion_state SET updated_at=? WHERE key='self'",
            ((datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),))
    eng.decay_step()
    with _connect(eng._db) as conn:
        rows = conn.execute("SELECT source, ts FROM emotion_log").fetchall()
    kept = {(s, ts) for s, ts in rows}
    assert ("time_decay", old_ts) not in kept, "14 天前的 time_decay 应被裁"
    assert ("time_decay", fresh_ts) in kept, "新 time_decay 应保留"
    assert ("experience", old_ts) in kept, "experience 行绝不裁（情绪来路）"
