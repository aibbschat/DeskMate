"""回归：chat_memory(free_only=True) 必须能正确引用 GROUP_FREE（2026-10-10 漏 import 事故）。

事故：deskmate/memory/llm.py:527 漏了 `GROUP_FREE`，导致
`group = GROUP_FREE if free_only else memory_group()` 在发起任何 LLM 调用前就
抛 `NameError: name 'GROUP_FREE' is not defined`。7 个自主/反思行为
（ai_circle_share / reply / column、reflection、worldview_reflection、
creative_suggest、understand_observations）全部空转崩溃、无产出。

本测试用 mock 把网络/熔断层全部替换，直接验证 free_only 分支：
1) 不再抛 NameError、能走到 _call_with_breaker 并返回；
2) 传给 route 的确实是 'free' 组（GROUP_FREE）。

2026-10-18 起免费组唯一成员是本地 llama-server 兜底（opencode / Kilo
外网免费车道已删除），故 mock channel 用 provider="local"。

不碰活库、不发任何网络请求。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import deskmate.llm_channels
import deskmate.memory.llm as llm_mod


def _fake_channel():
    return SimpleNamespace(
        provider="local",
        model="qwen3-4b-instruct",
        key="local:qwen3-4b-instruct",
    )


@pytest.fixture
def patched(monkeypatch, tmp_path):
    # 不走 OmniRoute 历史分支
    monkeypatch.setattr(llm_mod, "_memory_use_omniroute", lambda bible=False: False)
    # 熔断 base 目录用临时目录，避免依赖真实配置
    monkeypatch.setattr(llm_mod, "_breaker_base_dir", lambda: str(tmp_path))
    # 把真实网络调用替换成固定返回值
    monkeypatch.setattr(llm_mod, "_call_with_breaker",
                        lambda provider, model, fn: "OK-MEM")
    # route 是 chat_memory 内 `from deskmate.llm_channels import ... route` 取的，
    # 故 patch 模块属性即可被局部 import 捕获。
    monkeypatch.setattr(deskmate.llm_channels, "route",
                        lambda group, base_dir=None: (_fake_channel(), []))
    yield


def test_free_only_routing_reaches_breaker_and_returns(patched):
    """free_only=True 时不再 NameError，且能正常走到 _call_with_breaker 并返回。

    这是 2026-10-10 事故的直接回归：漏 import GROUP_FREE 时，此调用会在
    `group = GROUP_FREE if free_only ...` 处抛 NameError（RED）；修好后返回 'OK-MEM'（GREEN）。
    """
    out = llm_mod.chat_memory("sys", "usr", free_only=True)
    assert out == "OK-MEM"


def test_free_only_routing_passes_free_group_to_route():
    """route 必须收到 'free' 组（GROUP_FREE），而非付费组。"""
    seen = []

    def _route(group, base_dir=None):
        seen.append(group)
        return _fake_channel(), []

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(llm_mod, "_memory_use_omniroute", lambda bible=False: False)
        mp.setattr(llm_mod, "_breaker_base_dir", lambda: "/tmp")
        mp.setattr(llm_mod, "_call_with_breaker", lambda p, m, fn: "X")
        mp.setattr(deskmate.llm_channels, "route", _route)
        llm_mod.chat_memory("s", "u", free_only=True)

    assert seen == ["free"], "free_only 必须把 'free' 组传给 route"
