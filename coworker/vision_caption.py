"""Image-understanding fallback ("composition ability") for non-vision chat models.

Problem: when the user's active chat model lacks native vision (e.g. a text-only agent
model) but they attach an image, the engine used to silently replace the image with a
``[image … not viewable]`` placeholder — the model got nothing. That is a dead end.

Fix: route the attachment through a *configured* vision model and inject a one-line caption
as text, so the chat model still "sees" the picture via composition. Which vision model is
used is entirely settings-driven (``~/.deskmate/settings.json`` → ``visionModels`` /
``visionModel``) — this module hardcodes no provider.

Two guarantees, so this never regresses the no-vision path:
  * If no vision model is configured (or its credentials are missing), ``caption_content``
    returns ``None`` and the engine falls back to the original placeholder.
  * A captioning failure for any single image is contained: that image becomes a short
    "未能生成描述" stub instead of crashing the turn.

The network call is offloaded to a worker thread with a hard timeout, because
``_outbound_messages`` runs inside the async turn loop and must not block the event loop.
"""

from __future__ import annotations

import concurrent.futures
from typing import Optional

from .providers import vision_overrides


def _settings() -> dict:
    return vision_overrides.load_settings()


def resolve_vision_provider() -> Optional[tuple[str, str, str]]:
    """Return ``(api_key, base_url, model)`` for a usable vision model, else ``None``.

    Picks the first declared vision model id and resolves its provider credentials from the
    secret store. Only the model's own provider prefix is trusted — we never call a vision
    endpoint with a model id whose credentials we don't have (that would 400). A known
    provider prefix is stripped for the wire call: the router normally does that for chat
    models, but this helper talks to the provider endpoint directly. A bare Qwen-VL id
    (e.g. the legacy ``visionModel: "qwen-vl-plus"``) falls back to the ``provider:qwen``
    profile, so the existing Qwen vision subscription keeps working for composition.
    """
    from .providers.registry import get_descriptor
    from .secrets import SecretStore

    data = _settings()
    vmodels = data.get("visionModels") or []
    model = vmodels[0] if vmodels else data.get("visionModel")
    if not isinstance(model, str) or not model:
        return None

    store = SecretStore()
    provider = model.split(":", 1)[0] if ":" in model else ""

    # 1) Model carries its own provider prefix → use that provider's credentials.
    # Provider prefixes are runtime routing identifiers, not vendor model names.
    if provider and get_descriptor(provider) is not None:
        profile = store.get(f"provider:{provider}")
        if isinstance(profile, dict) and profile.get("api_key"):
            wire_model = model.split(":", 1)[1]
            return str(profile["api_key"]), str(profile.get("base_url") or ""), wire_model

    # 2) Bare Qwen-VL id → qwen profile (legacy screen-vision default).
    if (not provider or provider == "qwen") and model.lower().startswith("qwen"):
        profile = store.get("provider:qwen")
        if isinstance(profile, dict) and profile.get("api_key"):
            base = str(profile.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1")
            return str(profile["api_key"]), base, model

    return None


def _caption_once(data_url: str, *, model: str, api_key: str, base_url: str,
                  timeout: float) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url=base_url or None,
        timeout=timeout,
    )
    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "用一句话客观描述这张图片里可见的内容，不要猜测图中没有的信息。",
                    },
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _caption_threaded(url: str, *, timeout: float) -> Optional[str]:
    """Run the blocking vision call off the event loop, with a hard wall-clock timeout."""
    resolved = resolve_vision_provider()
    if resolved is None:
        return None
    api_key, base_url, model = resolved
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_caption_once, url, model=model, api_key=api_key,
                        base_url=base_url, timeout=timeout)
        try:
            out = fut.result(timeout=timeout + 5)
        except Exception:
            return None
    return out or None


def caption_content(content, *, timeout: float = 30.0) -> Optional[list]:
    """Replace ``image_url`` parts in a message ``content`` with ``[附图描述：…]`` text parts.

    Returns the new content list when at least one image was captioned, or ``None`` when no
    vision model is configured (caller keeps its original placeholder behavior). A caption
    that fails becomes a short stub so the image is dropped without breaking the turn.
    """
    if not isinstance(content, list):
        return None
    if not any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in content
    ):
        return None
    if resolve_vision_provider() is None:
        return None

    new_parts: list = []
    captioned = False
    for p in content:
        if isinstance(p, dict) and p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            cap = _caption_threaded(url, timeout=timeout) if url else None
            if cap:
                new_parts.append({"type": "text", "text": f"[附图描述：{cap}]"})
                captioned = True
            else:
                new_parts.append({"type": "text", "text": "[图片：未能生成描述]"})
        else:
            new_parts.append(p)
    return new_parts if captioned else None
