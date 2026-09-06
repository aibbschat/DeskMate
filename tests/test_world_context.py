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
