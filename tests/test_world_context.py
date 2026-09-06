"""build_world_context：把小桔「看世界」得来的观察回灌对话/主动推理上下文。

验证：
- 只回灌带「（外部·」溯源标的世界观察，过滤掉用户口述事实；
- 过期条目（valid_until < 现在）被 relevant() 自动排除，不注入；
- 无任何世界观察时返回空串（fail-open）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_build_world_context_includes_external_only(tmp_path: Path):
    from datetime import datetime, timedelta

    from deskmate.memory.learned_store import LearnedStore
    from deskmate.memory.world_context import build_world_context

    store = LearnedStore(base_dir=tmp_path)
    # 世界观察（带溯源标，7 天后过期）
    store.append(
        ["（外部·文化产业）网文风向：番茄小说上线 AI 辅助写作工具"],
        valid_until=(datetime.now() + timedelta(days=7)).isoformat(timespec="seconds"),
    )
    # 用户口述事实（不应被回灌）
    store.append(["萧哥喜欢用史料交叉比对"])

    ctx = build_world_context(base_dir=tmp_path)
    assert "（外部·文化产业）" in ctx
    assert "番茄小说" in ctx
    assert "萧哥喜欢用史料交叉比对" not in ctx
    assert "她观察到的世界" in ctx


def test_build_world_context_excludes_expired(tmp_path: Path):
    from datetime import datetime, timedelta

    from deskmate.memory.learned_store import LearnedStore
    from deskmate.memory.world_context import build_world_context

    store = LearnedStore(base_dir=tmp_path)
    # 已过期的世界观察：不应注入
    store.append(
        ["（外部·天下要闻）某旧闻"],
        valid_until=(datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
    )
    assert build_world_context(base_dir=tmp_path) == ""


def test_build_world_context_empty_when_no_world_facts(tmp_path: Path):
    from deskmate.memory.learned_store import LearnedStore
    from deskmate.memory.world_context import build_world_context

    store = LearnedStore(base_dir=tmp_path)
    store.append(["一条纯用户事实"])
    assert build_world_context(base_dir=tmp_path) == ""


def test_is_world_fact_recognizes_both_marker_positions():
    """溯源标有前缀式 / 后缀式两种位置，都必须认。

    回归：2026-09-06 打包前体检发现真实库 24 条外部观察里 17 条是**后缀式**
    （豆包搜索摄入、状态已 digested），旧判定只认前缀 → 整批静默漏掉。
    """
    from deskmate.memory.world_context import is_world_fact

    # 前缀式（digest 写入）
    assert is_world_fact("（外部·文化产业）网文风向：番茄上线 AI 工具") is True
    # 后缀式（外部信息流水线 ingestion_pipeline 写入）
    assert is_world_fact("伊朗革命卫队称打击了美航母（外部·news）") is True
    assert is_world_fact("白酒板块今日集体下挫（外部·finance）") is True
    # 用户口述事实 → 不是世界观察
    assert is_world_fact("萧哥喜欢用史料交叉比对") is False
    assert is_world_fact("") is False
    # 句中出现的字样不算（避免把用户原话误判成世界观察）
    assert is_world_fact("他提到（外部·news）这个词") is False


def test_build_world_context_includes_suffix_marked_observations(tmp_path: Path):
    """回归：后缀式标记的世界观察必须能回灌（旧实现只认前缀，一条都进不来）。"""
    from deskmate.memory.learned_store import LearnedStore
    from deskmate.memory.world_context import build_world_context

    store = LearnedStore(base_dir=tmp_path)
    store.append(["郑钦文晋级美网女单四强（外部·news）"])
    store.append(["白酒板块今日集体下挫（外部·finance）"])

    ctx = build_world_context(base_dir=tmp_path)
    assert "她观察到的世界" in ctx
    assert "郑钦文晋级美网女单四强" in ctx
    assert "白酒板块今日集体下挫" in ctx
