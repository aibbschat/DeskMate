"""P0-2 后台主动性「默认开启」回归测试。

背景：DESKMATE_PROACTIVE_BACKGROUND 过去需要显式 =1 才开（opt-in），
配合 run.py 的 env 死结（loader 未灌 .env），主动环「设了也 off」。
P0-2 修复后：run.py 启动期 _load_env_file 已把 ~/.config/coworker/.env 灌入 os.environ
（env 死结已解），且 `_combined_extra_tick` 的 gate 改为「默认开启」——
未显式设置或 =1 都开，仅 =0 显式关闭。

本测试单测 `_combined_extra_tick` 的 proactive gate（不经完整 scheduler），
验证三种 env 状态下的开关语义。
"""

from __future__ import annotations

import asyncio

import pytest

from coworker.server.manager import SessionManager


@pytest.fixture
def proactive_mgr(monkeypatch) -> SessionManager:
    """最小 SessionManager：绕过 __init__，只带 _combined_extra_tick 需要的属性。"""
    mgr = SessionManager.__new__(SessionManager)
    mgr._deskmate_proactive_engine = None

    async def _noop_scheduler_tick():
        return None

    async def _noop_broadcast(_result):
        return None

    mgr._scheduler_tick = _noop_scheduler_tick
    mgr._broadcast_deskmate_suggestion = _noop_broadcast

    # 用假 ProactiveEngine 替换真实引擎，避免 LLM 调用
    class _FakeEngine:
        def __init__(self, base_dir=None):
            self.base_dir = base_dir

    class _FakeTickResult:
        pass

    calls = {"ticks": 0}

    def _fake_make_extra_tick(engine):
        async def _tick():
            calls["ticks"] += 1
            return _FakeTickResult()

        return _tick

    monkeypatch.setattr(
        "deskmate.proactivity.tick.ProactiveEngine", _FakeEngine, raising=False
    )
    monkeypatch.setattr(
        "deskmate.proactivity.background.make_extra_tick", _fake_make_extra_tick, raising=False
    )

    mgr._calls = calls
    return mgr


async def _run_tick(mgr: SessionManager) -> None:
    await mgr._combined_extra_tick()


def test_proactive_background_defaults_on_without_env(proactive_mgr, monkeypatch):
    """未显式设置 DESKMATE_PROACTIVE_BACKGROUND → 默认开启（P0-2）。"""
    monkeypatch.delenv("DESKMATE_PROACTIVE_BACKGROUND", raising=False)
    asyncio.run(_run_tick(proactive_mgr))
    assert proactive_mgr._calls["ticks"] == 1, "未设置时后台主动性应默认开启"


def test_proactive_background_on_with_env_1(proactive_mgr, monkeypatch):
    """DESKMATE_PROACTIVE_BACKGROUND=1 → 开启。"""
    monkeypatch.setenv("DESKMATE_PROACTIVE_BACKGROUND", "1")
    asyncio.run(_run_tick(proactive_mgr))
    assert proactive_mgr._calls["ticks"] == 1


def test_proactive_background_off_with_env_0(proactive_mgr, monkeypatch):
    """DESKMATE_PROACTIVE_BACKGROUND=0 → 显式关闭。"""
    monkeypatch.setenv("DESKMATE_PROACTIVE_BACKGROUND", "0")
    asyncio.run(_run_tick(proactive_mgr))
    assert proactive_mgr._calls["ticks"] == 0, "=0 时应显式关闭后台主动性"
