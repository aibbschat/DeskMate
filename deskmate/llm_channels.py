"""LLM 通道组与组内自动路由（2026-10-06 · 设计文档 §二）。

**分工边界**（与 `llm_circuit_breaker` 分开，各自单一职责）：
- `llm_circuit_breaker`：**记账与判定**（这条通道此刻能不能试）
- 本模块：**选哪条通道**（组内自动排 + 两个总开关的手动档）

## 两个总开关（萧哥 21:04 拍板）

| 组 | 成员 | 用途 |
|---|---|---|
| **付费组**（默认） | 火山 `ark-agent-plan-cn` → 小米 `xiaomi:mimo-v2.6-pro` → 腾讯 `tencent-tokenplan:hy3`（2026-10-11 萧哥要求把小米加回，恢复 ark→小米→腾讯 三层冗余） | 她对你说的话（对话、主动递卡片） |
| **免费组** | **仅剩本地 `local` 兜底**（CVM llama-server）。opencode / Kilo 外网免费车道已于 2026-10-18 删除，见下节 | 后台加工（发帖草稿 / 理解加工 / 自主层）的本地兜底 |

- `qwen` **不进链**（早期测试配置，萧哥 21:00 明确不要）
- 手动切粒度 = **A**：切「付费组 / 免费组」，**组内自动排**
  （不提供手动指定具体模型——粒度 A 已够用，UI 更简单）
- 本地与云端**同一套**（同一份代码 + 同一份熔断表，见设计 §3.3）

## 组内排序

**付费组**：按配置顺序（主 → 备），跳过熔断中的。
**免费组**：仅剩本地兜底一条（CVM llama-server）。历史（opencode / Kilo
时代）曾是「自动路由入口排最前 + 其余按远端清单序」，随两条车道 2026-10-18
删除作废。

## 🔴 外网免费车道已删除（2026-10-18 萧哥拍板）

opencode 官方封禁了萧哥的免费使用——免费通道一条也用不了，连 opencode 官方
客户端都不可用。萧哥的判断链：10-13 先撤 picker 展示与后台刷新
（`ZEN_LANE_ENABLED=False`）→ 10-18 进一步确认**删除代码**——「今天才知道
探测多了会被封号，留着没用，就算恢复，下次也不敢再踩这个坑」。

| 车道 | 接入日 | 删除日 | 处置 |
|---|---|---|---|
| opencode zen | 2026-10-06 | 2026-10-18 | 整模块删除（provider / catalog / 静态条目 / 探针 / 启动钩子） |
| Kilo | 2026-10-13 | 2026-10-18 | 同上 |

删除范围：`opencode_provider` / `kilo_provider` / `zen_catalog` /
`kilo_catalog` 四个模块（git rm）+ matrix 静态条目 + registry descriptor
与 Test 探针 + app.py 启动钩子 + `memory/llm.py` 的 `_opencode_complete` /
`_kilo_complete` 分派。**本地 llama-server 兜底保留**（见下节）。

删除态契约：`tests/test_free_lane_removed.py`（模块不可 import、registry 无
descriptor、picker 无条目、免费组仅剩 local、deploy 脚本不再传输）。

**免费组现状 = 仅剩本地 `local` 兜底**（CVM 常驻 llama-server，OpenAI 兼容）。
这是小桔自己的设施，**不探测任何官方**，不影响放行；Mac 端无 llama-server →
首次调用失败被熔断跳过，无害。后台任务在 Mac 上因此实际全部走付费组
（免费组无可用 → `chat_memory` 降级回付费组，见 `deskmate/memory/llm.py`）。

## 记忆抽取走付费组（2026-10-13 萧哥「乙」方案改判）

`DESKMATE_MEMORY_GROUP=paid|free` 可覆盖，默认 **`paid`**。
历史：10-06 萧哥拍板「记忆抽取走免费」（省额度）；10-13 opencode 车道整条
死亡（403 FreeTierError，见 `reports/opencode免费车道排查_Kilo车道实测_2026-10-13.md`），
免费组换 Kilo 后萧哥改判「乙」：**Kilo 免费池 prompt 可能被上游记录**，记忆抽取
输入是萧哥的对话片段 → 记忆抽取回付费组（ark→小米→腾讯），Kilo 只跑发帖草稿
这类公开内容。prefs.json 的 `memory_llm_group` 是跨进程持久的手动档。

## 与旧 `DESKMATE_MEMORY_PROVIDER=omniroute` 的兼容

保留：设了它仍走 OmniRoute 免费网关（历史行为不变）。
⚠️ 2026-10-24 实测 OmniRoute 免费层全部上游失效（auto 元路由卡死 40s、
免费模型全 timeout/410/502），该 opt-in 当前无可用上游、等于空转——仅当本机
网关修复/替换为可用免费源后才真正生效。
新路径：设 `DESKMATE_MEMORY_GROUP=free` 走通道组免费组（现仅剩本地
llama-server 兜底；外网免费车道 2026-10-18 已删除）。
两者都设时 `MEMORY_GROUP` 优先（更明确的新语义）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

GROUP_PAID = "paid"
GROUP_FREE = "free"

#: 本地兜底模型（CVM 上常驻 llama-server，见 ``deskmate/memory/llm.py:_local_complete``）。
#: 2026-10-18 起为**免费组唯一成员**（外网免费车道 opencode / Kilo 已删除，
#: 见模块 docstring「外网免费车道已删除」节）：远端免费通道全熔断时，本地
#: Qwen3-Instruct 仍可跑后台加工（记忆抽取 / 反思 / 理解），宁可本地算也不让任务
#: 全挂。Mac 端无 llama-server → 首次调用失败被熔断跳过，无害。
LOCAL_LLM_PROVIDER = "local"
LOCAL_LLM_MODEL = "qwen3-4b-instruct"
LOCAL_LLM_BASE_URL_ENV = "DESKMATE_LOCAL_LLM_BASE_URL"
LOCAL_LLM_BASE_URL_DEFAULT = "http://127.0.0.1:18080/v1"

#: 付费组（顺序 = 优先级）。qwen 刻意不在此列（早期测试配置，萧哥 21:00 明确不要）。
#: 小米线 2026-10-11 恢复（萧哥要求加回，结束用量排查）；现 ark→小米→腾讯 三层冗余，
#: 任一家额度撞顶（如火山周额度 429）都能自动切到下一家，不再单点依赖腾讯兜底。
PAID_GROUP: Tuple[Tuple[str, str], ...] = (
    ("ark-agent-plan-cn", "deepseek-v4-flash"),
    ("xiaomi", "mimo-v2.6-pro"),
    ("tencent-tokenplan", "hy3"),
)

#: （2026-10-18 删除说明）免费组的外网车道曾用 Kilo 远端清单（kilo_catalog，
#: 不硬编码——手工铺货保鲜期只有几天，设计 §1.5）。opencode / Kilo 两条车道
#: 已随外网免费车道全撤**删除**（萧哥拍板），free_lane_order 等入口一并消失；
#: 免费组仅剩本地兜底。

#: 后台任务（发帖草稿/理解加工/自主层）默认走哪组。
#: 2026-10-13 萧哥「乙」方案改判为 **paid**：opencode 车道整条死亡后免费组主力是
#: Kilo，而 Kilo 免费池 prompt 可能被上游记录——记忆抽取输入是萧哥的对话片段，
#: 不能过可能被记录的车道。2026-10-18 Kilo 也随外网免费车道**删除**（非关闭），
#: 免费组仅剩本地兜底（不探测任何官方）；默认值保持 paid 不变。
#: 用环境变量 / prefs.json 的 memory_llm_group 可覆盖，便于回退。
_DEFAULT_MEMORY_GROUP = GROUP_PAID


def memory_group() -> str:
    """后台任务用哪组。

    优先级：环境变量 ``DESKMATE_MEMORY_GROUP`` > prefs.json 的 ``memory_llm_group``
    > 默认**付费组**（2026-10-13 萧哥「乙」：Kilo prompt 可能被上游记录，
    记忆抽取不过免费车道）。**prefs 兜底是为了跨进程持久**（环境变量只活在当前进程，
    Tauri 起的 sidecar 与 CLI/定时任务读到的是不同环境）。
    """
    v = os.environ.get("DESKMATE_MEMORY_GROUP", "").strip().lower()
    if v in (GROUP_PAID, GROUP_FREE):
        return v
    try:
        import json

        from coworker.secrets import state_dir

        prefs = state_dir() / "prefs.json"
        if prefs.is_file():
            data = json.loads(prefs.read_text(encoding="utf-8"))
            pv = str(data.get("memory_llm_group", "")).strip().lower()
            if pv in (GROUP_PAID, GROUP_FREE):
                return pv
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_MEMORY_GROUP


def set_memory_group(group: str) -> None:
    """切后台任务的组（手动档，**进程内立即生效**）。

    持久化请走 ``POST /v1/llm-channels/memory-group``（写 prefs.json，跨进程有效）。
    """
    if group not in (GROUP_PAID, GROUP_FREE):
        raise ValueError(f"未知组：{group!r}（应为 {GROUP_PAID!r} / {GROUP_FREE!r}）")
    os.environ["DESKMATE_MEMORY_GROUP"] = group


@dataclass
class Channel:
    """一条可试的通道。``key`` 形如 ``provider:model``（熔断表的键）。"""

    provider: str
    model: str
    free: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


def paid_channels() -> List[Channel]:
    """付费组候选（按配置顺序，跳过无凭证的 provider）。"""
    out: List[Channel] = []
    for provider, model in PAID_GROUP:
        if _has_credentials(provider):
            out.append(Channel(provider=provider, model=model, free=False))
    return out


def free_channels() -> List[Channel]:
    """免费组候选：**仅剩本地兜底一条**（外网免费车道 2026-10-18 已删除）。

    历史（2026-10-06 ~ 10-18）：自动路由入口排最前 + Kilo 远端清单序 + 本地
    兜底垫底。opencode 车道 10-13 整条 403 死亡；10-18 萧哥进一步拍板**删除**
    而非保留代码——「opencode 官方封禁免费使用，今天才知道探测多了会被封号，
    留着没用，下次也不敢再踩这个坑」。四个车道模块已 git rm
    （opencode_provider / kilo_provider / zen_catalog / kilo_catalog）。

    当前组成：`[local]`（CVM 常驻 llama-server，OpenAI 兼容，
    DESKMATE_LOCAL_LLM_BASE_URL，默认 127.0.0.1:18080）。本地模型是小桔自己的
    设施，不探测任何官方；Mac 端无 llama-server → 首次调用失败被熔断跳过，无害，
    后台任务随后降级回付费组（见 `deskmate/memory/llm.py` 的 group_fallback）。

    删除态契约：`tests/test_free_lane_removed.py::test_free_group_is_local_only`。
    """
    return [Channel(provider=LOCAL_LLM_PROVIDER, model=LOCAL_LLM_MODEL, free=True)]


def channels_for_group(group: str) -> List[Channel]:
    """该组的全部候选（**未滤熔断**——过滤在route 时做）。"""
    if group == GROUP_PAID:
        return paid_channels()
    if group == GROUP_FREE:
        return free_channels()
    raise ValueError(f"未知组：{group!r}")


def route(group: str, *, base_dir=None) -> Tuple[Optional[Channel], List[Channel]]:
    """选出该组下一条**可试**的通道。

    返回 ``(chosen, skipped)``：
    - ``chosen``：第一条非熔断的候选；None = 全组熔尽
    - ``skipped``：被跳过的（通道, 原因），供调用方留审计

    熔断判定读`llm_circuit_breaker.is_open`（跨端共享的那份，见设计 §3.3）。
    """
    from deskmate.llm_circuit_breaker import is_open

    candidates = channels_for_group(group)
    skipped: List[Tuple[Channel, str]] = []
    for ch in candidates:
        if is_open(ch.key, base_dir=base_dir):
            skipped.append((ch, "熔断中"))
            continue
        return ch, skipped
    return None, skipped


def _has_credentials(provider: str) -> bool:
    """该 provider 在 SecretStore 里是否已配key。"""
    try:
        from coworker.secrets import SecretStore

        prof = SecretStore().get(f"provider:{provider}")
        return isinstance(prof, dict) and bool(prof.get("api_key"))
    except Exception:  # noqa: BLE001
        return False


def describe() -> dict:
    """给设置面板/诊断用：两组各有哪些候选、哪些在熔断。"""
    from deskmate.llm_circuit_breaker import is_open

    out = {}
    for group in (GROUP_PAID, GROUP_FREE):
        chs = channels_for_group(group)
        out[group] = {
            "total": len(chs),
            "available": [c.key for c in chs if not is_open(c.key)],
            "open": [c.key for c in chs if is_open(c.key)],
        }
    out["memory_group"] = memory_group()
    return out
