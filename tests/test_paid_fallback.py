"""主链路付费组回退（2026-10-09，火山周额度撞顶时实测驱动）。

## 覆盖的真实故障

火山套餐 ``ark-agent-plan-cn`` 周额度用尽，日志::

    openai.RateLimitError 429 AccountQuotaExceeded
    "weekly usage quota ... reset at 2026-10-12 00:00:00 +0800"

熔断器正确记了账（``kind=quota_exhausted``），但主聊天仍每 35 秒撞一次 429 ——
``ProviderRouter.complete/stream`` 没有任何 try/except，既不查熔断也不换通道。

## 三条设计红线（各有测试锁定）

1. **同套餐换模型是无效动作**：火山套餐内 8 个模型共用同一份周额度（实测 MATRIX 清单），
   撞了额度后换 ``deepseek-v4-flash`` 照样 429。故尝试序列**跳过同 provider 的其它模型**。
2. **不越权**：非付费套餐（openrouter / 本地 ollama）的模型挂了就是挂了，不替换。
   判定按 **provider** 而非具体模型 —— ``default_model`` 常是付费套餐里不在候选表的模型
   （实测 ``doubao-seed-2.1-turbo`` 就不在 ``PAID_GROUP``），按模型判会让回退对它完全不启用。
3. **流式不得拼接**：一旦吐出首个 chunk 就不能再换通道，否则两个模型的输出会混成一条回复。
"""

from __future__ import annotations

import pytest

from coworker.providers import paid_fallback as pf

ARK = "ark-agent-plan-cn"
XIAOMI = "xiaomi"
TENCENT = "tencent-tokenplan"


@pytest.fixture
def groups(monkeypatch):
    """把付费组固定成三条，隔离真实配置（含熔断状态）。"""
    chans = [(ARK, "deepseek-v4-flash"), (XIAOMI, "mimo-v2.5-pro"), (TENCENT, "hy3")]
    monkeypatch.setattr(pf, "paid_group_channels", lambda: chans)
    monkeypatch.setattr(pf, "_is_open", lambda p, m: False)
    return chans


# ---- 红线1：同套餐换模型是无效动作 ----
def test_same_plan_other_models_skipped(groups):
    """火山撞额度后，**不应**去试同套餐的另一个模型（额度是套餐级的）。"""
    seq = pf.plan_attempts(ARK + ":doubao-seed-2.1-turbo")
    assert ARK + ":deepseek-v4-flash" not in seq, "同套餐换模型 = 白撞一次"
    assert seq == [
        ARK + ":doubao-seed-2.1-turbo",
        XIAOMI + ":mimo-v2.5-pro",
        TENCENT + ":hy3",
    ]


# ---- 红线2：不越权 ----
def test_non_paid_provider_never_falls_back(groups):
    """非付费套餐的模型挂了不替换（不替用户偷换模型）。"""
    assert pf.plan_attempts("openrouter:openai/gpt-4") == ["openrouter:openai/gpt-4"]
    assert pf.plan_attempts("ollama:qwen3") == ["ollama:qwen3"]


def test_paid_model_outside_candidate_table_still_falls_back(groups):
    """**实测病根**：default_model 常是付费套餐里不在候选表的模型。

    若按「模型是否在候选表」判定，回退对它完全不启用 —— 而它恰恰最需要回退。
    """
    seq = pf.plan_attempts(ARK + ":doubao-seed-2.1-turbo")
    assert XIAOMI + ":mimo-v2.5-pro" in seq


def test_requested_model_always_first(groups):
    """原模型永远在首位 —— 不悄悄换掉用户指定的模型。"""
    seq = pf.plan_attempts(XIAOMI + ":mimo-v2.5-pro")
    assert seq[0] == XIAOMI + ":mimo-v2.5-pro"


# ---- 熔断过滤 ----
def test_open_channels_filtered_except_head(groups, monkeypatch):
    """熔断中的通道被跳过，但**首位（用户指定的那个）保留**。"""
    monkeypatch.setattr(
        pf,
        "_is_open",
        lambda p, m: (p, m) == (XIAOMI, "mimo-v2.5-pro"),
    )
    seq = pf._filtered_attempts(ARK + ":doubao-seed-2.1-turbo")
    assert XIAOMI + ":mimo-v2.5-pro" not in seq
    assert TENCENT + ":hy3" in seq
    assert seq[0] == ARK + ":doubao-seed-2.1-turbo", "首位不该被熔断过滤掉"


# ---- complete：撞墙换下一条 ----
def test_complete_falls_back_to_next_channel(groups):
    """火山 429 → 自动走小米。这就是萧哥要验的「撞了切小米」。"""
    calls = []

    def call(m):
        calls.append(m)
        if m.startswith(ARK):
            raise RuntimeError("429 AccountQuotaExceeded")
        return "来自小米的回复"

    out = pf.complete_with_fallback(call, ARK + ":doubao-seed-2.1-turbo")
    assert out == "来自小米的回复"
    assert calls[0].startswith(ARK) and calls[1].startswith(XIAOMI)


def test_complete_all_fail_raises_last(groups):
    """全组挂 → 抛最后一个异常（不是静默成功，也不是首个异常）。"""
    def call(m):
        raise RuntimeError("boom " + m)

    with pytest.raises(RuntimeError) as ei:
        pf.complete_with_fallback(call, ARK + ":doubao-seed-2.1-turbo")
    assert TENCENT in str(ei.value)


def test_complete_single_channel_passthrough(groups):
    """只有一条可试时不做任何额外工作（openrouter 等非付费路径零开销）。"""
    calls = []

    def call(m):
        calls.append(m)
        return "ok"

    assert pf.complete_with_fallback(call, "openrouter:x") == "ok"
    assert calls == ["openrouter:x"]


# ---- 红线3：流式不得拼接 ----
def test_stream_falls_back_before_first_chunk(groups):
    """首个 chunk 之前失败 → 换通道重来（此时还没吐出内容，安全）。"""
    def factory(m):
        if m.startswith(ARK):
            raise RuntimeError("429 quota")
        yield "小米的chunk1"
        yield "小米的chunk2"

    got = list(pf.stream_with_fallback(factory, ARK + ":doubao-seed-2.1-turbo"))
    assert got == ["小米的chunk1", "小米的chunk2"]


def test_stream_never_mixes_two_models(groups):
    """**已吐出一个 chunk 后**失败 → 原样上抛，绝不换通道接着出内容。

    否则用户会看到「火山的前半句 + 小米的后半句」拼成的一条回复。
    """
    used = []

    def factory(m):
        used.append(m)
        yield "火山chunk1"
        raise RuntimeError("中途断了")

    got = []
    with pytest.raises(RuntimeError):
        for c in pf.stream_with_fallback(factory, ARK + ":doubao-seed-2.1-turbo"):
            got.append(c)
    assert got == ["火山chunk1"], "吐出内容后不应再有任何 chunk"
    assert used == [ARK + ":doubao-seed-2.1-turbo"], "不应尝试第二条通道"


def test_stream_lazy_no_call_until_iterated(groups):
    """惰性：生成器未开始迭代前不得触碰任何通道（否则 break/取消会白撞一次）。"""
    touched = []

    def factory(m):
        touched.append(m)
        yield "x"

    gen = pf.stream_with_fallback(factory, ARK + ":doubao-seed-2.1-turbo")
    assert touched == [], "创建生成器不应有副作用"
    list(gen)
    assert touched, "迭代后才该调用"


def test_stream_single_channel_passthrough(groups):
    got = list(
        pf.stream_with_fallback(lambda m: iter(["a", "b"]), "openrouter:x")
    )
    assert got == ["a", "b"]

