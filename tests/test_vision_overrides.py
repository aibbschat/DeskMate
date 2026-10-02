"""Vision override layer (P2 gate): any user-declared model becomes vision-capable.

This is the root-cause fix for "switching the chat to a vision model silently drops images".
The curated matrix hardcodes vision=False for text/agent models; the override layer promotes
whichever model ids the user lists in settings — provider-agnostic, never bound to one model.
"""

from coworker.providers import capabilities
from coworker.providers import vision_overrides
from coworker.providers.base import ModelCapabilities


def _patch_ids(monkeypatch, ids):
    monkeypatch.setattr(vision_overrides, "vision_override_ids", lambda: frozenset(ids))


def test_override_promotes_curried_text_model(monkeypatch):
    # Isolate this test from real user settings; first establish the matrix default.
    monkeypatch.setattr(vision_overrides, "load_settings", lambda: {})
    vision_overrides.clear_cache()

    # Doubao Seed 2.1 Turbo is a text/agent model (vision=False) in the matrix.
    assert capabilities.capabilities_for("ark-agent-plan-cn:doubao-seed-2.1-turbo").vision is False

    _patch_ids(monkeypatch, {"ark-agent-plan-cn:doubao-seed-2.1-turbo"})
    caps = capabilities.capabilities_for("ark-agent-plan-cn:doubao-seed-2.1-turbo")
    assert caps.vision is True
    # Curated tools/pdf flags are preserved (only vision is flipped).
    assert caps.tools is True


def test_override_promotes_custom_model(monkeypatch):
    _patch_ids(monkeypatch, {"custom:some-vision-model"})
    caps = capabilities.capabilities_for("custom:some-vision-model")
    assert caps.vision is True


def test_no_override_keeps_false(monkeypatch):
    _patch_ids(monkeypatch, set())
    assert capabilities.capabilities_for("ark-agent-plan-cn:doubao-seed-2.0-lite").vision is False


def test_vision_override_ids_reads_settings(monkeypatch):
    monkeypatch.setattr(
        vision_overrides,
        "load_settings",
        lambda: {"visionModels": ["a:b", "c:d"], "visionModel": "e:f"},
    )
    vision_overrides.clear_cache()
    ids = vision_overrides.vision_override_ids()
    assert ids == frozenset({"a:b", "c:d", "e:f"})
    vision_overrides.clear_cache()


def test_resolve_vision_provider_bare_qwen(monkeypatch):
    # Legacy bare id "qwen-vl-plus" must resolve via the provider:qwen profile.
    import coworker.vision_caption as vc
    from coworker import secrets as secrets_mod

    monkeypatch.setattr(
        vision_overrides, "load_settings", lambda: {"visionModel": "qwen-vl-plus"}
    )
    monkeypatch.setattr(
        secrets_mod,
        "SecretStore",
        lambda: _FakeStore({"provider:qwen": {"api_key": "K", "base_url": "U"}}),
    )
    assert vc.resolve_vision_provider() == ("K", "U", "qwen-vl-plus")


def test_resolve_vision_provider_prefixed(monkeypatch):
    import coworker.vision_caption as vc
    from coworker import secrets as secrets_mod

    monkeypatch.setattr(
        vision_overrides,
        "load_settings",
        lambda: {"visionModels": ["qwen:qwen-vl-max"]},
    )
    monkeypatch.setattr(
        secrets_mod,
        "SecretStore",
        lambda: _FakeStore({"provider:qwen": {"api_key": "K", "base_url": "U"}}),
    )
    assert vc.resolve_vision_provider() == ("K", "U", "qwen-vl-max")


def test_resolve_vision_provider_none_without_creds(monkeypatch):
    import coworker.vision_caption as vc
    from coworker import secrets as secrets_mod

    monkeypatch.setattr(
        vision_overrides, "load_settings", lambda: {"visionModel": "some-vl-x"}
    )
    monkeypatch.setattr(secrets_mod, "SecretStore", lambda: _FakeStore({}))
    assert vc.resolve_vision_provider() is None


class _FakeStore:
    def __init__(self, data):
        self._data = data

    def get(self, profile):
        return self._data.get(profile)

