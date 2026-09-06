"""L2 治理层配置（自主发起总开关 + 任务白名单）。

独立配置文件 ``~/.deskmate/l2_settings.json`` —— **不碰含 API key 的 settings.json**，
也不污染其他层。所有开关都可在设置区调，避免「每次改代码」。

设计同构意志层（deskmate/will/settings.py）：
- 默认值 fallback：文件缺失 / 字段缺失 / JSON 损坏 → 全用 ``DEFAULTS``
- 原子写：先写临时文件再 ``os.replace``，避免半截文件
- TTL 内存缓存：读盘有成本，30s 内重复读走缓存；写后主动清缓存
- kill switch：``enabled=False`` 或环境变量 ``DESKMATE_L2_ENABLED=0`` 都关
- fail-open：任何异常都返回默认值，绝不抛给调用方
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_KILL_SWITCH_ENV = "DESKMATE_L2_ENABLED"
_CACHE_TTL_SEC = 30

# 默认白名单主题域（萧哥的领域；可改 settings）。world_search 按这些域各搜一次。
DEFAULT_TOPICS: list[str] = [
    "历史研究",
    "文学创作",
    "出版行业",
    "AI 技术动态",
]

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 任务白名单：当前允许小桔自主执行的「动作类型」。风险等级是动作内禀的
    # （见 registry.py），这里只决定「开没开」。
    "whitelist": {
        "actions": ["world_search"],
        "topics": list(DEFAULT_TOPICS),
    },
    # world_search 行为参数
    "world_search": {
        "cooldown_hours": 24,   # 每个主题域两次搜索最小间隔
        "max_results": 5,       # 每次搜索返回条数
        "timelimit": "w",       # 时间窗：近一周（coworker web tool 支持 d/w/m/y）
        "per_tick_limit": 1,    # 每次 tick 最多实际搜几个域（摊薄负载）
    },
}


@dataclass
class AgencySettings:
    """L2 配置（dataclass 便于测试与序列化）。"""

    enabled: bool = True
    whitelist_actions: list[str] = field(default_factory=lambda: list(DEFAULTS["whitelist"]["actions"]))
    topics: list[str] = field(default_factory=lambda: list(DEFAULTS["whitelist"]["topics"]))
    world_search: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS["world_search"]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "whitelist": {
                "actions": list(self.whitelist_actions),
                "topics": list(self.topics),
            },
            "world_search": dict(self.world_search),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgencySettings":
        raw = data or {}
        wl = raw.get("whitelist", {}) or {}
        actions = wl.get("actions", DEFAULTS["whitelist"]["actions"])
        topics = wl.get("topics", DEFAULTS["whitelist"]["topics"])
        if not isinstance(actions, list):
            actions = list(DEFAULTS["whitelist"]["actions"])
        if not isinstance(topics, list):
            topics = list(DEFAULTS["whitelist"]["topics"])
        ws = raw.get("world_search", {}) or {}
        world_search = dict(DEFAULTS["world_search"])
        world_search.update({k: v for k, v in ws.items() if k in DEFAULTS["world_search"]})
        enabled = raw.get("enabled", DEFAULTS["enabled"])
        if not isinstance(enabled, bool):
            enabled = DEFAULTS["enabled"]
        return cls(enabled=enabled, whitelist_actions=list(actions), topics=list(topics), world_search=world_search)


def _default_path(base_dir: str | None = None) -> str:
    if base_dir:
        return os.path.join(base_dir, "l2_settings.json")
    return os.path.join(
        os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")),
        "l2_settings.json",
    )


def _kill_switch_off() -> bool:
    val = os.environ.get(_KILL_SWITCH_ENV)
    return val is not None and val.strip() == "0"


_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()


def _clear_cache() -> None:
    with _cache_lock:
        _cache["data"] = None
        _cache["ts"] = 0.0


def _reset_cache_for_test() -> None:
    """测试用：强制下次读盘（绕开 TTL）。"""
    _clear_cache()


def load_agency_settings(base_dir: str | None = None) -> AgencySettings:
    """读配置。任何异常都回落默认，绝不影响主流程。"""
    if _kill_switch_off():
        # kill switch 优先：强制关闭整个 L2 自主层
        return AgencySettings(enabled=False)
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_SEC:
            return _cache["data"]
    try:
        path = _default_path(base_dir)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = AgencySettings.from_dict(data)
    except FileNotFoundError:
        settings = AgencySettings.from_dict({})
    except Exception:
        settings = AgencySettings.from_dict({})
    with _cache_lock:
        _cache["data"] = settings
        _cache["ts"] = now
    return settings


def save_agency_settings(settings: AgencySettings, base_dir: str | None = None) -> bool:
    """原子写配置。成功返回 True，失败返回 False（不抛）。"""
    path = _default_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _clear_cache()
        return True
    except Exception:
        return False


def reset_agency_settings(base_dir: str | None = None) -> AgencySettings:
    """恢复默认并落盘。"""
    s = AgencySettings.from_dict({})
    save_agency_settings(s, base_dir=base_dir)
    return s
