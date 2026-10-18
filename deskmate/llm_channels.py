"""LLM 通道组与组内自动路由（2026-10-06 · 设计文档 §二）。

**分工边界**（与 `llm_circuit_breaker` 分开，各自单一职责）：
- `llm_circuit_breaker`：**记账与判定**（这条通道此刻能不能试）
- 本模块：**选哪条通道**（组内自动排 + 两个总开关的手动档）

## 两个总开关（萧哥 21:04 拍板）

| 组 | 成员 | 用途 |
|---|---|---|
| **付费组**（默认） | 火山 `ark-agent-plan-cn` → 小米 `xiaomi:mimo-v2.6-pro` → 腾讯 `tencent-tokenplan:hy3`（2026-10-11 萧哥要求把小米加回，恢复 ark→小米→腾讯 三层冗余） | 她对你说的话（对话、主动递卡片） |
| **免费组** | **仅剩本地 `local` 兜底**（CVM llama-server）。外网免费车道（opencode / Kilo）已于 2026-10-13 / 10-18 先后全撤，见下节 | 后台加工（发帖草稿 / 理解加工 / 自主层）的本地兜底 |

- `qwen` **不进链**（早期测试配置，萧哥 21:00 明确不要）
- 手动切粒度 = **A**：切「付费组 / 免费组」，**组内自动排**
  （不提供手动指定具体模型——粒度 A 已够用，UI 更简单）
- 本地与云端**同一套**（同一份代码 + 同一份熔断表，见设计 §3.3）

## 组内排序

**付费组**：按配置顺序（主 → 备），跳过熔断中的。
**免费组**：外网免费车道全撤后**只剩本地兜底**一条（CVM llama-server）。
历史上（opencode / Kilo 时代）曾是「自动路由入口排最前 + 其余按远端清单序」，
随两条车道退役作废，代码保留在各 catalog 模块的车道开关后。

## 🔴 外网免费车道全撤（2026-10-18 萧哥拍板）

opencode 官方封禁了萧哥的免费使用——免费通道一条也用不了，连 opencode 官方
客户端都不可用。萧哥判断：小桔持续的**动态探测**（catalog 6h 刷新 + 冷启动
即拉 + 缓存过期即拉）可能让官方一直不放行，故拍板把外网免费车道全部撤掉：

| 车道 | 退役日 | 开关 | 现状 |
|---|---|---|---|
| opencode zen | 2026-10-13 | `zen_catalog.ZEN_LANE_ENABLED=False` | picker 不展示、后台不刷新 |
| Kilo | 2026-10-18 | `kilo_catalog.KILO_LANE_ENABLED=False` | 同上；`fetch_catalog` 恒返 [] 不走网络 |

两条车道的**代码全部保留**（provider / catalog / 分派分支），官方放行后把
常量翻回 True 即整体复活，无需改任何调用方。

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
新路径：设 `DESKMATE_MEMORY_GROUP=free` 走通道组免费组（Kilo 远端清单）。
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
#: 作为免费组最末兜底：所有远端 opencode 免费通道熔断时，本地 Qwen3-Instruct 仍可跑后台加工
#: （记忆抽取 / 反思 / 理解），宁可本地算也不让任务全挂。Mac 端无 llama-server → 首次调用
#: 失败被熔断跳过，无害。
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

#: 免费组的外网车道曾用 Kilo 远端清单（kilo_catalog，不硬编码——手工铺货保鲜期
#: 只有几天，设计 §1.5）。2026-10-18 外网免费车道全撤（KILO_LANE_ENABLED=False），
#: free_lane_order() 恒空，免费组仅剩本地兜底；复活 = 翻回两个 catalog 的车道开关。

#: 后台任务（发帖草稿/理解加工/自主层）默认走哪组。
#: 2026-10-13 萧哥「乙」方案改判为 **paid**：opencode 车道整条死亡后免费组主力是
#: Kilo，而 Kilo 免费池 prompt 可能被上游记录——记忆抽取输入是萧哥的对话片段，
#: 不能过可能被记录的车道。Kilo 只跑发帖草稿/理解加工/自主层（公开内容）。
#: 2026-10-18 Kilo 也随外网免费车道全撤（KILO_LANE_ENABLED=False），免费组
#: 仅剩本地兜底；默认值保持 paid 不变。
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
    """免费组候选：**外网免费车道全撤后只剩本地兜底一条**。

    历史（2026-10-06 ~ 10-18）：自动路由入口排最前 + Kilo 远端清单序 + 本地兜底垫底。
    opencode 车道 10-13 整条死亡（zen_catalog.ZEN_LANE_ENABLED=False）；
    Kilo 车道 10-18 随外网免费车道全撤（kilo_catalog.KILO_LANE_ENABLED=False，
    `free_lane_order()` 恒空且不走网络）——opencode 官方封禁免费使用后，
    不能再有任何动态探测，等官方放行。

    当前组成：`[local]`（CVM 常驻 llama-server，OpenAI 兼容，
    DESKMATE_LOCAL_LLM_BASE_URL，默认 127.0.0.1:18080）。本地模型是小桔自己的
    设施，不探测任何官方；Mac 端无 llama-server → 首次调用失败被熔断跳过，无害，
    后台任务随后降级回付费组（见 `deskmate/memory/llm.py` 的 group_fallback）。

    复活路径：把两个 catalog 的车道开关翻回 True，本函数自动恢复「远端清单 +
    本地兜底」的组成，无需改这里。
    """
    try:
        from coworker.providers.kilo_catalog import free_lane_order
    except Exception:  # noqa: BLE001
        return []
    try:
        free_ids = free_lane_order()
    except Exception as e:  # noqa: BLE001
        logger.debug("Kilo 清单拉取失败（免费组将只剩本地兜底）: %s", e)
        free_ids = []
    chs = [Channel(provider="kilo", model=m, free=True) for m in free_ids]
    # 本地兜底通道（2026-10-11 萧哥拍板；2026-10-18 起为免费组唯一成员）：CVM 常驻
    # llama-server（OpenAI 兼容，trust_env=False 防代理劫持 localhost，见
    # deskmate/memory/llm._local_complete）。外网免费车道全撤后，它是免费组仅剩的
    # 候选——本地算力跑后台加工（发帖草稿 / 反思 / 理解），不碰任何官方端点。
    chs.append(Channel(provider=LOCAL_LLM_PROVIDER, model=LOCAL_LLM_MODEL, free=True))
    return chs


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
