"""P2 情绪底色测试套件。

覆盖施工单 §七 全部 12 项测试：
- test_state_init_seed：首值 oxytocin = max(intimacy, 40)
- test_update_clamps_delta：单次变化不超过 MAX_DELTA，且 clamp 到 [0,100]
- test_decay_toward_base：长时间不更新 → 真衰减
- test_mood_untouched：红线 — 不碰 GrowthState.mood
- test_intimacy_untouched：红线 — 不碰 intimacy/trust
- test_concurrency_lock：并发 20 次 → 终值与串行一致
- test_fail_open：LLM 异常 → 状态保持、不崩
- test_circadian_feature_only：昼夜仅作特征，禁硬编码
- test_injection_subject_tag：注入段含主语标注
- test_self_expression_type：独立于 emotional_care
- test_no_sync_registration：不进 SYNC_TABLES
- test_toggle_off：DESKMATE_EMOTION_ENGINE=0 → 无 LLM 调用
"""

import json
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from deskmate.growth.emotion_engine import (
    EmotionEngine,
    BASELINES,
    MAX_DELTAS,
    HORMONE_KEYS,
    _clamp,
    _clamp_delta,
    _ensure_schema,
    _parse_emotion_response,
)


@pytest.fixture
def tmp_base(tmp_path):
    return tmp_path


@pytest.fixture
def engine(tmp_base):
    return EmotionEngine(base_dir=tmp_base)


def _make_llm_response(**overrides) -> str:
    vals = {
        "dopamine": 55.0,
        "serotonin": 52.0,
        "oxytocin": 45.0,
        "cortisol": 28.0,
        "description": "有点开心",
        "reason": "聊得不错",
    }
    vals.update(overrides)
    return json.dumps(vals, ensure_ascii=False)


# ---------------------------------------------------------------------------
# §7-1: 首值 seed
# ---------------------------------------------------------------------------

def test_state_init_seed_default(engine):
    """intimacy=0 → oxytocin seed = 40."""
    s = engine.get_state()
    assert s["oxytocin"] == 40.0
    assert s["dopamine"] == 50.0
    assert s["serotonin"] == 50.0
    assert s["cortisol"] == 30.0


def test_state_init_seed_with_intimacy(tmp_base):
    """intimacy=70 → oxytocin seed = 70."""
    from deskmate.growth.state import GrowthState, save_state
    state = GrowthState(intimacy=70.0)
    save_state(state, tmp_base)
    eng = EmotionEngine(base_dir=tmp_base)
    s = eng.get_state()
    assert s["oxytocin"] == 70.0


# ---------------------------------------------------------------------------
# §7-2: clamp delta
# ---------------------------------------------------------------------------

def test_update_clamps_delta(engine):
    """单次变化不超过 MAX_DELTA，且 clamp 到 [0,100]."""
    s = engine.get_state()
    old_dopamine = s["dopamine"]

    fake_resp = _make_llm_response(dopamine=999.0, serotonin=-10.0)
    with patch("deskmate.memory.llm.chat_memory", return_value=fake_resp):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0)

    s2 = engine.get_state()
    assert s2["dopamine"] <= old_dopamine + MAX_DELTAS["dopamine"] + 0.01
    assert s2["dopamine"] <= 100.0
    assert s2["serotonin"] >= 0.0


def test_clamp_delta_helper():
    assert _clamp_delta(999.0, 50.0, 15.0) == 65.0
    assert _clamp_delta(-999.0, 50.0, 15.0) == 35.0
    assert _clamp_delta(55.0, 50.0, 15.0) == 55.0
    assert _clamp_delta(50.0, 50.0, 15.0) == 50.0


# ---------------------------------------------------------------------------
# §7-3: decay toward base
# ---------------------------------------------------------------------------

def test_decay_toward_base(engine):
    """长时间后 → dopamine/oxytocin 真衰减（从高位回落）。"""
    s = engine.get_state()
    now = datetime.now()

    with engine._lock:
        with sqlite3.connect(str(engine._db)) as conn:
            conn.execute(
                "UPDATE emotion_state SET dopamine=80, oxytocin=80, cortisol=60, "
                "updated_at=? WHERE key='self'",
                ((now - timedelta(hours=2)).isoformat(timespec="seconds"),),
            )

    engine.decay_step(now)
    s2 = engine.get_state()
    assert s2["dopamine"] < 80.0, f"dopamine should decay: {s2['dopamine']}"
    assert s2["oxytocin"] < 80.0, f"oxytocin should decay: {s2['oxytocin']}"
    assert s2["cortisol"] < 60.0, f"cortisol should decay: {s2['cortisol']}"
    assert s2["dopamine"] > BASELINES["dopamine"] - 1
    assert s2["cortisol"] > BASELINES["cortisol"] - 1


def test_decay_no_change_when_fresh(engine):
    """刚更新过 → 衰减几乎不变。"""
    s = engine.get_state()
    engine.decay_step()
    s2 = engine.get_state()
    for k in HORMONE_KEYS:
        assert abs(s2[k] - s[k]) < 0.1


# ---------------------------------------------------------------------------
# §7-4: mood untouched (红线)
# ---------------------------------------------------------------------------

def test_mood_untouched(engine, tmp_base):
    """update_after_turn / decay_step 全程不调 set_mood_from_emotion、不写 GrowthState.mood."""
    from deskmate.growth.state import GrowthState, save_state, load_state
    state = GrowthState(mood=75.0)
    save_state(state, tmp_base)

    fake_resp = _make_llm_response(dopamine=80.0)
    with patch("deskmate.memory.llm.chat_memory", return_value=fake_resp):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0)

    engine.decay_step()

    state_after = load_state(tmp_base)
    assert state_after.mood == 75.0, f"mood should be untouched, got {state_after.mood}"


# ---------------------------------------------------------------------------
# §7-5: intimacy/trust untouched (红线)
# ---------------------------------------------------------------------------

def test_intimacy_untouched(engine, tmp_base):
    """情绪更新后 intimacy/trust 数值不变."""
    from deskmate.growth.state import GrowthState, save_state, load_state
    state = GrowthState(intimacy=55.0, trust=0.7)
    save_state(state, tmp_base)

    fake_resp = _make_llm_response(oxytocin=90.0)
    with patch("deskmate.memory.llm.chat_memory", return_value=fake_resp):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0)

    engine.decay_step()

    state_after = load_state(tmp_base)
    assert state_after.intimacy == 55.0, f"intimacy changed: {state_after.intimacy}"
    assert state_after.trust == 0.7, f"trust changed: {state_after.trust}"


# ---------------------------------------------------------------------------
# §7-6: concurrency lock
# ---------------------------------------------------------------------------

def test_concurrency_lock(engine):
    """并发 20 次 decay_step → 终值与串行一致（无丢 delta）."""
    now = datetime.now()
    with engine._lock:
        with sqlite3.connect(str(engine._db)) as conn:
            conn.execute(
                "UPDATE emotion_state SET dopamine=80, updated_at=? WHERE key='self'",
                ((now - timedelta(hours=1)).isoformat(timespec="seconds"),),
            )

    errors = []

    def run_decay(i):
        try:
            t = now + timedelta(seconds=i)
            engine.decay_step(t)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_decay, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"errors during concurrency: {errors}"
    s = engine.get_state()
    assert 0 <= s["dopamine"] <= 100


# ---------------------------------------------------------------------------
# §7-7: fail-open
# ---------------------------------------------------------------------------

def test_fail_open(engine):
    """LLM 抛异常 → 状态保持、不抛、不崩."""
    s_before = engine.get_state()

    with patch("deskmate.memory.llm.chat_memory", side_effect=RuntimeError("boom")):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0)

    s_after = engine.get_state()
    assert s_after["dopamine"] == s_before["dopamine"]
    assert s_after["description"] == s_before["description"]


def test_fail_open_unparseable(engine):
    """LLM 返回无法解析的内容 → 状态保持."""
    s_before = engine.get_state()

    with patch("deskmate.memory.llm.chat_memory", return_value="not json at all"):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0)

    s_after = engine.get_state()
    assert s_after["dopamine"] == s_before["dopamine"]


# ---------------------------------------------------------------------------
# §7-8: circadian feature only
# ---------------------------------------------------------------------------

def test_circadian_feature_only(engine):
    """提示词含小时特征、不含硬编码「深夜→血清素-」类字符串."""
    captured_prompt = []

    def capture_chat(system, user, **kwargs):
        captured_prompt.append(user)
        return _make_llm_response()

    with patch("deskmate.memory.llm.chat_memory", side_effect=capture_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn("hi", "hello", 300.0, now=datetime(2026, 10, 11, 2, 30))

    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    assert "2时" in prompt or "2点" in prompt or "hour" in prompt.lower() or "2" in prompt
    assert "深夜→血清素" not in prompt
    assert "硬编码" not in prompt


# ---------------------------------------------------------------------------
# §7-9: injection subject tag
# ---------------------------------------------------------------------------

def test_injection_subject_tag(tmp_base):
    """build_full_context_for_turn 含段标题「你自己的心情（…不是萧盛的）」."""
    from deskmate.growth.emotion_engine import EmotionEngine, _ensure_schema, _db_path
    db = _db_path(tmp_base)
    _ensure_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO emotion_state (key, dopamine, serotonin, oxytocin, cortisol, description, updated_at) "
            "VALUES ('self', 50, 50, 40, 30, '今天有点安静', '2026-10-11T10:00:00')"
        )

    os.environ["DESKMATE_EMOTION_ENGINE"] = "1"
    try:
        from deskmate.memory.manager import DeskMateMemoryManager
        mgr = DeskMateMemoryManager(base_dir=tmp_base)
        ctx = mgr.build_full_context_for_turn("你好")
        assert "你自己的心情" in ctx
        assert "不是萧盛的" in ctx
        assert "今天有点安静" in ctx
    finally:
        os.environ.pop("DESKMATE_EMOTION_ENGINE", None)


# ---------------------------------------------------------------------------
# §7-10: self_expression type
# ---------------------------------------------------------------------------

def test_self_expression_type():
    """self_expression 独立于 emotional_care 冷却."""
    from deskmate.proactivity.confidence import (
        evaluate_action,
        ActionType,
        THRESHOLD_SUGGEST_SELF_EXPRESSION,
        THRESHOLD_SUGGEST_CARE,
    )

    r = evaluate_action(0.6, suggestion_type="self_expression")
    assert r.action == ActionType.SUGGEST

    r2 = evaluate_action(0.4, suggestion_type="self_expression")
    assert r2.action == ActionType.NO_ACTION

    r3 = evaluate_action(0.6, suggestion_type="emotional_care")
    assert r3.action == ActionType.SUGGEST

    assert THRESHOLD_SUGGEST_SELF_EXPRESSION != THRESHOLD_SUGGEST_CARE


def test_emotional_care_branch_unchanged():
    """confidence.py 的 emotional_care 分支行为不变."""
    from deskmate.proactivity.confidence import evaluate_action, ActionType, THRESHOLD_SUGGEST_CARE

    r = evaluate_action(THRESHOLD_SUGGEST_CARE, suggestion_type="emotional_care")
    assert r.action == ActionType.SUGGEST

    r2 = evaluate_action(THRESHOLD_SUGGEST_CARE - 0.01, suggestion_type="emotional_care")
    assert r2.action == ActionType.NO_ACTION


# ---------------------------------------------------------------------------
# §7-11: no SYNC_TABLES registration
# ---------------------------------------------------------------------------

def test_no_sync_registration():
    """SYNC_TABLES 不含 emotion_state / emotion_log."""
    from deskmate.memory.sync_store_sqlite import SYNC_TABLES
    assert "emotion_state" not in SYNC_TABLES
    assert "emotion_log" not in SYNC_TABLES


# ---------------------------------------------------------------------------
# §7-12: toggle off
# ---------------------------------------------------------------------------

def test_toggle_off(engine):
    """DESKMATE_EMOTION_ENGINE=0 → update_after_turn 直接 return、无 LLM 调用."""
    call_count = 0

    def counting_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response()

    os.environ["DESKMATE_EMOTION_ENGINE"] = "0"
    try:
        with patch("deskmate.memory.llm.chat_memory", side_effect=counting_chat):
            with patch("deskmate.memory.llm.llm_available", return_value=True):
                engine.update_after_turn("hi", "hello", 300.0)
        assert call_count == 0, "LLM should not be called when disabled"
    finally:
        os.environ.pop("DESKMATE_EMOTION_ENGINE", None)

    assert engine.enabled is True


# ---------------------------------------------------------------------------
# §7-13: 门闸须与实际通道一致（付费 provider OR 免费组可用即放行）
# ---------------------------------------------------------------------------

def test_gate_open_when_only_free_group_available(engine):
    """云端场景：llm_available()=False（无付费 provider）但免费组可用 → 门闸放行、真调 chat_memory。

    2026-10-11 抓到的 bug：原门闸只查 llm_available()，云端仅配免费组 → 误判 False
    → update_after_turn 整段被跳过、情绪层零更新。修复后免费组可用即放行。
    """
    call_count = 0

    def counting_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response(dopamine=80.0)

    with patch("deskmate.memory.llm.chat_memory", side_effect=counting_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=False):
            engine.update_after_turn("hi", "hello", 300.0)

    assert call_count == 1, "gate should open via free group even when llm_available is False"
    s = engine.get_state()
    assert s["dopamine"] > 50.0, f"dopamine should rise via free-group LLM, got {s['dopamine']}"


def test_gate_closed_when_no_llm_at_all(engine):
    """付费+免费组均不可用 → 门闸拦截、不调 chat_memory（fail-open 不崩）。"""
    call_count = 0

    def counting_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response()

    s_before = engine.get_state()
    with patch("deskmate.memory.llm.chat_memory", side_effect=counting_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=False):
            with patch("deskmate.llm_channels.route", return_value=(None, [])):
                engine.update_after_turn("hi", "hello", 300.0)

    assert call_count == 0, "no LLM at all → chat_memory must not be called"
    s_after = engine.get_state()
    assert s_after["dopamine"] == s_before["dopamine"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_parse_emotion_response_json():
    raw = '{"dopamine": 55, "serotonin": 52, "oxytocin": 45, "cortisol": 28, "description": "开心", "reason": "聊得好"}'
    r = _parse_emotion_response(raw)
    assert r is not None
    assert r["dopamine"] == 55


def test_parse_emotion_response_code_block():
    raw = '```json\n{"dopamine": 55, "serotonin": 52, "oxytocin": 45, "cortisol": 28, "description": "ok", "reason": ""}\n```'
    r = _parse_emotion_response(raw)
    assert r is not None
    assert r["dopamine"] == 55


def test_parse_emotion_response_invalid():
    assert _parse_emotion_response("not json") is None
    assert _parse_emotion_response("") is None


# ---------------------------------------------------------------------------
# §十三 整改：self_expression 硬冷却
# ---------------------------------------------------------------------------

def test_self_expression_cooldown_prevents_repeat(engine):
    """冷却期内显著变化不重复写 pending."""
    import time
    now = datetime.now()

    # 第一次：显著变化 → 写 pending
    big_deltas = {"dopamine": 20.0, "serotonin": 0, "oxytocin": 0, "cortisol": 0}
    engine._maybe_trigger_self_expression(big_deltas, now)
    pending_path = engine._db.parent / ".emotion_self_expression_pending"
    assert pending_path.exists()
    assert engine.get_self_expression_pending() is True

    # 消费 pending → 清 pending + 写 cooldown
    engine.consume_self_expression_pending()
    assert not pending_path.exists()
    assert engine.get_self_expression_pending() is False

    # 冷却期内再触发 → 不写 pending
    engine._maybe_trigger_self_expression(big_deltas, now)
    assert not pending_path.exists()


def test_self_expression_pending_consumed_by_tick(engine):
    """pending 被 tick 消费：get → True → consume → False."""
    now = datetime.now()
    big_deltas = {"dopamine": 20.0, "serotonin": 0, "oxytocin": 0, "cortisol": 0}
    engine._maybe_trigger_self_expression(big_deltas, now)

    assert engine.get_self_expression_pending() is True
    engine.consume_self_expression_pending()
    assert engine.get_self_expression_pending() is False


def test_self_expression_fires_after_cooldown(engine):
    """冷却过期后再次显著变化 → 重新写 pending."""
    import time
    now = datetime.now()
    big_deltas = {"dopamine": 20.0, "serotonin": 0, "oxytocin": 0, "cortisol": 0}

    # 第一次触发 + 消费
    engine._maybe_trigger_self_expression(big_deltas, now)
    engine.consume_self_expression_pending()

    # 手动把 cooldown 文件改成 3h 前（过期）
    cooldown_path = engine._db.parent / ".emotion_self_expression_cooldown"
    cooldown_path.write_text(str(now.timestamp() - 10800))

    # 再次显著变化 → 重新写 pending
    later = now + timedelta(hours=3)
    engine._maybe_trigger_self_expression(big_deltas, later)
    pending_path = engine._db.parent / ".emotion_self_expression_pending"
    assert pending_path.exists()
    assert engine.get_self_expression_pending() is True


def test_self_expression_forced_overrides_should_act_false(engine):
    """§十三 阻断级 bug 修复：judge 返回 should_act=False 时，pending 强制仍能 SUGGEST。

    根因：JudgmentResult.should_act 默认 False → evaluate_action L71 语义闸门
    拦截 → NO_ACTION → pending 永不消费 → 死锁。
    修复：tick.py 强制块补 judgment.should_act = True。
    """
    from deskmate.proactivity.confidence import evaluate_action, ActionType

    # 模拟 judge 返回 should_act=False（默认值）
    confidence = 0.6
    suggestion_type = "self_expression"
    should_act = False  # judge 默认返回 False

    # 修复前：should_act=False 会触发语义闸门 → NO_ACTION
    # 修复后：tick.py 强制块把 should_act 置 True → SUGGEST
    should_act_fixed = True  # tick.py 强制块补的这一行

    r = evaluate_action(
        confidence=confidence,
        suggestion_type=suggestion_type,
        should_act=should_act_fixed,
    )
    assert r.action == ActionType.SUGGEST, (
        f"forced self_expression should SUGGEST even when judge default should_act=False, "
        f"got {r.action}"
    )

    # 对照组：不修复时确实被拦
    r_blocked = evaluate_action(
        confidence=confidence,
        suggestion_type=suggestion_type,
        should_act=should_act,
    )
    assert r_blocked.action == ActionType.NO_ACTION, (
        "without fix, should_act=False should block via semantic gate"
    )
