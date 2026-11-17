"""OpenCode Go（付费订阅网关）接入契约（2026-10-28，2026-11-17 对齐官方全表）。

与 `test_free_lane_removed.py`（免费车道删除契约）的关系：那份锁的是**免费层**
（opencode: / kilo: 前缀、动态清单探测、探针、后台刷新）不许复活；本份锁的是**付费
接入**（provider 名 `opencode-go`、静态模型表、诚实 UA + 会话头）。两者前缀不同
（`opencode-go:` vs `opencode:`），互不冲突——本文件的存在**不**修改删除态契约的任何断言。

萧哥拍板的接入边界：
1. 2026-10-28「不用这么复杂」：只加设置页 provider 入口 + 静态矩阵条目，用户自己填 key；
2. 2026-11-17「一起补上」：官方 Go 列表 34 条，除 beam-free（国内限区，必被拦）与
   muse-spark-1.2/1.3-contributor（训练数据 + Meta 限区）外全部收进矩阵（31 条）；
   免费档（含 iceman，训练数据代价萧哥已知情）只做手动选择，不进后台循环/默认模型；
3. 不动付费组三链路（ark→小米→腾讯）——opencode-go 不入 PAID_GROUP，
   tests/test_free_lane_removed.py::test_paid_group_intact 继续锁三链路；
4. 不做任何动态探测（清单/健康检查/后台刷新一律不许有——封号教训 PITFALLS §P4）。

不碰活库、不发任何网络请求。
"""
from __future__ import annotations

#: 2026-11-17 官方 /console/go#endpoints 的 34 条文本模型，去掉三条刻意排除
#: （beam-free 国内限区 / muse-spark-1.2、1.3-contributor 训练+限区）后的全集。
#: 官方上下架时改这份清单——它是「picker 里应该有什么」的契约，别默默漂。
_GO_CURATED = {
    "grok-4.7",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3.1-pro",
    "kimi-k3",
    "kimi-k2.7-code",
    "longcat-2.0",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m3.1-flash",
    "minimax-m3",
    "qwen4-max",
    "qwen3.8-max",
    "qwen3.8-flash",
    "qwen3.7-plus",
    "hy4",
    "hy4-preview",
    "hy3",
    "step-5-preview",
    "claude-haiku-5-5",
    "gpt-6-luna",
    "gpt-5.6-luna",
    "gemini-4-argon",
    "longcat-2.5-free",
    "hy4-flash-free",
    "iceman-contributor-free",
}


def test_descriptor_registered_with_priced_gateway():
    """设置页有 opencode-go 入口：需要 key、端点预填官方 Go 网关。"""
    from coworker.providers.registry import get_descriptor

    d = get_descriptor("opencode-go")
    assert d is not None and d.needs_key
    assert d.env_key == "OPENCODE_GO_API_KEY"
    base = next(f for f in d.fields if f.key == "base_url")
    assert base.default == "https://opencode.ai/inference/go/openai/v1"
    assert not base.required  # 预填可编辑，空则回落默认
    # 推荐模型必须真在矩阵里（set_provider 自动加购依赖它）
    assert d.recommended_model == "glm-5.3-flash"


def test_free_lane_contract_still_holds():
    """付费接入不得把免费车道契约顶红：opencode/kilo descriptor 仍须为 None。"""
    from coworker.providers.registry import get_descriptor, provider_descriptors

    assert get_descriptor("opencode") is None
    assert get_descriptor("kilo") is None
    names = {d.name for d in provider_descriptors()}
    assert "opencode" not in names and "kilo" not in names
    # 但付费入口必须在
    assert "opencode-go" in names


def test_matrix_entries_are_static_and_agentic():
    """矩阵条目静态手铺：全部 opencode: 前缀无关、全部 agent 能力（tools on）。

    仍不收 beam-free（国内限区，请求必被拦）与 muse-spark-1.2/1.3（训练数据 + 限区）——
    改矩阵时别加回来。iceman 2026-11-17 已由萧哥拍板收下（仅手动选择）。
    """
    from coworker.providers import matrix as M

    go_ids = M.models_for_provider("opencode-go")
    assert go_ids, "opencode-go 静态条目丢失"
    for mid in M.MATRIX:
        assert not mid.startswith("opencode:"), f"免费前缀混入: {mid}"
    for bare in go_ids:
        caps = M.MATRIX[f"opencode-go:{bare}"].caps
        assert caps.tools and caps.streaming, bare
    # 关键排除项（限区红线 / 训练数据 + 限区）
    for banned in ("muse-spark", "beam-free"):
        assert not any(banned in m for m in go_ids), banned


def test_matrix_covers_official_go_catalog():
    """官方 34 条 → 本表 31 条（去掉 beam-free 与 muse-spark 两位）。

    这是「picker 里应该有什么」的快照契约：官方上下架时连同本文件一起改，
    避免模型表悄悄漂移或漏收。
    """
    from coworker.providers import matrix as M

    got = set(M.models_for_provider("opencode-go"))
    assert got == _GO_CURATED, (
        f"矩阵与官方清单不一致：缺 {sorted(_GO_CURATED - got)}，"
        f"多 {sorted(got - _GO_CURATED)}"
    )
    assert len(got) == 31


def test_free_tier_entries_present_and_marked():
    """三个免费档必须在（手动选项），标签要能看出「免费」——避免被当成计费模型。"""
    from coworker.providers import matrix as M

    for bare in ("longcat-2.5-free", "hy4-flash-free", "iceman-contributor-free"):
        entry = M.MATRIX[f"opencode-go:{bare}"]
        assert "免费" in entry.label, bare


def test_builder_sets_honest_ua_and_session_header():
    """合规核心：诚实 UA（不伪装官方客户端）+ 稳定 x-opencode-session 头。

    二者是官方对第三方 client 的硬要求（docs: /console/go#where-can-i-use-it），
    缺了随时可能再吃封号——本断言就是护栏。会话头在**进程内稳定**（官方要的正是
    "stable session ID"，供路由/prompt cache 用），进程重启才换。
    """
    from coworker.providers.registry import build_provider_client

    p = build_provider_client("opencode-go", {"api_key": "oc_sk_x"}, None)
    h = p._default_headers
    assert h["User-Agent"] == "xiaoju/1.0"
    assert h["x-opencode-session"].startswith("ses_")
    # 进程内稳定：再建一个 client 实例，会话头不变
    p2 = build_provider_client("opencode-go", {"api_key": "oc_sk_x"}, None)
    assert p2._default_headers["x-opencode-session"] == h["x-opencode-session"]


def test_builder_uses_own_key_only_never_openai_key(monkeypatch):
    """与其它 compat 厂商同一条红线：OPENAI_API_KEY 绝不静默流向 Go 网关。"""
    import pytest

    from coworker.providers.registry import build_provider_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-real")
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OpenCode Go"):
        build_provider_client("opencode-go", {}, None)
    # env key 兜底
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "oc_sk_env")
    p = build_provider_client("opencode-go", {}, None)
    assert p._api_key == "oc_sk_env"


def test_detect_provider_recognizes_oc_sk_keys():
    from coworker.providers.registry import detect_provider

    assert detect_provider("oc_sk_1a2b3c4d5e6f_...") == "opencode-go"
    # 不误伤其它形状
    assert detect_provider("sk-ant-x") == "anthropic"
    assert detect_provider("sk-x") == "openai"


def test_paid_group_untouched():
    """付费组三链路一个不动（opencode-go 是 picker 手动项，不入自动回退链）。"""
    from deskmate.llm_channels import PAID_GROUP

    assert PAID_GROUP == (
        ("ark-agent-plan-cn", "deepseek-v4-flash"),
        ("xiaomi", "mimo-v2.6-pro"),
        ("tencent-tokenplan", "hy3"),
    )


def test_no_probe_no_catalog_no_refresh():
    """封号教训护栏：opencode-go 不许有任何动态探测面。

    registry 里不许出现 opencode-go 的 /models 后台探针分支（用户点 Test 的
    一次性手动探测除外），matrix 里不许有 opencode-go 的 live catalog 函数。
    """
    import inspect

    from coworker.providers import matrix as M
    from coworker.providers import registry

    # matrix 模块无 opencode-go 动态清单函数（对照已删的 _live_zen 范式）
    live_fns = [
        n
        for n in dir(M)
        if n.startswith("_live") and "opencode" in n.lower()
    ]
    assert live_fns == [], f"不许有 opencode-go 动态清单: {live_fns}"
    # registry 的 verify 函数里，opencode-go 分支只允许一次手动 GET（Test 按钮）
    src = inspect.getsource(registry.verify_provider_key)
    go_section = src.split('name == "opencode-go"')[1].split("elif")[0]
    assert "httpx.get(" in go_section  # 只有一次手动探测
    assert "sleep" not in go_section and "loop" not in go_section  # 无轮询
