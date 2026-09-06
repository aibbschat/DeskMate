"""观察日记行为（observation_diary）测试。

验证「看世界 → 落日记」的 Plan B 闭环：
- 只取带「（外部·」溯源标的世界观察，不碰用户事实；
- 写进小桔日记（确定性渲染，不进叙事润色）；
- 同一条观察重复 tick 去重（不写多遍）；
- 冷却节流：未到点不写；
- fail-open：记忆/日记异常不抛。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest


def _base():
    d = tempfile.mkdtemp()
    # learned_history 与 diary 都落在 base_dir（统一 deskmate.db + diary/ 子目录）
    return d


def _seed_observations(base_dir, facts):
    """往 learned_history 写带「（外部·」标的世界观察（7 天过期）。"""
    from datetime import datetime, timedelta

    from deskmate.memory.learned_store import LearnedStore

    expiry = (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds")
    LearnedStore(base_dir=base_dir).append(list(facts), valid_until=expiry)


def test_writes_world_observations_to_diary(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, [
        "（外部·文化产业）网文风向：番茄小说上线 AI 辅助写作工具",
        "（外部·文化产业）AI圈风向：Cursor 推出团队版订阅",
        "（外部·财经）市场动向：白酒板块今日集体下挫",
    ])
    # 放一条用户事实，确认不会被当成观察写进日记
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥偏好用史料交叉比对"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})

    assert res["written"] == 3, res
    assert "萧哥偏好" not in "\n".join(res["observations"])

    # 日记文件确有内容，且含「观察日记」标记与原始观察
    diary_dir = Path(base) / "diary"
    files = list(diary_dir.glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "我留意到世界上的一些新动静" in text
    assert "番茄小说上线 AI 辅助写作工具" in text
    assert "Cursor 推出团队版订阅" in text
    assert "萧哥偏好用史料交叉比对" not in text  # 用户事实不进观察日记


def test_dedup_across_ticks(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, [
        "（外部·文化产业）网文风向：起点下架一批 AI 生成作品",
    ])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    r1 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})
    assert r1["written"] == 1
    # 第二次：同一条已写过 → 不重复写（去重）
    r2 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})
    assert r2["written"] == 0
    assert r2["skipped"] == 1

    diary_dir = Path(base) / "diary"
    text = (list(diary_dir.glob("*.md"))[0]).read_text(encoding="utf-8")
    assert text.count("起点下架一批 AI 生成作品") == 1  # 只出现一遍


def test_cooldown_skips_when_not_due(tmp_path):
    base = str(tmp_path)
    _seed_observations(base, ["（外部·文化产业）短剧风向：抖音微短剧备案新规"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    # 先跑一次（cooldown=0）→ 写一笔，并落冷却时间戳
    r0 = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})
    assert r0["written"] == 1

    # 紧接着用很大的 cooldown_hours → 没到点 → 不写
    res = run_observation_diary(base, {"cooldown_hours": 999, "max_observations": 6})
    assert res["written"] == 0
    assert res["due"] is False


def test_fail_open_on_memory_error(tmp_path, monkeypatch):
    base = str(tmp_path)

    # 让 LearnedStore.active_recent 抛异常 → 行为 fail-open 返回 written=0，不抛
    import deskmate.agency.behaviors.observation_diary as mod

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(mod, "_load_json", lambda p: {})
    # 直接 patch 内部读取：让 active_recent 抛
    import deskmate.memory.learned_store as ls

    monkeypatch.setattr(ls.LearnedStore, "active_recent", _boom)

    res = mod.run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})
    assert res["written"] == 0  # fail-open


def test_no_external_fact_writes_nothing(tmp_path):
    base = str(tmp_path)
    # 只有用户事实，无「（外部·」标的 → 没有可记的观察
    from deskmate.memory.learned_store import LearnedStore
    LearnedStore(base_dir=base).append(["萧哥是宁波人", "写作营讲网文技巧"])

    from deskmate.agency.behaviors.observation_diary import run_observation_diary

    res = run_observation_diary(base, {"cooldown_hours": 0, "max_observations": 6})
    assert res["written"] == 0
    assert "没有新的世界观察" in res["summary"]
