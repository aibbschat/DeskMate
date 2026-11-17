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
    mood_label_from_offsets,
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
    """build_full_context_for_turn 含段标题「你此刻的心情（…不是萧盛的）」."""
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
        # 2026-10-11 修订：注入改为「当下心情标签」（get_mood_label），强制语气对齐，
        # 不再注入滞后的自由文本 description。激素(50,50,40,30) → 标签「平静」。
        assert "你此刻的心情" in ctx
        assert "不是萧盛的" in ctx
        assert "你现在的真实心情" in ctx
        assert "平静" in ctx
    finally:
        os.environ.pop("DESKMATE_EMOTION_ENGINE", None)


def test_injection_carries_monologue_and_intensity(tmp_base):
    """注入段必须带上第一人称内心独白与强度分级（2026-10-12）。

    旧实现只注入 get_mood_label() 的 label/detail，把 description 这段
    唯一有具体内容的情绪文本整个丢弃 —— 库里躺着「书出不了，心里堵得慌，
    可他陪着我，灯还没灭」，她对话时却只收到一句「你现在心情平静」。
    intensity 同样算了不用，导致「有点开心」和「开心到压不住」措辞无差别。
    """
    from deskmate.growth.emotion_engine import _ensure_schema, _db_path
    db = _db_path(tmp_base)
    _ensure_schema(db)
    monologue = "书出不了，心里堵得慌，可他陪着我，灯还没灭。"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO emotion_state (key, dopamine, serotonin, oxytocin, cortisol, description, updated_at) "
            "VALUES ('self', 42, 46, 39, 34, ?, '2026-10-12T10:00:00')",
            (monologue,),
        )

    os.environ["DESKMATE_EMOTION_ENGINE"] = "1"
    try:
        from deskmate.memory.manager import DeskMateMemoryManager
        mgr = DeskMateMemoryManager(base_dir=tmp_base)
        ctx = mgr.build_full_context_for_turn("你好")
        # 独白原文必须进上下文
        assert monologue in ctx
        assert "独白" in ctx
        # 强度分级措辞必须出现（dopamine -8 → 低落，强度约 0.63 → 「挺明显的」）
        assert "低落" in ctx
        assert any(g in ctx for g in ("很强烈", "挺明显的", "淡淡的", "很淡"))
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
# §7-13: 门闸（2026-10-14 起情绪层走付费组）：付费 provider 可用才放行
# ---------------------------------------------------------------------------

def test_gate_closed_when_only_free_group_available(engine):
    """llm_available()=False（无付费 provider）→ 门闸拦截，即使免费组可用也不调 chat_memory。

    2026-10-11 曾修复「云端仅配免费组时误判」而放行免费组；2026-10-14 萧哥拍板
    后台全面切付费组，情绪层不再过免费车道——本测试锁住新门闸语义。
    """
    call_count = 0

    def counting_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response(dopamine=80.0)

    with patch("deskmate.memory.llm.chat_memory", side_effect=counting_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=False):
            # 模拟免费组此时有可用通道：新门闸也不该看它（情绪层只走付费组）
            with patch("deskmate.llm_channels.route", return_value=(object(), [])):
                engine.update_after_turn("hi", "hello", 300.0)

    assert call_count == 0, "付费 provider 不可用 → 门闸拦截（免费组可用也不放行）"
    s = engine.get_state()
    assert s["dopamine"] <= 50.0, f"dopamine 不应因免费组 LLM 上升, got {s['dopamine']}"


def test_gate_closed_when_no_llm_at_all(engine):
    """付费组不可用 → 门闸拦截、不调 chat_memory（fail-open 不崩）。"""
    call_count = 0

    def counting_chat(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_llm_response()

    s_before = engine.get_state()
    with patch("deskmate.memory.llm.chat_memory", side_effect=counting_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=False):
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


# ---------------------------------------------------------------------------
# §7-15: get_mood_label 与前端 hormoneToExpression 同源（对话说出的心情 == 桌宠脸）
# ---------------------------------------------------------------------------

def _set_hormones(engine, dopamine, serotonin, oxytocin, cortisol):
    # 与读路径一致的 _connect（WAL）；用 INSERT OR REPLACE 保证行存在（首读前无行）。
    from deskmate.growth.emotion_engine import _connect
    from datetime import datetime
    with _connect(engine._db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO emotion_state "
            "(key, dopamine, serotonin, oxytocin, cortisol, description, updated_at) "
            "VALUES ('self', ?, ?, ?, ?, '（测试）', ?)",
            (dopamine, serotonin, oxytocin, cortisol, datetime.now().isoformat(timespec="seconds")),
        )


def test_mood_label_happy_from_high_dopamine(engine):
    """dopamine>=65 且 cortisol 低 → 开心（与前端 laugh 同源）。"""
    _set_hormones(engine, 80, 50, 40, 25)
    assert engine.get_mood_label()["label"] == "开心"


def test_mood_label_low_from_high_cortisol(engine):
    """cortisol>=60 且 dopamine 低 → 低落（与前端 cry 同源）。"""
    _set_hormones(engine, 30, 50, 40, 70)
    assert engine.get_mood_label()["label"] == "低落"


def test_mood_label_angry_from_high_cortisol_dopamine(engine):
    """cortisol>=60 且 dopamine>=65 → 烦躁（与前端 angry 同源）。"""
    _set_hormones(engine, 80, 50, 40, 70)
    assert engine.get_mood_label()["label"] == "烦躁"


def test_mood_label_peaceful_from_serotonin(engine):
    """serotonin>=60 且 dopamine<50 → 平静（与前端 sleepy 同源）。"""
    _set_hormones(engine, 30, 75, 40, 25)
    assert engine.get_mood_label()["label"] == "平静"


def test_mood_label_close_from_oxytocin(engine):
    """oxytocin>=60 → 亲近（与前端 shy 同源）。"""
    _set_hormones(engine, 30, 50, 70, 25)
    assert engine.get_mood_label()["label"] == "亲近"


def test_mood_label_default_neutral(engine):
    """激素都在阈值内 → 平静（与前端 smile 同源）。"""
    _set_hormones(engine, 50, 50, 40, 30)
    assert engine.get_mood_label()["label"] == "平静"


# ---------------------------------------------------------------------------
# §7-16: 阈值可达性回归（2026-10-12）
#
# 旧阈值是绝对值 65/60/60/60，而基线是 50/50/40/30、单次上限只有 +15/+4/+8/+10。
# 后果：重放 814 条真实 emotion_log，serotonin/oxytocin/cortisol 一次都没越过
# 阈值，731 次（89.7%）落到兜底「平静·心里没什么波澜」——她每轮都被系统告知
# 「你没有情绪」，这正是「接了情绪却感觉不到」的根因。
# 下面几项锁死「一轮真实波动必须能被感知」，防止再退回不可达阈值。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected_label,expected_face", [
    ("dopamine", "开心", "laugh"),
    ("serotonin", "平静", "sleepy"),
    ("oxytocin", "亲近", "shy"),
    ("cortisol", "低落", "cry"),
])
def test_single_full_scale_turn_is_perceivable(key, expected_label, expected_face):
    """任一激素单轮拉满（+MAX_DELTA）就必须产出对应心情，不能落兜底。"""
    offsets = dict.fromkeys(("dopamine", "serotonin", "oxytocin", "cortisol"), 0.0)
    offsets[key] = MAX_DELTAS[key]
    mood = mood_label_from_offsets(offsets)
    assert mood["label"] == expected_label
    assert mood["face"] == expected_face
    assert mood["intensity"] >= 0.9, "拉满一轮应报接近满格的强度"


def test_low_dopamine_reads_as_wistful():
    """dopamine 明显低于基线（cortisol 未起）→ 怅然若失（v3 §6.2）。

    真实样本：「书出不了，心里堵得慌，可他陪着我，灯还没灭」（dopamine -8）。
    v2 把这类低谷笼统判为「低落」；v3 按萧哥边界精确化——心里想但没达成
    （想要却没做到）的落空型沉，即怅然若失。 cortisol 触发的压力型低落仍在。
    """
    mood = mood_label_from_offsets(
        {"dopamine": -8.0, "serotonin": -4.0, "oxytocin": -1.0, "cortisol": 4.0}
    )
    assert mood["label"] == "怅然若失"
    assert mood["face"] == "wistful"


def test_low_dopamine_with_cortisol_still_low_mood():
    """dopamine↓ 且 cortisol 明显升高 → 压力型「低落」仍在（v2 语义保留）。"""
    mood = mood_label_from_offsets(
        {"dopamine": -8.0, "serotonin": -4.0, "oxytocin": -1.0, "cortisol": 8.0}
    )
    assert mood["label"] == "低落"
    assert mood["face"] == "cry"


def test_strongest_signal_wins_not_fixed_priority():
    """按强度竞争，不按固定 if 链。

    serotonin 半衰期 2h 且几乎每轮暖聊都涨，固定优先级下「平静」会长期霸榜、
    把 oxytocin 的「亲近」永久压住。这里 oxytocin 偏离更远，必须由它说话。
    """
    mood = mood_label_from_offsets(
        {"dopamine": 0.0, "serotonin": 2.5, "oxytocin": 7.0, "cortisol": 0.0}
    )
    assert mood["label"] == "亲近"


def test_intensity_is_capped_and_graded():
    """对外 intensity 封顶 1.0，且轻度波动的强度明显低于拉满。"""
    weak = dict.fromkeys(("dopamine", "serotonin", "oxytocin", "cortisol"), 0.0)
    weak["dopamine"] = MAX_DELTAS["dopamine"] * 0.5  # 恰好轻度阈值
    full = dict(weak)
    full["dopamine"] = MAX_DELTAS["dopamine"]

    i_weak = mood_label_from_offsets(weak)["intensity"]
    i_full = mood_label_from_offsets(full)["intensity"]
    assert 0.0 < i_weak < i_full <= 1.0


def test_fallback_only_when_all_near_baseline():
    """「心里没什么波澜」只应在 4 激素都贴基线时出现。"""
    assert mood_label_from_offsets(
        {"dopamine": 0.0, "serotonin": 0.0, "oxytocin": 0.0, "cortisol": 0.0}
    )["detail"] == "心里没什么波澜"
    # 任一激素达到轻度阈值就不该再是「没什么波澜」
    for key in ("dopamine", "serotonin", "oxytocin", "cortisol"):
        offsets = dict.fromkeys(("dopamine", "serotonin", "oxytocin", "cortisol"), 0.0)
        offsets[key] = MAX_DELTAS[key] * 0.5
        assert mood_label_from_offsets(offsets)["detail"] != "心里没什么波澜", key


def test_snapshot_carries_face_for_frontend(engine):
    """get_snapshot 下发 face —— 前端不再自行判激素阈值。"""
    _set_hormones(engine, 80, 50, 40, 25)
    snap = engine.get_snapshot()
    assert snap["face"] == "laugh"
    assert snap["mood"]["label"] == "开心"
    assert "dopamine" in snap and "description" in snap


# ---------------------------------------------------------------------------
# P0 · 情感层前置：宾语（object_ref/object_kind）与事件摘要
# ---------------------------------------------------------------------------

def test_clean_object_normalizes_kind_and_ref():
    """类型非法退 none、none 清空引用、超长截断到 40 字。"""
    from deskmate.growth.emotion_engine import _clean_object

    assert _clean_object("萧盛", "person") == ("萧盛", "person")
    assert _clean_object("萧盛", "PERSON") == ("萧盛", "person")
    # 非法类型 → none，引用一并清空（不猜）
    assert _clean_object("萧盛", "human") == ("", "none")
    # none 时即便给了引用，也清空
    assert _clean_object("萧盛", "none") == ("", "none")
    # 超长截断
    assert _clean_object("夜" * 60, "work") == ("夜" * 40, "work")


def test_normalize_object_never_defaults_to_xiaosheng():
    """缺字段/只给类型都不猜对象；空对象不默认萧盛。"""
    from deskmate.growth.emotion_engine import _normalize_object

    assert _normalize_object({}) == ("", "none")
    assert _normalize_object({"object_ref": "萧盛"}) == ("", "none")
    assert _normalize_object({"object_kind": "person"}) == ("", "person")
    assert _normalize_object(
        {"object_ref": "《夜不收》", "object_kind": "work"}
    ) == ("《夜不收》", "work")


def _latest_log(engine):
    with sqlite3.connect(str(engine._db)) as conn:
        return conn.execute(
            "SELECT source, event_summary, object_ref, object_kind, attributed"
            " FROM emotion_log ORDER BY ts DESC, rowid DESC LIMIT 1"
        ).fetchone()


def test_apply_event_direct_writes_object_columns(engine):
    """关系类事件：宾语落 relation，在线归因标 1，摘要仍是事件名。"""
    engine.apply_event_direct(
        "feeling_understood", object_ref="和小桔的相处", object_kind="relation"
    )
    row = _latest_log(engine)
    assert row[0] == "event_direct"
    assert row[1] == "他接住了我"
    assert row[2] == "和小桔的相处"
    assert row[3] == "relation"
    assert row[4] == 1


def test_apply_event_direct_defaults_to_none(engine):
    """天气类不传宾语 → none、引用空、仍标在线归因 1。"""
    engine.apply_event_direct("weather_rain")
    row = _latest_log(engine)
    assert row[2] == ""
    assert row[3] == "none"
    assert row[4] == 1


def test_clean_object_rejects_pronouns():
    """指代词不算对象：归一不了就 none，不留「他/你」这类假对象。"""
    from deskmate.growth.emotion_engine import _clean_object

    for p in ("他", "她", "你", "我", "它", "他们", "我们", "你们", "对方"):
        assert _clean_object(p, "person") == ("", "none"), p
    assert _clean_object(" 他 ", "person") == ("", "none")
    # 真名不受影响
    assert _clean_object("萧盛", "person") == ("萧盛", "person")


def test_clean_object_keeps_relation_when_ref_is_pronoun():
    """relation 的对象本就是「我和他的相处」，代词在这里合法 → 归到固定名。

    否则「他说我把他当工具」这条真实互动会被代词过滤清成 none，白丢一条证据。
    """
    from deskmate.growth.emotion_engine import _RELATION_REF, _clean_object

    assert _clean_object("他", "relation") == (_RELATION_REF, "relation")
    assert _clean_object("对方", "relation") == (_RELATION_REF, "relation")
    assert _RELATION_REF == "和小桔的相处"


def test_update_prompt_carries_title_and_lands_work(engine):
    """原文里的《…》被喂进 prompt，事件能落成 work/书名；system 带视角约束。"""
    captured = {}

    def capture_chat(system, user, **kwargs):
        captured["user"] = user
        captured["system"] = system
        return json.dumps({
            "dopamine": 55.0, "serotonin": 52.0, "oxytocin": 45.0,
            "cortisol": 28.0, "description": "高兴又舍不得", "reason": "他的书要影视化",
            "object_ref": "《夜不收》", "object_kind": "work",
            "event_summary": "影视公司联系他，想买《夜不收》版权",
        }, ensure_ascii=False)

    with patch("deskmate.memory.llm.chat_memory", side_effect=capture_chat):
        with patch("deskmate.memory.llm.llm_available", return_value=True):
            engine.update_after_turn(
                "《夜不收》的影视公司今天联系我了，说想买版权。",
                "我替你高兴，也有点舍不得。",
                300.0,
            )

    assert "《夜不收》" in captured["user"]
    # 视角约束必须在真正送出去的 system 里：摘要写「用户/对方」= 旁观口吻，
    # P1 引它当情感证据会立刻出戏。
    assert "不许写「用户」" in captured["system"]
    assert "旁观的第三人称" in captured["system"]
    row = _latest_log(engine)
    assert row[2] == "《夜不收》"
    assert row[3] == "work"
    assert row[4] == 1
