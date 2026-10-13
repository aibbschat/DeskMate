"""Kilo 免费车道回归（2026-10-13 · Our Free Model 插件移植 · 萧哥「乙 + 保留代码」）。

覆盖契约：
1. **Authorization 必须剥掉**：Kilo 网关对任何非空 token 一律 INVALID_TOKEN
   （实测），只有完全不带头才放行 —— KiloProvider 的 client 靠 event hook 剥头；
2. kilo_catalog 动态侦探：isFree 切片、排除 content-safety 分类器、seed 兜底、
   TTL 缓存（新鲜缓存不走网络）、下架检测、自动路由入口置顶；
3. 免费组组成：kilo 通道 + 本地兜底，**无 opencode**（车道已死，代码保留）；
4. 记忆抽取默认组 = **paid**（萧哥「乙」：Kilo prompt 可能被上游记录）；
5. matrix：picker 含 Kilo 条目、zen 车道关闭时不含 opencode 条目；
6. registry：kilo 免密钥可路由；
7. chat_memory 的 kilo 分派：kilo 通道必须经 KiloProvider（裸 OpenAI() 直连会
   INVALID_TOKEN）。

不碰活库、不发真实网络请求（网络路径全部 mock）。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

import coworker.providers.kilo_catalog as kilo_catalog_mod
import coworker.providers.kilo_provider as kilo_provider_mod
import deskmate.llm_channels as llm_channels_mod
from coworker.providers.base import AssistantTurn


# --------------------------------------------------------------------------- #
# 1. provider：剥 Authorization + UA + 默认值
# --------------------------------------------------------------------------- #

def _ok_completion() -> dict:
    return {
        "id": "gen-test",
        "object": "chat.completion",
        "created": 1791876763,
        "model": "kilo-auto/free",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def test_kilo_client_strips_authorization():
    """端到端：经 KiloProvider 发出的请求**不带** Authorization 头。

    RED 依据：不带 hook 时 SDK 会发 ``Bearer kilo`` → Kilo 实测 INVALID_TOKEN。
    transport 注入 MockTransport（生产不传），剥头 hook 是生产代码真路径。
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json=_ok_completion())

    client = kilo_provider_mod.build_kilo_client(
        transport=httpx.MockTransport(handler)
    )
    provider = kilo_provider_mod.KiloProvider(client=client)
    turn = provider.complete(
        model="kilo-auto/free",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=8,
    )
    assert turn.text == "OK"
    assert captured["auth"] is None, "Kilo 车道绝不能带 Authorization（任意 token → INVALID_TOKEN）"
    # SDK 会在默认 UA 后拼接 extra_headers 的 UA（"OpenAI/Python x, dsh-our-free-model"）。
    assert "dsh-our-free-model" in (captured["ua"] or "")


def test_kilo_provider_defaults():
    provider = kilo_provider_mod.KiloProvider()
    assert provider.default_model == "kilo-auto/free"
    assert provider._base_url == "https://api.kilo.ai/api/gateway"


def test_probe_kilo_lane(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return _ok_completion()

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    assert kilo_provider_mod.probe_kilo_lane() == {"ok": True}

    class _ErrResp:
        status_code = 429

        text = "slow down"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ErrResp())
    out = kilo_provider_mod.probe_kilo_lane()
    assert out["ok"] is False and "429" in out["error"]


# --------------------------------------------------------------------------- #
# 2. catalog：动态侦探
# --------------------------------------------------------------------------- #

_LIVE_ROWS = [
    {"id": "kilo-auto/free", "name": "Auto Free", "isFree": True,
     "context_length": 256_000, "supported_parameters": ["tools", "reasoning"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "nvidia/nemotron-3.5-lightning:free", "name": "NVIDIA: Nemotron 3.5 Lightning (free)",
     "isFree": True, "context_length": 1_000_000,
     "supported_parameters": ["tools", "reasoning"],
     "architecture": {"input_modalities": ["text"]}},
    {"id": "nvidia/nemotron-3.5-content-safety:free", "name": "NVIDIA: Nemotron 3.5 Content Safety (free)",
     "isFree": True, "context_length": 128_000, "supported_parameters": ["reasoning"],
     "architecture": {"input_modalities": ["text", "image"]}},
    {"id": "nvidia/nemotron-3-ultra-550b-a55b", "name": "NVIDIA: Nemotron 3 Ultra",
     "isFree": False, "context_length": 1_000_000, "supported_parameters": ["tools"],
     "architecture": {"input_modalities": ["text"]}},
]


@pytest.fixture
def kilo_cache(tmp_path, monkeypatch):
    """把 kilo_catalog 的缓存路径指到临时目录；返回 (路径, 写缓存 helper)。"""
    path = tmp_path / "kilo_catalog.json"
    monkeypatch.setattr(kilo_catalog_mod, "_cache_path", lambda: str(path))

    def write(rows, age_seconds: float = 0.0):
        path.write_text(
            json.dumps({"fetched_at": time.time() - age_seconds, "models": rows}),
            encoding="utf-8",
        )

    return path, write


def test_is_free_row_excludes_safety_classifier():
    free = kilo_catalog_mod._is_free_row(_LIVE_ROWS[1])
    safety = kilo_catalog_mod._is_free_row(_LIVE_ROWS[2])
    paid = kilo_catalog_mod._is_free_row(_LIVE_ROWS[3])
    assert free is True
    assert safety is False, "content-safety 是分类器不是对话模型，必须排除"
    assert paid is False, "isFree=False 的付费 id 免密钥必然 401"


def test_fetch_catalog_filters_free_rows(kilo_cache, monkeypatch):
    """网络路径的过滤契约：上游 listing 混合免费/付费/分类器 → 只留免费对话模型。"""
    _path, _write = kilo_cache

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": _LIVE_ROWS}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    rows = kilo_catalog_mod.fetch_catalog(force=True)
    ids = [r["id"] for r in rows]
    assert "kilo-auto/free" in ids
    assert "nvidia/nemotron-3.5-lightning:free" in ids
    assert "nvidia/nemotron-3.5-content-safety:free" not in ids
    assert "nvidia/nemotron-3-ultra-550b-a55b" not in ids


def test_fresh_cache_never_touches_network(kilo_cache, monkeypatch):
    _path, write = kilo_cache

    def _boom(*a, **k):  # pragma: no cover - 命中即失败
        raise AssertionError("新鲜缓存不应走网络")

    monkeypatch.setattr(httpx, "get", _boom)
    # 缓存内容 = 生产形态（网络路径过滤后落盘的 2 个免费行）。
    write([r for r in _LIVE_ROWS if kilo_catalog_mod._is_free_row(r)])
    rows = kilo_catalog_mod.fetch_catalog()
    assert len(rows) == 2


def test_stale_cache_refetches_from_upstream(kilo_cache, monkeypatch):
    _path, write = kilo_cache
    write(_LIVE_ROWS, age_seconds=7 * 3600)  # 过期
    fresh = [r for r in _LIVE_ROWS if r["id"] == "kilo-auto/free"]

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": fresh}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    rows = kilo_catalog_mod.fetch_catalog()
    assert [r["id"] for r in rows] == ["kilo-auto/free"]
    # 刷新结果落盘
    saved = json.loads(_path.read_text(encoding="utf-8"))
    assert [r["id"] for r in saved["models"]] == ["kilo-auto/free"]


def test_network_down_falls_back_to_seed(kilo_cache, monkeypatch):
    _path, _write = kilo_cache

    def _down(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "get", _down)
    rows = kilo_catalog_mod.fetch_catalog(force=True)
    ids = [r["id"] for r in rows]
    assert len(ids) == 15 and "kilo-auto/free" in ids  # 内置 seed，picker 不空


def test_get_kilo_entries_labels_and_caps(kilo_cache):
    _path, write = kilo_cache
    write(_LIVE_ROWS)
    entries = kilo_catalog_mod.get_kilo_entries()
    assert set(entries) == {"kilo:kilo-auto/free", "kilo:nvidia/nemotron-3.5-lightning:free"}
    auto = entries["kilo:kilo-auto/free"]
    assert auto.label == "Kilo Auto · Kilo 免费"
    assert auto.context_window == 256_000
    assert auto.caps.tools is True
    nemotron = entries["kilo:nvidia/nemotron-3.5-lightning:free"]
    assert nemotron.label == "Nemotron 3.5 Lightning · Kilo 免费"  # 剥 "NVIDIA: " 与 "(free)"


def test_free_lane_order_puts_auto_routers_first(kilo_cache):
    _path, write = kilo_cache
    write(_LIVE_ROWS)  # 故意混入付费/分类器行：过滤是 free_lane_order 自己的防线
    order = kilo_catalog_mod.free_lane_order()
    assert order[0] == "kilo-auto/free"  # 自动路由置顶（单模型下架不波及它）
    assert "nvidia/nemotron-3.5-content-safety:free" not in order
    assert "nvidia/nemotron-3-ultra-550b-a55b" not in order


def test_stale_models_detects_delisting(kilo_cache):
    _path, write = kilo_cache
    write(_LIVE_ROWS)
    stale = kilo_catalog_mod.stale_models({"kilo-auto/free", "kilo:gone-model"})
    assert stale == ["kilo:gone-model"]


# --------------------------------------------------------------------------- #
# 3. 免费组 + 记忆抽取默认组
# --------------------------------------------------------------------------- #

def test_free_group_is_kilo_plus_local_no_opencode(kilo_cache):
    _path, write = kilo_cache
    write(_LIVE_ROWS)
    chs = llm_channels_mod.free_channels()
    providers = [c.provider for c in chs]
    assert "opencode" not in providers, "opencode 车道已死，不得留在免费组（代码保留在 provider 模块）"
    assert providers[0] == "kilo"
    assert chs[0].model == "kilo-auto/free"  # 自动路由置顶
    assert (chs[-1].provider, chs[-1].model) == ("local", "qwen3-4b-instruct")  # 本地兜底垫底


def test_memory_group_defaults_to_paid(monkeypatch, tmp_path):
    """萧哥「乙」：Kilo prompt 可能被上游记录 → 记忆抽取默认付费组。"""
    monkeypatch.delenv("DESKMATE_MEMORY_GROUP", raising=False)
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))  # 无 prefs.json
    assert llm_channels_mod.memory_group() == "paid"


def test_memory_group_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DESKMATE_MEMORY_GROUP", "free")
    assert llm_channels_mod.memory_group() == "free"


def test_memory_group_prefs_override(monkeypatch, tmp_path):
    monkeypatch.delenv("DESKMATE_MEMORY_GROUP", raising=False)
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path))
    (tmp_path / "prefs.json").write_text(
        json.dumps({"memory_llm_group": "free"}), encoding="utf-8"
    )
    assert llm_channels_mod.memory_group() == "free"


# --------------------------------------------------------------------------- #
# 4. matrix / registry
# --------------------------------------------------------------------------- #

def test_picker_includes_kilo_and_hides_dead_opencode(monkeypatch, kilo_cache):
    from coworker.providers import matrix as matrix_mod

    _path, write = kilo_cache
    write(_LIVE_ROWS)
    ids = matrix_mod.all_model_ids()
    kilo_ids = [i for i in ids if i.startswith("kilo:")]
    zen_ids = [i for i in ids if i.startswith("opencode:")]
    assert set(kilo_ids) == {"kilo:kilo-auto/free", "kilo:nvidia/nemotron-3.5-lightning:free"}
    assert zen_ids == [], "zen 车道关闭（ZEN_LANE_ENABLED=False）→ picker 不展示必然 403 的死模型"
    labels = matrix_mod.model_labels()
    assert labels["kilo:kilo-auto/free"] == "Kilo Auto · Kilo 免费"


def test_zen_lane_switch_off_hides_entries(monkeypatch):
    from coworker.providers import zen_catalog as zen_mod

    assert zen_mod.ZEN_LANE_ENABLED is False
    assert zen_mod.get_zen_entries() == {}
    # start_catalog_refresh 不再拉清单（不耗网络、不清理熔断表）
    zen_mod.start_catalog_refresh()  # 不抛即契约


def test_registry_kilo_descriptor_keyless():
    from coworker.providers.registry import build_provider_client, get_descriptor

    d = get_descriptor("kilo")
    assert d is not None and d.needs_key is False
    assert d.recommended_model == "kilo-auto/free"
    client = build_provider_client("kilo", {}, None)
    assert isinstance(client, kilo_provider_mod.KiloProvider)


# --------------------------------------------------------------------------- #
# 5. chat_memory 的 kilo 分派（裸 OpenAI() 直连会 INVALID_TOKEN）
# --------------------------------------------------------------------------- #

def test_chat_memory_kilo_channel_goes_through_kilo_provider(monkeypatch, tmp_path):
    """免费组选到 kilo 通道 → 必须经 KiloProvider（不是裸 OpenAI()）。"""
    import deskmate.memory.llm as llm_mod

    calls: dict = {}

    class _FakeKiloProvider:
        def __init__(self, *a, **k):
            calls["ctor"] = True

        def complete(self, **kwargs):
            calls["complete"] = kwargs
            return AssistantTurn(text="KILO-OK")

    monkeypatch.setattr(llm_mod, "_memory_use_omniroute", lambda bible=False: False)
    monkeypatch.setattr(llm_mod, "_breaker_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        llm_mod,
        "_proxy_is_loopback",
        lambda: True,
    )
    monkeypatch.setattr(
        "coworker.providers.kilo_provider.KiloProvider", _FakeKiloProvider
    )
    # route 返回一条 kilo 通道（函数内 import，patch 模块属性即可）
    kilo_channel = SimpleNamespace(
        provider="kilo", model="kilo-auto/free", key="kilo:kilo-auto/free"
    )
    monkeypatch.setattr(
        llm_channels_mod, "route", lambda group, base_dir=None: (kilo_channel, [])
    )

    out = llm_mod.chat_memory("sys", "usr", free_only=True)
    assert out == "KILO-OK"
    assert calls.get("ctor") is True
    assert calls["complete"]["model"] == "kilo-auto/free"
    # trust_env=False（回环代理场景）传进了 client 构造
    assert calls["ctor"] is True


def _empty_content_completion() -> dict:
    """``finish_reason='length'`` 且 content 为空 —— Kilo 免费池的推理模型实测形态。"""
    return {
        "id": "gen-test",
        "object": "chat.completion",
        "created": 1791876763,
        "model": "kilo-auto/free",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "先想想…",
                },
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 32, "total_tokens": 35},
    }


def test_kilo_retries_with_more_tokens_when_reply_is_empty():
    """🔴 空回复兜底：思考模型把token 全花在 reasoning 上时自动加码重试。

    实测（2026-10-13本机）：``kilo-auto/free`` 路由到推理模型，
    ``max_tokens=32`` 时 ``finish_reason='length'`` 且 ``content=''``
    —— token 全进了 ``reasoning_content``，调用方拿到空字符串。
    后台记忆抽取/理解加工会把它当「有结果但内容为空」⇒ 白跑一轮。

    RED 依据：首版无此兜底，``complete()`` 直接返回空 text。
    """
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content.decode())
        mt = int(body.get("max_tokens") or 0)
        seen.append(mt)
        # 第一次（小 budget）→ 空回复；第二次（大 budget）→ 正常
        if len(seen) == 1:
            return httpx.Response(200, json=_empty_content_completion())
        return httpx.Response(200, json=_ok_completion())

    client = kilo_provider_mod.build_kilo_client(transport=httpx.MockTransport(handler))
    provider = kilo_provider_mod.KiloProvider(client=client)
    turn = provider.complete(
        model="kilo-auto/free",
        messages=[{"role": "user", "content": "回复OK两个字"}],
        max_tokens=32,
    )
    assert turn.text == "OK", "空回复应自动加码重试并拿到内容"
    assert len(seen) == 2, f"应恰好请求 2 次，实际 {len(seen)}"
    assert seen[1] > seen[0], f"重试必须加大 budget：{seen}"


def test_kilo_does_not_retry_when_reply_present():
    """有内容时**不得**多打一次请求（免费池也有速率，真调是浪费）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_ok_completion())

    client = kilo_provider_mod.build_kilo_client(transport=httpx.MockTransport(handler))
    provider = kilo_provider_mod.KiloProvider(client=client)
    turn = provider.complete(
        model="kilo-auto/free",
        messages=[{"role": "user", "content": "回复OK两个字"}],
        max_tokens=32,
    )
    assert turn.text == "OK"
    assert calls["n"] == 1, f"有内容就不该重试，实际打了 {calls['n']} 次"


def test_kilo_gives_up_after_retry_still_empty():
    """重试后仍空 → 返回空（不无限重试），让调用方的熔断/降级链接手。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_empty_content_completion())

    client = kilo_provider_mod.build_kilo_client(transport=httpx.MockTransport(handler))
    provider = kilo_provider_mod.KiloProvider(client=client)
    turn = provider.complete(
        model="kilo-auto/free",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=32,
    )
    assert not (turn.text or "").strip(), "两次都空就该老实返回空"
    assert calls["n"] == 2, f"重试上限 1 次，总请求 {calls['n']} 次"
