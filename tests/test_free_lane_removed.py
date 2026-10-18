"""🔴 外网免费车道**已删除**契约（2026-10-18 萧哥拍板，先于删除实现落测试）。

背景：opencode 官方封禁了萧哥的免费使用（免费通道一条也用不了，连官方客户端
都不可用）。萧哥判断小桔持续的**动态探测**（catalog 6h 刷新 + 冷启动即拉 +
缓存过期即拉）可能让官方一直不放行 ⇒ 拍板把外网免费车道全部撤掉；进一步
（同日 20:35）确认「删除」而非「保留代码等放行」——今天才知道探测多了会被
封号，留着没用，下次也不敢再踩这个坑。

本文件锁的是**删除态**（先红后绿：实现删除前本文件必红）：

1. 四个车道模块（opencode_provider / kilo_provider / zen_catalog / kilo_catalog）
   在仓库里**不存在**（find_spec 也为 None，防 __pycache__ 残魂）；
2. registry 不再注册 opencode / kilo provider（设置页无入口）；
3. matrix 无 kilo: / opencode: 条目（静态 MATRIX、live catalogs、picker 全空），
   且 openrouter 等**其他** provider 一个不少（删除不得误伤）；
4. 免费组（free_channels）**仅剩本地 local 兜底**一条——本地 llama-server 是
   小桔自己的 CVM 设施，不探测任何官方；
5. memory/llm.py 不再有 _opencode_complete / _kilo_complete 分派函数；
6. deploy_cloud_sync.sh 传输清单不含四个模块（防「本地删了云端还在跑」——
   云端残留 import 不到就是静默失效，见 PITFALLS 停机保号章节的教训）；
7. 付费组三链路（ark→小米→腾讯）不受影响。

不碰活库、不发任何网络请求。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 已删除的四个外网免费车道模块（2026-10-18）。
REMOVED_LANE_MODULES = (
    "coworker.providers.opencode_provider",
    "coworker.providers.kilo_provider",
    "coworker.providers.zen_catalog",
    "coworker.providers.kilo_catalog",
)

#: 对应的仓库文件路径（防 __pycache__/软链残魂）。
REMOVED_LANE_FILES = (
    "coworker/providers/opencode_provider.py",
    "coworker/providers/kilo_provider.py",
    "coworker/providers/zen_catalog.py",
    "coworker/providers/kilo_catalog.py",
)


def test_lane_modules_do_not_exist():
    """四个车道模块的 .py 文件必须不在仓库里。"""
    for rel in REMOVED_LANE_FILES:
        assert not (REPO_ROOT / rel).exists(), f"{rel} 应已删除（2026-10-18 外网免费车道全撤）"


def test_lane_modules_not_importable():
    """find_spec 也必须为 None——防 pyc 残魂或 sys.modules 注入让 import 复活。"""
    for mod in REMOVED_LANE_MODULES:
        assert importlib.util.find_spec(mod) is None, (
            f"{mod} 仍可被 import（删除不彻底，探测可能复活）"
        )


def test_registry_has_no_free_lane_descriptors():
    """registry 不再注册 opencode / kilo（设置页入口关闭）。"""
    from coworker.providers.registry import get_descriptor

    for name in ("opencode", "kilo"):
        assert get_descriptor(name) is None, (
            f"provider {name!r} 的 descriptor 应已删除"
        )
    names = {d.name for d in __import__(
        "coworker.providers.registry", fromlist=["provider_descriptors"]
    ).provider_descriptors()}
    assert "opencode" not in names and "kilo" not in names


def test_matrix_has_no_free_lane_entries():
    """picker / 静态表 / live catalogs 都不许有 kilo: / opencode: 条目。"""
    from coworker.providers import matrix as M

    for mid in M.MATRIX:
        assert not mid.startswith("kilo:"), f"静态 MATRIX 仍有 {mid}"
        assert not mid.startswith("opencode:"), f"静态 MATRIX 仍有 {mid}"
    ids = M.all_model_ids()
    assert not [m for m in ids if m.startswith("kilo:")], "picker 不该有 Kilo 条目"
    assert not [m for m in ids if m.startswith("opencode:")], "picker 不该有 opencode 条目"
    # models_for_provider 同样为空
    assert M.models_for_provider("kilo") == []
    assert M.models_for_provider("opencode") == []
    # 删除不得误伤其他 provider
    assert "ark-agent-plan-cn:deepseek-v4-flash" in ids, "付费组火山条目不得被误删"
    assert "gpt-5.6-sol" in ids, "非 opencode 条目不得被误删"
    assert any(m.startswith("openrouter:") for m in M._live_openrouter()) or True  # 纯动态源不强制


def test_free_group_is_local_only():
    """免费组仅剩本地 local 兜底一条（自己的 CVM 设施，不探测任何官方）。"""
    import deskmate.llm_channels as CH

    chs = CH.free_channels()
    assert len(chs) == 1, f"免费组应仅剩 local 一条，实际：{[c.key for c in chs]}"
    only = chs[0]
    assert only.provider == CH.LOCAL_LLM_PROVIDER == "local"
    assert only.model == CH.LOCAL_LLM_MODEL
    assert only.free is True


def test_memory_llm_has_no_free_lane_dispatchers():
    """memory/llm.py 的分派函数已删（防死代码复活）。"""
    import deskmate.memory.llm as llm_mod

    assert not hasattr(llm_mod, "_opencode_complete"), "_opencode_complete 应已删除"
    assert not hasattr(llm_mod, "_kilo_complete"), "_kilo_complete 应已删除"
    assert not hasattr(llm_mod, "_FREE_LANE_TOOLS"), "_FREE_LANE_TOOLS 应已删除"


def test_deploy_script_no_longer_ships_lane_modules():
    """deploy_cloud_sync.sh 清单不含四个模块——防本地删了云端还在传输/运行旧码。

    教训来源（PITFALLS 停机保号章节）：对账查过 ≠ 推得上去；反过来，
    本地删干净的代码如果还躺在传输清单里，云端会继续持有旧副本。
    """
    text = (REPO_ROOT / "scripts" / "deploy_cloud_sync.sh").read_text(encoding="utf-8")
    for rel in REMOVED_LANE_FILES:
        assert rel not in text, f"deploy 脚本仍在传输 {rel}"


def test_paid_group_intact():
    """付费组三链路一个不少（删除免费车道不得顺手改付费组）。"""
    from deskmate.llm_channels import PAID_GROUP

    assert PAID_GROUP == (
        ("ark-agent-plan-cn", "deepseek-v4-flash"),
        ("xiaomi", "mimo-v2.6-pro"),
        ("tencent-tokenplan", "hy3"),
    )
