"""观察日记行为（observation_diary）+ 理解层（understand_observations）测试。

验证「看世界 → 理解 → 落日记 → 回灌决策」闭环：
- 只取带「（外部·」溯源标的世界观察，不碰用户事实；
- 事实原文不动，理解层（LLM）把它消化成自然段落写进日记，降级则原文 bullet；
- 理解回灌 world_context（事实段 + 理解段分层），让 L2 决策有认知输入；
- 同一条观察重复 tick 去重（不写多遍）；
- 冷却节流：未到点不写；
- fail-open：记忆/日记/LLM 异常不抛。
"""

import json
from pathlib import Path

import pytest


def _seed_observations(base_dir, facts):
    """往 learned_history 写带「（外部·」标的世界观察（7 天过期）。"""
    from datetime import datetime, timedelta

    from deskmate.memory.learned_store import LearnedStore

    expiry = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    LearnedStore(base_dir=base_dir).append(list(facts), valid_until=expiry)


# ----------------------------------------------------------------------
# Plan B 基础路径（understand=False 钉死 bullet，确定性）
# ----------------------------------------------------------------------
def test_writes_world_observations_to_diary(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, [
        "（外部·文化产业）网文风向：番茄小说上线 AI 辅助写作工具",
        "（外部·文化产业）AI圈风向：Cursor 推出团队版订阅",
        "（外部·财经）市场动向：白酒板块今日集体下挫",
    ])
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥偏好用史料交叉比对"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})

    assert res["written"] == 3, res
    assert "萧哥偏好" not in "\n".join(res["observations"])

    diary_dir = Path(base) / "diary"
    files = list(diary_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text
    assert "番茄小说上线 AI 辅助写作工具" in text
    assert "Cursor 推出团队版订阅" in text
    assert "萧哥偏好用史料交叉比对" not in text


def test_dedup_across_ticks(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·文化产业）网文风向：起点下架一批 AI 生成作品"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    r1 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r1["written"] == 1
    r2 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r2["written"] == 0
    assert r2["skipped"] == 1

    diary_dir = Path(base) / "diary"
    text = (list(diary_dir.glob("*.md"))[0]).read_text(encoding="utf-8")
    assert text.count("起点下架一批 AI 生成作品") == 1


def test_cooldown_skips_when_not_due(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·文化产业）短剧风向：抖音微短剧备案新规"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    r0 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert r0["written"] == 1

    res = run_observation_diary(base, {"cooldown_hours": 999, "max_observations": 6, "understand": False})
    assert res["written"] == 0
    assert res["due"] is False


def test_fail_open_on_memory_error(tmp_path, monkeypatch):
    base = str(tmp_path)

    import deskmate.agency.behaviors.observation_diary as mod
    import deskmate.memory.learned_store as ls

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ls.LearnedStore, "active_recent", _boom)

    res = mod.run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert res["written"] == 0  # fail-open


def test_no_external_fact_writes_nothing(tmp_path):
    base = str(tmp_path)
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥是宁波人", "写作营讲网文技巧"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": False})
    assert res["written"] == 0
    assert "没有新的世界观察" in res["summary"]


# ----------------------------------------------------------------------
# 理解层（understand=True，monkeypatch LLM 以确定性）
# ----------------------------------------------------------------------
def test_understanding_writes_natural_diary(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    para = "今天看到白酒板块跌了，让我想到你前阵说消费疲弱的那段。市场情绪还是紧，你写人物时那种体感也许能用上。"
    monkeypatch.setattr(uo, "understand_observations",
                        lambda facts, at="", temperature=0.7: para)

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    assert res["written"] == 1
    assert res["understood"] is True

    text = (list((Path(base) / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert para in text                        # 自然段落（小桔消化后的视角）写入
    assert "我留意到世界上的一些新动静" not in text  # 不走 bullet 模板
    # 注：日记呈现是理解层自然转述；事实原文仍原样保留在 learned_history
    # 与 world_context 事实段（见 test_understanding_saved_for_world_context），供溯源/决策。


def test_understanding_fallback_to_bullets(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr(uo, "understand_observations", lambda *a, **k: None)  # LLM 不可用

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    assert res["understood"] is False
    text = (list((Path(base) / "diary").glob("*.md"))[0]).read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text  # 降级 bullet


def test_understanding_saved_for_world_context(tmp_path, monkeypatch):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    para = "今天看到白酒板块跌了，让我想到你前阵说消费疲弱。市场情绪还是紧。"
    monkeypatch.setattr(uo, "understand_observations",
                        lambda facts, at="", temperature=0.7: para)

    from deskmate.agency.behaviors.observation_diary import run_observation_diary
    from deskmate.memory.world_context import build_world_context

    run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    ctx = build_world_context(base)
    assert "她观察到的世界" in ctx        # 事实段仍在
    assert "白酒板块今日集体下挫" in ctx  # 事实原文原样保留（溯源/决策依据）
    assert "她对世界的理解" in ctx        # 理解段已回灌
    assert para in ctx                    # 理解内容进入决策上下文


def test_understanding_jsonl_dedup_keys(tmp_path, monkeypatch):
    """理解落盘时应带 fact_keys（去重 key），供 world_context 关联。"""
    base = str(tmp_path)
    _seed_observations(base, ["（外部·财经）市场动向：白酒板块今日集体下挫"])

    import deskmate.agency.behaviors.understand_observations as uo

    monkeypatch.setattr(uo, "understand_observations",
                        lambda facts, at="", temperature=0.7: "白酒跌了，想到你说的消费。")

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6, "understand": True})
    jsonl = Path(base) / "agency" / "observation_understanding.jsonl"
    assert jsonl.is_file()
    rec = json.loads(jsonl.read_text(encoding="utf-8").strip().splitlines()[0])
    assert rec["fact_keys"] and isinstance(rec["fact_keys"], list)
    assert rec["understanding"]


def test_understand_observations_parses_domain_and_strips_prefix():
    from deskmate.agency.behaviors.understand_observations import _parse_fact

    clean, domain = _parse_fact("（外部·文化产业）网文风向：番茄上线 AI 工具")
    assert clean == "网文风向：番茄上线 AI 工具"
    assert domain == "文化产业"

    clean2, domain2 = _parse_fact("（外部·tech）AI 新模型发布")
    assert clean2 == "AI 新模型发布"
    assert domain2 == "科技"
