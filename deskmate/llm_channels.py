"""LLM 通道组与组内自动路由（2026-10-06 · 设计文档 §二）。

**分工边界**（与 `llm_circuit_breaker` 分开，各自单一职责）：
- `llm_circuit_breaker`：**记账与判定**（这条通道此刻能不能试）
- 本模块：**选哪条通道**（组内自动排 + 两个总开关的手动档）

## 两个总开关（萧哥 21:04 拍板）

| 组 | 成员 | 用途 |
|---|---|---|
| **付费组**（默认） | 火山 `ark-agent-plan-cn` → 腾讯 `tencent-tokenplan:hy3`（小米线 2026-10-10 起暂停排查） | 她对你说的话（对话、主动递卡片） |
| **免费组** | opencode `*-free`（**远端清单驱动**，见 `zen_catalog`） | 后台加工（记忆抽取 / 理解加工 / 发帖草稿） |

- `qwen` **不进链**（早期测试配置，萧哥 21:00 明确不要）
- 手动切粒度 = **A**：切「付费组 / 免费组」，**组内自动排**
  （不提供手动指定具体模型——粒度 A 已够用，UI 更简单）
- 本地与云端**同一套**（同一份代码 + 同一份熔断表，见设计 §3.3）

## 组内排序

**付费组**：按配置顺序（主 → 备），跳过熔断中的。
**免费组**：**非推理模型优先**（后台批量活要速度）—— 推理模型实测 14.3s 才出content
（`zen_catalog.is_reasoning`）；同档内按清单顺序稳定排序。

## 记忆抽取走免费组（萧哥 21:24 拍板）

`DESKMATE_MEMORY_GROUP=free` 时走免费组，默认 `paid`。
原先只有 `DESKMATE_MEMORY_PROVIDER=omniroute` 一条opt-in（且本机 OmniRoute
并未在跑），现已由通道组取代 —— 免费组走 opencode 远端清单，供给更稳。

## 与旧 `DESKMATE_MEMORY_PROVIDER=omniroute` 的兼容

保留：设了它仍走 OmniRoute 免费网关（历史行为不变）。
新路径：设 `DESKMATE_MEMORY_GROUP=free` 走通道组免费组（opencode 远端清单）。
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

#: 付费组（顺序 = 优先级）。qwen 刻意不在此列（早期测试配置，萧哥 21:00 明确不要）。
#: 小米线 2026-10-10 起暂停（排查套餐用量异常）；恢复时把 ("xiaomi","mimo-v2.5-pro") 加回 ark 与 tencent 之间。
PAID_GROUP: Tuple[Tuple[str, str], ...] = (
    ("ark-agent-plan-cn", "deepseek-v4-flash"),
    ("tencent-tokenplan", "hy3"),
)

#: 免费组用远端清单（zen_catalog），此处不硬编码 —— 手工铺货保鲜期只有几天
#: （设计 §1.5：实测当天已漂 5 个模型）。空元组表示「清单未就绪」。

#: 后台任务（记忆抽取/理解加工）默认走哪组。
#: 萧哥 21:24 拍板「记忆抽取走免费」；用环境变量可覆盖，便于回退。
_DEFAULT_MEMORY_GROUP = GROUP_FREE


def memory_group() -> str:
    """后台任务用哪组。

    优先级：环境变量 ``DESKMATE_MEMORY_GROUP`` > prefs.json 的 ``memory_llm_group``
    > 默认免费组。**prefs 兜底是为了跨进程持久**（环境变量只活在当前进程，
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
    """免费组候选：**非推理模型优先**，同档内按远端清单顺序。

    清单来自 `zen_catalog`（远端驱动）；拉不到时用其内置 seed（8 个），
    再拉不到则返回空列表（调用方据此判定「免费组无可用」）。
    """
    try:
        from coworker.providers.zen_catalog import fetch_catalog, is_reasoning
    except Exception:  # noqa: BLE001
        return []
    try:
        raw = fetch_catalog()
    except Exception as e:  # noqa: BLE001
        logger.debug("免费清单拉取失败（免费组将为空）: %s", e)
        return []
    # fetch_catalog 已按远端顺序返回 list（顺序稳定）；不转 set —— 转了就丢顺序。
    free_ids = [m["id"] for m in raw if m.get("id") and m["id"].endswith("-free")]
    # 非推理优先；同档内保持远端清单原序（稳定）。
    # ⚠️ 排序键不能写 `free_ids.index(mid)` —— sort 的 key 在列表被原地改动后
    # 再 index 会 ValueError（2026-10-06 首版踩过：排序到一半的元素已不在表里）。
    order = {mid: i for i, mid in enumerate(free_ids)}
    free_ids.sort(key=lambda mid: (is_reasoning(mid), order[mid]))
    return [Channel(provider="opencode", model=m, free=True) for m in free_ids]


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
