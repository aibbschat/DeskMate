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


# --- 2026-11-09 复查补强：全 persona 覆盖 + 不走 hook 的旁路写入口 -------------

def _engine_with_workspace(persona: str, memory_store, ws):
    agent = get_registry().get(persona).agent()
    return build_engine(
        agent=agent,
        provider=_SilentProvider(),
        model="test-model",
        workspace=str(ws),
        memory_store=memory_store,
        memory_saving_enabled=lambda: True,
    )


def test_all_personas_memory_gate(tmp_path) -> None:
    """34 个 persona 全覆盖：只有 deskmate / cowork 拿到记忆工具 + hook + engine 标记。

    原测试跳过了 requires_folder 的 editor-in-chief，也没测 plot-architect /
    screenwriter —— 正是萧哥点名的几位。统一传 tmp workspace 后它们都能构造。
    """
    from coworker.memory.sqlite_store import SQLiteMemoryStore

    store = SQLiteMemoryStore(tmp_path / "mem.db")
    entries = get_registry()._entries
    checked = 0
    for pid, entry in sorted(entries.items()):
        agent = entry.agent()
        eng = _engine_with_workspace(pid, store, tmp_path)
        expect = agent.name in MEMORY_COMPANION_AGENTS
        has_tools = bool(
            {"memory_read", "memory_update", "memory_forget"} & set(eng.registry.names())
        )
        assert has_tools is expect, f"{pid} 记忆工具不符合 companion 判定"
        assert (eng._post_turn_hook is not None) is expect, f"{pid} post_turn_hook 漏挂/误挂"
        assert getattr(eng, "deskmate_memory_enabled", False) is expect, (
            f"{pid} engine 标记错误（_xiaosheng_note 会据此放行/拦下）"
        )
        checked += 1
    assert checked >= 30, f"persona 覆盖不足：{checked}"
    for pid in ("editor-in-chief", "plot-architect", "screenwriter", "script-reviewer"):
        assert pid in entries, f"{pid} 未注册，白名单判定可能 fallback 到默认 cowork"


def test_xiaosheng_note_blocked_for_experts(monkeypatch, tmp_path) -> None:
    """engine.py::_xiaosheng_note 不走 post_turn_hook，是独立的萧盛素材写入口。"""
    import types

    import deskmate.agency.behaviors.xiaosheng_ingestion as ingestion
    from coworker.engine import TurnEngine

    calls: list = []
    monkeypatch.setattr(ingestion, "_summarize_web_search", lambda _r: "检索摘要")
    monkeypatch.setattr(
        ingestion, "record_xiaosheng_observation", lambda *a, **k: calls.append(a)
    )
    eng = object.__new__(TurnEngine)
    eng.is_automation = False
    tool_call = types.SimpleNamespace(name="web_search", arguments={"q": "x"})

    # 未打标（fail-closed）与专家标记都不写；companion 才放行（先红后绿对照组）。
    if hasattr(eng, "deskmate_memory_enabled"):
        del eng.deskmate_memory_enabled
    eng._xiaosheng_note(tool_call, {}, "ok")
    eng.deskmate_memory_enabled = False
    eng._xiaosheng_note(tool_call, {}, "ok")
    assert calls == [], "专家/未打标引擎仍把检索结果写进了萧盛素材池"

    eng.deskmate_memory_enabled = True
    eng._xiaosheng_note(tool_call, {}, "ok")
    assert len(calls) == 1, "companion 的正常摄入被误拦"


def test_expert_context_has_no_deskmate_blocks(monkeypatch, tmp_path) -> None:
    """读侧：小桔技能索引 + @召唤协议都不进专家 1v1 上下文。"""
    import coworker.agent as agent_mod
    from coworker.memory.sqlite_store import SQLiteMemoryStore

    monkeypatch.setattr(
        agent_mod, "_deskmate_skill_index", lambda: "<available_skills>SENTINEL</available_skills>"
    )
    monkeypatch.setattr(agent_mod, "_deskmate_mention_context", lambda *a, **k: "MENTION_SENTINEL")
    monkeypatch.setattr(agent_mod, "_deskmate_memory_context", lambda *a, **k: "")
    store = SQLiteMemoryStore(tmp_path / "mem.db")

    def _instructions(engine) -> str:
        # TurnEngine 把系统 prompt 作为 messages[0] 的 system 消息保存。
        first = engine.messages[0] if engine.messages else {}
        return str(first.get("content", "")) if first.get("role") == "system" else ""

    for pid in ("editor-in-chief", "plot-architect", "screenwriter", "adaptation"):
        eng = _engine_with_workspace(pid, store, tmp_path)
        assert "SENTINEL" not in _instructions(eng), f"{pid} 仍读到小桔技能索引"
        assert "MENTION_SENTINEL" not in eng.context_provider([]), f"{pid} 仍注入 @召唤协议"

    eng = _engine_with_workspace("deskmate", store, tmp_path)
    assert "SENTINEL" in _instructions(eng), "小桔本人的技能索引被误撤"
    assert "MENTION_SENTINEL" in eng.context_provider([]), "小桔本人的 @召唤被误撤"
