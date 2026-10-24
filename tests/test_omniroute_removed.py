"""🔴 OmniRoute 免费网关**已删除**契约（2026-10-24 实测确认后落测试）。

实测（2026-10-24 19:2x）：localhost:20128 进程在（pid）、端口在听、/v1/models 正常返回
模型列表，但所有免费上游全部失效——
  - `auto` 元路由：HTTP=000、卡满 40s 无响应（网关找不到任何可用上游 → 挂死）；
  - `auto/best-free` / `coding:free` / `chat` / `cheap` / `offline`：全部 timeout（20s 无响应）；
  - `auto/mimo`：400（上游 mimo-v2.5-free 已 410 下线）；
  - `auto/glm`：502（上游 GLM 挂了）；
  - `auto/llama`：400（组合里无任何可用候选）。
与用户「偶尔去用一下都不通」的体验完全一致 ⇒ 免费层已全死，移除该 provider + 模型项。

本文件锁删除态（先红后绿：实现删除前本文件必红）：
1. registry 不再注册 omniroute provider（设置页无入口）；
2. registry 无 _build_omniroute 函数（防死代码复活）；
3. matrix 无 omniroute: 条目（静态 MATRIX、picker 全空）；
4. 其他 provider 一个不少（ark / xiaomi / tencent / ollama 等不得误删）；
5. 付费组三链路（ark→小米→腾讯）不受影响。

不碰活库、不发任何网络请求。
"""
from __future__ import annotations

from coworker.providers.registry import get_descriptor


def test_registry_has_no_omniroute_descriptor():
    """registry 不再注册 omniroute（设置页入口关闭）。"""
    assert get_descriptor("omniroute") is None, "provider 'omniroute' 的 descriptor 应已删除"


def test_registry_has_no_build_omniroute():
    """registry 不再有 _build_omniroute（防死代码复活）。"""
    import coworker.providers.registry as R

    assert not hasattr(R, "_build_omniroute"), "_build_omniroute 应已删除（死代码）"


def test_matrix_has_no_omniroute_entries():
    """picker / 静态表都不许有 omniroute: 条目。"""
    from coworker.providers import matrix as M

    for mid in M.MATRIX:
        assert not mid.startswith("omniroute:"), f"静态 MATRIX 仍有 {mid}"
    ids = M.all_model_ids()
    assert not [m for m in ids if m.startswith("omniroute:")], "picker 不该有 omniroute 条目"
    assert M.models_for_provider("omniroute") == []
    # 删除不得误伤其他 provider（ark / xiaomi / tencent 等静态条目仍在）
    assert "ark-agent-plan-cn:deepseek-v4-flash" in ids, "付费组火山条目不得被误删"
    assert "xiaomi:mimo-v2.6-pro" in ids, "小米条目不得被误删"
    assert "tencent-tokenplan:hy3" in ids, "腾讯条目不得被误删"


def test_paid_group_intact():
    """付费组三链路一个不少（删除 OmniRoute 不得顺手改付费组）。"""
    from deskmate.llm_channels import PAID_GROUP

    assert PAID_GROUP == (
        ("ark-agent-plan-cn", "deepseek-v4-flash"),
        ("xiaomi", "mimo-v2.6-pro"),
        ("tencent-tokenplan", "hy3"),
    )
