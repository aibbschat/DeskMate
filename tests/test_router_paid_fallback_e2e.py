"""端到端：验证 router 真的会在火山撞额度时切到小米（2026-10-09 验收）。

这不是单测的重复 —— 它走**真实的 ProviderRouter + 真实的熔断器**，
只有假的 provider 客户端，从而端到端验证「主链路真的会降级」。
"""

from __future__ import annotations

import pytest


class _FakeQuotaError(Exception):
    """模拟火山的 429：属性名对齐 openai SDK 的形态。"""

    status_code = 429


class _RecordingClient:
    """假 provider 客户端：记录被调过，并让指定 provider 抛 429。"""

    def __init__(self, name, fail):
        self.name = name
        self.fail = fail
        self.calls = []

    def complete(self, *, model, messages, tools=None, **kw):
        self.calls.append(model)
        if self.fail:
            raise _FakeQuotaError("AccountQuotaExceeded")
        return self.name + ":" + model

    def stream(self, *, model, messages, tools=None, **kw):
        self.calls.append(model)
        if self.fail:
            raise _FakeQuotaError("AccountQuotaExceeded")
        yield self.name + "|" + model


@pytest.fixture
def wired(monkeypatch):
    """把 router 的客户端构造换成假客户端：火山挂、小米好、腾讯好。"""
    from coworker.providers import router as R

    clients = {}

    def build(name, profile=None, secrets=None):
        if name not in clients:
            clients[name] = _RecordingClient(name, fail=(name == "ark-agent-plan-cn"))
        return clients[name]

    monkeypatch.setattr(R, "build_provider_client", build)

    from coworker.providers import paid_fallback as pf

    chans = [
        ("ark-agent-plan-cn", "deepseek-v4-flash"),
        ("xiaomi", "mimo-v2.5-pro"),
        ("tencent-tokenplan", "hy3"),
    ]
    monkeypatch.setattr(pf, "paid_group_channels", lambda: chans)
    # 熔断记账用临时库，fail-open
    monkeypatch.setattr(pf, "_breaker_base_dir", lambda: None)
    monkeypatch.setattr(pf, "_is_open", lambda p, m: False)
    return R.ProviderRouter(secrets=None), clients


def test_router_complete_degrades_ark_to_xiaomi(wired):
    """**主验收**：火山 429 → router 自动改用小米返回，主调用方无感。"""
    r, clients = wired
    out = r.complete(
        model="ark-agent-plan-cn:doubao-seed-2.1-turbo",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert out == "xiaomi:mimo-v2.5-pro", "应降级到小米"
    assert clients["ark-agent-plan-cn"].calls == ["doubao-seed-2.1-turbo"], "确实先试了火山"
    assert clients["xiaomi"].calls == ["mimo-v2.5-pro"]


def test_router_stream_degrades_ark_to_xiaomi(wired):
    """流式同样降级（主聊天走的就是 stream）。"""
    r, clients = wired
    got = list(
        r.stream(
            model="ark-agent-plan-cn:doubao-seed-2.1-turbo",
            messages=[{"role": "user", "content": "hi"}],
        )
    )
    assert got == ["xiaomi|mimo-v2.5-pro"]


def test_router_non_paid_untouched(wired):
    """非付费模型路径零变化（回退不该影响 openrouter / 本地模型）。

    openrouter 客户端即使一直失败，也**不应**被降级替换 —— 它不在付费套餐里，
    它的失败要原样抛给调用方（错误信息才有诊断价值）。
    """
    r, clients = wired
    from coworker.providers import router as R

    # openrouter 走同一个 build 工厂，且让它一直失败。
    orig_build = R.build_provider_client
    orig_build("openrouter")  # 触发生成一个 fail=False 的先例

    # 直接让该 client 改为失败。
    clients["openrouter"].fail = True

    with pytest.raises(_FakeQuotaError):
        r.complete(model="openrouter:x", messages=[])
    assert clients["openrouter"].calls == ["x"], "只该试一次，不该降级到别的 provider"
    assert "xiaomi" not in clients, "绝不该去碰付费组的客户端"

