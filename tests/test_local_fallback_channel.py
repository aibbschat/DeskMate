"""回归：免费组末尾追加本地 llama-server 兜底通道（2026-10-11 萧哥拍板）。

设计：免费组 = 远端免费清单（动态，会下架）+ **本地 local 兜底**（CVM 常驻
llama-server，OpenAI 兼容）。local 排最末——优先用远端免费通道，仅当它们全熔断时，本地
Qwen3-Instruct 兜底跑后台加工（记忆抽取 / 反思 / 理解），宁可本地算也不让任务全挂。
Mac 端无 llama-server → 首次调用失败被熔断跳过，无害。

🔴 2026-10-13 远端车道换人：opencode 匿名车道整条 403（``FreeTierError``），
免费组主力改成 **Kilo**（Our Free Model 插件移植，免密钥+剥Authorization）。
「远端免费通道 = opencode」这个前提随之作废，本文件断言已改为 provider 无关
（只要求「兜底前全是远端免费车道，且不含 local」），并新增
``test_free_channels_excludes_dead_opencode_lane``锁住 opencode 不再进组。

不碰活库、不发任何网络请求（fetch_catalog / _local_complete 全被 mock 替换）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import deskmate.llm_channels
import deskmate.memory.llm as llm_mod
from coworker.providers import kilo_catalog as K
from coworker.providers import zen_catalog as Z

#: 免费组的**远端** provider 车道（不含 local 兜底）。Kilo 是当前主力；
#: opencode 已于 2026-10-13 因整条 403 被摘出免费组（代码保留）。
REMOTE_FREE_PROVIDERS = ("kilo",)


def test_free_channels_appends_local_fallback_last(monkeypatch):
    """免费组候选末尾必须是本地兜底通道（provider='local'，排最末）。"""
    monkeypatch.setattr(
        K,
        "fetch_catalog",
        lambda: [
            {"id": "kilo-auto/free", "isFree": True},
            {"id": "nvidia/nemotron-3.5-lightning:free", "isFree": True},
        ],
    )
    chs = deskmate.llm_channels.free_channels()
    assert chs, "免费组不应为空"
    # 前 N-1 条都是远端免费车道（provider 无关；不再锁死 opencode）
    assert all(c.provider in REMOTE_FREE_PROVIDERS for c in chs[:-1]), (
        f"本地兜底前的候选必须全是远端免费车道，实际：{[c.provider for c in chs]}"
    )
    last = chs[-1]
    assert last.provider == deskmate.llm_channels.LOCAL_LLM_PROVIDER, (
        "本地兜底必须排最末"
    )
    assert last.model == deskmate.llm_channels.LOCAL_LLM_MODEL
    assert last.free is True


def test_free_channels_excludes_dead_opencode_lane(monkeypatch):
    """🔴 opencode 车道整条 403 后，免费组里不许再有 ``provider='opencode'`` 的通道。

    锁的是**处置**而非实现细节：即使 Kilo 清单某个模型后缀恰好长得像
    ``*-free``，也不该被当成 opencode 通道（provider 由组装处写死）。这条红了
    说明有人把死车道又接回了组里 → 后台任务会全部撞 403 并熔断。
    """
    monkeypatch.setattr(
        K,
        "fetch_catalog",
        lambda: [
            {"id": "kilo-auto/free", "isFree": True},
            # 长得像 opencode 免费模型的 id：验证不是靠后缀匹配路由的
            {"id": "space-bunny-free", "isFree": True},
        ],
    )
    chs = deskmate.llm_channels.free_channels()
    offenders = [c.model for c in chs if c.provider == "opencode"]
    assert not offenders, f"死车道 opencode 仍在免费组里：{offenders}"


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
