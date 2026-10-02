"""User-declared vision-capable models — the GENERAL vision override layer.

The curated matrix (``matrix.py``) hardcodes ``vision=False`` for the text/agent models
on purpose (owner call, 2026-07-04): it only vouches for models it has verified. But that
means *switching* the chat to any vision model the user has added (a Volcengine doubao-vision
endpoint, Qwen-VL, Gemini vision, a local Llava, …) silently drops images — the engine sees
``caps.vision == False`` and replaces every attachment with a ``[image … not viewable]``
placeholder.

This module is the single, provider-agnostic fix: **any** model id the user lists in
``~/.deskmate/settings.json`` is promoted to vision-capable, regardless of provider. It does
not bind to or hardcode any particular model — the user is the source of truth for "this
model can see".

Settings keys (both honored, ``visionModels`` takes precedence when listing several):
    "visionModels": ["ark-agent-plan-cn:doubao-vision-lite", "qwen:qwen-vl-max", …]
    "visionModel":  "qwen-vl-plus"   # legacy single-model key (also drives screen vision)

Caching: re-reads the settings file at most once per ``_TTL`` seconds so a user who edits
settings mid-session sees the change without a restart, while normal turns don't pay a
filesystem read every call.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

def _settings_path() -> Path:
    """``settings.json`` 的路径 —— **调用时**解析 ``DESKMATE_DIR``。

    ⚠ 历史坑（2026-10-02）：这里曾是模块级常量 ``Path.home()/".deskmate"``，完全不认
    环境变量 ⇒ ``tests/conftest.py`` 的 autouse 隔离（把 ``DESKMATE_DIR`` 指到 tmp）
    对本模块全线失效，单测会读**开发者真机**的 settings.json —— ``test_providers`` 里
    ``doubao-seed-2.1-turbo`` 因此被判成视觉模型，断言 ``not caps.vision`` 恒红。
    同族修复见 ``deskmate/memory/external_store.py``（那里也是先踩后修）。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir() / "settings.json"


_LOCK = threading.Lock()
_TTL = 30.0
_cache: "tuple[float, frozenset]" | None = None


def load_settings() -> dict:
    """Read ``~/.deskmate/settings.json``; return {} on any failure (missing/corrupt)."""
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def vision_override_ids() -> frozenset[str]:
    """Model ids the user has declared as vision-capable. Drives ``capabilities_for``."""
    now = time.time()
    global _cache
    with _LOCK:
        if _cache is not None and (now - _cache[0]) < _TTL:
            return _cache[1]
        data = load_settings()
        ids: set[str] = set()
        for mid in data.get("visionModels") or []:
            if isinstance(mid, str) and mid:
                ids.add(mid)
        single = data.get("visionModel")
        if isinstance(single, str) and single:
            ids.add(single)
        fs = frozenset(ids)
        _cache = (now, fs)
        return fs


def clear_cache() -> None:
    """Drop the cached override set (call after mutating settings at runtime)."""
    global _cache
    with _LOCK:
        _cache = None
