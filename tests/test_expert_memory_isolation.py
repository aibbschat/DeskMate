"""小桔记忆隔离（2026-11-09 红线段）：专家/工人 1v1 不得读写小桔记忆。

背景：build_engine 此前对所有 agent 都挂 post_turn_hook + memory_store +
context_provider，实测影视专家 1v1 被写进 ~/.deskmate/sessions、
xiaosheng_observations、USER.md，随后被"自我表达"行为当作小桔本人的话弹出。
本组锁住：只有 deskmate / cowork 进小桔记忆；专家 engine 无钩子、无记忆工具。
"""
from __future__ import annotations

from typing import Any

from coworker.agent import MEMORY_COMPANION_AGENTS, build_engine
from coworker.personas.registry import get_registry


class _SilentProvider:
    def capabilities(self, model: str) -> Any:
        from coworker.providers.base import ModelCapabilities

        return ModelCapabilities()

    def complete(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn

        return AssistantTurn(text="ok")

    def stream(self, *, model, messages, tools=None, **settings):  # noqa: ANN001
        from coworker.providers.base import AssistantTurn, StreamChunk

        yield StreamChunk(turn=AssistantTurn(text="ok"))


def _engine(persona: str, memory_store):
    agent = get_registry().get(persona).agent()
    return build_engine(
        agent=agent,
        provider=_SilentProvider(),
        model="test-model",
        workspace=None,
        memory_store=memory_store,
        memory_saving_enabled=lambda: True,
    )


def test_companion_set_is_deskmate_and_cowork() -> None:
    assert MEMORY_COMPANION_AGENTS == frozenset({"deskmate", "cowork"})


def test_expert_engine_does_not_touch_deskmate_memory(tmp_path) -> None:
    from coworker.memory.sqlite_store import SQLiteMemoryStore

    store = SQLiteMemoryStore(tmp_path / "mem.db")
    for persona in ("adaptation", "brainstormer", "lit-editor", "editor-in-chief"):
        if persona == "editor-in-chief":
            continue  # 该 persona requires_folder，测试环境不建 workspace/沙箱
        eng = _engine(persona, store)
        assert eng._post_turn_hook is None, f"{persona} 仍会写小桔记忆"
        names = set(eng.registry.names())
        assert not (names & {"memory_read", "memory_update", "memory_forget"}), (
            f"{persona} 仍暴露小桔记忆工具"
        )


def test_deskmate_engine_keeps_her_memory(tmp_path) -> None:
    from coworker.memory.sqlite_store import SQLiteMemoryStore

    store = SQLiteMemoryStore(tmp_path / "mem.db")
    eng = _engine("deskmate", store)
    assert eng._post_turn_hook is not None, "小桔本人必须保留记忆抽取"
    assert {"memory_read", "memory_update", "memory_forget"} & set(eng.registry.names())
