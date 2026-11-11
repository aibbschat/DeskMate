"""群聊里 7 位老专家的独门手艺可达性（2026-11-11 萧哥拍板）。

1v1 / 专家中心已经能加载各自的 OPE-58 bundle 手艺；群聊此前把 ``load_skill``
从工具白名单里摘掉，而且建 engine 时没传 ``extra_skill_dirs`` —— 手艺根本
不在 SkillLoader 的目录集合里，群里看不到也读不到。

本文件锁住放开之后的行为，同时守住三条红线：
- 每位专家只见自己的那一本（目录物理隔离 + manifest ``skills:`` 白名单双闸）；
- 书稿工具面仍然只读（``save_skill`` / 写文件 / 派活一律不放开）；
- 小桔不走群聊运行时，物理上读不到任何专家手艺。

先红后绿：实现落在 ``deskmate/bots/runtime.py::BotRuntime.engine``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


# ``roundtable: true`` 且有独门手艺的 7 位老专家。
# brainstormer 在 BOT_ORDER 里但没有 roundtable: true（已退出群名册，走独立专家），
# 所以不在本清单内；它有手艺，从 solo 形态起 engine 时同样能挂载。
GROUP_CRAFT_BOTS = (
    "historian",
    "lit-editor",
    "proofreader",
    "reader",
    "publisher",
    "adaptation",
    "genre",
)

WRITE_FORBIDDEN = {
    "write_file", "apply_patch", "apply_unified_diff", "replace_in_file",
    "run_shell", "shell_task_kill", "save_skill", "a2a_call", "web_fetch",
}


def _skill_name(persona_id: str) -> str:
    return f"{persona_id}-method"


class _IdleProvider:
    """离线 provider：只为把真实 TurnEngine 建起来，不跑完整一轮。"""

    def capabilities(self, model: str) -> Any:  # noqa: ANN401
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, **kw: Any) -> Any:
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="ok")

    def stream(self, **kw: Any) -> Any:
        from coworker.providers.base import AssistantTurn, StreamChunk

        yield StreamChunk(turn=AssistantTurn(text="ok"))


@pytest.fixture
def runtime(tmp_path: Path):
    from deskmate.bots.runtime import BotRuntime

    book = tmp_path / "books" / "16.测试书"
    book.mkdir(parents=True)
    rt = BotRuntime(
        books_root=tmp_path / "books",
        workspace_root=tmp_path / "ws",
        provider=_IdleProvider(),
        model="test-model",
    )
    yield rt
    rt.close()


def _call_load_skill(eng: Any, name: str) -> dict:
    """从 engine 的真实工具注册表里取出 load_skill 并调用（不是另造一个 loader）。"""
    spec = eng.registry._tools.get("load_skill")
    assert spec is not None, "群聊工具面里没有 load_skill"
    fn = getattr(spec, "fn", None) or getattr(spec, "func", None)
    if fn is None and callable(spec):
        fn = spec
    assert callable(fn), f"load_skill spec 形态不明：{type(spec)!r}"
    return fn(name)


# --------------------------------------------------------------- 一、工具面放开

@pytest.mark.parametrize("bot_id", GROUP_CRAFT_BOTS)
def test_group_engine_exposes_load_skill(runtime: Any, bot_id: str) -> None:
    eng = runtime.engine(bot_id)
    names = set(eng.registry.names())
    assert "load_skill" in names, f"{bot_id} 在群里看不到自己的手艺入口"
    assert "read_book" in names and "list_books" in names, "读稿能力不能被挤掉"


@pytest.mark.parametrize("bot_id", GROUP_CRAFT_BOTS)
def test_group_tool_surface_stays_readonly(runtime: Any, bot_id: str) -> None:
    """放开 load_skill 不等于放开写面；这条守住 11-06 起的群聊红线。"""
    eng = runtime.engine(bot_id)
    names = set(eng.registry.names())
    assert not (names & WRITE_FORBIDDEN), f"{bot_id} 暴露了非只读工具：{names & WRITE_FORBIDDEN}"


# --------------------------------------------------------------- 二、真能取到本人手艺

@pytest.mark.parametrize("bot_id", GROUP_CRAFT_BOTS)
def test_load_skill_returns_own_craft_in_group(runtime: Any, bot_id: str) -> None:
    eng = runtime.engine(bot_id)
    out = _call_load_skill(eng, _skill_name(bot_id))
    assert "error" not in out, f"{bot_id} 的 load_skill 失败：{out}"
    assert out.get("name") == _skill_name(bot_id)
    instructions = str(out.get("instructions") or "")
    assert "逐字" in instructions and "覆盖率" in instructions, (
        f"{bot_id} 取到的不是那本带可核验纪律的手艺：{instructions[:120]}"
    )


@pytest.mark.parametrize("bot_id", GROUP_CRAFT_BOTS)
def test_load_skill_cannot_read_another_experts_craft(runtime: Any, bot_id: str) -> None:
    """每人只见自己：别人的手艺连名字都不该出现在 available 里。"""
    other = next(b for b in GROUP_CRAFT_BOTS if b != bot_id)
    eng = runtime.engine(bot_id)
    out = _call_load_skill(eng, _skill_name(other))
    assert "error" in out, f"{bot_id} 竟能读到 {other} 的手艺（串味）"
    available = out.get("available") or []
    assert _skill_name(other) not in available
    assert _skill_name(bot_id) in available, (
        f"{bot_id} 的可用技能里反而没有自己那本：{available}"
    )


# --------------------------------------------------------------- 三、隔离边界

def test_xiaoju_cannot_build_group_engine(runtime: Any) -> None:
    """小桔不走 BotRuntime；群聊手艺绝不可能漏进她的技能池。"""
    with pytest.raises(ValueError, match="未知 bot"):
        runtime.engine("deskmate")


def test_engines_are_isolated_per_bot(runtime: Any) -> None:
    """每位 bot 独立 engine，各自 SkillLoader 只装自己那一本。"""
    a = runtime.engine("historian")
    b = runtime.engine("lit-editor")
    assert a is not b
    assert "error" not in _call_load_skill(a, _skill_name("historian"))
    assert "error" not in _call_load_skill(b, _skill_name("lit-editor"))
    assert "error" in _call_load_skill(a, _skill_name("lit-editor"))
    assert "error" in _call_load_skill(b, _skill_name("historian"))


# --------------------------------------------------------------- 四、实现不回退 / 不漏传

def test_runtime_wires_bundle_scope_into_build_engine() -> None:
    """防回退：建 engine 时必须真的传 extra_skill_dirs + skill_filter。"""
    from deskmate.bots import runtime as rt_mod

    src = Path(rt_mod.__file__).read_text(encoding="utf-8")
    assert "extra_skill_dirs" in src, "没给 SkillLoader 专家 bundle 目录"
    assert "skill_filter" in src, "没有用 manifest skills: 白名单闸住可见性"


def test_runtime_is_in_deploy_manifest() -> None:
    """改了 runtime.py 而不在传输清单里 ⇒ 云端静默失效（既有红线）。"""
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/deploy_cloud_sync.sh").read_text(encoding="utf-8")
    assert "deskmate/bots/runtime.py" in script
