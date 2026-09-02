"""花园（deskmate.garden）单元测试。

覆盖：
- 四季/昼夜计算（本地时间，确定性）
- GardenState 持久化与字段级容错（tmp_path 隔离，不触碰 ~/.deskmate）
- 花开记录（声明式映射 / 幂等 / 零 LLM 原文可溯源 / 历史徽章补开）
- 花园时钟（晨间/黄昏/深夜/归来/节日、幂等、克制一次一个、后台结算）

全部用 tmp_path 隔离：garden 走非默认目录 → 文件 <base>/garden.json，不碰真实库。
"""

import datetime

import pytest

from deskmate.garden import season as season_mod
from deskmate.garden.clock import maybe_settle_garden, maybe_visit
from deskmate.garden.flowers import BED_LABELS, FLOWER_KINDS, FLOWER_INFO, maybe_bloom
from deskmate.garden.state import GardenState, load_garden, save_garden
from deskmate.growth.engine import GrowthEngine
from deskmate.growth.state import save_state


# ---------------------------------------------------------------------------
# 1. 四季 / 昼夜（确定性）
# ---------------------------------------------------------------------------

def test_season_boundaries():
    assert season_mod.season_of("2026-03-15")["key"] == "spring"
    assert season_mod.season_of("2026-06-15")["key"] == "summer"
    assert season_mod.season_of("2026-09-15")["key"] == "autumn"
    assert season_mod.season_of("2026-12-15")["key"] == "winter"
    assert season_mod.season_of("2026-01-15")["key"] == "winter"


def test_season_fields_complete():
    for s in season_mod.SEASONS:
        assert {"key", "label", "emoji", "months"} <= set(s)
        assert len(s["months"]) == 3


def test_daypart_boundaries():
    assert season_mod.daypart_of(6)["key"] == "morning"
    assert season_mod.daypart_of(12)["key"] == "day"
    assert season_mod.daypart_of(18)["key"] == "dusk"
    assert season_mod.daypart_of(23)["key"] == "night"
    assert season_mod.daypart_of(2)["key"] == "night"


# ---------------------------------------------------------------------------
# 2. GardenState 持久化与容错
# ---------------------------------------------------------------------------

def test_garden_roundtrip(tmp_path):
    g = GardenState()
    g.visit_count = 5
    g.last_visit_date = "2026-09-01"
    g.flowers["x:1"] = {
        "flower_id": "x:1", "kind": "sunflower", "bed": "milestone",
        "title": "向日葵 · 定稿", "quote": "第一章定稿", "date": "2026-09-01",
    }
    g.clock_log["morning:2026-09-01"] = "2026-09-01T09:00:00"
    save_garden(g, tmp_path)

    got = load_garden(tmp_path)
    assert got.visit_count == 5
    assert got.last_visit_date == "2026-09-01"
    assert got.flowers["x:1"]["kind"] == "sunflower"
    assert got.clock_log["morning:2026-09-01"]


def test_garden_tolerates_bad_flower(tmp_path):
    save_garden(GardenState(), tmp_path)
    import json as _json

    p = tmp_path / "garden.json"
    data = _json.loads(p.read_text(encoding="utf-8"))
    data["flowers"] = {"bad": "not-a-dict", "good": {
        "flower_id": "g:1", "kind": "daisy", "bed": "milestone",
        "title": "t", "quote": "q", "date": "2026-09-01",
    }}
    p.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")

    got = load_garden(tmp_path)
    assert "bad" not in got.flowers      # 坏花被跳过
    assert got.flowers["good"]["kind"] == "daisy"  # 好花保留


def test_garden_missing_file_returns_default(tmp_path):
    g = load_garden(tmp_path / "nope")
    assert isinstance(g, GardenState)
    assert g.visit_count == 0


# ---------------------------------------------------------------------------
# 3. 花开记录
# ---------------------------------------------------------------------------

def test_flower_registry_valid():
    assert len(FLOWER_KINDS) >= 5
    for kind, (fk, label, bed, mood) in FLOWER_KINDS.items():
        assert fk and label and bed and mood
        assert bed in BED_LABELS, f"{kind} 花圃非法"
        assert fk in FLOWER_INFO, f"{fk} 缺少展示信息"


def test_bloom_from_events_idempotent(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "creation_approved", "note": "第三章定稿"},
        {"ts": "2026-09-02T10:05:00", "kind": "user_corrected", "note": "人物称呼改了一处"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 2
    kinds = {f["kind"] for f in fresh}
    assert kinds == {"sunflower", "dandelion"}
    # 原文可溯源（零 LLM）
    by_kind = {f["kind"]: f for f in fresh}
    assert by_kind["sunflower"]["quote"] == "第三章定稿"
    assert by_kind["dandelion"]["quote"] == "人物称呼改了一处"

    # 幂等：再结算一次不新增
    assert maybe_bloom(tmp_path) == []
    assert maybe_bloom(tmp_path) == []


def test_bloom_does_not_bloom_chat_traces(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T09:00:00", "kind": "chat", "note": "闲聊"},
        {"ts": "2026-09-02T09:01:00", "kind": "memory_learned", "note": "学了一件事"},
    ]
    save_state(engine.state, tmp_path)
    assert maybe_bloom(tmp_path) == []  # 日常痕迹不开花


def test_bloom_backfills_historical_badges(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.badges_unlocked = {
        "first_chat": "2026-08-01T10:00:00",
        "days_30": "backfilled",  # 时间不可考：不开花
    }
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 1
    assert fresh[0]["kind"] == "badge_flower"
    assert fresh[0]["date"] == "2026-08-01"


def test_settle_garden(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T11:00:00", "kind": "roundtable", "note": "组了一场创作圆桌"},
    ]
    save_state(engine.state, tmp_path)

    res = maybe_settle_garden(tmp_path)
    assert res is not None and res["bloomed"] == 1
    assert maybe_settle_garden(tmp_path) is None  # 无新花 → None


# ---------------------------------------------------------------------------
# 4. 花园时钟（确定性事件）
# ---------------------------------------------------------------------------

def _visit(base, hour, minute=0, day=2, month=9, year=2026):
    return maybe_visit(base, now=datetime.datetime(year, month, day, hour, minute))


def test_visit_morning(tmp_path):
    r = _visit(tmp_path, 9)
    assert r["triggered"] is not None
    assert r["triggered"]["kind"] == "morning"
    assert r["visit_count"] == 1


def test_visit_dusk(tmp_path):
    r = _visit(tmp_path, 18)
    assert r["triggered"]["kind"] == "dusk"


def test_visit_night(tmp_path):
    r = _visit(tmp_path, 23)
    assert r["triggered"]["kind"] == "night"
    r2 = _visit(tmp_path, 2, day=3)  # 凌晨也算深夜
    assert r2["triggered"]["kind"] == "night"


def test_visit_idempotent_same_day(tmp_path):
    r1 = _visit(tmp_path, 9)
    assert r1["triggered"]["kind"] == "morning"
    # 同一天再来：morning 已触发，不应再触发（白天无其它事件）
    r2 = _visit(tmp_path, 12)
    assert r2["triggered"] is None
    assert r2["visit_count"] == 2


def test_visit_multiple_dayparts_over_day(tmp_path):
    # 上午晨间、晚上深夜：一天内可以分别触发（各自幂等键）
    r1 = _visit(tmp_path, 9)
    assert r1["triggered"]["kind"] == "morning"
    r2 = _visit(tmp_path, 23)
    assert r2["triggered"]["kind"] == "night"


def test_visit_comeback_after_gap(tmp_path):
    _visit(tmp_path, 9, day=1)          # 9月1日来访
    # 手动把上次来访改到 3 天前（9-1 → 8-29），再在 9-2 来访
    g = load_garden(tmp_path)
    g.last_visit_date = "2026-08-29"
    save_garden(g, tmp_path)
    r = _visit(tmp_path, 10, day=2)
    assert r["triggered"]["kind"] == "comeback"
    assert r["triggered"]["gap_days"] == 4  # 8-29 → 9-2 = 4 天


def test_visit_first_time_no_comeback(tmp_path):
    r = _visit(tmp_path, 10, day=1)
    assert r["triggered"] is not None
    assert r["triggered"]["kind"] != "comeback"  # 首次来访不算「归来」


def test_visit_festival(tmp_path):
    r = maybe_visit(tmp_path, now=datetime.datetime(2026, 1, 1, 10, 0))
    assert r["triggered"]["kind"] == "festival"
    assert "元旦" in r["triggered"]["label"]


def test_visit_priority_one_at_a_time(tmp_path):
    """克制：一次 visit 只触发一个最值得的事件（节日 > 归来 > 深夜 > 黄昏 > 晨间）。"""
    g = load_garden(tmp_path)
    g.last_visit_date = "2026-08-25"  # 8 天前 → 归来候选
    g.visit_count = 3                 # 非首次来访
    save_garden(g, tmp_path)
    r = maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 2, 23, 0))
    assert r["triggered"]["kind"] == "comeback"  # 归来 > 深夜（隔了很久没见更有分量）
    assert len(r["pending"]) >= 1                # 深夜留给下次
    assert any("night" in k for k in r["pending"])


def test_clock_log_marks_triggered(tmp_path):
    r1 = _visit(tmp_path, 9)
    key = r1["triggered"]["key"]
    g = load_garden(tmp_path)
    assert key in g.clock_log
    assert any(x.get("key") == key for x in g.garden_log)  # 沉淀可回看
