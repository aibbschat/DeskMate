"""花园 P1 单元测试。

覆盖：
- 节日事件：农历节日（春节/中秋）逐年公历映射、固定阳历节日、普通日不触发；
  中秋日 maybe_visit 触发 festival（专属氛围的事件载体）
- 花园图鉴：flora_snapshot 全花种定义 + 解锁计数 + 花语非空
- 花园回响：花开时生成 echo（零 LLM 模板，非空、可溯源字段）
- 羁绊故事簿：bond 阶段叙事模板齐全；build_storybook 结构正确（注入 portrait stub）

全部 tmp_path 文件模式隔离：garden/growth 走非默认目录 → 文件，不触碰 ~/.deskmate。
"""

import datetime

from deskmate.garden.clock import _festival_name, maybe_visit
from deskmate.garden.flowers import flora_snapshot, maybe_bloom
from deskmate.garden.state import load_garden
from deskmate.garden.storybook import (
    bond_stage_narrative,
    bond_stage_title,
    build_storybook,
)
from deskmate.garden.state import GardenState
from deskmate.growth.engine import GrowthEngine
from deskmate.growth.state import save_state


# ---------------------------------------------------------------------------
# 1. 节日事件（P1-1）
# ---------------------------------------------------------------------------

def test_festival_lunar_spring():
    assert _festival_name(datetime.datetime(2027, 2, 6, 9, 0)) == "春节"
    assert _festival_name(datetime.datetime(2028, 1, 26, 9, 0)) == "春节"
    assert _festival_name(datetime.datetime(2029, 2, 13, 9, 0)) == "春节"
    assert _festival_name(datetime.datetime(2030, 2, 3, 9, 0)) == "春节"
    assert _festival_name(datetime.datetime(2031, 1, 23, 9, 0)) == "春节"


def test_festival_lunar_midautumn():
    assert _festival_name(datetime.datetime(2026, 9, 25, 9, 0)) == "中秋"
    assert _festival_name(datetime.datetime(2027, 9, 15, 9, 0)) == "中秋"
    assert _festival_name(datetime.datetime(2028, 10, 3, 9, 0)) == "中秋"
    assert _festival_name(datetime.datetime(2029, 9, 22, 9, 0)) == "中秋"
    assert _festival_name(datetime.datetime(2030, 9, 12, 9, 0)) == "中秋"
    # 2031-10-01 国庆中秋同日 → 合并显示
    name = _festival_name(datetime.datetime(2031, 10, 1, 9, 0))
    assert "中秋" in name and "国庆" in name


def test_festival_solar_fixed():
    assert _festival_name(datetime.datetime(2026, 1, 1, 9, 0)) == "元旦"
    assert _festival_name(datetime.datetime(2026, 12, 25, 9, 0)) == "圣诞节"


def test_festival_plain_day_none():
    assert _festival_name(datetime.datetime(2026, 9, 2, 9, 0)) is None


def test_visit_midautumn_triggers_festival(tmp_path):
    """中秋当天进园 → 触发节日事件（专属氛围载体）。"""
    r = maybe_visit(tmp_path, now=datetime.datetime(2026, 9, 25, 10, 0))
    assert r["triggered"]["kind"] == "festival"
    assert "中秋" in r["triggered"]["label"]


def test_visit_spring_festival_triggers(tmp_path):
    r = maybe_visit(tmp_path, now=datetime.datetime(2027, 2, 6, 10, 0))
    assert r["triggered"]["kind"] == "festival"
    assert "春节" in r["triggered"]["label"]


# ---------------------------------------------------------------------------
# 2. 花园图鉴（P1-3）
# ---------------------------------------------------------------------------

def test_flora_snapshot_all_kinds_and_unlocked():
    g = GardenState()
    g.flowers["a"] = {"kind": "sprout", "bed": "milestone"}
    g.flowers["b"] = {"kind": "sprout", "bed": "milestone"}
    g.flowers["c"] = {"kind": "badge_flower", "bed": "badge"}
    flora = flora_snapshot(g)
    assert len(flora) == 9  # FLOWER_INFO 全花种
    by = {f["kind"]: f for f in flora}
    assert by["sprout"]["unlocked"] == 2
    assert by["badge_flower"]["unlocked"] == 1
    assert by["iris"]["unlocked"] == 0
    # 字段完整 + 花语非空（零 LLM 静态文案）
    for f in flora:
        assert f["label"] and f["emoji"] and f["desc"]


# ---------------------------------------------------------------------------
# 3. 花园回响（P1-4）
# ---------------------------------------------------------------------------

def test_echo_generated_on_bloom(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T10:00:00", "kind": "creation_approved", "note": "第三章定稿"},
        {"ts": "2026-09-02T10:05:00", "kind": "user_corrected", "note": "人物称呼改了一处"},
    ]
    save_state(engine.state, tmp_path)

    fresh = maybe_bloom(tmp_path)
    assert len(fresh) == 2
    g = load_garden(tmp_path)
    by_kind = {f["kind"]: f for f in fresh}
    assert by_kind["sunflower"]["echo"]  # 向日葵有回响
    assert by_kind["dandelion"]["echo"]
    # 回响来自模板（零 LLM），且随真实事件沉淀
    assert "拍板" in by_kind["sunflower"]["echo"]
    assert "蒲公英" in by_kind["dandelion"]["echo"]
    # 持久化后仍可读
    reloaded = load_garden(tmp_path)
    assert all(f.get("echo") for f in reloaded.flowers.values())


def test_echo_empty_when_no_flower(tmp_path):
    engine = GrowthEngine(base_dir=tmp_path)
    engine.state.recent_events = [
        {"ts": "2026-09-02T09:00:00", "kind": "chat", "note": "闲聊"},
    ]
    save_state(engine.state, tmp_path)
    assert maybe_bloom(tmp_path) == []  # 不开花就没有回响


# ---------------------------------------------------------------------------
# 4. 羁绊故事簿（P1-2）
# ---------------------------------------------------------------------------

def test_bond_narratives_complete():
    for stage in ["初识", "熟络", "默契", "知己", "灵魂伴侣"]:
        assert bond_stage_narrative(stage)
        assert bond_stage_title(stage)
    # 未知阶段回退初识，不炸
    assert bond_stage_narrative("不存在阶段")


def test_build_storybook_structure(tmp_path):
    def fake_portrait():
        return {"sections": [
            {"title": "写作风格", "status": "ready",
             "articles": [{"content": "平均 60 字，短句多"}]},
            {"title": "决策偏好", "status": "learning",
             "articles": [{"content": ""}, {"content": "保守 3 次"}]},
            {"title": "行为规律", "status": "empty", "articles": []},
        ]}

    sb = build_storybook(tmp_path, portrait_builder=fake_portrait)
    # bond：新库初识 + 叙事
    assert sb["bond"]["stage"] == "初识"
    assert sb["bond"]["title"] == "第一粒种子"
    assert sb["bond"]["narrative"]
    assert sb["bond"]["depth"] == 0.0
    # numbers：真实（空库 0 花）
    assert sb["numbers"]["flower_count"] == 0
    assert sb["numbers"]["total_chats"] == 0
    # portrait：聚合摘要（取每栏目首篇有内容文章）
    assert len(sb["portrait"]) == 3
    assert sb["portrait"][0]["title"] == "写作风格"
    assert sb["portrait"][0]["summary"] == "平均 60 字，短句多"
    assert sb["portrait"][1]["summary"] == "保守 3 次"  # 跳过空 content
    assert sb["portrait"][2]["summary"] == ""


def test_build_storybook_counts_flowers(tmp_path):
    g = GardenState()
    g.flowers["x"] = {"kind": "sprout", "bed": "milestone"}
    from deskmate.garden.state import save_garden
    save_garden(g, tmp_path)
    sb = build_storybook(tmp_path, portrait_builder=lambda: {"sections": []})
    assert sb["numbers"]["flower_count"] == 1


def test_build_storybook_fail_open_on_portrait(tmp_path):
    def boom():
        raise RuntimeError("portrait 挂了")

    sb = build_storybook(tmp_path, portrait_builder=boom)
    assert sb["portrait"] == []  # fail-open：画像失败不影响整体
    assert sb["bond"]["stage"]  # bond 仍可用
