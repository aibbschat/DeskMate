"""接入层契约：上下文总预算钩子挂在唯一 provider 出口，默认关闭必须 zero-diff。"""

from __future__ import annotations

import os

import pytest

from coworker.engine import TurnEngine
from coworker.permissions import PermissionEngine
from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
from coworker.tools import ToolRegistry


class _Provider(ProviderClient):
    def __init__(self):
        self.calls: list[list[dict]] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.calls.append([dict(m) for m in messages])
        return AssistantTurn(text="ok", finish_reason="stop")

    def capabilities(self, model):
        return ModelCapabilities()


def _engine(tmp_path, *, context_provider=None):
    return TurnEngine(
        provider=_Provider(),
        registry=ToolRegistry(),
        permissions=PermissionEngine(workspace_root=tmp_path),
        model="test-model",
        context_provider=context_provider,
    )


def _seed(engine):
    engine.messages.extend(
        [
            {"role": "system", "content": "人格底座"},
            {"role": "user", "content": "原始任务"},
            {"role": "assistant", "content": "好的我先读原文"},
            {"role": "tool", "content": "原文正文不能裁" * 5},
            {"role": "assistant", "content": "读到第一章继续"},
            {"role": "assistant", "content": "让我再想想地形"},
            {"role": "user", "content": "最终问题"},
        ]
    )


def test_default_off_is_byte_identical(tmp_path, monkeypatch):
    """默认关闭：钩子的存在对出站消息零影响（含 sidecar 剥离、context 注入）。"""
    monkeypatch.delenv("COWORKER_CONTEXT_TOTAL_BUDGET", raising=False)
    e = _engine(tmp_path, context_provider=lambda _: "实时上下文")
    _seed(e)
    out = e._outbound_messages()
    # 人格、原文、两端 user、注入块都在，中间过程轮也没被碰
    assert out[0]["content"] == "人格底座"
    assert any("原文正文不能裁" in m.get("content", "") for m in out)
    assert any("好的我先读原文" in m.get("content", "") for m in out)
    assert "<system-context>" in out[-1]["content"]
    assert e._last_context_budget_report["enabled"] is False


def test_enabled_trims_middle_keeps_context_block_and_manifest(tmp_path, monkeypatch):
    """开启：中间过程轮被裁，末条 user 的注入块与原文 tool 完好。"""
    # 豁免底线约 89（system+tool+两端 user 含注入块），总量 110；
    # 预算取 100 → 只裁得动中间过程轮，且不触及核心。
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "100")
    e = _engine(tmp_path, context_provider=lambda _: "实时上下文")
    _seed(e)
    out = e._outbound_messages()
    assert out[0]["content"] == "人格底座"
    assert any("原文正文不能裁" in m.get("content", "") for m in out)
    assert "<system-context>" in out[-1]["content"]
    assert all("好的我先读原文" not in m.get("content", "") for m in out)
    r = e._last_context_budget_report
    assert r["enabled"] is True
    assert r["removed_turns"] >= 1
    assert r["at_exempt_floor"] is False
    assert r["after"] <= 100


def test_persisted_history_never_mutated(tmp_path, monkeypatch):
    """无论开关，self.messages 是持久化真源，裁剪只在出站副本上发生。"""
    monkeypatch.setenv("COWORKER_CONTEXT_TOTAL_BUDGET", "70")
    e = _engine(tmp_path)
    _seed(e)
    before = [dict(m) for m in e.messages]
    e._outbound_messages()
    assert e.messages == before
