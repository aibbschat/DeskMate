"""花园（deskmate.garden）单元测试。

覆盖：
- 四季/昼夜计算（本地时间，确定性）
- GardenState 持久化与字段级容错（tmp_path 隔离，不触碰 ~/.deskmate）
- 花开记录（声明式映射 / 幂等 / 零 LLM 原文可溯源 / 历史徽章补开）
- 花园时钟（晨间/黄昏/深夜/归来/节日、幂等、克制一次一个、后台结算）

全部用 tmp_path 隔离：garden 走非默认目录 → 文件 <base>/garden.json，不碰真实库。
"""

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from deskmate.garden import season as season_mod
from deskmate.garden.clock import maybe_settle_garden, maybe_visit
from deskmate.garden import flowers as flowers_mod
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


def test_candidate_flowers_skips_mood_for_already_existing(tmp_path):
    """根因锁死：已开过的花在做情绪定色前就被丢弃，绝不为它重开数据库连接。

    2026-10-13 性能事故根因 = 只新增 1 朵却为全部 614 朵重算情绪（每朵新开 EmotionTimeline）。
    修复后 `_candidate_flowers` 拿到 existing_ids，已存在的直接跳过，不调 `_mood_for_event`。
    """
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "creation_approved", "note": "第三章定稿"},
        {"ts": "2026-09-02T10:05:00", "kind": "user_corrected", "note": "人物称呼改了一处"},
    ]
    save_state(engine.state, tmp_path)

    # 先以空 existing 拿到全部候选 fid
    all_candidates = flowers_mod._candidate_flowers(engine, set(), tmp_path)
    all_fids = {c.flower_id for c in all_candidates}
    assert all_fids, "应有候选花"

    # existing = 全部候选 → 不应触发任何情绪定色
    with patch.object(flowers_mod, "_mood_for_event", wraps=flowers_mod._mood_for_event) as spy:
        flowers_mod._candidate_flowers(engine, all_fids, tmp_path)
        assert spy.call_count == 0, f"已开过的花不应触发情绪定色，实际 {spy.call_count} 次"

    # existing = 空 → 应照常为每朵算情绪
    with patch.object(flowers_mod, "_mood_for_event", wraps=flowers_mod._mood_for_event) as spy:
        got = flowers_mod._candidate_flowers(engine, set(), tmp_path)
        assert spy.call_count == len(got) == len(all_candidates)


def test_new_event_gate_skips_when_no_new_events(tmp_path):
    """新事件闸门：自上次 bloom 起无新可开花事件时，整段跳过、返回 [] 且绝不重复开花。"""
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "creation_approved", "note": "定稿"},
    ]
    save_state(engine.state, tmp_path)

    assert len(maybe_bloom(tmp_path)) == 1
    g = load_garden(tmp_path)
    assert g.last_bloom_watermark, "首开后应写入水印"
    count = len(g.flowers)

    # 无新事件再开园 → 闸门拦下
    assert maybe_bloom(tmp_path) == []
    assert maybe_bloom(tmp_path) == []
    g2 = load_garden(tmp_path)
    assert len(g2.flowers) == count, "闸门不应导致重复开花"


def test_new_event_gate_opens_on_new_event(tmp_path):
    """闸门在水印变化（确有新事件）时正常放行，只新增对应的新花。"""
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "creation_approved", "note": "定稿"},
    ]
    save_state(engine.state, tmp_path)
    assert len(maybe_bloom(tmp_path)) == 1

    engine.state.recent_events.append(
        {"ts": "2026-09-03T10:00:00", "kind": "skill_created", "note": "新技能", "skill_name": "X"}
    )
    save_state(engine.state, tmp_path)
    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 1, "新事件应触发一次新开花"
    assert fresh[0]["kind"] == "starflower"


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


# ---------------------------------------------------------------------------
# 回归：花的日期取「发生日」，花种源不被 50 条易失窗口截断
# ---------------------------------------------------------------------------

def test_flower_date_is_local_event_date_not_discovery_date():
    """花的 date 取事件发生的本地日期，不是「被发现的那天」。

    回归背景：曾恒用 ``date.today()``。事件的 ts 带 UTC 偏移，跨天时段开出的
    花会系统性标错一天，且与按 source_ts 统计的 year_stats 口径打架。
    """
    # UTC 23:30 在东八区已是次日 07:30 —— 发现日与发生日不在同一天
    ts_utc = "2026-09-02T23:30:00+00:00"
    local = datetime.datetime.fromisoformat(ts_utc).astimezone()
    assert local.date().isoformat() == "2026-09-03"

    from deskmate.garden.flowers import _local_date_from_ts

    assert _local_date_from_ts(ts_utc) == local.date().isoformat()
    # 无时区标记的时间串按本地时间解释，取前 10 位
    assert _local_date_from_ts("2026-09-02T10:00:00") == "2026-09-02"
    # 取不到时返回空串，由调用方兜底为今天
    assert _local_date_from_ts("") == ""


def test_bloom_uses_event_date(tmp_path):
    """补开的历史花，日期必须是事件发生日而非今天。"""
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-08-01T10:00:00", "kind": "creation_approved", "note": "旧稿定稿"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 1
    assert fresh[0]["date"] == "2026-08-01", "花的日期应是发生日，不是今天"
    assert fresh[0]["source_ts"] == "2026-08-01T10:00:00"


def test_bloom_reads_material_ledger_beyond_event_window(tmp_path):
    """滚出 recent_events 窗口的事件，靠日记素材账本也能补开成花。

    回归背景：花种源只读 recent_events（50 条易失窗口，落盘即截断），
    几天不进花园，之前的事就永远开不出花了。
    """
    # 账本写在 tmp 下，模拟 append-only 全量流水
    materials = tmp_path / "diary" / "materials"
    materials.mkdir(parents=True)
    (materials / "2026-08-01.jsonl").write_text(
        '{"ts": "2026-08-01T09:00:00", "kind": "creation_approved", "note": "账本里的旧稿定稿"}\n',
        encoding="utf-8",
    )

    # recent_events 里已没有这条事件（被窗口挤掉）
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-04T10:00:00", "kind": "chat", "note": "日常对话"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    kinds = {f["kind"]: f for f in fresh}
    assert "sunflower" in kinds, "账本里的历史事件应当补开成花"
    assert kinds["sunflower"]["date"] == "2026-08-01"
    assert kinds["sunflower"]["quote"] == "账本里的旧稿定稿"


def test_garden_snapshot_water_reflects_real_log(tmp_path):
    """浇水状态从 water_log 真实派生，不再硬编码 streak=0。

    回归背景：garden_snapshot 曾恒返回 {"streak": 0, "today": ""} 且缺 watered
    字段，前端拿它兜底时，今天明明浇过水也会显示「还没浇水 · 连续 0 天」。
    """
    from deskmate.garden.flowers import garden_snapshot

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    g = load_garden(tmp_path)
    g.water_log = {
        yesterday.isoformat(): "2026-09-04T08:00:00",
        today.isoformat(): "2026-09-05T08:00:00",
    }
    snap = garden_snapshot(g)
    assert snap["water"]["watered"] is True
    assert snap["water"]["streak"] == 2
    assert snap["water"]["today"] == today.isoformat()

    # 今天没浇 → watered 为 False，但昨天的连续记录仍在
    g2 = load_garden(tmp_path)
    g2.water_log = {yesterday.isoformat(): "2026-09-04T08:00:00"}
    snap2 = garden_snapshot(g2)
    assert snap2["water"]["watered"] is False
    assert snap2["water"]["today"] == ""


# ---------------------------------------------------------------------------
# 回归：花的日期取「发生日」，花种源不被 50 条易失窗口截断
# ---------------------------------------------------------------------------

def test_flower_date_is_local_event_date_not_discovery_date():
    """花的 date 取事件发生的本地日期，不是「被发现的那天」。

    回归背景：曾恒用 ``date.today()``。事件的 ts 带 UTC 偏移，跨天时段开出的
    花会系统性标错一天，且与按 source_ts 统计的 year_stats 口径打架。
    """
    # UTC 23:30 在东八区已是次日 07:30 —— 发现日与发生日不在同一天
    ts_utc = "2026-09-02T23:30:00+00:00"
    local = datetime.datetime.fromisoformat(ts_utc).astimezone()
    assert local.date().isoformat() == "2026-09-03"

    from deskmate.garden.flowers import _local_date_from_ts

    assert _local_date_from_ts(ts_utc) == local.date().isoformat()
    # 无时区标记的时间串按本地时间解释，取前 10 位
    assert _local_date_from_ts("2026-09-02T10:00:00") == "2026-09-02"
    # 取不到时返回空串，由调用方兜底为今天
    assert _local_date_from_ts("") == ""


def test_bloom_uses_event_date(tmp_path):
    """补开的历史花，日期必须是事件发生日而非今天。"""
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-08-01T10:00:00", "kind": "creation_approved", "note": "旧稿定稿"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 1
    assert fresh[0]["date"] == "2026-08-01", "花的日期应是发生日，不是今天"
    assert fresh[0]["source_ts"] == "2026-08-01T10:00:00"


def test_bloom_reads_material_ledger_beyond_event_window(tmp_path):
    """滚出 recent_events 窗口的事件，靠日记素材账本也能补开成花。

    回归背景：花种源只读 recent_events（50 条易失窗口，落盘即截断），
    几天不进花园，之前的事就永远开不出花了。
    """
    # 账本写在 tmp 下，模拟 append-only 全量流水
    materials = tmp_path / "diary" / "materials"
    materials.mkdir(parents=True)
    (materials / "2026-08-01.jsonl").write_text(
        '{"ts": "2026-08-01T09:00:00", "kind": "creation_approved", "note": "账本里的旧稿定稿"}\n',
        encoding="utf-8",
    )

    # recent_events 里已没有这条事件（被窗口挤掉）
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-04T10:00:00", "kind": "chat", "note": "日常对话"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    kinds = {f["kind"]: f for f in fresh}
    assert "sunflower" in kinds, "账本里的历史事件应当补开成花"
    assert kinds["sunflower"]["date"] == "2026-08-01"
    assert kinds["sunflower"]["quote"] == "账本里的旧稿定稿"


def test_garden_snapshot_water_reflects_real_log(tmp_path):
    """浇水状态从 water_log 真实派生，不再硬编码 streak=0。

    回归背景：garden_snapshot 曾恒返回 {"streak": 0, "today": ""} 且缺 watered
    字段，前端拿它兜底时，今天明明浇过水也会显示「还没浇水 · 连续 0 天」。
    """
    from deskmate.garden.flowers import garden_snapshot

    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    g = load_garden(tmp_path)
    g.water_log = {
        yesterday.isoformat(): "2026-09-04T08:00:00",
        today.isoformat(): "2026-09-05T08:00:00",
    }
    snap = garden_snapshot(g)
    assert snap["water"]["watered"] is True
    assert snap["water"]["streak"] == 2
    assert snap["water"]["today"] == today.isoformat()

    # 今天没浇 → watered 为 False，但昨天的连续记录仍在
    g2 = load_garden(tmp_path)
    g2.water_log = {yesterday.isoformat(): "2026-09-04T08:00:00"}
    snap2 = garden_snapshot(g2)
    assert snap2["water"]["watered"] is False
    assert snap2["water"]["today"] == ""


# ---------------------------------------------------------------------------
# 回归（C2）：情感时间线 → 花园的接线
# ---------------------------------------------------------------------------

def _seed_emotion_timeline(base: Path, day: str, state: str, mood: float = 80.0) -> None:
    """往情感时间线写一天快照（走 BlobStore，与生产同一条路径）。"""
    from deskmate.growth.emotion_timeline import EmotionTimeline
    from deskmate.persistence import BlobStore

    tl = EmotionTimeline(base_dir=base)
    tl._data[day] = {"date": day, "state": state, "mood": mood, "note": "测试用"}
    BlobStore(db_path=base / "deskmate.db").set(BlobStore.KEY_EMOTION, tl._data)


def test_flower_tint_follows_real_emotion_not_event_type(tmp_path):
    """花的颜色跟当天真实心情走，不是跟事件类型走。

    回归背景：mood_tint 只从花种注册表按事件 kind 取默认值，skill_evolved
    恒为「顺利」——赶稿压力最大的那几天开出的花也一片「顺利」，与
    EmotionTimeline 记下的真实状态对不上（实测 119 朵里 108 朵不符）。
    """
    # 9-02 真实情感是「压力大」，但事件类型是 skill_evolved（默认色「顺利」）
    _seed_emotion_timeline(tmp_path, "2026-09-02", "压力大")

    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "skill_evolved", "note": "技能进化"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 1
    assert fresh[0]["date"] == "2026-09-02"
    assert fresh[0]["mood_tint"] == "压力", "应取当日真实情感，而非事件类型的默认色"


def test_flower_tint_falls_back_when_no_snapshot(tmp_path):
    """那天没有情感快照时回落到事件类型查表，不把花染成空白。"""
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "skill_evolved", "note": "技能进化"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert fresh[0]["mood_tint"], "无情感数据时仍应有兜底色"


def test_retint_flowers_fixes_existing_but_keeps_unknown_days(tmp_path):
    """重算只改「情感时间线里有当天快照」的花，查不到的保持原样。"""
    from deskmate.garden.flowers import retint_flowers

    _seed_emotion_timeline(tmp_path, "2026-09-02", "压力大")

    g = load_garden(tmp_path)
    g.flowers = {
        "f1": {"date": "2026-09-02", "kind": "sprout", "mood_tint": "顺利"},  # 应被改
        "f2": {"date": "2026-07-01", "kind": "sprout", "mood_tint": "顺利"},  # 无快照，不动
    }
    changed = retint_flowers(g, tmp_path)
    assert changed == 1
    assert g.flowers["f1"]["mood_tint"] == "压力"
    assert g.flowers["f2"]["mood_tint"] == "顺利", "无据可依的花不能被清空或误改"


def test_retint_flowers_is_idempotent(tmp_path):
    """重算跑第二次不应再改动任何花。"""
    from deskmate.garden.flowers import retint_flowers

    _seed_emotion_timeline(tmp_path, "2026-09-02", "低落")
    g = load_garden(tmp_path)
    g.flowers = {"f1": {"date": "2026-09-02", "kind": "sprout", "mood_tint": "顺利"}}

    assert retint_flowers(g, tmp_path) == 1
    assert retint_flowers(g, tmp_path) == 0, "第二次应无改动"


def test_maybe_bloom_retints_stale_flowers(tmp_path):
    """进花园时顺带把存量花的错色改回来（用户无感，不必手动迁移）。"""
    _seed_emotion_timeline(tmp_path, "2026-09-02", "压力大")

    g = load_garden(tmp_path)
    g.flowers = {"sprout:old": {"date": "2026-09-02", "kind": "sprout", "mood_tint": "顺利"}}
    save_garden(g, tmp_path)

    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = []
    save_state(engine.state, tmp_path)

    maybe_bloom(tmp_path)
    after = load_garden(tmp_path)
    assert after.flowers["sprout:old"]["mood_tint"] == "压力", "存量花应被重新定色"


def test_timeline_mood_covers_all_emotion_states():
    """情感时间线的每种状态都要有对应的花色，缺一个就静默失效。"""
    from deskmate.garden.flowers import _TIMELINE_MOOD
    from deskmate.growth.emotion_timeline import _infer_emotion

    # 构造能触发全部状态分支的输入（2026-09-05 E2 后：不再吃 mood 参数，
    # 「低落」改由「不顺 + 当天几乎没动静」判定，不再依赖那个恒为 80 的 mood）
    cases = [
        ({"user_corrected": 2}, "低落"),                          # 不顺 + 没动静
        ({"user_corrected": 3, "writing": 2}, "压力大"),          # 不顺占优且还在忙
        ({"creation_approved": 1}, "顺利"),                       # 有正反馈
        ({"writing": 2}, "忙碌"),                                 # 创作量不小
        ({}, "平静"),                                             # 什么都没有
    ]
    for events, expected in cases:
        actual, _ = _infer_emotion(events)
        assert actual == expected, f"输入 {events} 应得 {expected}，实得 {actual}"
        assert actual in _TIMELINE_MOOD, f"情感状态「{actual}」没有对应的花色映射"
        assert _TIMELINE_MOOD[actual], "映射值不能为空串"
