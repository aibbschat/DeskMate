"""动态路由 judge 层测试（PilotDeck 借鉴 · 2026-10-15）。

先红后绿：本文件锁住 judge 层的契约行为——fail-open、尊重显式模型（不越权）、
免费组/付费组选择、短续接继承。router 接线部分用 monkeypatch 隔离真实 provider。
"""
from __future__ import annotations

from coworker.providers.dynamic_route import (
    AUTO_MODEL,
    GROUP_FREE,
    GROUP_PAID,
    TIER_COMPLEX,
    TIER_SIMPLE,
    classify_difficulty,
    is_short_continuation,
    last_user_text,
    resolve_auto_model,
    select_group_for_turn,
)
from coworker.providers.router import ProviderRouter


# -- last_user_text ---------------------------------------------------------
def test_last_user_text_returns_last_user_str():
    msgs = [
        {"role": "user", "content": "第一个"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": "第二个"},
    ]
    assert last_user_text(msgs) == "第二个"


def test_last_user_text_parts():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "你好"},
                {"type": "text", "text": "世界"},
            ],
        }
    ]
    assert last_user_text(msgs) == "你好世界"


def test_last_user_text_none_when_no_user():
    assert last_user_text([{"role": "assistant", "content": "x"}]) is None
    assert last_user_text([]) is None


# -- is_short_continuation --------------------------------------------------
def test_is_short_continuation():
    assert is_short_continuation("好的") is True
    assert is_short_continuation("继续。") is True
    assert is_short_continuation("ok") is True
    assert is_short_continuation("嗯") is True
    assert is_short_continuation("") is True
    assert is_short_continuation("谢") is True  # <=2 字
    assert is_short_continuation("谢谢你的详细解释，请问……") is False
    assert is_short_continuation("帮我写个函数") is False


# -- classify_difficulty (heuristic) ---------------------------------------
def test_classify_simple_chat():
    assert classify_difficulty("你好，今天天气怎么样") == TIER_SIMPLE


def test_classify_complex_code():
    assert (
        classify_difficulty("帮我写一段 python 代码实现快速排序并解释") == TIER_COMPLEX
    )


def test_classify_complex_reasoning():
    assert classify_difficulty("解释一下量子纠缠和相对论的区别") == TIER_COMPLEX


def test_classify_complex_long():
    long_text = "需求" * 200  # > 300 字
    assert classify_difficulty(long_text) == TIER_COMPLEX


def test_classify_empty_is_complex_fail_open():
    assert classify_difficulty("") == TIER_COMPLEX
    assert classify_difficulty("   ") == TIER_COMPLEX


# -- select_group_for_turn --------------------------------------------------
def test_select_group_simple_to_free():
    msgs = [{"role": "user", "content": "你好"}]
    assert select_group_for_turn(msgs) == GROUP_FREE


def test_select_group_complex_to_paid():
    msgs = [{"role": "user", "content": "帮我写一段代码实现快速排序"}]
    assert select_group_for_turn(msgs) == GROUP_PAID


def test_select_group_empty_messages_falls_back():
    assert select_group_for_turn([]) == GROUP_PAID


def test_select_group_short_continuation_inherits_prev():
    msgs = [{"role": "user", "content": "好的"}]
    # 上一轮是免费组 → 续接继承 free，不被重新分级成 paid/简单
    assert select_group_for_turn(msgs, prev_group=GROUP_FREE) == GROUP_FREE
    assert select_group_for_turn(msgs, prev_group=GROUP_PAID) == GROUP_PAID


def test_select_group_short_continuation_no_prev_classifies():
    msgs = [{"role": "user", "content": "好的"}]
    # 无 prev_group 时按内容判（"好的" 简单 → free）
    assert select_group_for_turn(msgs, prev_group=None) == GROUP_FREE


# -- resolve_auto_model -----------------------------------------------------
class _StubChannel:
    def __init__(self, key: str) -> None:
        self.key = key


def test_resolve_disabled_returns_none():
    msgs = [{"role": "user", "content": "你好"}]
    assert resolve_auto_model(msgs, dynamic_enabled=False) is None


def test_resolve_free_channel():
    msgs = [{"role": "user", "content": "你好"}]
    route_fn = lambda g: (_StubChannel("omniroute:stub"), [])  # 永远是免费组通道
    assert resolve_auto_model(msgs, dynamic_enabled=True, route_fn=route_fn) == "omniroute:stub"


def test_resolve_free_unavailable_falls_back_to_paid():
    msgs = [{"role": "user", "content": "你好"}]
    def route_fn(group):
        if group == GROUP_FREE:
            return (None, [])  # 免费组全熔
        return (_StubChannel("ark-agent-plan-cn:deepseek-v4-flash"), [])
    assert (
        resolve_auto_model(msgs, dynamic_enabled=True, route_fn=route_fn)
        == "ark-agent-plan-cn:deepseek-v4-flash"
    )


def test_resolve_both_unavailable_returns_none():
    msgs = [{"role": "user", "content": "你好"}]
    route_fn = lambda g: (None, [])  # 两组都熔尽
    assert resolve_auto_model(msgs, dynamic_enabled=True, route_fn=route_fn) is None


def test_resolve_route_fn_exception_is_fail_open():
    msgs = [{"role": "user", "content": "你好"}]
    def route_fn(group):
        raise RuntimeError("boom")
    assert resolve_auto_model(msgs, dynamic_enabled=True, route_fn=route_fn) is None


# -- ProviderRouter 接线（monkeypatch 隔离真实 provider）----------------------
class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, model, messages, tools=None, **settings):
        self.calls.append(model)
        return {"model": model, "choices": [{"message": {"content": "ok"}}]}

    def stream(self, *, model, messages, tools=None, **settings):
        self.calls.append(model)
        yield {"choices": [{"delta": {"content": "ok"}}]}


def test_router_resolves_auto_when_enabled(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(
        "coworker.providers.router.build_provider_client",
        lambda *a, **k: fake,
    )
    r = ProviderRouter(dynamic_routing=True, auto_route_fn=lambda m, **k: "omniroute:stub")
    r.complete(model=AUTO_MODEL, messages=[{"role": "user", "content": "你好"}])
    assert fake.calls == ["stub"]  # 被解析成 omniroute:stub 的 bare 名（kilo 车道 2026-10-18 删除，fixture 换现存 provider）
    assert r._last_dynamic_group == GROUP_FREE


def test_router_passes_through_explicit_model(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(
        "coworker.providers.router.build_provider_client",
        lambda *a, **k: fake,
    )
    r = ProviderRouter(dynamic_routing=True, auto_route_fn=lambda m, **k: "omniroute:stub")
    r.complete(model="gpt-4", messages=[{"role": "user", "content": "你好"}])
    assert fake.calls == ["gpt-4"]  # 显式模型不被动态路由覆盖（不越权）


def test_router_disabled_does_not_resolve(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(
        "coworker.providers.router.build_provider_client",
        lambda *a, **k: fake,
    )
    # dynamic_routing 默认关 → "auto" 透传（裸名仍是 "auto"，不解析）
    r = ProviderRouter(auto_route_fn=lambda m, **k: "omniroute:stub")
    r.complete(model=AUTO_MODEL, messages=[{"role": "user", "content": "你好"}])
    assert fake.calls == [AUTO_MODEL]


def test_router_stream_resolves_auto(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(
        "coworker.providers.router.build_provider_client",
        lambda *a, **k: fake,
    )
    r = ProviderRouter(dynamic_routing=True, auto_route_fn=lambda m, **k: "omniroute:stub")
    list(r.stream(model=AUTO_MODEL, messages=[{"role": "user", "content": "你好"}]))
    assert fake.calls == ["stub"]
