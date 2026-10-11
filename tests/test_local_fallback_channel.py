"""回归：免费组末尾追加本地 llama-server 兜底通道（2026-10-11 萧哥拍板）。

设计：免费组 = 远端 opencode `*-free` 清单（动态，会下架）+ **本地 local 兜底**（CVM 常驻
llama-server，OpenAI 兼容）。local 排最末——优先用远端免费通道，仅当它们全熔断时，本地
Qwen3-Instruct 兜底跑后台加工（记忆抽取 / 反思 / 理解），宁可本地算也不让任务全挂。
Mac 端无 llama-server → 首次调用失败被熔断跳过，无害。

不碰活库、不发任何网络请求（fetch_catalog / _local_complete 全被 mock 替换）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import deskmate.llm_channels
import deskmate.memory.llm as llm_mod
from coworker.providers import zen_catalog as Z


def test_free_channels_appends_local_fallback_last(monkeypatch):
    """免费组候选末尾必须是本地兜底通道（provider='local'，排最末）。"""
    monkeypatch.setattr(
        Z,
        "fetch_catalog",
        lambda: [
            {"id": "space-bunny-free"},
            {"id": "mimo-v2.6-flash-free"},
        ],
    )
    chs = deskmate.llm_channels.free_channels()
    assert chs, "免费组不应为空"
    # 前 N-1 条都是远端 opencode 免费通道
    assert all(c.provider == "opencode" for c in chs[:-1]), (
        "本地兜底前的候选必须全是 opencode 远端免费通道"
    )
    last = chs[-1]
    assert last.provider == deskmate.llm_channels.LOCAL_LLM_PROVIDER, (
        "本地兜底必须排最末"
    )
    assert last.model == deskmate.llm_channels.LOCAL_LLM_MODEL
    assert last.free is True


def test_free_only_routing_invokes_local_complete(monkeypatch, tmp_path):
    """free_only 路径选到 local 通道时，必须走 ``_local_complete``（本地 llama-server）。

    这是 local 兜底接线的直接回归：若 ``route`` 返回 local 通道，``chat_memory`` 应命中
    ``if ch.provider == "local"`` 分支并调用 ``_local_complete``，而非 opencode/付费分支。
    """
    monkeypatch.setattr(llm_mod, "_memory_use_omniroute", lambda bible=False: False)
    monkeypatch.setattr(llm_mod, "_breaker_base_dir", lambda: str(tmp_path))
    # 让 _call_with_breaker 直接执行传入的 lambda（不真走熔断/网络）
    monkeypatch.setattr(llm_mod, "_call_with_breaker", lambda p, m, fn: fn())

    seen = {}

    def _fake_local(model, system, user, temperature, max_tokens, timeout):
        seen["model"] = model
        seen["system"] = system
        seen["user"] = user
        return "OK-LOCAL"

    monkeypatch.setattr(llm_mod, "_local_complete", _fake_local)

    local_ch = SimpleNamespace(
        provider="local",
        model="qwen3-4b-instruct",
        key="local:qwen3-4b-instruct",
    )
    monkeypatch.setattr(
        deskmate.llm_channels,
        "route",
        lambda group, base_dir=None: (local_ch, []),
    )

    out = llm_mod.chat_memory("sys-prompt", "usr-prompt", free_only=True)
    assert out == "OK-LOCAL", "选到 local 通道应返回 _local_complete 的结果"
    assert seen.get("model") == "qwen3-4b-instruct", "应把 local 模型传给 _local_complete"
    assert seen.get("system") == "sys-prompt"
    assert seen.get("user") == "usr-prompt"
