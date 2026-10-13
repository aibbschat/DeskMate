"""日记回复降级链总预算锁（2026-10-13 13:45 事故：小桔回复等了 6 分钟）。

事故机制：付费组全半开、免费组全熔断的日子里，generate_reply 的降级链
串行尝试 10+ 通道（chat_memory 25s + ark 链 12s×N + secrets.json 全量
provider 25s×M）——单通道都按设计快速失败，但**总和没有上限**，实测
13:45:53 发日记、13:51:58 才出话（6 分 05 秒）。

修复：_REPLY_BUDGET_SECONDS=90 墙钟预算——超预算立即放弃降级链，
回落 _fallback_reply（她自己写过的话，活感 v2 §三.3），保证用户
在预算内一定等到一句真话，而不是对着「正在想…」等 6 分钟。
"""

from __future__ import annotations

import json
import time
import types

from deskmate.growth import moments as moments_mod


def test_generate_reply_budget_bounds_worst_case(tmp_path, monkeypatch):
    """全通道慢失败时，generate_reply 必须在预算内放弃并回落她的话。

    拓扑：chat_memory 0.05s 失败 + 8 个假 provider 各 0.3s 失败。
    修复前（无预算）：全链跑完 ≈ 2.45s → 断言失败（红）；
    修复后（预算 1.0s）：跑不完链即放弃 ≈ 1.25s，回落固定池非空（绿）。
    """
    # 8 个假 provider：每次尝试 0.3s 后失败（模拟半开通道撞墙）
    def _fake_openai_factory(**kwargs):
        class _Client:
            def __init__(self):
                self.chat = types.SimpleNamespace(
                    completions=types.SimpleNamespace(create=self._create)
                )

            @staticmethod
            def _create(**kw):
                time.sleep(0.3)
                raise RuntimeError("channel down (half-open)")

        return _Client()

    monkeypatch.setattr("openai.OpenAI", _fake_openai_factory)

    def _slow_chat_memory(*a, **kw):
        time.sleep(0.05)
        raise RuntimeError("primary down")

    monkeypatch.setattr("deskmate.memory.llm.chat_memory", _slow_chat_memory)

    state = tmp_path / "state"
    state.mkdir()
    secrets = {
        f"provider:fake-{i}": {
            "api_key": "sk-fake",
            "base_url": "http://127.0.0.1:9/v1",
            "model": "fake-model",
        }
        for i in range(8)
    }
    (state / "secrets.json").write_text(json.dumps(secrets), encoding="utf-8")
    monkeypatch.setenv("COWORKER_STATE_DIR", str(state))
    # 预算压到 1.0s 让测试跑得快；生产默认 90s
    monkeypatch.setattr(moments_mod, "_REPLY_BUDGET_SECONDS", 1.0)

    store = moments_mod.MomentsStore(base_dir=tmp_path)
    t0 = time.monotonic()
    got = store.generate_reply("今天测试预算")
    dt = time.monotonic() - t0

    assert got, "超预算也必须回落她的话，绝不空回复"
    assert dt < 1.7, f"全通道降级应在预算内放弃（实测 {dt:.2f}s，修复前≈2.45s）"
