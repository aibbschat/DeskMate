"""Kilo 免费模型动态清单（2026-10-13 · 移植 Our Free Model 插件的「动态侦探」）。

**为什么需要这个模块**（与 ``zen_catalog`` 同一套设计，设计文档 §1.5）：
Kilo 免费池随上游增删（今天实测 16 个 ``isFree:true`` 行，其中
``nemotron-3.5-content-safety`` 是安全分类器不是对话模型，须排除）。手工铺货
保鲜期只有几天，所以**远端清单是唯一真相源**：``GET /api/gateway/models`` 的
``isFree === true`` 切片（免密钥，实测 200）。

## 与 zen_catalog 的差异（及原因）

| | zen_catalog | 本模块 |
|---|---|---|
| 免费判定 | ``id.endswith("-free")`` | ``row.isFree === true``（网关显式声明） |
| 能力字段 | 网关只给 id，能力靠探测登记 | 网关给 ``context_length`` / ``supported_parameters`` / ``architecture.input_modalities``，直接采信 |
| 排除项 | 无（交给熔断） | 排除 content-safety 分类器（非对话模型，进了 picker 点了必错） |
| 探活 | 无（清单即 verdict） | 同左——插件原话 "their presence in the roster is the verdict" |

## 缓存与刷新（「更新」= 模型随上游一起更新）

- 缓存：``<state_dir>/kilo_catalog.json``，TTL **6h**（与 openrouter/zen 一致）
- 刷新：后台线程每 6h 拉一次；**请求路径永不阻塞网络**（读缓存即返回）
- 冷启动：缓存缺失 → 立即返回内置 seed + 后台一次性拉取（几秒后到位）
- 兜底：网络不可达且无缓存 → 用 seed，保证 picker 不空；**seed 永不作为真相源**
- 变更可见：每次成功刷新 diff 出新上架/下架的 id，写一行日志（「更新」留痕）
- 下架清理：刷新后清理**已下架模型**的熔断记录（只清 ``kilo:`` 前缀键，
  付费组人工配置不受影响；与 zen 的 ``_prune_breaker_for_delisted`` 同构）

⚠️ 插件自更新机制（updater.js：Ed25519 签名 manifest 热替换插件代码）**不移植**——
我们的「插件」就是仓库代码，上游变更走 git + deploy_cloud_sync；模型清单的
「随上游更新」由本模块的 6h 刷新承担。

## 🔴 车道开关 KILO_LANE_ENABLED（2026-10-18 萧哥拍板）

opencode 官方把萧哥的**免费使用封了**（实测免费通道一条也用不了，连 opencode
官方客户端都不可用）。萧哥判断：小桔这边**持续的动态探测**（6h 清单刷新 +
冷启动即拉 + 缓存过期即拉）可能让官方一直不放行，故拍板把**外网免费车道全撤**。

处置（与 zen 的 `ZEN_LANE_ENABLED=False` 同构，萧哥「保留代码」口径）：
本模块**代码全部保留**，仅把 `KILO_LANE_ENABLED` 置 False——

- `get_kilo_entries()` 恒返回 `{}` → picker（matrix）不再展示 Kilo 免费模型；
- `fetch_catalog()` 恒返回 `[]` **且不走网络**（连 seed 都不给——picker 不空
  不再是目标，免费组即将清空是有意为之）；
- `start_catalog_refresh()` 直接返回 → 后台 6h 刷新线程不再启动，**零探测**；
- `free_lane_order()` 恒空 → `llm_channels.free_channels()` 只剩本地兜底
  （CVM llama-server，小桔自己的设施，不探测任何官方）。

上游放行后把常量翻回 True 即整体复活，无需改任何调用方（同 zen 的约定）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

import httpx

from .base import ModelCapabilities
from .matrix import ModelEntry

logger = logging.getLogger(__name__)

KILO_MODELS_URL = "https://api.kilo.ai/api/gateway/models"
CACHE_TTL_SECONDS = 6 * 3600
REFRESH_INTERVAL_SECONDS = 6 * 3600
CACHE_FILENAME = "kilo_catalog.json"

#: 免费池的自动路由入口（ listing 里排第一）。排最前：单模型下架不会波及它，
#: 后台加工优先用它最稳。
AUTO_ROUTER_IDS = ("kilo-auto/free", "openrouter/free")

#: 🔴 车道总开关（2026-10-18）：opencode 官方封禁萧哥的免费使用（免费通道一条
#: 也用不了，连官方客户端都不可用），持续动态探测可能让官方一直不放行 ⇒ 外网
#: 免费车道全撤（zen 已于 10-13 同方式关闭，见 zen_catalog.ZEN_LANE_ENABLED）。
#: False = picker 不展示、后台不刷新、fetch_catalog 不走网络恒返 []（免费组仅剩
#: 本地 llama-server 兜底）。代码全部保留；官方放行后翻回 True 即整体复活。
KILO_LANE_ENABLED = False

#: 排除的 id 片段：安全分类器不是对话模型（2026-10-13 实测它在免费池里）。
_EXCLUDE_SUBSTRINGS = ("content-safety",)

#: 免费池默认上下文窗口（个别行缺 context_length 时的兜底，非真相）。
_KILO_DEFAULT_CONTEXT = 131_072


def _cache_path() -> str:
    from ..secrets import state_dir

    return os.path.join(state_dir(), CACHE_FILENAME)


def _seed() -> List[dict]:
    """离线兜底 —— 2026-10-13 实测在池的 15 个对话模型（content-safety 已排除）。

    仅此用途：网络不可达且无缓存时保持 picker 不空。真相源永远是远端清单。
    """
    return [
        {"id": "kilo-auto/free", "isFree": True, "name": "Auto Free", "context_length": 256_000,
         "supported_parameters": ["tools", "reasoning"], "architecture": {"input_modalities": ["text"]}},
        {"id": "stepfun/step-5-preview-free", "isFree": True, "name": "StepFun: Step 5 Preview (free)",
         "context_length": 1_000_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "stealth/glyph-cluster", "isFree": True, "name": "Stealth: Glyph Cluster (free)",
         "context_length": 256_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "inclusionai/ling-3.1-flash", "isFree": True, "name": "inclusionAI: Ling 3.1 Flash",
         "context_length": 262_144, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "dots-studio/dots-3-note-preview:free", "isFree": True, "name": "Dots Studio: Dots3-Note Preview (free)",
         "context_length": 512_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "liquid/lfm-2.5-2.6b:free", "isFree": True, "name": "LiquidAI: LFM2.5-2.6B (free)",
         "context_length": 65_536, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "nvidia/nemotron-3.5-lightning:free", "isFree": True, "name": "NVIDIA: Nemotron 3.5 Lightning (free)",
         "context_length": 1_000_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "thinkingmachines/inkling-small:free", "isFree": True, "name": "Thinking Machines: Inkling Small (free)",
         "context_length": 1_048_576, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "poolside/laguna-s-2.1:free", "isFree": True, "name": "Poolside: Laguna S 2.1 (free)",
         "context_length": 262_144, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "poolside/laguna-xs-2.1:free", "isFree": True, "name": "Poolside: Laguna XS 2.1 (free)",
         "context_length": 262_144, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "cohere/north-mini-code:free", "isFree": True, "name": "Cohere: North Mini Code (free)",
         "context_length": 256_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "isFree": True, "name": "NVIDIA: Nemotron 3 Ultra (free)",
         "context_length": 1_000_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "isFree": True, "name": "NVIDIA: Nemotron 3 Nano Omni (free)",
         "context_length": 256_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "isFree": True, "name": "NVIDIA: Nemotron 3 Super (free)",
         "context_length": 262_144, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text"]}},
        {"id": "openrouter/free", "isFree": True, "name": "OpenRouter Free Models Router",
         "context_length": 200_000, "supported_parameters": ["tools", "reasoning"],
         "architecture": {"input_modalities": ["text", "image"]}},
    ]


def _load_cache() -> Optional[dict]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(rows: List[dict]) -> None:
    try:
        # 原子写（记忆红线：持久化须临时文件 + os.replace）；临时名唯一（pid+线程号）。
        tmp = f"{_cache_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": rows}, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass


def _is_free_row(row: dict) -> bool:
    """Kilo 的免费判定：网关显式 ``isFree === true`` + 排除非对话模型。"""
    if row.get("isFree") is not True:
        return False
    mid = str(row.get("id") or "")
    if not mid:
        return False
    return not any(bad in mid for bad in _EXCLUDE_SUBSTRINGS)


def fetch_catalog(force: bool = False) -> List[dict]:
    """返回远端 Kilo 免费池清单（原始 row 列表，含 id/name/context_length 等）。

    - 🔴 车道关闭时（``KILO_LANE_ENABLED=False``，2026-10-18 起）**恒返回 [] 且
      不走网络**：官方封禁免费使用后不再探测；连 seed 都不返回（picker 不空不再是
      目标，免费组清空是有意为之）。复活 = 常量翻回 True。
    - 缓存新鲜且 ``force=False`` → 直接返回（**不走网络**）
    - 否则拉远端；成功则存缓存、diff 记一行「上架/下架」日志并返回
    - 网络失败 → 有缓存用缓存，否则用 seed
    """
    if not KILO_LANE_ENABLED:
        return []
    cache = _load_cache()
    if not force and cache is not None:
        if time.time() - cache.get("fetched_at", 0) < CACHE_TTL_SECONDS:
            return cache["models"]

    try:
        # 免密钥读清单（实测 200，无 Authorization 头——带了反而 INVALID_TOKEN）。
        resp = httpx.get(
            KILO_MODELS_URL,
            headers={"accept": "application/json", "user-agent": "dsh-our-free-model"},
            timeout=15.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        rows = [r for r in (resp.json().get("data") or []) if _is_free_row(r)]
        if rows:
            _log_roster_diff(cache, rows)
            _save_cache(rows)
            return rows
    except (httpx.HTTPError, ValueError, KeyError):
        pass

    if cache is not None:
        return cache["models"]
    return _seed()


def _log_roster_diff(cache: Optional[dict], rows: List[dict]) -> None:
    """「更新」留痕：刷新成功后 diff 出新上架/下架的 id（无变化不写）。"""
    try:
        old = {r.get("id") for r in (cache or {}).get("models", []) if r.get("id")}
        new = {r.get("id") for r in rows if r.get("id")}
        added = sorted(new - old)
        removed = sorted(old - new)
        if added or removed:
            logger.info(
                "kilo 免费池清单更新：新上架 %s；下架 %s",
                added or "无", removed or "无",
            )
    except Exception:  # noqa: BLE001 — 留痕失败绝不影响刷新本身
        pass


def is_reasoning(row: dict) -> bool:
    """该行是否声明推理能力（``supported_parameters`` 含 ``reasoning``）。

    采信网关声明（与插件 ``kiloEffortMenuFor`` 同源）。免费池基本全员推理模型，
    此标记仅供诊断/排序参考，不参与免费组排序（排序 = 清单序，自动路由天然第一）。
    """
    params = row.get("supported_parameters")
    return isinstance(params, list) and "reasoning" in params


def _pretty_name(row: dict) -> str:
    """``NVIDIA: Nemotron 3.5 Lightning (free)`` → ``Nemotron 3.5 Lightning``。

    与插件 ``kiloDisplayName`` 同规则：剥厂商前缀与 ``(free)`` 后缀；自动路由入口
    给人话名。``org/`` 前缀是路由细节，永远不给 picker 看。
    """
    mid = str(row.get("id") or "")
    if mid == "kilo-auto/free":
        return "Kilo Auto"
    if mid == "openrouter/free":
        return "OpenRouter Auto"
    raw = str(row.get("name") or "").strip() or mid
    # 剥厂商前缀（"NVIDIA: "）与 "(free)" 后缀 —— 与插件 kiloDisplayName 同规则。
    bare = re.sub(r"\s*\(free\)\s*$", "", re.sub(r"^[^:]{1,40}:\s+", "", raw)).strip()
    return bare or mid


def _number(value) -> Optional[int]:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _model_entry(row: dict) -> ModelEntry:
    mid = str(row.get("id") or "")
    params = row.get("supported_parameters") or []
    modalities = (row.get("architecture") or {}).get("input_modalities") or []
    caps = ModelCapabilities(
        tools="tools" in params,
        vision="image" in modalities,
        pdf=False,
        parallel_tool_calls="tools" in params,
        streaming=True,
    )
    return ModelEntry(
        label=f"{_pretty_name(row)} · Kilo 免费",
        caps=caps,
        context_window=_number(row.get("context_length")) or _KILO_DEFAULT_CONTEXT,
    )


def get_kilo_entries() -> Dict[str, ModelEntry]:
    """Kilo 免费模型 entries，键为路由 id（``kilo:<id>``）。

    **请求路径永不阻塞网络**：读缓存即返回；缺失/过期时触发后台刷新
    （冷启动另加一次性填充，几秒内到位）。同 ``get_openrouter_entries`` 的范式。

    🔴 车道关闭时（``KILO_LANE_ENABLED=False``，2026-10-18 起）恒返回 {}：
    opencode 官方封禁免费使用后不再展示/不再探测（原因见模块 docstring）。
    复活 = 把常量翻回 True，无需改任何调用方。
    """
    if not KILO_LANE_ENABLED:
        return {}
    cache = _load_cache()
    missing = cache is None
    stale = cache is not None and (time.time() - cache.get("fetched_at", 0)) >= CACHE_TTL_SECONDS
    if missing or stale:
        _ensure_refresh_thread(oneshot=missing)

    source = cache["models"] if cache is not None else _seed()
    out: Dict[str, ModelEntry] = {}
    for row in source:
        mid = row.get("id")
        if mid and _is_free_row(row):
            out[f"kilo:{mid}"] = _model_entry(row)
    return out


def known_model_ids() -> set:
    """当前清单里的全部 Kilo 免费模型 id（裸名，不含 ``kilo:`` 前缀）。

    🔴 车道关闭时（``KILO_LANE_ENABLED=False``）经 ``fetch_catalog`` 恒返 []，
    本函数随之恒空（熔断表清理因此也不会误判「全下架」）。
    """
    return {r["id"] for r in fetch_catalog() if r.get("id")}


def free_lane_order() -> List[str]:
    """免费组的试用顺序：自动路由入口排最前，其余保持远端清单序。

    实测免费池基本全员 ``reasoning``（排序区分不出快慢），所以不搞「非推理优先」——
    清单序 + 自动路由置顶就是最稳的顺序（zen 那套排序在这里没有信息量）。

    🔴 车道关闭时（``KILO_LANE_ENABLED=False``）经 ``fetch_catalog`` 恒返 []，
    本函数随之恒空 → ``llm_channels.free_channels()`` 只剩本地兜底通道。
    """
    rows = fetch_catalog()
    ids = [r["id"] for r in rows if r.get("id") and _is_free_row(r)]
    head = [i for i in AUTO_ROUTER_IDS if i in ids]
    tail = [i for i in ids if i not in head]
    return head + tail


def stale_models(previous_ids: set) -> List[str]:
    """返回 ``previous_ids`` 中**已不在当前远端清单**的模型 id（下架）。

    供熔断表清理记录。远端不可达时 ``fetch_catalog`` 退回缓存/seed —— 此时不做
    判断（返回空），宁可不清理也不误删（网络故障 ≠ 模型下架）。
    """
    cache = _load_cache()
    if cache is None:
        return []
    current = {
        r.get("id") for r in cache["models"] if r.get("id") and _is_free_row(r)
    }
    return sorted(pid for pid in previous_ids if pid not in current)


# --- background refresh -------------------------------------------------------

_refresh_thread: Optional[threading.Thread] = None
_refresh_lock = threading.Lock()


def _refresh_loop() -> None:
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            fetch_catalog(force=True)
            _prune_breaker_for_delisted()
        except Exception:
            pass


def _prune_breaker_for_delisted() -> None:
    """清单刷新后，清理**已下架模型**的熔断记录（与 zen 同构）。

    两个理由：①下架模型不该再参与轮替；②熔断表会随模型进出无限膨胀。
    只清 ``kilo:`` 前缀的键；**保留 = 现存清单 + 所有非 kilo 键**（付费组/
    opencode 等人工或另一清单驱动，不受本清单影响）。fail-open。
    """
    try:
        from deskmate.llm_circuit_breaker import prune_stale

        current = {f"kilo:{m}" for m in known_model_ids()}
    except Exception:  # noqa: BLE001
        return
    if not current:
        return  # 清单不可用时不做判断（网络故障 ≠ 模型下架）
    try:
        import sqlite3

        from deskmate.llm_circuit_breaker import _db

        db = _db(None)
        if not db.exists():
            return
        with sqlite3.connect(str(db), check_same_thread=False) as c:
            # ⚠️ 取**全表**行，不是只取 kilo: 前缀 —— keep 要靠它保住别的键。
            all_rows = [r[0] for r in c.execute("SELECT channel_key FROM llm_circuit_breaker")]
        if not all_rows:
            return
        keep = current | {k for k in all_rows if not k.startswith("kilo:")}
        n = prune_stale(keep, base_dir=None)
        if n:
            logger.info("kilo 清单刷新：清理 %d 条已下架模型的熔断记录", n)
    except Exception:  # noqa: BLE001
        pass


def _ensure_refresh_thread(oneshot: bool = False) -> None:
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()
    if oneshot:
        threading.Thread(target=lambda: fetch_catalog(force=True), daemon=True).start()


def start_catalog_refresh() -> None:
    """服务器启动时调一次，开始周期刷新。

    🔴 车道关闭时（``KILO_LANE_ENABLED=False``，2026-10-18 起）直接返回：
    外网免费车道全撤后没有可服务的模型，不必每 6h 拉一次清单、不必清理熔断表
    ——**零探测**是这次退役的核心目的（官方放行前不再出现任何 Kilo 流量）。
    """
    if not KILO_LANE_ENABLED:
        return
    _ensure_refresh_thread()
