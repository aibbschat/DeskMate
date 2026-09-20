"""意志层（成为我）用户配置。

独立配置文件 ``~/.deskmate/will_settings.json`` —— **不碰含 API key 的
``settings.json``**，也不污染认知画像库。所有阈值都可在设置区调，
避免「每次改代码」。

设计要点：
- 默认值 fallback：文件缺失 / 字段缺失 / JSON 损坏 → 全用 ``DEFAULTS``
- 原子写：先写临时文件再 ``os.replace``，避免半截文件
- TTL 内存缓存：读盘有成本，30s 内重复读走缓存；PUT 后主动清缓存
- kill switch：``enabled=False`` 或环境变量 ``DESKMATE_WILL_ENABLED=0`` 都关
- fail-open：任何异常都返回默认值，绝不抛给调用方
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_KILL_SWITCH_ENV = "DESKMATE_WILL_ENABLED"
_CACHE_TTL_SEC = 30

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "thresholds": {
        # W2a：今日情绪为压力/低落时，打扰间隔倍数上限（>1 = 更克制）
        "emotional_proactive_frequency_max": 1.4,
        # W2b-1：认知高置信立场（≥0.7 且命中本轮领域）→ 阈值下调（更敢主动）
        "cognitive_threshold_delta_pos": 0.15,
        # W2b-1：认知存在 open gap（盲区）→ 阈值上调（更克制）
        "cognitive_threshold_delta_neg": 0.15,
        # W2b-2：价值观排序前 3 有 tradeoff 记录 → 记忆预算倍数
        "memory_budget_ratio": 1.1,
        # W2b-3：语气追加句长度上限（字节/字符，统一按字符截断）
        "tone_prompt_max_len": 80,
    },
}

# 各阈值的合法范围（前端滑块 + 后端校验用），超出则 clamp
_THRESHOLD_BOUNDS: dict[str, tuple[float, float]] = {
    "emotional_proactive_frequency_max": (1.0, 3.0),
    "cognitive_threshold_delta_pos": (0.0, 0.5),
    "cognitive_threshold_delta_neg": (0.0, 0.5),
    "memory_budget_ratio": (1.0, 2.0),
    "tone_prompt_max_len": (0, 200),
}


@dataclass
class WillSettings:
    """意志层配置（dataclass 便于测试与序列化）。"""

    enabled: bool = True
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULTS["thresholds"]))

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "thresholds": dict(self.thresholds)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WillSettings":
        raw_thr = data.get("thresholds", {}) or {}
        thr: dict[str, float] = {}
        for key, default in DEFAULTS["thresholds"].items():
            val = raw_thr.get(key, default)
            if not isinstance(val, (int, float)):
                val = default
            lo, hi = _THRESHOLD_BOUNDS.get(key, (0.0, 1e9))
            thr[key] = max(lo, min(hi, float(val)))
        enabled = data.get("enabled", DEFAULTS["enabled"])
        if not isinstance(enabled, bool):
            enabled = DEFAULTS["enabled"]
        return cls(enabled=enabled, thresholds=thr)


def _default_path(base_dir: str | None = None) -> str:
    if base_dir:
        return os.path.join(base_dir, "will_settings.json")
    return os.path.join(
        os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")),
        "will_settings.json",
    )


def _kill_switch_off() -> bool:
    val = os.environ.get(_KILL_SWITCH_ENV)
    return val is not None and val.strip() == "0"


_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _reset_cache_for_test() -> None:
    """测试用：强制下次读盘（绕开 TTL）。"""
    _clear_cache()


def load_will_settings(base_dir: str | None = None) -> WillSettings:
    """读配置。任何异常都回落默认，绝不影响主流程。"""
    if _kill_switch_off():
        # kill switch 优先：强制关闭整个意志层
        return WillSettings(enabled=False)
    key = base_dir or ""
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and now - entry["ts"] < _CACHE_TTL_SEC:
            return entry["data"]
    try:
        path = _default_path(base_dir)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = WillSettings.from_dict(data)
    except FileNotFoundError:
        settings = WillSettings.from_dict({})
    except Exception:
        # JSON 损坏 / 权限问题 → 默认开启但阈值全默认
        settings = WillSettings.from_dict({})
    with _cache_lock:
        _cache[key] = {"data": settings, "ts": now}
    return settings


def save_will_settings(settings: WillSettings, base_dir: str | None = None) -> bool:
    """原子写配置。成功返回 True，失败返回 False（不抛）。"""
    path = _default_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _cache.pop(base_dir or "", None)
        return True
    except Exception:
        return False


def reset_will_settings(base_dir: str | None = None) -> WillSettings:
    """恢复默认并落盘。"""
    s = WillSettings.from_dict({})
    save_will_settings(s, base_dir=base_dir)
    return s
