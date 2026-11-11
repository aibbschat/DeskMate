"""Hook 的 base_dir 必须在引擎构建期固化，不能在 daemon 线程里读 env。

背景（2026-11-11）：TurnEngine 用 daemon 线程跑 post_turn_hook；线程可能活过
测试用例 teardown。旧实现到线程内部才读 DESKMATE_MEMORY_DIR，monkeypatch 回滚
后就会回落到真实 ~/.deskmate。这里锁住「构建期绑定」这一行为。
"""

from __future__ import annotations

from pathlib import Path

import coworker.agent as agent_mod


def test_hook_binds_base_at_build_time(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("DESKMATE_MEMORY_DIR", str(first))

    seen = {}

    def _fake_hook(user_msg, assistant_msg, base=None):
        seen["base"] = base

    monkeypatch.setattr(agent_mod, "_deskmate_post_turn_hook", _fake_hook)

    hook = agent_mod._make_deskmate_post_turn_hook(agent_mod._deskmate_base_dir())
    # 模拟用例结束后 env 回滚/换到下一用例
    monkeypatch.setenv("DESKMATE_MEMORY_DIR", str(second))
    hook("hi", "hello")

    assert Path(seen["base"]) == first


def test_manager_singleton_rebuilds_when_base_changes(tmp_path):
    m1 = agent_mod._get_deskmate_manager(tmp_path / "a")
    m2 = agent_mod._get_deskmate_manager(tmp_path / "a")
    m3 = agent_mod._get_deskmate_manager(tmp_path / "b")

    assert m1 is m2
    assert m3 is not m1
    assert Path(m3.base_dir) == tmp_path / "b"
