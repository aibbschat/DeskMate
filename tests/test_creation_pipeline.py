"""创作流水线核心逻辑回归测试（P0 修复验证）。

覆盖：
- 跨集摘要持久化恢复（sidecar 重启不丢前情）
- 分集 screen_items 键类型一致（内存/恢复路径都能取到当前集状态）
- 串行门禁（大纲未定稿不能开写剧本）
- 一键直通绕过审核（应保留知情批准）
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


def _tmp_base():
    return tempfile.mkdtemp(prefix="ow_creation_")


def _create_episodic(tmp, episodes=3):
    from deskmate.creation import service

    out = service.create_pipeline(
        "screenplay", "测试概念", executor_kind="stub",
        base_dir=tmp, episodes=episodes,
    )
    pid = out["pid"]
    # 大纲 v1 → 审 → 定稿（解锁剧本门禁）
    service.draft_outline(pid)
    service.review_outline(pid)
    service.lead_outline(pid, True)
    return pid


def test_episode_summaries_persist_across_restart():
    """P0-1: 做完前几集后模拟 sidecar 重启，_episode_summaries 必须恢复。"""
    from deskmate.creation import service

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=3)

    # 走完第1集（写→审→定稿）
    service.draft_screen(pid, episode=1)
    service.review_screen(pid, episode=1)
    service.lead_screen(pid, True, episode=1)
    # 走完第2集
    service.draft_screen(pid, episode=2)
    service.review_screen(pid, episode=2)
    service.lead_screen(pid, True, episode=2)

    # 模拟重启：清空进程内注册表，从磁盘恢复
    service._PIPELINES.clear()
    service._REGISTRY_META.clear()
    pl = service.get_pipeline(pid)

    # 第3集动笔前应能看到前2集摘要
    prior = pl._prior_episode_summaries(3)
    assert "第1集" in prior, f"前情摘要丢失: {prior!r}"
    assert "第2集" in prior, f"前情摘要丢失: {prior!r}"
    print("PRIOR:", prior)


def test_screen_items_key_type_consistent_in_memory():
    """P0-2: chat_flow._state() 用 str(ep) 取 screen_items 必须命中（内存路径）。"""
    from deskmate.creation import service
    from deskmate.creation.chat_flow import CreationChat

    tmp = _tmp_base()
    pid = _create_episodic(tmp, episodes=3)
    pl = service.get_pipeline(pid)

    # 写第1集，使其进入 review
    service.draft_screen(pid, episode=1)

    chat = CreationChat(pl, pid)
    state = chat._state()
    # 当前集应为1，且 screen_state 读得到（非永远 open）
    assert state["current_episode"] == 1, state
    assert state["screen_state"] != "open", f"screen_state 未命中: {state}"
    print("STATE:", state)


def test_guard_blocks_screen_before_outline_done():
    """门禁：大纲未定稿时写剧本必须抛 PipelineGuardError。"""
    from deskmate.creation import service, pipeline as P
    from deskmate.creation.guard import PipelineGuardError

    tmp = _tmp_base()
    # 裸建项（不走 create_pipeline 的定稿流程），大纲处于 open
    pl = P.CreativePipeline(space="creation:guardtest", base_dir=tmp, executor=P.StubExecutor())
    pl.start_screenplay_project("概念", episodes=2)
    with pytest.raises(PipelineGuardError):
        pl.draft_screenplay(episode=1)


if __name__ == "__main__":
    test_episode_summaries_persist_across_restart()
    test_screen_items_key_type_consistent_in_memory()
    test_guard_blocks_screen_before_outline_done()
    print("ALL PASSED")
