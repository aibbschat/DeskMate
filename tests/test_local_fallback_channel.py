"""回归：本地 llama-server 兜底通道（2026-10-11 萧哥拍板；2026-10-18 起为免费组唯一成员）。

设计：免费组曾 = 远端免费清单（opencode → Kilo，动态，会下架）+ **本地 local
兜底**（CVM 常驻 llama-server，OpenAI 兼容）。2026-10-18 萧哥拍板**删除**
外网免费车道（opencode 官方封禁免费使用；「今天才知道探测多了会被封号，
留着没用，下次也不敢再踩这个坑」）⇒ 免费组仅剩 local 一条——组成契约由
``tests/test_free_lane_removed.py`` 锁（模块不可 import、picker 无条目、
free_channels 仅剩 local），本文件专注 local 兜底自身的接线：
free_only 路径选到 local → 必走 ``_local_complete``；命中 → 埋点落盘。

不碰活库、不发任何网络请求（_local_complete 全被 mock 替换）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import deskmate.llm_channels
import deskmate.memory.llm as llm_mod


def test_free_channels_is_local_only():
    """免费组仅剩本地兜底一条（外网免费车道 2026-10-18 删除后的组成契约）。

    与 test_free_lane_removed.py::test_free_group_is_local_only 同口径——
    这里从「兜底接线」角度再锁一次：免费组 = [local]，且 free=True。
    """
    chs = deskmate.llm_channels.free_channels()
    assert [c.key for c in chs] == [
        f"{deskmate.llm_channels.LOCAL_LLM_PROVIDER}:{deskmate.llm_channels.LOCAL_LLM_MODEL}"
    ]
    assert chs[0].free is True


def test_free_only_routing_invokes_local_complete(monkeypatch, tmp_path):
    """free_only 路径选到 local 通道时，必须走 ``_local_complete``（本地 llama-server）。

    这是 local 兜底接线的直接回归：若 ``route`` 返回 local 通道，``chat_memory`` 应命中
    ``if ch.provider == "local"`` 分支并调用 ``_local_complete``。
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


def test_local_fallback_records_stats_on_hit(monkeypatch, tmp_path):
    """local 兜底被触发时，``_record_local_fallback`` 必须在 ``local_fallback.log`` 写一行。

    B 任务埋点回归：验证埋点真的落盘（成功行含 ts/success/elapsed_s/prompt_chars），
    供后续观测『本地兜底命中频率与耗时』。失败静默不影响主链路由函数自身保证。
    """
    import json as _json

    monkeypatch.setenv("DESKMATE_DIR", str(tmp_path))
    monkeypatch.setattr(llm_mod, "_memory_use_omniroute", lambda bible=False: False)
    # 强制 route 只返回 local 通道 → 必走 local 分支
    local_ch = SimpleNamespace(
        provider="local", model="qwen3-4b-instruct", key="local:qwen3-4b-instruct"
    )
    monkeypatch.setattr(
        deskmate.llm_channels, "route", lambda group, base_dir=None: (local_ch, [])
    )
    monkeypatch.setattr(llm_mod, "_call_with_breaker", lambda p, m, fn: fn())
    monkeypatch.setattr(llm_mod, "_local_complete", lambda *a, **k: "小桔兜底回复")

    out = llm_mod.chat_memory("s", "u", free_only=True, max_tokens=20, temperature=0.7)
    assert out == "小桔兜底回复"

    log = tmp_path / "local_fallback.log"
    assert log.exists(), "local 兜底命中应写 local_fallback.log"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1, f"应恰好记 1 行，实际 {len(lines)}"
    rec = _json.loads(lines[0])
    assert rec["success"] is True
    assert "elapsed_s" in rec and "prompt_chars" in rec
    assert rec["prompt_chars"] == len("u")
