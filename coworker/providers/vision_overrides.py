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

_SETTINGS_PATH = Path.home() / ".deskmate" / "settings.json"
_LOCK = threading.Lock()
_TTL = 30.0
_cache: "tuple[float, frozenset]" | None = None


def load_settings() -> dict:
    """Read ``~/.deskmate/settings.json``; return {} on any failure (missing/corrupt)."""
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
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
