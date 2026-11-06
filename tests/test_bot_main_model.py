"""Bot 群模型链路（2026-11-06 萧哥拍板）：跟随 GUI 主模型。

旧红线「绝不跟随 default_model」的根因不是「跟随」本身，而是旧代码**自己拼裸
OpenAIProvider**，绕过 ``registry`` descriptor —— opencode-go 网关要
``x-opencode-session``，裸调必得 400 MissingSessionID（11-06 圣经抽取事故）。
改走官方 ``ProviderRouter`` 后，descriptor 自带合规 UA + 会话头，原因根治。

这一组锁住：主模型来源优先级、auto/缺失的显式回退、Router 语义（前缀→裸模型 id）、
网关会话头不丢、默认不再走付费组、以及 /bots 挂载双形态的 API 路径。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_prefs(monkeypatch, tmp_path: Path, **data) -> None:
    """把 GUI prefs 指到测试目录（conftest 已隔离 COWORKER_STATE_DIR，这里再钉死一次）。"""
    from coworker import secrets as _secrets

    monkeypatch.setattr(_secrets, "state_dir", lambda: tmp_path)
    (tmp_path / "prefs.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.delenv("DESKMATE_BOT_MODEL", raising=False)


# ------------------------------------------------------------------ 来源优先级

def test_model_choice_follows_gui_main_model(tmp_path: Path, monkeypatch) -> None:
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, default_model="deepseek:deepseek-flash")
    assert rt.model_choice() == ("deepseek:deepseek-flash", "main", "")


def test_explicit_override_wins(tmp_path: Path, monkeypatch) -> None:
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, default_model="deepseek:deepseek-flash")
    monkeypatch.setenv(rt.MODEL_OVERRIDE_ENV, "opencode-go:glm-5.3-flash")
    model, source, _ = rt.model_choice()
    assert (model, source) == ("opencode-go:glm-5.3-flash", "override")


@pytest.mark.parametrize(
    "prefs,reason_frag",
    [
        ({}, "prefs 未设"),
        ({"default_model": "auto"}, "auto"),
    ],
)
def test_missing_or_auto_falls_back_with_reason(
    tmp_path: Path, monkeypatch, prefs: dict, reason_frag: str
) -> None:
    """没有具体模型可直连时才回付费组，且必须给出 reason —— 不静默降级。"""
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, **prefs)
    model, source, reason = rt.model_choice()
    assert model == ""
    assert source == "paid_fallback"
    assert reason_frag in reason


# ------------------------------------------------------------------ Router 语义

def test_default_provider_is_official_router(tmp_path: Path, monkeypatch) -> None:
    from coworker.providers.router import ProviderRouter
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, default_model="deepseek:deepseek-flash")
    provider, model, source = rt.resolve_default_provider_model()
    assert isinstance(provider, ProviderRouter)
    assert (model, source) == ("deepseek:deepseek-flash", "main")
    # 完整 provider:model 字符串交给 Router，由它剥前缀；底层 SDK 拿裸 id。
    assert provider._provider_name(model) == "deepseek"
    assert provider._bare(model) == "deepseek-flash"


def test_gateway_main_model_keeps_session_header(tmp_path: Path, monkeypatch) -> None:
    """防 400 MissingSessionID 复发：主模型切到网关类时，descriptor 自带会话头。

    这是旧红线被撤销的**唯一理由**——群聊不再自己拼裸 OpenAIProvider。
    """
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, default_model="opencode-go:deepseek-v4.1-flash")
    monkeypatch.setattr(
        "coworker.secrets.SecretStore.get",
        lambda self, key, *a, **kw: (
            {"api_key": "fake-key", "base_url": "https://opencode.ai/inference/go/openai/v1"}
            if str(key).startswith("provider:")
            else {}
        ),
    )
    provider, model, _source = rt.resolve_default_provider_model()
    client = provider._client_for(model)
    headers = {
        str(k).lower(): str(v)
        for k, v in (getattr(client, "_default_headers", None) or {}).items()
    }
    assert headers.get("user-agent") == "xiaoju/1.0", "丢了诚实 UA"
    assert headers.get("x-opencode-session", "").startswith("ses_"), "丢了网关会话头"


def test_runtime_never_routes_to_paid_group_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """默认 engine 不再碰付费组：付费组全熔断也不该把群聊打挂。"""
    from deskmate.bots import runtime as rt

    _write_prefs(monkeypatch, tmp_path, default_model="deepseek:deepseek-flash")

    def _boom() -> tuple[str, str, str]:
        raise AssertionError("默认群聊不应再调 paid_credentials()")

    monkeypatch.setattr(rt, "paid_credentials", _boom)
    bot = rt.BotRuntime(books_root=tmp_path, workspace_root=tmp_path / "ws")
    eng = bot.engine("historian", room_id="model-link-test")
    assert eng is not None
    bot.close()


# ------------------------------------------------------------------ 挂载双形态

def test_page_prefix_injection_and_health(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from deskmate.bots.web import create_app

    standalone = TestClient(create_app(base_dir=str(tmp_path)))
    h1 = standalone.get("/").text
    assert 'const BASE = "";' in h1
    assert "__BOT_BASE__" not in h1, "占位符必须由服务端替换"
    assert "fetch(url(path), opt)" in h1

    mounted = TestClient(create_app(base_dir=str(tmp_path), base_prefix="/bots"))
    h2 = mounted.get("/").text
    assert 'const BASE = "/bots";' in h2
    assert "fetch(url('/api/rooms/'" in h2, "SSE 路径也必须带挂载前缀"
    health = mounted.get("/api/health").json()
    assert health["ok"] is True
    assert health["model_source"] in {"main", "override", "paid_fallback"}


def test_mounted_bots_routes_reachable(tmp_path: Path) -> None:
    """Mount 排在 /{tenant} 通配前，否则 /bots/ 会被多租户路由抢匹配返回 404。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.routing import Mount

    from deskmate.bots.web import create_app

    parent = FastAPI()
    parent.router.routes.insert(
        0, Mount("/bots", create_app(base_dir=str(tmp_path), base_prefix="/bots"))
    )

    @parent.get("/{tenant}")
    def _wildcard(tenant: str) -> dict:
        return {"wrong": tenant}

    c = TestClient(parent)
    assert c.get("/bots/").status_code == 200
    assert c.get("/bots/api/health").json()["ok"] is True
    assert len(c.get("/bots/api/bots").json()) >= 2
