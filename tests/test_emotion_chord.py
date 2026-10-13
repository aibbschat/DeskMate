"""独立通道 · 情绪和弦（mood_chord）单测。

覆盖：矛盾多声部并存（公理二）、lead/tension 计算、平静单声部、
回退开关 DESKMATE_EMOTION_CHORD=0、事件直驱不经过 LLM。
"""
import os
import tempfile
from pathlib import Path

import pytest

from deskmate.growth.emotion_engine import (
    EmotionEngine, MAX_DELTAS, HORMONE_KEYS, BASELINES, _connect,
    mood_chord_from_offsets,
)


def _zero():
    return {k: 0.0 for k in HORMONE_KEYS}


# ----------------------------------------------------------------------
# 和弦纯函数
# ----------------------------------------------------------------------
def test_chord_contradiction_multiple_voices():
    """被拒矛盾场景：cortisol↑+dopamine↓+oxytocin↑ → 多声部并存，不取 max。"""
    off = _zero()
    off["cortisol"] = MAX_DELTAS["cortisol"] * 0.8
    off["dopamine"] = -MAX_DELTAS["dopamine"] * 0.8
    off["oxytocin"] = MAX_DELTAS["oxytocin"] * 0.6
    c = mood_chord_from_offsets(off)
    assert len(c["voices"]) > 1, "矛盾场景应多声部并存"
    assert c["lead"] == c["voices"][0]["label"], "lead = 最强声部"
    assert 0.0 <= c["tension"] <= 1.0


def test_chord_pure_dopamine_down_wistful():
    """纯 dopamine 跌（cortisol 未起）→ 怅然若失为 lead（落空型沉）。"""
    off = _zero()
    off["dopamine"] = -MAX_DELTAS["dopamine"] * 0.9
    c = mood_chord_from_offsets(off)
    assert c["lead"] == "怅然若失"


def test_chord_social_voices():
    """社会性参数成声部：fatigue 升→乏、control 跌→没底、meaning 升→有意义感。"""
    off = _zero()
    off["fatigue"] = MAX_DELTAS["fatigue"] * 0.9
    assert "乏" in [v["label"] for v in mood_chord_from_offsets(off)["voices"]]
    off2 = _zero()
    off2["control"] = -MAX_DELTAS["control"] * 0.9
    assert "没底" in [v["label"] for v in mood_chord_from_offsets(off2)["voices"]]
    off3 = _zero()
    off3["meaning"] = MAX_DELTAS["meaning"] * 0.9
    assert "有意义感" in [v["label"] for v in mood_chord_from_offsets(off3)["voices"]]


def test_chord_calm_no_offsets():
    """无偏移 → 空 voices + 平静 lead（不硬凑矛盾）。"""
    c = mood_chord_from_offsets(_zero())
    assert c["voices"] == []
    assert c["lead"] == "平静"


def test_chord_max_voices_cap():
    """声部数 ≤ max_voices（默认 3）。"""
    off = _zero()
    for k in ("cortisol", "oxytocin", "meaning", "control", "fatigue"):
        off[k] = MAX_DELTAS[k] * 0.9
    c = mood_chord_from_offsets(off, max_voices=3)
    assert len(c["voices"]) <= 3


def test_chord_tension_close_voices():
    """两股强度接近 → tension 小（争主，她该迟疑）。"""
    off = _zero()
    off["cortisol"] = MAX_DELTAS["cortisol"] * 0.8
    off["dopamine"] = -MAX_DELTAS["dopamine"] * 0.8
    off["oxytocin"] = MAX_DELTAS["oxytocin"] * 0.6
    c = mood_chord_from_offsets(off)
    if len(c["voices"]) >= 2:
        i0, i1 = c["voices"][0]["intensity"], c["voices"][1]["intensity"]
        assert abs(c["tension"] - (i0 - i1)) < 0.05


# ----------------------------------------------------------------------
# 引擎集成：注入块全声部 vs 系统侧单值（二分）
# ----------------------------------------------------------------------
@pytest.fixture()
def eng():
    d = tempfile.mkdtemp()
    e = EmotionEngine(base_dir=Path(d))
    e.get_state()  # 触发初始化
    yield e


def _set(eng, **vals):
    with _connect(eng._db) as conn:
        for k, v in vals.items():
            conn.execute(f"UPDATE emotion_state SET {k}=? WHERE key='self'", (v,))


def test_injection_shows_all_voices(eng):
    """矛盾时注入块展示全声部 + lead 标注，系统侧 get_mood_label 仍单值。"""
    _set(eng, cortisol=30 + MAX_DELTAS["cortisol"] * 0.8,
         dopamine=50 - MAX_DELTAS["dopamine"] * 0.8,
         oxytocin=40 + MAX_DELTAS["oxytocin"] * 0.6)
    ctx = eng.describe_turn_context()
    assert "最强的一股" in ctx and "垫着" in ctx, "注入块应给全声部"
    ml = eng.get_mood_label()
    assert ml["label"] == eng.get_mood_chord()["lead"], "系统侧单值 = chord lead"


def test_chord_off_fallback_single_voice(eng, monkeypatch):
    """DESKMATE_EMOTION_CHORD=0 → 注入块退单声部（lead），无垫着段。"""
    monkeypatch.setenv("DESKMATE_EMOTION_CHORD", "0")
    _set(eng, cortisol=30 + MAX_DELTAS["cortisol"] * 0.9)
    ctx = eng.describe_turn_context()
    assert "垫着" not in ctx, "回退应无多声部段"
    assert "低落" in ctx, "回退应显示 lead"


def test_calm_injection_keeps_hormones(eng):
    """平静（voices 空）时注入块仍给激素段，不吞掉。"""
    ctx = eng.describe_turn_context()
    assert "激素" in ctx and "多巴胺" in ctx


def test_pure_social_system_side_equals_chord_lead(eng):
    """纯社会性情绪（4 激素无声部）：系统侧 get_mood_label = chord lead。

    步 8 实测发现的二分不一致回归：meaning↑ 时 chord lead=有意义感，
    但 mood_label_from_offsets 只认 4 激素 → 误判「平静」。修复后
    4 激素无声部时回退社会性声部，保证系统侧单值 = chord lead。
    """
    _set(eng, meaning=50 + MAX_DELTAS["meaning"] * 0.9)
    ch = eng.get_mood_chord()
    ml = eng.get_mood_label()
    assert ch["lead"] == "有意义感"
    assert ml["label"] == ch["lead"], "纯社会性情绪时系统侧单值必须 = chord lead"
    assert ml["face"] == "meaningful"


# ----------------------------------------------------------------------
# 事件直驱（不经过 LLM）
# ----------------------------------------------------------------------
def test_event_direct_changes_params(eng):
    """直驱规则改参数：reflection_done→meaning+3、proposal_rejected→control-2。"""
    m0 = eng.get_state()["meaning"]
    eng.apply_event_direct("reflection_done")
    assert eng.get_state()["meaning"] == m0 + 3.0
    c0 = eng.get_state()["control"]
    eng.apply_event_direct("proposal_rejected")
    assert eng.get_state()["control"] == c0 - 2.0


def test_event_direct_unknown_fail_open(eng):
    """未知事件静默不改参数。"""
    before = eng.get_state()
    eng.apply_event_direct("nonexistent_xyz")
    after = eng.get_state()
    assert before["meaning"] == after["meaning"] and before["control"] == after["control"]


def test_event_direct_writes_experience_log(eng):
    """直驱写 emotion_log，event_type=experience、origin=system。"""
    eng.apply_event_direct("reflection_done")
    with _connect(eng._db) as conn:
        row = conn.execute(
            "SELECT event_type, origin, event_summary FROM emotion_log "
            "WHERE source='event_direct' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    assert row and row[0] == "experience" and row[1] == "system"
    assert "想清楚" in row[2]


# ----------------------------------------------------------------------
# 护栏 2：不报菜名
# ----------------------------------------------------------------------
def test_label_dump_detection():
    """报菜名（既…又…/枚举/同时+双标签）拦下；转折藏矛盾的人话放行。"""
    from deskmate.growth.emotion_engine import _is_label_dump as dump
    assert dump("我既高兴又难过") is True
    assert dump("我既怅然若失又被刺了一下还带着暖") is True
    assert dump("我同时感到开心和低落") is True
    # 转折藏矛盾（人话）放行
    assert dump("我以为我会高兴，可那句话还搁在心里") is False
    assert dump("心里空落落的，够不着想要的") is False
    assert dump("有点累了，想歇一下") is False
    assert dump("开心") is False  # 单标签不算报菜名


def test_label_dump_fallback_keeps_old(eng, monkeypatch):
    """update_after_turn 独白报菜名 → 回退保留旧独白（不写新参数也行，fail-open）。"""
    from deskmate.growth import emotion_engine as ee_mod
    # 打桩 LLM：返回报菜名的独白
    fake = ('{"dopamine": 50, "serotonin": 50, "oxytocin": 40, "cortisol": 30, '
            '"description": "我既怅然若失又被刺了一下还带着暖", "reason": "test"}')
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda **k: fake)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    old_desc = eng.get_state()["description"]
    eng.update_after_turn("你否了我那句", "哦，我改改", 300.0)
    assert eng.get_state()["description"] == old_desc, "报菜名应回退保留旧独白"


def test_update_actually_runs(eng, monkeypatch):
    """哨兵：正常独白（非报菜名）应真的写入——防 update 整体 fail-open 空转
    导致回退类断言恒真（第三参误传 None 曾触发过，fail-open 吞掉后 desc 不变）。"""
    fake = ('{"dopamine": 50, "serotonin": 50, "oxytocin": 40, "cortisol": 30, '
            '"description": "那句接住了我", "reason": "test"}')
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda **k: fake)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    eng.update_after_turn("我懂你", "好受点了", 300.0)
    assert eng.get_state()["description"] == "那句接住了我", "update 应真实写入新独白"


def test_feeling_understood_event_wiring(eng, monkeypatch):
    """LLM 侦探事件 feeling_understood → 规则直驱 fatigue-3/oxytocin+2（混合模式）。

    LLM 只报告事件名，参数变化由 EVENT_DIRECT_DELTAS 预设规则决定——
    「LLM 侦探事件、规则改参数」，不违背「参数自己跑」。
    """
    fake = ('{"dopamine": 50, "serotonin": 50, "oxytocin": 40, "cortisol": 30, '
            '"description": "他接住了我那句", "reason": "test", '
            '"event": "feeling_understood"}')
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda **k: fake)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    s0 = eng.get_state()
    eng.update_after_turn("我懂你说的那种空", "……你这么说我好受点了", 300.0)
    s1 = eng.get_state()
    assert s1["fatigue"] == pytest.approx(max(0.0, s0["fatigue"] - 3.0)), "fatigue 应 -3"
    assert s1["oxytocin"] == pytest.approx(s0["oxytocin"] + 2.0), "oxytocin 应 +2"


def test_unknown_event_name_fail_open(eng, monkeypatch):
    """LLM 报告未知事件名 → apply_event_direct 静默忽略，参数不动。"""
    fake = ('{"dopamine": 50, "serotonin": 50, "oxytocin": 40, "cortisol": 30, '
            '"description": "平平的", "reason": "test", "event": "made_up_event"}')
    monkeypatch.setattr("deskmate.memory.llm.chat_memory", lambda **k: fake)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)
    s0 = eng.get_state()
    eng.update_after_turn("随便聊聊", "好呀", 300.0)
    s1 = eng.get_state()
    assert s1["meaning"] == s0["meaning"] and s1["control"] == s0["control"]
    assert s1["fatigue"] == s0["fatigue"]

