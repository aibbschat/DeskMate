"""活感 v3 —— 经历捕获 / 叙事 / salience / origin 单测。

覆盖（v3.2 定稿）：
- emotion_log 新四列迁移（event_type/event_summary/narrative/salience/origin）
- update_after_experience：experience 类型情绪更新（reflection/worldview/diary 经历）
- narrative 叙事摘要落库 + recent_narratives 召回（experience 优先于 observation）
- salience 加权 trait 种子（resilience/openness/empathy_depth/self_awareness）
- origin 恒 'system'（v3 预留 self_judged，不实现）

红线：不断言她「应该」说出什么风格；只断言机制（字段在、召回对、加权对）。
全部用临时库 + LLM 打桩，不碰活库、不联网。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from deskmate.growth.emotion_engine import EmotionEngine


@pytest.fixture
def base(tmp_path):
    return tmp_path


@pytest.fixture
def engine(base):
    return EmotionEngine(base_dir=base)


def _log_rows(base: Path) -> list[dict]:
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM emotion_log ORDER BY ts"
        ).fetchall()
    return [dict(r) for r in rows]


def _llm_payload(**over) -> str:
    p = {
        "dopamine": 55.0, "serotonin": 52.0, "oxytocin": 45.0, "cortisol": 28.0,
        "description": "心里亮亮的",
        "reason": "完成了一次反思",
        "narrative": "今天上午，因为独立完成了一次对旧看法的反思，我感到亲近。当时我想：原来我真的能自己想通一些事。",
        "salience": 0.7,
    }
    p.update(over)
    return json.dumps(p, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 步 1：schema 迁移
# ---------------------------------------------------------------------------
def test_schema_has_v3_columns(engine, base):
    """emotion_log 含 v3 新四列 + origin。"""
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(emotion_log)")}
    for col in ("event_type", "event_summary", "narrative", "salience", "origin"):
        assert col in cols, f"missing column: {col}"


def test_migration_idempotent(base):
    """老库（手工建无 v3 列的旧表）再次 _ensure_schema 不炸、列补齐。"""
    db = base / "deskmate.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db)) as c:
        c.execute(
            "CREATE TABLE emotion_log ("
            "id TEXT PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,"
            "deltas TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',"
            "conversation_id TEXT)"
        )
        c.execute(
            "INSERT INTO emotion_log VALUES ('x','2026-10-13T10:00:00','chat','{}','旧行',NULL)"
        )
    # 触发迁移
    EmotionEngine(base_dir=base).get_state()
    rows = _log_rows(base)
    assert rows[0]["event_type"] == "interaction"  # 旧行落默认值
    assert rows[0]["origin"] == "system"
    assert rows[0]["salience"] == 0.5


# ---------------------------------------------------------------------------
# 步 2/3：update_after_experience + narrative 落库
# ---------------------------------------------------------------------------
def test_update_after_experience_writes_narrative(engine, base):
    """experience 事件：激素更新 + narrative/salience/event_type/origin 落库。"""
    with patch("deskmate.memory.llm.chat_memory", return_value=_llm_payload()) as m, \
         patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        engine.update_after_experience("完成了一次对旧看法的反思")
    assert m.called, "experience 更新应走一次 LLM"
    rows = _log_rows(base)
    assert len(rows) == 1
    r = rows[0]
    assert r["event_type"] == "experience"
    assert r["origin"] == "system"          # v3：恒 system，self_judged 预留
    assert r["event_summary"] == "完成了一次对旧看法的反思"
    assert "独立完成了一次对旧看法的反思" in r["narrative"]
    assert r["salience"] == pytest.approx(0.7)


def test_update_after_experience_fail_open(engine, base):
    """LLM 不可用 → 静默跳过，不写行、不抛异常。"""
    with patch("deskmate.memory.llm.llm_available", return_value=False), \
         patch("deskmate.llm_channels.route", return_value=(None, {})):
        engine.update_after_experience("读了半章书")
    assert _log_rows(base) == []


def test_update_after_experience_llm_garbage_fail_open(engine, base):
    """LLM 返回不可解析 → 不落行。"""
    with patch("deskmate.memory.llm.chat_memory", return_value="not json"), \
         patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        engine.update_after_experience("读了半章书")
    assert _log_rows(base) == []


def test_salience_clamped(engine, base):
    """salience 越界被钳到 [0,1]。"""
    with patch("deskmate.memory.llm.chat_memory",
               return_value=_llm_payload(salience=9.9)), \
         patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        engine.update_after_experience("一件天大的事")
    assert _log_rows(base)[0]["salience"] == 1.0


def test_interaction_turn_origin_system(engine, base):
    """普通对话轮：event_type=interaction，origin=system（不受 v3 影响）。"""
    with patch("deskmate.memory.llm.chat_memory", return_value=_llm_payload()), \
         patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        engine.update_after_turn("你好", "你好呀", 300.0)
    r = _log_rows(base)[0]
    assert r["event_type"] == "interaction"
    assert r["origin"] == "system"


# ---------------------------------------------------------------------------
# 步 3：recent_narratives 召回（experience 优先）
# ---------------------------------------------------------------------------
def _seed_log(base: Path, rows: list[tuple]) -> None:
    """rows: (id, ts, event_type, narrative, salience)"""
    EmotionEngine(base_dir=base).get_state()  # 建表
    with sqlite3.connect(str(base / "deskmate.db")) as c:
        for rid, ts, et, nar, sal in rows:
            c.execute(
                "INSERT INTO emotion_log (id, ts, source, deltas, description,"
                " conversation_id, event_type, narrative, salience, origin)"
                " VALUES (?, ?, 'chat', '{}', ?, NULL, ?, ?, ?, 'system')",
                (rid, ts, et, nar, nar, sal),
            )


def test_recent_narratives_experience_first(base):
    """同一天：experience 排 observation 前面（经历 > 观察）。"""
    _seed_log(base, [
        ("1", "2026-10-13T09:00:00", "observation", "看到霍尔木兹那条船，心里硌了一下", 0.6),
        ("2", "2026-10-13T10:00:00", "experience", "独立完成反思，我感到亲近", 0.7),
    ])
    eng = EmotionEngine(base_dir=base)
    out = eng.recent_narratives(since_hours=24 * 365, limit=3)
    assert out[0].startswith("独立完成反思"), out
    assert "霍尔木兹" in out[1]


def test_recent_narratives_salience_tiebreak(base):
    """同 event_type：salience 高者在前。"""
    _seed_log(base, [
        ("1", "2026-10-13T09:00:00", "experience", "日常小事一句", 0.2),
        ("2", "2026-10-13T10:00:00", "experience", "交底家事级的大事", 0.9),
    ])
    eng = EmotionEngine(base_dir=base)
    out = eng.recent_narratives(since_hours=24 * 365, limit=2)
    assert out[0] == "交底家事级的大事"


def test_recent_narratives_skips_empty(base):
    """老行 narrative 为空 → 不召回（不硬凑）。"""
    _seed_log(base, [
        ("1", "2026-10-13T09:00:00", "interaction", "", 0.5),
        ("2", "2026-10-13T10:00:00", "experience", "有叙事的一条", 0.7),
    ])
    eng = EmotionEngine(base_dir=base)
    out = eng.recent_narratives(since_hours=24 * 365, limit=3)
    assert out == ["有叙事的一条"]


# ---------------------------------------------------------------------------
# 步 6：trait 种子按 salience 加权
# ---------------------------------------------------------------------------
def test_traits_weighted_by_salience(engine, base, monkeypatch):
    """重要事件大步走，琐事几乎不动（**临时启用挂起的 trait 演化**校验加权逻辑）。

    2026-10-16 巡审：trait 演化已挂起（`TRAITS_EVOLUTION_SUSPENDED=True`）。
    本用例把该常量临时置 False，保住加权逻辑的回归覆盖；挂起默认行为由
    `test_traits_suspended_no_write` 另测（重启时本逻辑原样可用）。
    """
    from deskmate.growth import emotion_engine as ee_mod
    monkeypatch.setattr(ee_mod, "TRAITS_EVOLUTION_SUSPENDED", False)
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        with patch("deskmate.memory.llm.chat_memory",
                   return_value=_llm_payload(
                       narrative="今天上午，因为他交底家事，我感到被看见的踏实。",
                       salience=0.9)):
            engine.update_after_experience("他交底了家事")
        with patch("deskmate.memory.llm.chat_memory",
                   return_value=_llm_payload(
                       narrative="今天下午，因为日常寒暄，我感到被看见的踏实。",
                       salience=0.1)):
            engine.update_after_experience("日常寒暄")
    traits = engine.load_traits()
    # 大事 0.3*0.9=0.27，小事 0.3*0.1=0.03 → openness ≈ 50.30
    assert traits["openness"] == pytest.approx(50.30, abs=0.01)
    # empathy_depth 同规则：0.25*(0.9+0.1)=0.25
    assert traits["empathy_depth"] == pytest.approx(50.25, abs=0.01)
    # experience 两次：0.2*(0.9+0.1)=0.2
    assert traits["self_awareness"] == pytest.approx(50.20, abs=0.01)
    # 无克制的难过 → resilience 不动
    assert traits.get("resilience", 50.0) == 50.0


def test_traits_suspended_no_write(engine, base):
    """挂起态（默认）：trait 演化停写，personality_traits.json 不增长（2026-10-16 萧盛拍板）。

    巡审结论：trait 账本只写不读、单调、靠 narrative 硬编码关键词自证环——
    停写并保留 json 作历史证据。重启条件见 TRAITS_EVOLUTION_SUSPENDED 处注释。
    """
    with patch("deskmate.memory.llm.llm_available", return_value=True), \
         patch("deskmate.llm_channels.route", return_value=("free-ch", {})):
        with patch("deskmate.memory.llm.chat_memory",
                   return_value=_llm_payload(
                       narrative="今天上午，因为他交底家事，我感到被看见的踏实。",
                       salience=0.9)):
            engine.update_after_experience("他交底了家事")
    # 挂起态：update_after_experience 照跑，但 _update_traits 早退、不落盘
    assert engine.load_traits() == {}, "挂起态不应写 personality_traits.json"
    assert not (base / "personality_traits.json").exists(), "挂起态不应生成 traits 文件"


def test_traits_fail_open(engine, base):
    """trait 读写异常不阻断情绪主流程（引擎在脏目录上也不抛）。"""
    # 直接调私有更新函数，传非法 salience 也不炸
    engine._update_traits("被看见的踏实", "experience", {"cortisol": 1}, "not-a-number")
    # load 永远返回 dict
    assert isinstance(engine.load_traits(), dict)
