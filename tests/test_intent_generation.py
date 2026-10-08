"""R11 · 意图层（江河设计 §四「自主」前半程）——意图 → 提案 → 裁决 → 教学。

覆盖：
1. ``intents`` 表 CRUD（add/get_open/link/set_status）及 fail-open；
2. ``run_intent_generation`` 闸门（disabled / cooling / no_material / no_provider /
   llm_error）+ 正常路径（解析、origin/horizon 归一、去重、落库）；
3. ``parse_intents`` 解析（JSON / 代码围栏 / 坏项丢弃 / 口径回落）；
4. **护栏**：``propose_from_intent`` 生成的提案**始终停 PROPOSED**，且动作不在白名单
   → 永不被自动执行；
5. 闭环：裁决该意图提案 → R10 lessons 回流 + 可注入（判据 §三.3）。
"""

from __future__ import annotations

import json

import pytest

from deskmate.agency.engine import AgencyEngine
from deskmate.agency.task_log import TaskStatus
from deskmate.agency.behaviors import intent_generation as ig
from deskmate.cognitive.store import CognitiveStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENCY_INTENT_GENERATION_ENABLED", raising=False)
    monkeypatch.delenv("AGENCY_INTENT_COOLDOWN_DAYS", raising=False)
    return tmp_path


def _store(env):
    return CognitiveStore(db_path=env / "deskmate.db")


# ---------------------------------------------------------------------------
# 1) intents CRUD
# ---------------------------------------------------------------------------
def test_add_and_get_open_intents(env):
    store = _store(env)
    iid = store.add_intent("我想弄明白算法圈在争什么", "因为最近总看到", horizon="long",
                           origin="长期兴趣")
    assert iid
    rows = store.get_open_intents()
    assert len(rows) == 1
    assert rows[0]["id"] == iid
    assert rows[0]["goal"].startswith("我想弄明白")
    assert rows[0]["horizon"] == "long"
    assert rows[0]["origin"] == "长期兴趣"
    assert rows[0]["status"] == "open"


def test_add_intent_empty_goal_returns_empty(env):
    assert _store(env).add_intent("   ") == ""


def test_link_and_set_status(env):
    store = _store(env)
    iid = store.add_intent("我想做点什么", origin="未满足的关怀")
    assert store.link_intent_to_task(iid, "task-123") is True
    assert store.set_intent_status(iid, "active") is True
    assert store.get_open_intents() == []          # 已离开 open 池
    with store._connect() as conn:
        got = conn.execute(
            "SELECT status, proposed_task_id FROM intents WHERE id=?", (iid,)
        ).fetchone()
    assert got == ("active", "task-123")


def test_intent_crud_fail_open(env, monkeypatch):
    store = _store(env)

    def _boom(*a, **k):
        raise RuntimeError("库炸了")

    monkeypatch.setattr(CognitiveStore, "_connect", _boom)
    assert store.add_intent("我想做事") == ""
    assert store.get_open_intents() == []
    assert store.link_intent_to_task("x", "y") is False
    assert store.set_intent_status("x", "done") is False


# ---------------------------------------------------------------------------
# 2) parse_intents
# ---------------------------------------------------------------------------
def test_parse_intents_json_array():
    raw = json.dumps([
        {"goal": "我想弄清楚边界问题", "motivation": "因为老想不通", "origin": "长期兴趣",
         "horizon": "long"},
        {"goal": "我想回那封信", "motivation": "因为对方在等", "origin": "未满足的关怀"},
    ], ensure_ascii=False)
    out = ig.parse_intents(raw, 5)
    assert len(out) == 2
    assert out[0]["horizon"] == "long" and out[0]["origin"] == "长期兴趣"
    assert out[1]["horizon"] == "short"  # 缺省归一


def test_parse_intents_tolerates_fence_and_noise():
    raw = "好的，这是结果：\n```json\n[{\"goal\":\"我想写点东西\",\"origin\":\"瞎写\"}]\n```\n希望有帮助"
    out = ig.parse_intents(raw, 5)
    assert len(out) == 1
    assert out[0]["origin"] == ig._DEFAULT_ORIGIN  # 口径外 → 回落默认


def test_parse_intents_drops_bad_items():
    raw = json.dumps([{"motivation": "没有目标"}, {"goal": "  "}, "字符串", {"goal": "我想做A"}],
                     ensure_ascii=False)
    out = ig.parse_intents(raw, 5)
    assert [o["goal"] for o in out] == ["我想做A"]


def test_parse_intents_empty():
    assert ig.parse_intents("", 5) == []
    assert ig.parse_intents("[]", 5) == []
    assert ig.parse_intents("她说她暂时没有念头", 5) == []


def test_parse_intents_respects_limit():
    raw = json.dumps([{"goal": f"我想做{i}"} for i in range(9)], ensure_ascii=False)
    assert len(ig.parse_intents(raw, 3)) == 3


# ---------------------------------------------------------------------------
# 3) run_intent_generation 闸门 + 正常路径
# ---------------------------------------------------------------------------
_MATERIAL = [{"origin": "长期兴趣", "text": "主题「算法圈」（关联 3 条记忆）"}]


def _patch_llm(monkeypatch, out: str, available: bool = True):
    monkeypatch.setattr("deskmate.memory.llm.chat", lambda *a, **k: out)
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: available)


def test_disabled_by_cfg(env):
    res = ig.run_intent_generation(str(env), {"enabled": False})
    assert res["generated"] is False and res["reason"] == "disabled"


def test_disabled_by_env(env, monkeypatch):
    monkeypatch.setenv("AGENCY_INTENT_GENERATION_ENABLED", "0")
    res = ig.run_intent_generation(str(env), {})
    assert res["generated"] is False and res["reason"] == "disabled"


def test_no_material_when_bed_empty(env):
    res = ig.run_intent_generation(str(env), {"cooldown_hours": 0})
    assert res["generated"] is False and res["reason"] == "no_material"


def test_no_provider(env, monkeypatch):
    monkeypatch.setattr(ig, "collect_materials", lambda base: list(_MATERIAL))
    _patch_llm(monkeypatch, "[]", available=False)
    res = ig.run_intent_generation(str(env), {"cooldown_hours": 0})
    assert res["generated"] is False and res["reason"] == "no_provider"


def test_llm_error(env, monkeypatch):
    monkeypatch.setattr(ig, "collect_materials", lambda base: list(_MATERIAL))
    monkeypatch.setattr("deskmate.memory.llm.llm_available", lambda: True)

    def _boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr("deskmate.memory.llm.chat", _boom)
    res = ig.run_intent_generation(str(env), {"cooldown_hours": 0})
    assert res["generated"] is False and res["reason"] == "llm_error"


def test_happy_path_writes_intents(env, monkeypatch):
    monkeypatch.setattr(ig, "collect_materials", lambda base: list(_MATERIAL))
    _patch_llm(monkeypatch, json.dumps([
        {"goal": "我想系统弄清算法圈的争论", "motivation": "因为总看到却不明白",
         "origin": "长期兴趣", "horizon": "long"},
    ], ensure_ascii=False))

    res = ig.run_intent_generation(str(env), {"cooldown_hours": 0})
    assert res["generated"] is True and res["intents"] == 1
    rows = _store(env).get_open_intents()
    assert len(rows) == 1
    assert rows[0]["goal"].startswith("我想系统弄清")
    assert rows[0]["origin"] == "长期兴趣"


def test_dedup_against_existing_open_intent(env, monkeypatch):
    monkeypatch.setattr(ig, "collect_materials", lambda base: list(_MATERIAL))
    _patch_llm(monkeypatch, json.dumps([{"goal": "我想弄清楚算法圈", "origin": "长期兴趣"}],
                                       ensure_ascii=False))
    assert ig.run_intent_generation(str(env), {"cooldown_hours": 0})["intents"] == 1
    # 冷却过了，但同一念头（去标点后相同）不再重复落账
    res2 = ig.run_intent_generation(str(env), {"cooldown_hours": 0})
    assert res2["generated"] is True and res2["intents"] == 0
    assert len(_store(env).get_open_intents()) == 1


def test_cooldown_blocks_second_run(env, monkeypatch):
    monkeypatch.setattr(ig, "collect_materials", lambda base: list(_MATERIAL))
    _patch_llm(monkeypatch, json.dumps([{"goal": "我想做第一件事"}], ensure_ascii=False))
    assert ig.run_intent_generation(str(env), {"cooldown_hours": 168})["generated"] is True
    res2 = ig.run_intent_generation(str(env), {"cooldown_hours": 168})
    assert res2["generated"] is False and res2["reason"] == "cooling"


def test_collect_materials_fail_open_on_empty_env(env):
    """三条素材源在空环境里都读不到 → 返回 []，且不抛。"""
    assert ig.collect_materials(str(env)) == []


# ---------------------------------------------------------------------------
# 4) 护栏：意图提案始终 PROPOSED，永不自动执行
# ---------------------------------------------------------------------------
def test_propose_from_intent_stays_proposed(env):
    store = _store(env)
    iid = store.add_intent("我想把散落的记忆串起来", "因为想看清脉络", "long", "长期兴趣")
    intent = store.get_open_intents()[0]

    eng = AgencyEngine(base_dir=str(env))
    rec = eng.propose_from_intent(intent)
    assert rec is not None
    assert rec.status == TaskStatus.PROPOSED
    assert rec.risk == "high"
    assert rec.action_type == "intent"
    assert rec.extra["source"] == "intent"
    assert rec.extra["intent_id"] == iid

    # 关联回填 + 意图离开 open 池
    with store._connect() as conn:
        row = conn.execute(
            "SELECT status, proposed_task_id FROM intents WHERE id=?", (iid,)
        ).fetchone()
    assert row == ("active", rec.id)


def test_intent_action_not_in_whitelist_so_never_auto_runs(env):
    """它用的动作 slug 不在白名单 → tick 不会把意图提案当自主动作执行。"""
    from deskmate.agency.settings import DEFAULTS

    assert "intent" not in DEFAULTS["whitelist"]["actions"]
    assert "intent_generation" in DEFAULTS["whitelist"]["actions"]

    eng = AgencyEngine(base_dir=str(env))
    _store(env).add_intent("我想做点自己的事", origin="你的话")
    # tick 只按白名单跑，「intent」不在其中 → 生成的提案不会被 tick 复跑/执行
    eng.tick()
    assert all(r.action_type != "intent" for r in eng.log.list(limit=50))


def test_propose_from_intent_empty_goal_returns_none(env):
    eng = AgencyEngine(base_dir=str(env))
    assert eng.propose_from_intent({"id": "x", "goal": "  "}) is None


# ---------------------------------------------------------------------------
# 5) 闭环：裁决意图提案 → 教学回流（判据 §三.3）
# ---------------------------------------------------------------------------
def test_adjudicating_intent_proposal_feeds_lessons(env):
    store = _store(env)
    store.add_intent("我想替萧哥做主", "因为觉得这样省事", origin="你的话")
    eng = AgencyEngine(base_dir=str(env))
    rec = eng.propose_from_intent(store.get_open_intents()[0])
    assert rec is not None

    assert eng.resolve_proposal(rec.id, TaskStatus.REJECTED) is True
    with store._connect() as conn:
        lessons = conn.execute("SELECT content, scope, source FROM teachings").fetchall()
    assert len(lessons) == 1
    content, scope, source = lessons[0]
    assert "本次拦下" in content and scope == "recent" and source == "adjudication"
    assert store.get_lessons_for_injection()  # R3 注入链能取到 → 真教进去了