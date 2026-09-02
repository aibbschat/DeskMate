"""P2 花园测试：成长树 / 每日浇水 / 点亮 / 年花 / 年鉴归档。

隔离要点（复刻 P0/P1）：BlobStore 硬编码 ~/.deskmate/deskmate.db 不读 DESKMATE_DIR，
所有用例必须传非默认目录 tmp_path 走文件模式 garden.json，避免读到真实库。
"""

from __future__ import annotations

import datetime

import pytest

from deskmate.garden.clock import maybe_visit
from deskmate.garden.flowers import (
    garden_snapshot,
    lit_flower,
    maybe_archive,
    tree_stage,
    water_today,
    year_flower_ensure,
    year_stats,
)
from deskmate.garden.state import load_garden, save_garden


def _flower(fid: str, year: str, kind: str = "sprout") -> dict:
    return {
        "flower_id": fid,
        "kind": kind,
        "bed": "milestone",
        "title": f"{kind} · {fid}",
        "quote": "",
        "date": f"{year}-01-01",
        "source_ts": f"{year}-01-01T10:00:00",
        "mood_tint": "",
        "echo": "",
    }


def _garden_with_flowers(tmp_path, years: list[str]) -> None:
    from deskmate.garden.state import GardenState

    st = GardenState()
    for i, y in enumerate(years):
        fid = f"f{i:03d}"
        st.flowers[fid] = _flower(fid, y)
    save_garden(st, tmp_path)


# ---------------------------------------------------------------------------
# 成长树（纯函数派生）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count,expected_key,expected_title",
    [
        (0, "seed", "一粒种子"),
        (9, "seed", "一粒种子"),
        (10, "sprout", "一株小苗"),
        (29, "sprout", "一株小苗"),
        (30, "sapling", "一棵花树"),
        (59, "sapling", "一棵花树"),
        (60, "bloom", "一树繁花"),
        (99, "bloom", "一树繁花"),
        (100, "canopy", "亭亭如盖"),
        (200, "canopy", "亭亭如盖"),
    ],
)
def test_tree_stage_thresholds(count, expected_key, expected_title):
    t = tree_stage(count)
    assert t["key"] == expected_key
    assert t["title"] == expected_title
    assert t["count"] == count


def test_tree_stage_next_hint():
    t = tree_stage(5)
    assert t["next"]["threshold"] == 10
    t2 = tree_stage(300)
    assert t2["next"] is None


# ---------------------------------------------------------------------------
# 每日浇水（幂等 + 连续天数）
# ---------------------------------------------------------------------------


def test_water_today_first_and_idempotent(tmp_path):
    day = datetime.date(2026, 9, 2)
    r1 = water_today(tmp_path, now=datetime.datetime(2026, 9, 2, 10, 0))
    assert r1["watered"] is True
    assert r1["streak"] == 1
    assert r1["today"] == "2026-09-02"
    # 同一天再浇：不再新增，连续仍 1
    r2 = water_today(tmp_path, now=datetime.datetime(2026, 9, 2, 23, 0))
    assert r2["watered"] is False
    assert r2["streak"] == 1


def test_water_streak_counts_consecutive_days(tmp_path):
    water_today(tmp_path, now=datetime.datetime(2026, 9, 1, 9, 0))
    water_today(tmp_path, now=datetime.datetime(2026, 9, 2, 9, 0))
    water_today(tmp_path, now=datetime.datetime(2026, 9, 3, 9, 0))
    r = water_today(tmp_path, now=datetime.datetime(2026, 9, 3, 20, 0))
    assert r["watered"] is False
    assert r["streak"] == 3
    # 中断一天后：连续从当天重新数
    water_today(tmp_path, now=datetime.datetime(2026, 9, 5, 9, 0))
    r2 = water_today(tmp_path, now=datetime.datetime(2026, 9, 5, 21, 0))
    assert r2["streak"] == 1


def test_water_log_persisted(tmp_path):
    water_today(tmp_path, now=datetime.datetime(2026, 9, 2, 8, 0))
    st = load_garden(tmp_path)
    assert st.water_log.get("2026-09-02") is not None
    assert any(x.get("type") == "water" for x in st.garden_log)


# ---------------------------------------------------------------------------
# 点亮（幂等 + 忽略不存在）
# ---------------------------------------------------------------------------


def test_lit_flower_idempotent_and_ignores_missing(tmp_path):
    _garden_with_flowers(tmp_path, ["2026", "2026", "2025"])
    st = load_garden(tmp_path)
    fid = next(iter(st.flowers))
    r1 = lit_flower(st, fid)
    assert r1["lit"] is True
    assert r1["total"] == 1
    r2 = lit_flower(st, fid)
    assert r2["lit"] is False
    assert r2["total"] == 1
    save_garden(st, tmp_path)
    # 不存在 id：忽略
    st2 = load_garden(tmp_path)
    r3 = lit_flower(st2, "nope")
    assert r3["lit"] is False
    assert r3["total"] == 1


def test_lit_persisted_and_logged(tmp_path):
    _garden_with_flowers(tmp_path, ["2026"])
    st = load_garden(tmp_path)
    fid = next(iter(st.flowers))
    lit_flower(st, fid)
    save_garden(st, tmp_path)
    st2 = load_garden(tmp_path)
    assert fid in st2.lit_flowers
    assert any(x.get("type") == "lit" for x in st2.garden_log)


# ---------------------------------------------------------------------------
# 年花（跨年结算，幂等）
# ---------------------------------------------------------------------------


def test_year_flower_fresh_once(tmp_path):
    st = load_garden(tmp_path)
    r1 = year_flower_ensure(st, now=datetime.datetime(2026, 9, 2))
    assert r1["year"] == "2026"
    assert r1["fresh"] is True
    assert r1["first_visit_date"] == "2026-09-02"
    r2 = year_flower_ensure(st, now=datetime.datetime(2026, 12, 31))
    assert r2["fresh"] is False
    assert r2["first_visit_date"] == "2026-09-02"
    save_garden(st, tmp_path)
    st2 = load_garden(tmp_path)
    assert "2026" in st2.year_flowers
    assert any(x.get("type") == "year_flower" for x in st2.garden_log)


def test_year_flower_multi_year(tmp_path):
    st = load_garden(tmp_path)
    year_flower_ensure(st, now=datetime.datetime(2025, 1, 15))
    year_flower_ensure(st, now=datetime.datetime(2026, 2, 20))
    save_garden(st, tmp_path)
    st2 = load_garden(tmp_path)
    assert set(st2.year_flowers.keys()) == {"2025", "2026"}


# ---------------------------------------------------------------------------
# 年份统计（从真实 source_ts 派生）
# ---------------------------------------------------------------------------


def test_year_stats_from_source_ts(tmp_path):
    _garden_with_flowers(tmp_path, ["2025", "2026", "2026", "2026"])
    st = load_garden(tmp_path)
    ys = year_stats(st)
    assert ys == {"2025": 1, "2026": 3}


# ---------------------------------------------------------------------------
# 年鉴归档（超封顶移入，幂等）
# ---------------------------------------------------------------------------


def test_archive_moves_oldest_over_cap(tmp_path):
    # 造 205 朵（超过 FLOWER_CAP=200）
    years = ["2024"] * 30 + ["2025"] * 175
    _garden_with_flowers(tmp_path, years)
    st = load_garden(tmp_path)
    overflow = maybe_archive(st)
    save_garden(st, tmp_path)
    assert len(overflow) == 5
    assert len(st.flowers) <= 200
    st2 = load_garden(tmp_path)
    assert len(st2.archive) == 5
    assert all(x.get("source_ts", "").startswith("2024") for x in st2.archive)
    assert any(x.get("type") == "archive" for x in st2.garden_log)
    # 幂等：再跑不重复归档（已 <= cap）
    st3 = load_garden(tmp_path)
    assert maybe_archive(st3) == []


def test_archive_snapshot_years(tmp_path):
    years = ["2024"] * 30 + ["2025"] * 175
    _garden_with_flowers(tmp_path, years)
    st = load_garden(tmp_path)
    maybe_archive(st)
    save_garden(st, tmp_path)
    snap = garden_snapshot(load_garden(tmp_path))
    assert snap["archive"]["count"] == 5
    assert any(y["year"] == "2024" for y in snap["archive"]["years"])


# ---------------------------------------------------------------------------
# garden_snapshot 结构
# ---------------------------------------------------------------------------


def test_garden_snapshot_shape(tmp_path):
    _garden_with_flowers(tmp_path, ["2025", "2026"])
    st = load_garden(tmp_path)
    st.year_flowers = {"2026": {"year": "2026", "first_visit_date": "2026-09-02"}}
    st.lit_flowers = {next(iter(st.flowers)): "2026-09-02T10:00:00"}
    save_garden(st, tmp_path)
    snap = garden_snapshot(load_garden(tmp_path))
    assert snap["tree"]["key"] == "seed"
    assert snap["tree"]["count"] == 2
    assert snap["years"] == {"2025": 1, "2026": 1}
    assert snap["year_flowers"][0]["year"] == "2026"
    assert snap["lit"]["count"] == 1
    assert snap["archive"]["count"] == 0


# ---------------------------------------------------------------------------
# maybe_visit 集成：浇水 + 年花随 visit 自动结算（幂等）
# ---------------------------------------------------------------------------


def test_visit_includes_water_and_year_flower(tmp_path):
    r = maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 2, 10, 0))
    assert r["water"]["watered"] is True
    assert r["water"]["streak"] == 1
    assert r["year_flower"]["year"] == "2026"
    assert r["year_flower"]["fresh"] is True
    # 同一天再 visit：不重复浇水、年花不重建
    r2 = maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 2, 22, 0))
    assert r2["water"]["watered"] is False
    assert r2["year_flower"]["fresh"] is False
    assert r2["visit_count"] == 2


def test_visit_streak_accumulates(tmp_path):
    maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 1, 9, 0))
    maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 2, 9, 0))
    r = maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 2, 15, 0))
    assert r["water"]["streak"] == 2
