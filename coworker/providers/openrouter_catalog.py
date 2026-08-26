"""Live OpenRouter model catalog.

OpenRouter's free/paid status (and the model list itself) changes constantly, and the
vendor's public ``/api/v1/models`` is the authoritative source. We mirror it at startup
and on a timer instead of hand-curating a static list that goes stale — the old
``matrix.py`` approach let "declared-free but actually-billing" models linger in the
免费 picker because nobody re-verified them.

Design notes (owner call 2026-08-26):
- Free = vendor-declared ``pricing`` 0 (prompt AND completion). We trust OpenRouter's
  ``pricing`` field — the same signal the old static list used — so a model OpenRouter
  still bills in practice will still surface as 免费 here. That is an accepted tradeoff
  for "先用一段时间再说"; a later empirical probe / denylist can subtract known-bad
  slugs without touching this fetch.
- Caching: results are written to ``<state_dir>/openrouter_catalog.json`` with a 6h TTL so
  a settings request never blocks on the network. A background thread refreshes every 6h;
  a cache miss kicks a one-shot background fetch so a cold start returns the seed at once
  and live data arrives within seconds.
- Fallback: if the network is unreachable and no cache exists, a small built-in seed keeps
  the picker populated. The seed is offline resilience ONLY — never a source of truth.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

import httpx

from .base import ModelCapabilities
from .matrix import ModelEntry

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_TTL_SECONDS = 6 * 3600
REFRESH_INTERVAL_SECONDS = 6 * 3600
CACHE_FILENAME = "openrouter_catalog.json"


def _cache_path() -> str:
    from ..secrets import state_dir

    return os.path.join(state_dir(), CACHE_FILENAME)


def _seed() -> list[dict]:
    """Minimal offline fallback — NOT a source of truth, just keeps the picker populated
    when the network is down and no cache exists yet."""
    return [
        {"id": "minimax/minimax-m3:free", "name": "MiniMax M3", "context_length": 1_048_576,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "minimax/minimax-m2.7:free", "name": "MiniMax M2.7", "context_length": 196_608,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "cohere/north-mini-code:free", "name": "North Mini Code", "context_length": 256_000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3.5-lightning:free", "name": "Nemotron 3.5 Lightning", "context_length": 1_000_000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free", "name": "Nemotron 3 Ultra 550B", "context_length": 1_000_000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 3 Super 120B", "context_length": 262_144,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", "name": "Nemotron 3 Nano Omni", "context_length": 256_000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "thinkingmachines/inkling:free", "name": "Inkling", "context_length": 1_048_576,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "thinkingmachines/inkling-small:free", "name": "Inkling Small", "context_length": 1_048_576,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "google/gemma-4-31b-it:free", "name": "Gemma 4 31B", "context_length": 262_144,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "z-ai/glm-5.2:free", "name": "GLM-5.2 免费", "context_length": 256_000,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        {"id": "poolside/laguna-s-2.1:free", "name": "Laguna S 2.1", "context_length": 262_144,
         "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        # OpenRouter's recommended model (descriptor lockstep) — must be in the seed so the
        # picker and the settings auto-add work even fully offline.
        {"id": "z-ai/glm-5.2", "name": "GLM-5.2", "context_length": 1_048_576,
         "pricing": {"prompt": "0.00000119", "completion": "0.00000374"},
         "supported_parameters": ["tools", "parallel_tool_calls"]},
    ]


def _is_free(pricing: Optional[dict]) -> bool:
    if not pricing:
        return False
    return str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0"


def _load_cache() -> Optional[dict]:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(models: list[dict]) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "models": models}, f)
    except OSError:
        pass


def fetch_catalog(force: bool = False) -> list[dict]:
    """Return the raw OpenRouter model list.

    - If a fresh cache exists and ``force`` is False, return it (no network).
    - Otherwise fetch live; on success save + return.
    - On network failure, fall back to cache if present, else the built-in seed.
    """
    cache = _load_cache()
    if not force and cache is not None:
        age = time.time() - cache.get("fetched_at", 0)
        if age < CACHE_TTL_SECONDS:
            return cache["models"]

    try:
        resp = httpx.get(OPENROUTER_MODELS_URL, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        if models:
            _save_cache(models)
            return models
    except (httpx.HTTPError, ValueError):
        pass

    if cache is not None:
        return cache["models"]
    return _seed()


def _model_entry(raw: dict) -> ModelEntry:
    name = raw.get("name") or raw["id"]
    free = _is_free(raw.get("pricing"))
    supported = raw.get("supported_parameters") or []
    caps = ModelCapabilities(
        tools="tools" in supported,
        vision=False,  # OpenRouter free-tier upstreams vary; conservative default
        parallel_tool_calls="parallel_tool_calls" in supported,
        streaming=True,
    )
    ctx = raw.get("context_length")
    label = f"{name} · via OpenRouter" + (" 免费" if free else "")
    return ModelEntry(label=label, caps=caps, context_window=ctx if isinstance(ctx, int) else None)


def get_openrouter_entries() -> Dict[str, ModelEntry]:
    """Live OpenRouter models keyed by full routed id (``openrouter:<id>``).

    Non-blocking on the request path: returns cache/seed immediately and triggers a
    background refresh if the data is missing or stale, so the picker is never empty and
    a settings request never waits on the network.
    """
    cache = _load_cache()
    missing = cache is None
    stale = cache is not None and (time.time() - cache.get("fetched_at", 0)) >= CACHE_TTL_SECONDS
    if missing or stale:
        _ensure_refresh_thread(oneshot=missing)

    source = cache["models"] if cache is not None else _seed()
    return {
        f"openrouter:{m['id']}": _model_entry(m)
        for m in source
        if m.get("id")
    }


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
    """Guarantee the periodic refresh thread is alive; if data was missing, also kick a
    one-shot fill so a cold start gets live data without blocking request handling."""
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()
    if oneshot:
        threading.Thread(target=lambda: fetch_catalog(force=True), daemon=True).start()


def start_catalog_refresh() -> None:
    """Call once at server startup to begin the periodic refresh."""
    _ensure_refresh_thread()
