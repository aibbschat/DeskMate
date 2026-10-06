"""OpenCode Zen 免费模型动态清单（2026-10-06 · 移植深证 harness 的「动态侦探」）。

**为什么需要这个模块**（设计文档 §1.5）：
OpenCode Zen 的免费模型是**动态更换**的——某个模型会下架，又有新模型上架。
原先`matrix.py` 里8 个 `*-free` 是手工铺货（注释自述「2026-10-06 重新铺货……
上游已下架（deepseek-v4-flash-free、jev-1.13）未入选」），实测当天就已漂移：
远端 13 个 vs 本地 8 个，其中 `deepseek-v4-flash-free` / `jev-1.13-free`
这两个「注释里写已下架」的又回来了。手工铺货保鲜期 = 几天。

**本模块的权威源**：`GET https://opencode.ai/zen/v1/models`（实测 200，86 个模型，
其中 `id.endswith("-free")` 13 个）。这与 `openrouter_catalog.py` 是同一套模式
（远端权威源 + 6h TTL 缓存 + 后台线程刷新 + 冷启动 seed 兜底），
但**刻意有一处不同**（见下）。

## 与 openrouter_catalog 的差异（及原因）

| | openrouter_catalog | 本模块 |
|---|---|---|
| 免费档过滤 | **白名单制**（`FREE_TIER_ALLOWLIST_EXACT`） | **不用白名单**，远端 `*-free` 即入池 |
| 原因 | 官方标 `pricing=0` 的模型大量实际不可用/ 频繁限流，纯对齐官方会让 picker 被占满且大部分用不了（2026-08-26 萧哥踩坑后加） | Zen 的 `*-free` 列表本身已足够干净（13 个里 5 个是区域封锁/上游问题），「远端清单 + 探活」比白名单更简单，且**新模型能自动进来** |

其余（缓存 TTL、后台刷新、seed 兜底、非阻塞请求路径）完全沿用 harness 的做法。

## 缓存与刷新

-缓存：`<state_dir>/zen_catalog.json`，TTL **6h**（与 openrouter 一致）
- 刷新：后台线程每 6h 拉一次；**请求路径永不阻塞网络**（读缓存即返回）
- 冷启动：缓存缺失 → 立即返回内置 seed + 后台一次性拉取（几秒后到位）
- 兜底：网络不可达且无缓存 → 用 seed，保证 picker 不空
- **seed 沿用`matrix.py` 现有 8 个**，仅作离线兜底，**永不作为真相源**

## 与熔断表的联动

`stale_models()` 返回远端已消失的模型 id，供熔断表清理记录——
否则下架模型的熔断记录会无限膨胀，且下架模型不该再参与轮替。

免费模型能力：实测 `fledge-alpha-free` 是**推理模型**（先吐 `reasoning_content`、
14.3s 才出 `content`），而 `ModelCapabilities` **没有「是否推理模型」这一格**
⇒ 这里用模块级 `REASONING_MODELS` 集合补上，供免费组按速度排序（后台批量活要速度）。
**这是探测结论，不是网关声明** —— 网关模型对象只有 `id/object/created/owned_by`
四个字段，不带能力标记（实测）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

import httpx

from .base import ModelCapabilities
from .matrix import ModelEntry

ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
CACHE_TTL_SECONDS = 6 * 3600
REFRESH_INTERVAL_SECONDS = 6 * 3600
CACHE_FILENAME = "zen_catalog.json"

# Zen 网关的默认上下文窗口（实测 8 个模型均为 262_144；网关模型对象不带该字段）。
# 与 matrix.py 里手填的 262_144 保持一致。
_ZEN_DEFAULT_CONTEXT = 262_144

#: 实测为推理模型的免费模型（2026-10-06 探测：先吐 reasoning_content，14.3s 才出content）。
#: `ModelCapabilities` 无此字段，故在此登记，供按速度排序用。**探测所得，非网关声明。**
REASONING_MODELS = frozenset({"fledge-alpha-free"})

#: 已知区域封锁 / 上游过载的免费模型（2026-10-06 实测403 FreeTierError / 503）。
#: **不排除它们** —— 区域封锁会随出口 IP 变化；它们仍在远端清单里，
#: 由「探活 + 熔断」在运行时处理，比静态排除更准确。
KNOWN_UNAVAILABLE = frozenset({"muse-spark-1.2-contributor-free",
                               "muse-spark-1.3-contributor-free"})


def _cache_path() -> str:
    from ..secrets import state_dir

    return os.path.join(state_dir(), CACHE_FILENAME)


def _seed() -> List[dict]:
    """离线兜底 —— 沿用 matrix.py 现有 8 个手铺模型，**仅此用途**。

    网络不可达且无缓存时用它保持 picker 不空。真相源永远是远端清单。
    """
    return [
        {"id": "space-bunny-free"},
        {"id": "fledge-alpha-free"},
        {"id": "nemotron-3.5-lightning-free"},
        {"id": "nemotron-3-ultra-free"},
        {"id": "mimo-v2.5-free"},
        {"id": "mimo-v2.6-flash-free"},
        {"id": "longcat-2.5-preview-free"},
        {"id": "ling-3.1-flash-free"},
    ]


def _load_cache() -> Optional[dict]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(models: List[dict]) -> None:
    try:
        # 原子写（记忆红线：持久化须临时文件 + os.replace）
        tmp = f"{_cache_path()}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass


def fetch_catalog(force: bool = False) -> List[dict]:
    """返回远端 ``*-free`` 模型清单（原始 dict 列表）。

    - 缓存新鲜且 ``force=False`` → 直接返回（**不走网络**）
    - 否则拉远端；成功则存缓存并返回
    - 网络失败 → 有缓存用缓存，否则用 seed
    """
    cache = _load_cache()
    if not force and cache is not None:
        if time.time() - cache.get("fetched_at", 0) < CACHE_TTL_SECONDS:
            return cache["models"]

    try:
        # 匿名 public key 即可读清单（实测 200）；与 opencode_provider 的匿名口径一致。
        resp = httpx.get(
            ZEN_MODELS_URL,
            headers={"authorization": "Bearer public"},
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        models = [m for m in (resp.json().get("data") or []) if _is_free(m)]
        if models:
            _save_cache(models)
            return models
    except (httpx.HTTPError, ValueError, KeyError):
        pass

    if cache is not None:
        return cache["models"]
    return _seed()


def _is_free(raw: dict) -> bool:
    """Zen 的免费判定：id 以 ``-free`` 结尾。

    不用白名单（与 openrouter 有意不同，见模块 docstring）：Zen 的 `*-free`
    列表本身已足够干净，新模型能自动进来；不可用的交给探活 + 熔断处理。
    """
    mid = raw.get("id")
    return bool(mid) and str(mid).endswith("-free")


def is_reasoning(model_id: str) -> bool:
    """该免费模型是否推理模型（实测登记，见``REASONING_MODELS``）。"""
    return model_id in REASONING_MODELS


def _display_name(model_id: str) -> str:
    """``fledge-alpha-free`` → ``Fledge Alpha``（给 UI 用的可读名）。"""
    stem = model_id[: -len("-free")] if model_id.endswith("-free") else model_id
    return " ".join(w.capitalize() for w in stem.replace("-", " ").split())


def _model_entry(model_id: str) -> ModelEntry:
    # Zen 全部免费模型实测均支持 tools + streaming（2026-10-06 逐个探测）。
    caps = ModelCapabilities(
        tools=True,
        vision=False,
        pdf=False,
        parallel_tool_calls=True,
        streaming=True,
    )
    return ModelEntry(
        label=f"{_display_name(model_id)} · OpenCode 免费",
        caps=caps,
        context_window=_ZEN_DEFAULT_CONTEXT,
    )


def get_zen_entries() -> Dict[str, ModelEntry]:
    """Zen 免费模型 entries，键为路由 id（``opencode:<model>``）。

    **请求路径永不阻塞网络**：读缓存即返回；缺失/过期时触发后台刷新
    （冷启动另加一次性填充，几秒内到位）。同 ``get_openrouter_entries`` 的范式。
    """
    cache = _load_cache()
    missing = cache is None
    stale = cache is not None and (time.time() - cache.get("fetched_at", 0)) >= CACHE_TTL_SECONDS
    if missing or stale:
        _ensure_refresh_thread(oneshot=missing)

    source = cache["models"] if cache is not None else _seed()
    out: Dict[str, ModelEntry] = {}
    for m in source:
        mid = m.get("id")
        if mid and _is_free(m):
            out[f"opencode:{mid}"] = _model_entry(mid)
    return out


def known_model_ids() -> set:
    """当前清单里的全部 ``*-free`` 模型 id（裸名，不含 ``opencode:`` 前缀）。"""
    return {m["id"] for m in fetch_catalog() if m.get("id")}


def stale_models(previous_ids: set) -> List[str]:
    """返回 ``previous_ids`` 中**已不在当前远端清单**的模型 id（下架）。

    供熔断表清理记录：下架模型不该再参与轮替，其熔断记录留着只会让表无限膨胀。
    远端不可达时 ``fetch_catalog`` 退回缓存/seed —— 此时不做判断（返回空），
    宁可不清理也不误删（网络故障≠ 模型下架）。
    """
    cache = _load_cache()
    if cache is None:
        return []  # 无缓存 → 可能刚从冷启动，判不准
    current = {m.get("id") for m in cache["models"] if m.get("id")}
    return sorted(pid for pid in previous_ids if pid not in current)


# --- background refresh -------------------------------------------------------

_refresh_thread: Optional[threading.Thread] = None
_refresh_lock = threading.Lock()


def _refresh_loop() -> None:
    while True:
        time.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            fetch_catalog(force=True)
        except Exception:
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
    """服务器启动时调一次，开始周期刷新。"""
    _ensure_refresh_thread()
