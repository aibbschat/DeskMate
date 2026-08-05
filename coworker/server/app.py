"""FastAPI app — OpenAI-compatible endpoint + WS session API + REST.

The control plane every surface (GUI/IDE/messaging) rides on. The WS carries the engine
event stream and the approval channel; `/v1/chat/completions` is the OpenAI-compatible
proxy so any OpenAI-format client can use the runtime as a backend.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Origins allowed to talk to the local sidecar. It binds to 127.0.0.1, but a page in the
# user's own browser can still reach loopback — so without an origin gate, any website they
# visit could read `GET /v1/sessions` (CORS was `*`) and drive a session over the WS (which
# CORS never covers) into shell/file tools. We pin to the desktop webview's own origins
# (`tauri://localhost`, Windows' `http(s)://tauri.localhost`) and localhost dev/browser
# builds. Requests with NO Origin header (curl, native clients, tests, server-to-server) are
# allowed — the gate targets browsers, which always attach an unforgeable Origin.
_ALLOWED_ORIGIN_RE = re.compile(
    r"^(tauri://localhost"
    r"|https?://localhost(:\d+)?"
    r"|https?://127\.0\.0\.1(:\d+)?"
    r"|https?://tauri\.localhost)$"
)


def _origin_allowed(origin: str | None) -> bool:
    """True if a browser Origin may use the API. Missing Origin (non-browser) passes."""
    return origin is None or bool(_ALLOWED_ORIGIN_RE.match(origin))


# Caps on inbound WebSocket traffic. The loopback socket is unauthenticated (any local
# process can reach it), so bound frames, messages, and per-connection request rate before
# building model content or starting a turn.
_WS_MAX_FRAME_BYTES = 16 * 1024 * 1024
_WS_RATE_LIMIT_COUNT = 30
_WS_RATE_LIMIT_WINDOW_SECONDS = 10.0
_MAX_MESSAGE_TEXT_CHARS = 200_000
_MAX_ATTACHMENTS_BYTES = 15_000_000  # leaves JSON overhead below the 16 MiB frame cap


def _json_value_size(value: Any) -> int:
    """Conservative UTF-8 size of parsed JSON without allocating another giant string."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(_json_value_size(k) + _json_value_size(v) for k, v in value.items())
    if isinstance(value, list):
        return sum(_json_value_size(v) for v in value)
    return 8  # numbers, booleans, null, separators


# Brand colors for the connector badge riding the ✓ (UX-DECISIONS §30). The GUI owns the
# real logos; this page must render offline with zero assets, so a colored initial stands in.
_BRAND_COLORS = {
    "slack": "#4A154B",
    "github": "#24292f",
    "hubspot": "#ff7a59",
    "gmail": "#ea4335",
    "google_calendar": "#4285f4",
}


def _browser_page(
    title: str, detail: str, *, ok: bool = True, error: str = "", connector: str = ""
) -> str:
    """The page shown in the user's browser at the end of a loopback flow (sign-in or
    connector callback) — one branded card (UX-DECISIONS §30): OCW mark, ok/fail icon
    (the connector's initial rides the ✓), the friendly detail, and the raw error
    preserved on failures (it's the debugging breadcrumb). Inline CSS, light/dark via
    prefers-color-scheme, no external assets — it must render offline."""
    import html as _html

    badge = ""
    if ok and connector:
        color = _BRAND_COLORS.get(connector, "#3670b2")
        initial = _html.escape((connector[:1] or "?").upper())
        badge = f'<span class="mini" style="background:{color}">{initial}</span>'
    icon = (
        f'<div class="ico ok">✓{badge}</div>' if ok else '<div class="ico bad">✕</div>'
    )
    err = f'<div class="err">{_html.escape(error)}</div>' if error else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_html.escape(title)} — OpenWorker</title><style>"
        ":root{--paper:#f6f5f2;--panel:#fff;--line:#e4e2dc;--ink:#2c2c2a;--muted:#6f6e68;"
        "--faint:#a3a19a;--accent:#3670b2;--ok:#2e7d4f;--ok-soft:#e3f2e9;--bad:#b3423a;"
        "--bad-soft:#f8e7e5}"
        "@media(prefers-color-scheme:dark){:root{--paper:#191918;--panel:#232322;"
        "--line:#373633;--ink:#e8e6e1;--muted:#9d9b94;--faint:#6b6a64;--accent:#6ba3dd;"
        "--ok:#5cb884;--ok-soft:#20362a;--bad:#d97b74;--bad-soft:#3a2422}}"
        "body{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
        "justify-content:center;gap:18px;background:var(--paper);color:var(--ink);"
        'font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px}'
        ".card{background:var(--panel);border:1px solid var(--line);border-radius:16px;"
        "padding:34px 32px 28px;max-width:320px;width:100%;text-align:center;"
        "box-shadow:0 10px 30px rgba(0,0,0,.06);box-sizing:border-box}"
        ".mark{display:flex;align-items:center;justify-content:center;gap:7px;margin-bottom:22px;"
        "font-size:13px;font-weight:650}"
        ".mark i{width:20px;height:20px;border-radius:6px;background:var(--accent);"
        "display:inline-block;position:relative}"
        ".mark i::after{content:'';position:absolute;inset:5px;border-radius:2px;"
        "background:conic-gradient(from 0deg,#fff 0 25%,transparent 0 50%,#fff 0 75%,transparent 0)}"
        ".ico{width:52px;height:52px;border-radius:50%;margin:0 auto 14px;display:flex;"
        "align-items:center;justify-content:center;font-size:24px;position:relative}"
        ".ico.ok{background:var(--ok-soft);color:var(--ok)}"
        ".ico.bad{background:var(--bad-soft);color:var(--bad)}"
        ".mini{position:absolute;right:-3px;bottom:-3px;width:22px;height:22px;border-radius:7px;"
        "display:flex;align-items:center;justify-content:center;color:#fff;font-size:10px;"
        "font-weight:700;border:2px solid var(--panel)}"
        "h1{font-size:17px;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}"
        "p{font-size:12.5px;color:var(--muted);margin:0}"
        ".err{font-size:11.5px;color:var(--bad);background:var(--bad-soft);border-radius:8px;"
        "padding:7px 10px;margin-top:12px;text-align:left;word-break:break-word}"
        ".foot{font-size:10.5px;color:var(--faint)}"
        "</style></head><body>"
        '<div class="card"><div class="mark"><i></i>OpenWorker</div>'
        f"{icon}<h1>{_html.escape(title)}</h1><p>{_html.escape(detail)}</p>{err}</div>"
        '<div class="foot">Served locally by OpenWorker on your Mac</div>'
        "</body></html>"
    )


def _connector_title(name: str) -> str:
    """Display name for the loopback page — 'Slack connected', never 'slack connected'."""
    from ..connectors.descriptors import get_descriptor

    d = get_descriptor(name)
    return d.title if d else (name[:1].upper() + name[1:])


_CONNECT_FAILED_DETAIL = (
    "Something went wrong finishing this connection. "
    "Close this tab and try again from OpenWorker."
)

from ..attachments import (
    MAX_ATTACHMENTS as _MAX_ATTACHMENTS,
    MAX_IMAGE_CHARS,
    MAX_PDF_CHARS,
    MAX_TEXT_CHARS,
    build_user_content,
)
from ..engine import ApprovalOutcome
from ..inbox import VIS_INBOX, VIS_INLINE, args_preview
from ..permissions import Mode
from ..providers import AssistantTurn
from .manager import SessionManager


def create_app(manager: SessionManager) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            live = (
                await manager.start_gateway()
            )  # start messaging listeners (if configured)
            if live:
                print(f"[coworker] messaging gateway live: {', '.join(live)}")
        except Exception:  # never let a bad connector stop the server
            import traceback

            traceback.print_exc()
        yield
        await manager.aclose()  # stop gateway + close MCP connections on shutdown

    app = FastAPI(title="coworker", version="0.0.0", lifespan=lifespan)
    api_token = os.environ.get("COWORKER_API_TOKEN", "")
    # A2A 路由前缀放行的路由（SDK 注册的 /tasks/{id} 等带路径参数的端点用前缀匹配）
    A2A_OPEN_PREFIXES = ("/tasks/", "/message:", "/.well-known/agent-card")
    tokenless_paths = {
        "/v1/health",
        "/auth/callback",
        "/mcp/oauth/callback",
        "/oauth/callback",
        # A2A：本地可信网络内开放发现与调用；企业级鉴权后续补
        "/.well-known/agent-card.json",
        "/a2a",
        "/",
        "/message:send",
        "/message:stream",
        "/extendedAgentCard",
    }

    def _request_authenticated(request: Request) -> bool:
        provided = request.headers.get("x-openworker-token", "")
        return bool(
            api_token
            and provided
            and secrets.compare_digest(provided, api_token)
        )

    def _websocket_authenticated(ws: WebSocket) -> bool:
        if not api_token:
            return True
        protocols = {
            part.strip()
            for part in ws.headers.get("sec-websocket-protocol", "").split(",")
            if part.strip()
        }
        return any(secrets.compare_digest(part, api_token) for part in protocols)

    @app.middleware("http")
    async def require_sidecar_token(request: Request, call_next):
        # Preflights carry the requested header name, not its value. CORS checks the
        # Origin; the actual state-changing request still must authenticate.
        if (
            not api_token
            or request.method == "OPTIONS"
            or request.url.path in tokenless_paths
            or request.url.path.startswith(A2A_OPEN_PREFIXES)
            or _request_authenticated(request)
        ):
            return await call_next(request)
        return JSONResponse(
            {"error": "missing or invalid OpenWorker sidecar token"},
            status_code=401,
        )

    app.add_middleware(
        CORSMiddleware,
        # Pinned to the desktop webview + localhost (see _ALLOWED_ORIGIN_RE): stops a random
        # website the user visits from reading local API responses cross-origin.
        allow_origin_regex=_ALLOWED_ORIGIN_RE.pattern,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.manager = manager

    # ---- DeskMate 学习历史辅助函数（模块级，供 @app.get 路由和 ws_session 闭包共用）----
    def _classify_learned_fact(fact: str) -> str:
        """启发式归类一条学到的记忆（用于「了解度」统计）。"""
        t = (fact or "").lower()
        if any(k in t for k in ("偏好", "喜欢", "倾向", "爱用", "常用", "希望", "想要", "期望", "不愿", "讨厌", "避免", "prefer", "like", "hate")):
            return "preference"
        if any(k in t for k in ("习惯", "通常", "一般", "每天", "每次", "经常", "往往", "固定", "habit", "usually", "always")):
            return "habit"
        if any(k in t for k in ("工具", "用", "使用", "命令", "快捷键", "软件", "编辑器", "ide", "tool", "use", "command")):
            return "tool"
        return "other"

    def _append_learned_history(desk_dir: "Path", facts: list) -> None:
        """把本轮学到的记忆追加落盘到 learned_history.jsonl（带时间戳 + 分类）。"""
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path
        hist_dir = Path(desk_dir) / "memory"
        hist_dir.mkdir(parents=True, exist_ok=True)
        hist_path = hist_dir / "learned_history.jsonl"
        ts = _dt.now().isoformat(timespec="seconds")
        with hist_path.open("a", encoding="utf-8") as f:
            for fact in facts:
                fact = str(fact).strip() if fact else ""
                if not fact:
                    continue
                rec = {"ts": ts, "fact": fact, "category": _classify_learned_fact(fact)}
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")

    def _read_learned_history(desk_dir: "Path", limit: int = 30) -> dict:
        """读取 learned_history.jsonl，返回最近条目 + 统计（了解度）。"""
        import json as _json
        from pathlib import Path
        hist_path = Path(desk_dir) / "memory" / "learned_history.jsonl"
        items: list = []
        if hist_path.exists():
            for line in reversed(hist_path.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(_json.loads(line))
                except Exception:
                    continue
                if len(items) >= limit:
                    break
            items.reverse()
        by_category: dict = {}
        for it in items:
            c = it.get("category", "other")
            by_category[c] = by_category.get(c, 0) + 1
        total = 0
        if hist_path.exists():
            total = sum(1 for _ in hist_path.read_text(encoding="utf-8").splitlines() if _.strip())
        return {"items": items, "stats": {"total": total, "by_category": by_category}}
    # ---- end DeskMate 学习历史辅助函数 ----

    @app.get("/v1/health")
    def health(request: Request) -> dict[str, Any]:
        if api_token and not _request_authenticated(request):
            return {"status": "ok"}
        return {
            "status": "ok",
            "default_workspace": manager.default_workspace,
            "model": manager.model,
        }

    # ---- A2A (Agent-to-Agent) 服务端：基于官方 a2a-sdk（canonical v1.0）----
    # 让桌搭可被其它 agent 发现与调用；支持 message/send 与旧 v0.3 方言 SendMessage 双方言。
    # 路由（Agent Card / JSON-RPC / REST）由 SDK 直接并入本 app，详见 a2a_server.register_a2a。
    try:
        from coworker.server.a2a_server import register_a2a
        register_a2a(app, manager=manager)
    except Exception as _e:  # A2A 注册失败不应拖垮主服务
        logging.getLogger(__name__).warning("A2A 服务端注册失败（不影响主流程）：%s", _e)

    @app.get("/v1/agents")
    def agents() -> dict[str, Any]:
        return {"agents": manager.list_agents()}

    @app.get("/v1/personas")
    def personas() -> dict[str, Any]:
        return {"personas": manager.personas.list_all()}

    @app.get("/v1/inbox")
    def inbox(session_id: str = "", state: str = "") -> dict[str, Any]:
        from dataclasses import asdict

        # The cross-session Inbox list shows only Unattended (inbox-visibility) items; a per-session
        # query returns inline ones too, so the answer-in-context card sees parked attended prompts.
        items = manager.inbox.list(
            session_id=session_id or None,
            state=state or None,
            visibility=None if session_id else VIS_INBOX,
        )
        # Enrich with the originating session's context so the Inbox is self-contained — the
        # "go to session" chip needs title/agent/workspace without depending on a (possibly stale)
        # client-side session list, and can link straight to it.
        out: list[dict[str, Any]] = []
        for i in items:
            d = asdict(i)
            rec = manager.session_store.load(i.session_id)
            if (
                rec is None
                and not session_id
                and i.state == "pending"
                and i.session_id not in manager._engines
            ):
                # Lazy cleanup for legacy orphans (sessions deleted before delete_session
                # started closing their items): an orphaned prompt can never be answered.
                # A LIVE engine without a record yet (brand-new session, first turn still
                # running) is NOT an orphan — hence the engine guard.
                manager.inbox.resolve_session(i.session_id)
                continue
            d["session_title"] = (rec.title if rec else None) or i.session_id
            d["session_agent"] = rec.agent if rec else None
            d["session_workspace"] = rec.workspace if rec else None
            d["session_exists"] = rec is not None
            out.append(d)
        return {"items": out}

    @app.post("/v1/inbox/{item_id}/resolve")
    async def resolve_inbox_item(item_id: str, body: dict) -> dict[str, Any]:
        # Idempotent + first-responder-wins: ok=False means it was already resolved elsewhere.
        # Routes through resolve_inbox so a restart-orphaned prompt durably resumes its turn.
        ok = await manager.resolve_inbox(item_id, str(body.get("resolution", "deny")))
        return {"ok": ok}

    @app.get("/v1/subscriptions")
    def subscriptions() -> dict[str, Any]:
        # Global view-only list: each (session → channel) subscription, enriched with the session's
        # title/agent and the channel its Inbox routes OUT to (so an inbound/outbound collision on
        # the same channel is visible).
        out: list[dict[str, Any]] = []
        for sub in manager.subscriptions.all():
            rec = manager.session_store.load(sub.session_id)
            agent = rec.agent if rec else ""
            routing = manager._routing_targets(sub.session_id, agent or "cowork")
            out.append(
                {
                    "session_id": sub.session_id,
                    "session_title": (rec.title if rec else None) or sub.session_id,
                    "agent": agent,
                    "channel": sub.channel,
                    # Display name from the channel buffer ("#ocw-test"), when any inbound
                    # message has carried one — the address stays the identifier.
                    "channel_name": manager.channel_buffer.name_for(sub.channel),
                    "routing_target": routing[0] if routing else None,
                    "collision": bool(routing and sub.channel in routing),
                }
            )
        return {"subscriptions": out}

    @app.get("/v1/channels/recent")
    def recent_channels() -> dict[str, Any]:
        # The picker's "recently-seen" source: channels the bot has received messages from.
        return {"channels": manager.channel_buffer.channels()}

    @app.get("/v1/unrouted")
    def unrouted() -> dict[str, Any]:
        # Dead-letter view: inbound messages with no destination + background-turn failures.
        return {"items": manager.unrouted.list()}

    @app.post("/v1/subscriptions")
    def subscribe(body: dict) -> dict[str, Any]:
        from ..subscriptions import resolve_channel

        session_id = str(body.get("session_id", "")).strip()
        raw = str(body.get("channel", ""))
        addr = resolve_channel(raw)
        if not session_id or not addr or ":" not in addr:
            if raw.strip().startswith("#"):
                # A bare #name can't be looked up locally — storing it literally would create a
                # subscription that never matches real traffic (resolve_channel returns "").
                return {
                    "ok": False,
                    "error": "Channel names can't be looked up — paste the channel ID "
                    "(channel name ▸ About) or the channel's Copy-link URL.",
                }
            return {"ok": False, "error": "need a session_id and a channel"}
        manager.subscriptions.subscribe(session_id, addr)
        return {"ok": True, "channel": addr}

    @app.post("/v1/subscriptions/remove")
    def unsubscribe(body: dict) -> dict[str, Any]:
        from ..subscriptions import resolve_channel

        session_id = str(body.get("session_id", "")).strip()
        addr = resolve_channel(str(body.get("channel", "")))
        removed = manager.subscriptions.unsubscribe(session_id, addr)
        return {"ok": True, "removed": removed}

    @app.get("/v1/inbox/reconcile")
    def reconcile_inbox(session_id: str) -> dict[str, Any]:
        # Called when a session resumes attended control (surface pending + recap inline).
        return manager.inbox.reconcile_on_resume(session_id)

    @app.get("/v1/inbox/routing")
    def inbox_routing() -> dict[str, Any]:
        return {"bindings": manager.inbox_routing.bindings()}

    @app.post("/v1/inbox/routing/binding")
    def set_inbox_binding(body: dict) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not name:
            return {"ok": False, "error": "binding needs a `name`"}
        return manager.set_inbox_binding(
            name,
            channel=body.get("channel") or None,
            target=str(body.get("target", "")),
        )

    @app.get("/v1/sessions/{session_id}/unattended")
    def get_unattended(session_id: str) -> dict[str, Any]:
        return {"unattended": manager.unattended.is_unattended(session_id)}

    @app.post("/v1/sessions/{session_id}/unattended")
    def set_unattended(session_id: str, body: dict) -> dict[str, Any]:
        # The GUI gates the on-transition behind a one-tap confirm.
        on = bool(body.get("unattended"))
        manager.unattended.set(session_id, on)
        return {"ok": True, "session_id": session_id, "unattended": on}

    @app.get("/v1/sessions/{session_id}/connections")
    def session_connections(session_id: str, persona: str = "") -> dict[str, Any]:
        # `persona` is the GUI's hint for brand-new sessions (no record yet) — without it the
        # view resolves to the default persona and shows the wrong defaults/recommends.
        # §6: the Sources drawer payload — connected connectors w/ state + recommended + ⚠ count.
        return manager.session_connections_view(session_id, persona or None)

    @app.post("/v1/sessions/{session_id}/connections")
    def set_session_connection(session_id: str, body: dict) -> dict[str, Any]:
        # §6: a session override. `clear` drops the override (inherit the persona default again);
        # otherwise set an explicit on/off. Return the refreshed view so the drawer can re-render.
        body = body or {}
        connector = str(body.get("connector", "")).strip()
        if not connector:
            return {"ok": False, "error": "connector required"}
        if body.get("clear"):
            manager.session_connections.clear(session_id, connector)
        else:
            manager.session_connections.set(
                session_id, connector, bool(body.get("enabled", False))
            )
        persona = str(body.get("persona", "")) or None
        return {
            "ok": True,
            "connections": manager.session_connections_view(session_id, persona),
        }

    @app.post("/v1/personas/install")
    def install_persona(body: dict) -> dict[str, Any]:
        # Returns a consent summary per persona; they land disabled pending the user's approval
        # (then POST /v1/personas/{id} {enabled:true, surfaced:true}).
        reg = manager.personas
        try:
            if body.get("git_url"):
                summaries = reg.install_from_git(str(body["git_url"]))
            elif body.get("dir"):
                summaries = reg.install_from_dir(str(body["dir"]))
            elif body.get("gallery_slug"):
                # Gallery install = fetch the manifest markdown from the cloud
                # (sign-in required), verify its hash, then reuse the exact
                # same parser + consent path as a local/Git install. The
                # gallery never changes the trust model: no executable code,
                # lands disabled pending consent.
                import hashlib
                import tempfile

                from .. import cloud
                from ..config import load_config

                slug = str(body["gallery_slug"]).strip()
                manifest = cloud.gallery_manifest(manager.secrets, load_config(), slug)
                if manifest is None:
                    return {
                        "ok": False,
                        "error": "gallery requires cloud sign-in (or the cloud is unreachable)",
                    }
                markdown = manifest.get("manifest_markdown", "")
                digest = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
                if (
                    manifest.get("manifest_hash")
                    and manifest["manifest_hash"] != digest
                ):
                    return {"ok": False, "error": "manifest hash mismatch"}
                with tempfile.TemporaryDirectory() as td:
                    (Path(td) / f"{slug}.md").write_text(markdown)
                    summaries = reg.install_from_dir(td)
                cloud.gallery_install_event(manager.secrets, load_config(), slug)
            else:
                return {
                    "ok": False,
                    "error": "provide a `dir`, `git_url`, or `gallery_slug`",
                }
        except Exception as e:  # surface manifest/clone errors to the caller
            return {"ok": False, "error": str(e)}
        return {"ok": True, "consent": summaries, "personas": reg.list_all()}

    @app.get("/v1/cloud/gallery/{slug}")
    def cloud_gallery_detail(slug: str) -> dict[str, Any]:
        """Solo page for one gallery coworker: publisher pitch + capabilities
        derived locally from the manifest (same parser as install)."""
        from .. import cloud
        from ..config import load_config

        body = cloud.gallery_detail(manager.secrets, load_config(), slug)
        if body is None:
            return {"ok": False, "error": "gallery requires cloud sign-in"}
        return body

    @app.get("/v1/cloud/gallery")
    def cloud_gallery() -> dict[str, Any]:
        """Gallery cards for the GUI. Signed out ⇒ ok:false (the gallery is a
        signed-in feature by design; local personas are unaffected)."""
        from .. import cloud
        from ..config import load_config

        body = cloud.gallery_list(manager.secrets, load_config())
        if body is None:
            return {
                "ok": False,
                "error": "gallery requires cloud sign-in",
                "personas": [],
            }
        return {"ok": True, "personas": body.get("personas", [])}

    @app.post("/v1/personas/{persona_id}")
    def update_persona(persona_id: str, body: dict) -> dict[str, Any]:
        reg = manager.personas
        archived = 0
        try:
            if "enabled" in body:
                # Disable archives the persona's sessions atomically (server-side, one
                # request) so any client gets the same semantic. See set_persona_enabled.
                archived = manager.set_persona_enabled(
                    persona_id, bool(body["enabled"])
                )["archived_sessions"]
            if "surfaced" in body:
                reg.set_surfaced(persona_id, bool(body["surfaced"]))
            if body.get("default"):
                reg.set_default(persona_id)
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return {"ok": True, "personas": reg.list_all(), "archived_sessions": archived}

    @app.delete("/v1/personas/{persona_id}")
    def persona_delete(persona_id: str) -> dict[str, Any]:
        # Uninstall a non-builtin persona (snapshot dir + lifecycle state). Local
        # operation — works signed out, regardless of where the persona came from.
        try:
            manager.personas.uninstall(persona_id)
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "personas": manager.personas.list_all()}

    @app.get("/v1/personas/{persona_id}")
    def persona_detail(persona_id: str) -> dict[str, Any]:
        # §5 detail page: identity + capabilities + recommends(+connected) + default connections.
        detail = manager.persona_detail(persona_id)
        if detail is None:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return detail

    @app.post("/v1/personas/{persona_id}/enable")
    def persona_enable(persona_id: str, body: dict) -> dict[str, Any]:
        # Dedicated §5/§8 route; delegates to the same manager toggle as POST /v1/personas/{id}
        # (so disable archives the persona's sessions here too).
        try:
            manager.set_persona_enabled(
                persona_id, bool((body or {}).get("enabled", True))
            )
        except KeyError:
            return {"ok": False, "error": f"unknown persona: {persona_id}"}
        return {"ok": True, "personas": manager.personas.list_all()}

    @app.post("/v1/personas/{persona_id}/connections")
    def persona_set_connection(persona_id: str, body: dict) -> dict[str, Any]:
        # §5: flip a persona-default connector on/off; re-reads so the client can refresh.
        body = body or {}
        connector = str(body.get("connector", "")).strip()
        if not connector:
            return {"ok": False, "error": "connector required"}
        return manager.set_persona_connection(
            persona_id, connector, bool(body.get("enabled", False))
        )

    @app.get("/v1/skills")
    def skills() -> dict[str, Any]:
        return {"skills": manager.list_skills()}

    @app.get("/v1/workspaces/recent")
    def recent_workspaces() -> dict[str, Any]:
        return {"workspaces": manager.recent_workspaces()}

    @app.post("/v1/workspaces/open")
    def open_workspace(body: dict) -> dict[str, Any]:
        return manager.open_workspace(
            body.get("path", ""), create=bool(body.get("create"))
        )

    @app.get("/v1/workspaces/trusted")
    def trusted_workspaces() -> dict[str, Any]:
        return {"workspaces": manager.trusted_workspaces()}

    @app.post("/v1/workspaces/trust")
    def set_workspace_trust(body: dict) -> dict[str, Any]:
        return manager.set_workspace_trust(
            str((body or {}).get("path", "")),
            trusted=bool((body or {}).get("trusted", False)),
        )

    @app.post("/v1/workspaces/pick")
    async def pick_workspace() -> dict[str, Any]:
        # Native folder picker opened by the LOCAL sidecar (browser GUIs can't get absolute
        # paths from web file dialogs). Off the event loop: blocks until pick/cancel.
        return await asyncio.to_thread(manager.pick_native_folder)

    @app.get("/v1/sessions")
    def sessions(workspace: str | None = None) -> dict[str, Any]:
        return {"sessions": manager.list_sessions(workspace)}

    @app.get("/v1/sessions/{session_id}/messages")
    def session_messages(session_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        messages = manager.session_messages(session_id)
        # 分页：返回 offset 到 offset+limit 的消息
        paginated = messages[offset:offset + limit]
        return {
            "messages": paginated,
            "total": len(messages),
            "offset": offset,
            "limit": limit
        }

    @app.patch("/v1/sessions/{session_id}")
    def session_patch(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        if "pinned" in body or "archived" in body:
            return manager.set_session_flags(
                session_id,
                pinned=bool(body["pinned"]) if "pinned" in body else None,
                archived=bool(body["archived"]) if "archived" in body else None,
            )
        return manager.rename_session(session_id, str(body.get("title", "")))

    @app.delete("/v1/sessions/{session_id}")
    def session_delete(session_id: str) -> dict[str, Any]:
        return manager.delete_session(session_id)

    @app.get("/v1/sessions/{session_id}/roots")
    def session_roots(session_id: str) -> dict[str, Any]:
        return {"roots": manager.get_roots(session_id)}

    @app.post("/v1/sessions/{session_id}/roots")
    def session_add_root(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.add_root(
            session_id, str(body.get("path", "")), bool(body.get("writable", False))
        )

    @app.delete("/v1/sessions/{session_id}/roots")
    def session_remove_root(session_id: str, path: str) -> dict[str, Any]:
        return manager.remove_root(session_id, path)

    @app.get("/v1/sessions/{session_id}/artifacts")
    def session_artifacts(session_id: str) -> dict[str, Any]:
        return {"artifacts": manager.list_artifacts(session_id)}

    @app.get("/v1/sessions/{session_id}/artifacts/read")
    def session_artifact_read(session_id: str, path: str) -> dict[str, Any]:
        return manager.read_artifact(session_id, path)

    @app.post("/v1/sessions/{session_id}/artifacts/reveal")
    def session_artifact_reveal(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.reveal_artifact(
            session_id, str(body.get("path", "")), str(body.get("mode", "reveal"))
        )

    @app.get("/v1/memory")
    def memory() -> dict[str, Any]:
        return {"memory": manager.list_memory()}

    @app.post("/v1/memory")
    def add_memory(body: dict) -> dict[str, Any]:
        return manager.add_memory(
            body.get("content", ""), body.get("scope", "workspace")
        )

    @app.post("/v1/chat/completions")
    def chat_completions(body: dict) -> dict[str, Any]:
        model = body.get("model", manager.model)
        turn = manager.provider_complete(
            model, body.get("messages", []), body.get("tools")
        )
        return _openai_response(model, turn)

    # -- MCP servers ------------------------------------------------------------
    @app.get("/v1/mcp")
    def mcp_list() -> dict[str, Any]:
        return {"servers": manager.list_mcp()}

    @app.post("/v1/mcp")
    def mcp_add(body: dict) -> dict[str, Any]:
        name = body.get("name")
        config = body.get("config")
        if not name or not isinstance(config, dict):
            return {"ok": False, "error": "name and config required"}
        return manager.add_mcp(name, config)

    @app.patch("/v1/mcp/{name}")
    def mcp_patch(name: str, body: dict) -> dict[str, Any]:
        return manager.patch_mcp(name, body or {})

    @app.delete("/v1/mcp/{name}")
    def mcp_delete(name: str) -> dict[str, Any]:
        return manager.delete_mcp(name)

    @app.get("/v1/mcp/{name}/tools")
    async def mcp_tools(name: str) -> dict[str, Any]:
        return await manager.mcp_tools(name)

    @app.post("/v1/mcp/{name}/connect")
    async def mcp_connect(name: str) -> dict[str, Any]:
        # Connect now. For `auth: oauth` servers the first connect opens the system
        # browser and waits on the loopback callback — that can take minutes, so it
        # runs as a background task; the GUI polls /v1/mcp for the status flip
        # (authorizing → connected | needs_auth + last_error).
        asyncio.create_task(manager.connect_mcp(name))
        return {"ok": True, "started": True}

    @app.post("/v1/mcp/{name}/signout")
    async def mcp_signout(name: str) -> dict[str, Any]:
        return await manager.signout_mcp(name)

    @app.get("/mcp/oauth/callback")
    async def mcp_oauth_callback(
        code: str = "", state: str = "", error: str = ""
    ) -> Any:
        # Loopback landing for the MCP OAuth browser flow (mcp/oauth.py). Browser-facing:
        # returns the same styled page as the managed-connector callbacks.
        from fastapi.responses import HTMLResponse

        from ..mcp import oauth as mcp_oauth

        if error:
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed",
                    "The service reported an error. Return to OpenWorker and try again.",
                    ok=False,
                    error=error,
                ),
                status_code=400,
            )
        if not code or not mcp_oauth.deliver_callback(code, state or None):
            return HTMLResponse(
                _browser_page(
                    "Nothing waiting for this sign-in",
                    "The sign-in may have timed out. Return to OpenWorker and start it again.",
                    ok=False,
                ),
                status_code=400,
            )
        return HTMLResponse(
            _browser_page(
                "Connected",
                "Sign-in complete. You can close this tab and return to OpenWorker.",
                ok=True,
            )
        )

    @app.post("/v1/mcp/reload")
    async def mcp_reload() -> dict[str, Any]:
        return await manager.reload_mcp()

    # -- connectors (Slack / Telegram / …) --------------------------------------
    @app.get("/v1/connectors")
    def connectors_list() -> dict[str, Any]:
        return {"connectors": manager.list_connectors()}

    async def _refresh_listeners_if_two_way(name: str) -> None:
        # New/removed creds only take effect when the platform socket reconnects (Socket Mode
        # authenticates at connect time) — hot-reload the listeners in-process so pasting
        # tokens works immediately, no sidecar restart (§19).
        from ..connectors.config import PLATFORMS

        if name in PLATFORMS:
            try:
                await manager.refresh_gateway()
            except Exception:
                pass  # a listener that fails to come up must not fail the save

    @app.post("/v1/connectors/{name}/connect")
    async def connector_connect(name: str, body: dict) -> dict[str, Any]:
        fields = body.get("fields") if isinstance(body, dict) else None
        # experimental connectors require the caller to explicitly acknowledge the risk notice
        acknowledged = bool(isinstance(body, dict) and body.get("acknowledge_risk"))
        # token validation does a blocking HTTP call → keep it off the event loop
        result = await asyncio.to_thread(
            lambda: manager.connect_connector(
                name, fields or {}, acknowledged=acknowledged
            )
        )
        if result.get("ok"):
            await _refresh_listeners_if_two_way(name)
        return result

    @app.post("/v1/connectors/{name}/mcp-connect")
    async def connector_mcp_connect(name: str) -> dict[str, Any]:
        # One-click connect for an MCP-backed connector: the browser OAuth flow can
        # take minutes, so it runs in the background; the GUI polls /v1/connectors
        # until the card flips to connected (mode "mcp").
        from ..connectors.descriptors import get_descriptor

        d = get_descriptor(name)
        if d is None or not d.mcp_url:
            return {"ok": False, "error": f"{name} has no MCP connect path"}
        asyncio.create_task(manager.mcp_connect_connector(name))
        return {"ok": True, "started": True}

    @app.post("/v1/connectors/{name}/disconnect")
    async def connector_disconnect(name: str) -> dict[str, Any]:
        # Managed profiles: best-effort flip of the cloud metadata record first
        # (network call → off the loop). Local deletion always proceeds.
        from .. import cloud
        from ..config import load_config

        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(manager.secrets, load_config(), name)
        )
        result = manager.disconnect_connector(name)
        await _refresh_listeners_if_two_way(name)
        return result

    @app.post("/v1/connectors/slack/workspaces/{team_id}/disconnect")
    async def slack_workspace_disconnect(team_id: str) -> dict[str, Any]:
        """Stop relaying one workspace (managed relay). Cloud routing row deleted
        best-effort, local per-team token removed, gateway hot-reloaded."""
        return await manager.disconnect_slack_workspace(team_id)

    @app.get("/v1/connectors/slack/status")
    async def slack_status() -> dict[str, Any]:
        """Slack health, three layers: relay socket / cloud sign-in / per-team tokens."""
        return manager.slack_status()

    @app.post("/v1/connectors/github/installations/{installation_id}/disconnect")
    async def github_installation_disconnect(installation_id: str) -> dict[str, Any]:
        """Stop relaying one GitHub App installation (managed relay). Cloud
        routing rows deleted best-effort, local profile removed, gateway
        hot-reloaded."""
        return await manager.disconnect_github_installation(installation_id)

    @app.get("/v1/connectors/github/status")
    async def github_status() -> dict[str, Any]:
        """GitHub health: relay socket / cloud sign-in / per-installation tokens."""
        return manager.github_status()

    @app.post("/v1/connectors/gmail/accounts/{email}/disconnect")
    async def gmail_account_disconnect(email: str) -> dict[str, Any]:
        """Drop ONE mailbox (cloud metadata best-effort first, like a full
        disconnect); the default pointer moves to the next account."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import gmail_accounts

        profile_key = gmail_accounts.PREFIX + email.strip().lower()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets, load_config(), "gmail", profile_key=profile_key
            )
        )
        return gmail_accounts.disconnect_account(manager.secrets, email)

    @app.post("/v1/connectors/gmail/accounts/{email}/default")
    def gmail_account_default(email: str) -> dict[str, Any]:
        from ..connectors import gmail_accounts

        return gmail_accounts.set_default(manager.secrets, email)

    @app.patch("/v1/connectors/gmail/filters")
    def gmail_filters(body: dict) -> dict[str, Any]:
        """Replace the "Never show agents" lists. Enforced in the local tool
        layer; agents see silent omissions, the user sees counts + audit."""
        from ..connectors import gmail_accounts

        senders = body.get("senders") if isinstance(body, dict) else None
        labels = body.get("labels") if isinstance(body, dict) else None
        if senders is not None and not isinstance(senders, list):
            return {"ok": False, "error": "senders must be a list"}
        if labels is not None and not isinstance(labels, list):
            return {"ok": False, "error": "labels must be a list"}
        return gmail_accounts.set_filters(manager.secrets, senders, labels)

    @app.post("/v1/connectors/google_calendar/accounts/{email}/disconnect")
    async def gcal_account_disconnect(email: str) -> dict[str, Any]:
        """Drop ONE Google Calendar account (cloud metadata best-effort first);
        the default pointer moves to the next account."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import gcal_accounts

        profile_key = gcal_accounts.PREFIX + email.strip().lower()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets,
                load_config(),
                "google_calendar",
                profile_key=profile_key,
            )
        )
        return gcal_accounts.disconnect_account(manager.secrets, email)

    @app.post("/v1/connectors/google_calendar/accounts/{email}/default")
    def gcal_account_default(email: str) -> dict[str, Any]:
        from ..connectors import gcal_accounts

        return gcal_accounts.set_default(manager.secrets, email)

    @app.post("/v1/connectors/hubspot/portals/{hub_id}/disconnect")
    async def hubspot_portal_disconnect(hub_id: str) -> dict[str, Any]:
        from .. import cloud
        from ..config import load_config
        from ..connectors import hubspot_portals

        profile_key = hubspot_portals.PREFIX + hub_id.strip()
        await asyncio.to_thread(
            lambda: cloud.cloud_disconnect(
                manager.secrets, load_config(), "hubspot", profile_key=profile_key
            )
        )
        return hubspot_portals.disconnect_portal(manager.secrets, hub_id)

    @app.post("/v1/connectors/hubspot/portals/{hub_id}/default")
    def hubspot_portal_default(hub_id: str) -> dict[str, Any]:
        from ..connectors import hubspot_portals

        return hubspot_portals.set_default(manager.secrets, hub_id)

    @app.post("/v1/connectors/{name}/accounts/{account_id}/disconnect")
    async def account_disconnect(name: str, account_id: str) -> dict[str, Any]:
        """Generic per-account disconnect for account-patterned connectors
        (batch 2+). Gmail/Calendar keep their specific email routes."""
        from .. import cloud
        from ..config import load_config
        from ..connectors import accounts

        if not accounts.is_account_connector(name):
            return {"ok": False, "error": "not a multi-account connector"}
        _id, profile_key, profile = accounts.resolve(manager.secrets, name, account_id)
        if profile and profile.get("managed"):
            await asyncio.to_thread(
                lambda: cloud.cloud_disconnect(
                    manager.secrets, load_config(), name, profile_key=profile_key
                )
            )
        return accounts.disconnect_account(manager.secrets, name, account_id)

    @app.post("/v1/connectors/{name}/accounts/{account_id}/default")
    def account_default(name: str, account_id: str) -> dict[str, Any]:
        from ..connectors import accounts

        if not accounts.is_account_connector(name):
            return {"ok": False, "error": "not a multi-account connector"}
        return accounts.set_default(manager.secrets, name, account_id)

    @app.patch("/v1/connectors/hubspot/hidden-fields")
    def hubspot_hidden_fields(body: dict) -> dict[str, Any]:
        """Replace the hidden-fields denylist (property names stripped from every
        record agents read — model-facing policy, not a human ACL)."""
        from ..connectors import hubspot_portals

        fields = body.get("hidden_fields") if isinstance(body, dict) else None
        if not isinstance(fields, list):
            return {"ok": False, "error": "hidden_fields must be a list"}
        return hubspot_portals.set_hidden_fields(manager.secrets, fields)

    @app.post("/v1/connectors/{name}/unauthorized/{item_id}")
    async def connector_unauthorized_resolve(
        name: str, item_id: str, body: dict
    ) -> dict[str, Any]:
        # Resolve a parked unauthorized message: dismiss / allow / allow_deliver (§19).
        action = str((body or {}).get("action", "")).strip()
        return await manager.resolve_unauthorized(name, item_id, action)

    # -- OpenWorker Cloud: sign-in + managed one-click connect ---------------
    # All optional: the app is fully functional signed out (manual token paste
    # stays available for every connector, before and after sign-in).

    @app.get("/v1/cloud/status")
    def cloud_status() -> dict[str, Any]:
        from .. import cloud

        return {
            **cloud.status(manager.secrets),
            "telemetry_enabled": cloud.telemetry_enabled(manager.secrets),
        }

    @app.post("/v1/cloud/telemetry")
    def cloud_telemetry(body: dict) -> dict[str, Any]:
        """The Phase 5 opt-out toggle. Local preference only — signed-out users
        send nothing regardless of this value."""
        from .. import cloud

        return cloud.set_telemetry_enabled(
            manager.secrets, bool((body or {}).get("enabled", True))
        )

    @app.post("/v1/cloud/login")
    def cloud_login() -> dict[str, Any]:
        """Start browser sign-in. The sidecar opens the system browser itself
        (works identically under Tauri and plain-browser dev)."""
        import webbrowser

        from .. import cloud
        from ..config import load_config

        out = cloud.begin_login(load_config())
        webbrowser.open(out["authorize_url"])
        return {"ok": True, "authorize_url": out["authorize_url"]}

    @app.post("/v1/cloud/logout")
    def cloud_logout() -> dict[str, Any]:
        from .. import cloud

        return cloud.logout(manager.secrets)

    @app.get("/auth/callback")
    async def cloud_auth_callback(code: str = "", state: str = "", error: str = ""):
        from fastapi.responses import HTMLResponse

        from .. import cloud
        from ..config import load_config

        signin_failed_detail = (
            "Close this tab and try signing in again from OpenWorker."
        )
        if error:
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed", signin_failed_detail, ok=False, error=error
                ),
                status_code=400,
            )
        result = await asyncio.to_thread(
            lambda: cloud.complete_login(manager.secrets, load_config(), code, state)
        )
        if not result.get("ok"):
            return HTMLResponse(
                _browser_page(
                    "Sign-in failed",
                    signin_failed_detail,
                    ok=False,
                    error=result.get("error", ""),
                ),
                status_code=400,
            )

        # Restore managed connections in the background: best-effort metadata work
        # that must not hold the "Signed in" page (or the GUI's signed-in flip)
        # hostage to another broker round trip. Restored GitHub installs hot-add
        # the gateway so the relay connects without a restart.
        async def _restore_connections() -> None:
            try:
                out = await asyncio.to_thread(
                    lambda: cloud.sync_connections(manager.secrets, load_config())
                )
                if out.get("restored"):
                    await manager.refresh_gateway()
            except Exception:
                pass  # sign-in stands; the user can still connect by hand

        asyncio.get_running_loop().create_task(_restore_connections())
        return HTMLResponse(
            _browser_page(
                "Signed in",
                "You're signed in to OpenWorker Cloud. "
                "You can close this tab and return to OpenWorker.",
            )
        )

    @app.post("/v1/connectors/{name}/connect-managed")
    async def connector_connect_managed(
        name: str, body: Optional[dict] = None
    ) -> dict[str, Any]:
        """One-click managed OAuth (requires cloud sign-in). Opens the provider
        consent page in the system browser; the broker's callback page will
        form-POST the tokens to /oauth/callback below. `access` picks a consent
        tier by NAME (e.g. hubspot read | write) — the broker owns the scopes."""
        import webbrowser

        from .. import cloud
        from ..config import load_config
        from ..connectors.descriptors import get_descriptor

        d = get_descriptor(name)
        if d is not None and d.managed_paused:
            # GUI shows the Coming-soon state; this guard covers stale GUIs/API callers.
            return {
                "ok": False,
                "error": f"one-click connect for {d.title} is coming soon — connect manually for now",
            }
        access = str((body or {}).get("access") or "")
        flow = str((body or {}).get("flow") or "")  # github: "" install | "authorize"
        out = await asyncio.to_thread(
            lambda: cloud.begin_managed_connect(
                manager.secrets, load_config(), name, access=access, flow=flow
            )
        )
        if out.get("ok"):
            webbrowser.open(out["authorize_url"])
        return out

    @app.post("/oauth/callback")
    async def managed_oauth_callback(request: Request) -> Any:
        from fastapi.responses import HTMLResponse

        from .. import cloud
        from ..connectors.setup import (
            managed_connect_connector,
            managed_connect_slack_install,
        )

        form = await request.form()
        data = {k: str(v) for k, v in form.items()}
        connector = data.get("connector", "")
        if not cloud.consume_managed_state(data.get("app_state", "")):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error="unknown or expired connection attempt",
                ),
                status_code=400,
            )
        if data.get("error"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error=data["error"],
                ),
                status_code=400,
            )
        # Managed GitHub deliberately carries NO token fields — the loopback POST
        # is routing metadata only (installation tokens are minted on demand,
        # github-relay-spec §4) — so its branch precedes the access_token check.
        if connector == "github" and data.get("installation_id"):
            from ..connectors.github_installs import managed_connect_install

            result = managed_connect_install(manager.secrets, data)
            if result.get("ok"):
                await manager.refresh_gateway()  # hot-add, like a workspace
            if not result.get("ok"):
                return HTMLResponse(
                    _browser_page(
                        "Connection failed",
                        _CONNECT_FAILED_DETAIL,
                        ok=False,
                        error=result.get("error", ""),
                    ),
                    status_code=400,
                )
            return HTMLResponse(
                _browser_page(
                    "GitHub connected",
                    "You can close this tab and return to OpenWorker.",
                    connector="github",
                )
            )
        if not connector or not data.get("access_token"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error="missing fields",
                ),
                status_code=400,
            )
        # Managed Slack is multi-workspace + relay: store the per-team bot token
        # and flip to relay mode, rather than the single-token connector path.
        if connector == "slack" and data.get("team_id"):
            result = managed_connect_slack_install(manager.secrets, data)
            if result.get("ok"):
                # Hot-add: rebuild the gateway so the new workspace's token loads
                # (and the relay socket opens on a first-ever install) right away.
                await manager.refresh_gateway()
        elif connector == "gmail":
            # Multi-account: each sign-in lands in its own gmail:account:<email>
            # profile; the first becomes the default mailbox.
            from ..connectors import gmail_accounts

            result = gmail_accounts.managed_connect_account(
                manager.secrets, cloud.managed_profile_from_callback(data)
            )
        elif connector == "google_calendar":
            # Multi-account, same shape as gmail: google_calendar:account:<email>.
            from ..connectors import gcal_accounts

            result = gcal_accounts.managed_connect_account(
                manager.secrets, cloud.managed_profile_from_callback(data)
            )
        elif connector == "hubspot" and data.get("hub_id"):
            # Multi-portal: keyed by hub_id (broker sends it like Slack's team_id).
            from ..connectors import hubspot_portals

            profile = cloud.managed_profile_from_callback(data)
            profile["hub_id"] = data.get("hub_id", "")
            if data.get("sandbox"):
                profile["sandbox"] = True
            result = hubspot_portals.managed_connect_portal(manager.secrets, profile)
        else:
            result = managed_connect_connector(
                manager.secrets, connector, cloud.managed_profile_from_callback(data)
            )
        if not result.get("ok"):
            return HTMLResponse(
                _browser_page(
                    "Connection failed",
                    _CONNECT_FAILED_DETAIL,
                    ok=False,
                    error=result.get("error", ""),
                ),
                status_code=400,
            )
        return HTMLResponse(
            _browser_page(
                f"{_connector_title(connector)} connected",
                "You can close this tab and return to OpenWorker.",
                connector=connector,
            )
        )

    @app.patch("/v1/connectors/{name}/tools")
    def connector_tools_patch(name: str, body: dict) -> dict[str, Any]:
        enabled = (body or {}).get("enabled")
        if not isinstance(enabled, dict):
            return {"ok": False, "error": "enabled map required"}
        return manager.update_connector_tools(name, enabled)

    @app.post("/v1/connectors/{name}/allow")
    def connector_allow(name: str, body: dict) -> dict[str, Any]:
        # `team_id` scopes the edit to one workspace (managed relay); absent → flat list.
        # `name` (optional) seeds the people directory so a directory-picked user's
        # chip shows their display name before they've ever sent a message.
        return manager.allow_user(
            name,
            str(body.get("user_id", "")),
            str(body.get("team_id", "")) or None,
            display_name=str(body.get("name", "")),
        )

    @app.get("/v1/connectors/slack/workspaces/{team_id}/directory")
    async def slack_directory(
        team_id: str, q: str = "", limit: int = 25
    ) -> dict[str, Any]:
        """Workspace member roster for the people picker (team_id "default" =
        the manual Socket-Mode workspace). Cached locally; never leaves this machine."""
        from ..connectors import slack_directory as roster

        return await asyncio.to_thread(
            lambda: roster.list_members(manager.secrets, team_id, q, limit)
        )

    @app.get("/v1/connectors/slack/workspaces/{team_id}/channels")
    async def slack_channels(
        team_id: str, q: str = "", limit: int = 25
    ) -> dict[str, Any]:
        """Channel roster for the channel typeahead: all public channels, private
        ones only where the bot is a member (Slack API constraint)."""
        from ..connectors import slack_directory as roster

        return await asyncio.to_thread(
            lambda: roster.list_channels(manager.secrets, team_id, q, limit)
        )

    @app.post("/v1/connectors/{name}/disallow")
    def connector_disallow(name: str, body: dict) -> dict[str, Any]:
        return manager.disallow_user(
            name, str(body.get("user_id", "")), str(body.get("team_id", "")) or None
        )

    @app.post("/v1/connectors/slack/approval-owners/add")
    def slack_approval_owner_add(body: dict) -> dict[str, Any]:
        return manager.set_slack_approval_owner(
            str(body.get("user_id", "")),
            add=True,
            display_name=str(body.get("name", "")),
        )

    @app.post("/v1/connectors/slack/approval-owners/remove")
    def slack_approval_owner_remove(body: dict) -> dict[str, Any]:
        return manager.set_slack_approval_owner(
            str(body.get("user_id", "")), add=False
        )

    # -- audit / browser observability ------------------------------------------
    @app.get("/v1/audit")
    def audit_list(
        limit: int = 100,
        session_id: str | None = None,
        connector: str | None = None,
        tool: str | None = None,
    ) -> dict[str, Any]:
        return {
            "events": manager.list_audit(
                limit=limit, session_id=session_id, connector=connector, tool=tool
            )
        }

    @app.get("/v1/browser/state")
    def browser_state_get() -> dict[str, Any]:
        return manager.browser_state()

    @app.post("/v1/browser/screenshot")
    def browser_screenshot_post() -> dict[str, Any]:
        return manager.browser_screenshot()

    @app.post("/v1/browser/close")
    def browser_close_post() -> dict[str, Any]:
        return manager.browser_close()

    # -- web search -------------------------------------------------------------
    @app.get("/v1/web-search")
    def web_search_get() -> dict[str, Any]:
        return manager.get_web_search()

    @app.post("/v1/web-search")
    def web_search_set(body: dict) -> dict[str, Any]:
        provider = (body or {}).get("provider", "")
        if not provider:
            return {"ok": False, "error": "provider required"}
        return manager.set_web_search(provider, (body or {}).get("api_key"))

    # -- model providers (OpenAI, Ollama, …) ------------------------------------
    @app.get("/v1/providers")
    def providers_get() -> list[dict[str, Any]]:
        return manager.get_providers()

    @app.post("/v1/providers")
    def providers_set(body: dict) -> dict[str, Any]:
        name = (body or {}).get("name", "")
        if not name:
            return {"ok": False, "error": "name required"}
        return manager.set_provider(name, (body or {}).get("fields"))

    @app.delete("/v1/providers/{name}")
    def providers_remove(name: str) -> dict[str, Any]:
        return manager.remove_provider(name)

    @app.post("/v1/providers/verify")
    async def providers_verify(body: dict) -> dict[str, Any]:
        # Live read-only credential check (sync httpx) — run off the event loop.
        name = (body or {}).get("name", "") or "openai"
        return await asyncio.to_thread(
            manager.verify_provider, name, (body or {}).get("fields")
        )

    # -- settings (model API key) -----------------------------------------------
    @app.get("/v1/settings")
    def settings_get() -> dict[str, Any]:
        return manager.get_settings()

    @app.post("/v1/settings/model-key")
    def settings_set_model_key(body: dict) -> dict[str, Any]:
        return manager.set_model_key((body or {}).get("api_key", ""))

    @app.post("/v1/settings/default-model")
    def settings_set_default_model(body: dict) -> dict[str, Any]:
        return manager.set_default_model((body or {}).get("model", ""))

    @app.post("/v1/settings/models/add")
    def settings_models_add(body: dict) -> dict[str, Any]:
        return manager.add_model((body or {}).get("model", ""))

    @app.post("/v1/settings/models/remove")
    def settings_models_remove(body: dict) -> dict[str, Any]:
        return manager.remove_model((body or {}).get("model", ""))

    @app.post("/v1/settings/onboarded")
    def settings_set_onboarded(body: dict) -> dict[str, Any]:
        return manager.set_onboarded(bool((body or {}).get("value", True)))

    @app.post("/v1/settings/governance")
    def settings_set_governance(body: dict) -> dict[str, Any]:
        """Set governance/sandbox choices (execution level + sandbox toggles). Persists to
        prefs and hot-applies the execution level to running engines."""
        return manager.set_governance_settings(
            execution_level=body.get("execution_level"),
            sandbox_enabled=body.get("sandbox_enabled"),
            sandbox_network=body.get("sandbox_network"),
            sandbox_deny_paths=body.get("sandbox_deny_paths"),
        )

    @app.post("/v1/settings/experimental-connectors")
    def settings_set_experimental(body: dict) -> dict[str, Any]:
        return manager.set_experimental_connectors(bool((body or {}).get("value")))

    @app.post("/v1/settings/surfaces")
    def settings_set_surfaces(body: dict) -> dict[str, Any]:
        b = body or {}
        return manager.set_surfaces(chat=b.get("chat"), code=b.get("code"))

    @app.post("/v1/settings/scratch-base")
    def settings_set_scratch_base(body: dict) -> dict[str, Any]:
        return manager.set_scratch_base(str((body or {}).get("path", "")))

    @app.post("/v1/settings/nav-layout")
    def settings_set_nav_layout(body: dict) -> dict[str, Any]:
        return manager.set_nav_layout(str((body or {}).get("nav_layout", "")))

    @app.post("/v1/settings/sessions-peek")
    def settings_set_sessions_peek(body: dict) -> dict[str, Any]:
        # Sidebar: sessions shown per group before "Show more" (owner ask, 2026-07-03).
        return manager.set_sessions_peek((body or {}).get("sessions_peek", 5))

    @app.post("/v1/settings/context-bar")
    def settings_set_context_bar(body: dict) -> dict[str, Any]:
        # Composer: show the context-window fill bar, or just the popover (owner ask).
        return manager.set_context_bar((body or {}).get("context_bar", True))

    @app.post("/v1/settings/pdf")
    def settings_set_pdf(body: dict) -> dict[str, Any]:
        # Token savings (owner ask, 2026-07-17): fallback mode for models without native
        # PDF support + attach-time page/size thresholds.
        b = body or {}
        return manager.set_pdf_settings(
            fallback=b.get("pdf_fallback"),
            max_pages=b.get("pdf_max_pages"),
            max_mb=b.get("pdf_max_mb"),
        )

    @app.post("/v1/settings/compaction")
    def settings_set_compaction(body: dict) -> dict[str, Any]:
        # Auto-compaction overrides (OPE-27): threshold % of the context window, the
        # absolute token cap, and the summarizer-model pin ("" → session's own model).
        b = body or {}
        return manager.set_compaction_settings(
            threshold_pct=b.get("compaction_threshold_pct"),
            cap_tokens=b.get("compaction_cap_tokens"),
            model=b.get("compaction_model"),
        )

    @app.post("/v1/attachments/inspect-pdf")
    def attachments_inspect_pdf(body: dict) -> dict[str, Any]:
        # Attach-time page/size probe for the composer's threshold check. Local only.
        from ..pdf_support import inspect

        return inspect(str((body or {}).get("data_url", "")))

    # -- direct-message routing -------------------------------------------------
    @app.get("/v1/messaging/dm-route")
    def dm_route_get() -> dict[str, Any]:
        return {"dm_session": manager.dm_session()}

    @app.post("/v1/messaging/dm-route")
    def dm_route_set(body: dict) -> dict[str, Any]:
        # A falsy session_id clears the designation (DMs then park as unrouted).
        return manager.set_dm_session((body or {}).get("session_id", ""))

    if os.environ.get("COWORKER_DEBUG_INJECT") == "1":
        # Dev-only (env-gated, localhost): feed a message through the real inbound path so the
        # messaging stack can be exercised without a live bot connection. Not registered otherwise.
        @app.post("/v1/_debug/inject_inbound")
        async def debug_inject_inbound(body: dict) -> dict[str, Any]:
            from ..connectors.base import MessageEvent, SessionSource

            event = MessageEvent(
                text=str((body or {}).get("text", "")),
                source=SessionSource(
                    platform=str(body.get("platform", "slack")),
                    chat_id=str(body.get("chat_id", "C0BD7KZ1AH5")),
                    user_id=str(body.get("user_id", "U07JK68S4BH")),
                    user_name=str(body.get("user_name", "tester")),
                    chat_type=str(body.get("chat_type", "channel")),
                    chat_name=str(body.get("chat_name", "")) or None,
                    thread_id=str(body.get("thread_ts", "")) or None,
                    team_id=str(body.get("team_id", "")) or None,
                ),
                message_id=str(body.get("ts", "")) or None,
                # §31 mention router: the flag is normally computed from the raw Slack text
                # at mapping time; the injector sets it directly.
                mentions_me=bool(body.get("mentions_me")),
            )
            await manager._dispatch_inbound(event)
            return {"ok": True}

    # -- automations (scheduled tasks) ------------------------------------------
    @app.get("/v1/automations")
    def automations_list() -> dict[str, Any]:
        return manager.list_automations()

    @app.post("/v1/automations")
    def automations_create(body: dict) -> dict[str, Any]:
        return manager.create_automation(body or {})

    @app.get("/v1/automations/{task_id}")
    def automation_get(task_id: str) -> dict[str, Any]:
        return manager.get_automation(task_id)

    @app.patch("/v1/automations/{task_id}")
    def automation_update(task_id: str, body: dict) -> dict[str, Any]:
        return manager.update_automation(task_id, body or {})

    @app.delete("/v1/automations/{task_id}")
    def automation_delete(task_id: str) -> dict[str, Any]:
        return manager.delete_automation(task_id)

    @app.post("/v1/automations/{task_id}/seen")
    def automations_seen(task_id: str) -> dict[str, Any]:
        return manager.mark_automation_seen(task_id)

    @app.post("/v1/automations/{task_id}/run")
    def automation_run(task_id: str) -> dict[str, Any]:
        # Prepare a live manual run; the GUI opens the returned session and drives it.
        return manager.prepare_manual_run(task_id)

    @app.post("/v1/automations/{task_id}/runs/{run_id}/finalize")
    def automation_run_finalize(task_id: str, run_id: str) -> dict[str, Any]:
        return manager.finalize_manual_run(task_id, run_id)

    @app.post("/v1/deskmate/memory")
    async def save_deskmate_memory(request: Request) -> dict[str, Any]:
        """保存 DeskMate 记忆文件（SOUL/USER/MEMORY）并广播更新到指定会话浮层。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        section = body.get("section")
        content = body.get("content", "")
        session_id_req = body.get("session_id")
        if section not in ("soul", "user", "memory"):
            return JSONResponse({"error": "invalid section"}, status_code=400)
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            mm = DeskMateMemoryManager(base_dir=desk_dir)
            message = mm.write_memory(section, content)
            fname = f"{section.upper()}.md"
            fpath = desk_dir / fname
            # 广播更新给该会话（浮层 onMemoryUpdate 会刷新对应文件）
            if session_id_req:
                try:
                    await manager.broadcast_session(
                        session_id_req,
                        {
                            "type": "deskmate_memory_update",
                            "data": {
                                "files": [
                                    {
                                        "id": section,
                                        "name": fname,
                                        "path": str(fpath),
                                        "content": content,
                                        "lastModified": fpath.stat().st_mtime * 1000
                                        if fpath.exists()
                                        else None,
                                    }
                                ]
                            },
                        },
                    )
                except Exception:
                    logging.exception("broadcast deskmate memory save failed")
            return {"ok": True, "message": message}
        except Exception:
            logging.exception("save deskmate memory failed")
            return JSONResponse({"error": "save failed"}, status_code=500)

    @app.get("/v1/deskmate/memory")
    async def get_deskmate_memory() -> dict[str, Any]:
        """读取 DeskMate 记忆文件（SOUL/USER/MEMORY），供前端打开记忆查看器时即时填充。"""
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            # Reuse a module-level singleton to avoid creating a ThreadPoolExecutor
            # on every request (this endpoint fires on every session switch + memory panel open)
            global _deskmate_mm_rest
            if "_deskmate_mm_rest" not in globals():
                _deskmate_mm_rest = DeskMateMemoryManager(base_dir=desk_dir)
            mm = _deskmate_mm_rest
            # SOUL.md 为「运行期演化层」：若文件缺失，先落盘默认模板，
            # 使其成为真实可编辑文件（否则 lastModified 为 null、标签看似空白）。
            from deskmate.memory.soul import save_soul, load_soul

            soul_path = desk_dir / "SOUL.md"
            if not soul_path.exists():
                try:
                    save_soul(desk_dir, load_soul(desk_dir))
                except Exception:
                    logging.exception("seed SOUL.md failed")
            files = []
            for fid, sec, name in (
                ("soul", "soul", "SOUL.md"),
                ("user", "user", "USER.md"),
                ("memory", "memory", "MEMORY.md"),
            ):
                path = desk_dir / name
                content = mm.read_memory(sec)
                mtime = path.stat().st_mtime * 1000 if path.exists() else None
                files.append(
                    {
                        "id": fid,
                        "name": name,
                        "path": str(path),
                        "content": content,
                        "lastModified": mtime,
                    }
                )
            return {"files": files}
        except Exception:
            logging.exception("get deskmate memory failed")
            return JSONResponse({"error": "read failed"}, status_code=500)

    @app.get("/v1/deskmate/vault")
    async def get_deskmate_vault() -> dict[str, Any]:
        """读取 DeskMate vault（Obsidian 长期记忆）的文件列表和统计。"""
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            mm = DeskMateMemoryManager(base_dir=desk_dir)
            entries = mm.list_vault_entries()
            stats = mm.vault_stats()
            return {"entries": entries, "stats": stats}
        except Exception:
            logging.exception("get deskmate vault failed")
            return JSONResponse({"error": "read failed"}, status_code=500)

    @app.post("/v1/deskmate/vault/import")
    async def import_memory_to_vault() -> dict[str, Any]:
        """把 MEMORY.md 的所有内容导入到 vault（首次启用时用）。"""
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            mm = DeskMateMemoryManager(base_dir=desk_dir)
            result = mm.import_memory_to_vault()
            return result
        except Exception:
            logging.exception("import memory to vault failed")
            return JSONResponse({"error": "import failed"}, status_code=500)

    @app.get("/v1/deskmate/learned-history")
    async def get_deskmate_learned_history(limit: int = 30) -> dict[str, Any]:
        """读取 DeskMate 学习历史（越用越懂你的时间线 + 了解度统计）。

        前端感知面板「我对你的了解」在展开时调用：返回最近学到的 facts（带时间戳+分类）
        与统计（total / 按 category 计数）。落盘在 run_turn 每轮自动记忆抽取时完成。
        """
        try:
            from pathlib import Path as _P

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            return _read_learned_history(desk_dir, limit=max(1, min(limit, 200)))
        except Exception:
            logging.exception("get deskmate learned history failed")
            return {"items": [], "stats": {"total": 0, "by_category": {}}}

    @app.get("/v1/deskmate/growth")
    async def get_deskmate_growth() -> dict[str, Any]:
        """获取 DeskMate 成长系统状态（等级/亲密度/信任感/默契度/技能熟练度/事件时间线）。

        前端成长面板在展开时调用。所有数值都有真实行为后果（proactive 阈值、
        记忆预算、技能排序、语气），非展示用空数字。
        """
        try:
            from pathlib import Path as _P

            from deskmate.growth.engine import GrowthEngine

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            return GrowthEngine(base_dir=desk_dir).snapshot()
        except Exception:
            logging.exception("get deskmate growth failed")
            return JSONResponse({"error": "growth failed"}, status_code=500)

    @app.post("/v1/deskmate/growth/proactive")
    async def set_deskmate_growth_proactive(request: Request) -> dict[str, Any]:
        """设置信任感是否调制 proactive 主动打扰阈值。

        Body: {"control": true/false}
        - control=true  → 信任感影响主动打扰（默认）
        - control=false → 关闭该调制
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        control = body.get("control")
        if control is None or not isinstance(control, bool):
            return JSONResponse(
                {"error": "missing or invalid 'control' (must be boolean)"},
                status_code=400,
            )
        try:
            from pathlib import Path as _P

            from deskmate.growth.engine import GrowthEngine

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            engine = GrowthEngine(base_dir=desk_dir)
            engine.state.trust_controls_proactive = control
            engine.save()
            return {"ok": True, "control": control}
        except Exception:
            logging.exception("deskmate growth proactive failed")
            return JSONResponse({"error": "set failed"}, status_code=500)

    @app.post("/v1/deskmate/feedback")
    async def deskmate_feedback(request: Request) -> dict[str, Any]:
        """记录用户对主动建议的反馈（接受/忽略），写入防打扰持久化文件。

        前端 DeskMateOverlay 在用户点「接受」/「忽略」时发出 deskmate:feedback
        CustomEvent，App.tsx 监听后调本端点。本端点直接操作 DisturbManager 的
        持久化文件（与 proactivity MCP 服务用的是同一份 disturb_state.json），
        这样防打扰规则（连续忽略 3 次降级、30 分钟冷却）就能真正生效。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        suggestion_key = body.get("suggestion_key", "")
        accepted = bool(body.get("accepted", False))
        if not suggestion_key:
            return JSONResponse({"error": "missing suggestion_key"}, status_code=400)
        try:
            from pathlib import Path as _P

            from deskmate.proactivity.disturb import DisturbManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            disturb_path = desk_dir / "memory" / "disturb_state.json"
            mgr = DisturbManager()
            mgr.persist(disturb_path)  # 加载已有的持久化状态
            mgr.record_feedback(suggestion_key, accepted)
            # record_feedback 内部会调 _persist()，但 DisturbManager 可能未设 persist_path
            mgr.persist_path = disturb_path
            mgr._persist()
            return {"ok": True, "accepted": accepted}
        except Exception:
            logging.exception("deskmate feedback failed")
            return JSONResponse({"error": "feedback failed"}, status_code=500)

    @app.get("/v1/deskmate/proactive")
    async def get_deskmate_proactive_status() -> dict[str, Any]:
        """获取 DeskMate 主动推理状态（静默模式、统计）。"""
        try:
            from pathlib import Path as _P

            from deskmate.proactivity.disturb import DisturbManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            disturb_path = desk_dir / "memory" / "disturb_state.json"
            mgr = DisturbManager()
            mgr.persist(disturb_path)
            stats = mgr.get_stats()
            return {
                "silent": mgr.silent_mode,
                "stats": stats,
            }
        except Exception:
            logging.exception("deskmate proactive status failed")
            return JSONResponse({"error": "status failed"}, status_code=500)

    @app.post("/v1/deskmate/proactive")
    async def toggle_deskmate_proactive(request: Request) -> dict[str, Any]:
        """切换 DeskMate 主动推理开关（静默模式）。

        Body: {"enabled": true/false}
        - enabled=true  → 取消静默模式，恢复主动建议
        - enabled=false → 开启静默模式，暂停所有建议
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        enabled = body.get("enabled")
        if enabled is None or not isinstance(enabled, bool):
            return JSONResponse(
                {"error": "missing or invalid 'enabled' (must be boolean)"},
                status_code=400,
            )
        try:
            from pathlib import Path as _P

            from deskmate.proactivity.disturb import DisturbManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            disturb_path = desk_dir / "memory" / "disturb_state.json"
            mgr = DisturbManager()
            mgr.persist(disturb_path)
            mgr.persist_path = disturb_path
            mgr.set_silent_mode(not enabled)  # enabled=true → silent=false
            return {"ok": True, "enabled": enabled, "silent": mgr.silent_mode}
        except Exception:
            logging.exception("deskmate proactive toggle failed")
            return JSONResponse({"error": "toggle failed"}, status_code=500)

    @app.post("/v1/deskmate/proactive/tick")
    async def deskmate_proactive_tick(request: Request) -> dict[str, Any]:
        """执行一次 DeskMate 主动推理 TICK（感知->思考->行动->学习）。

        前端每 15 秒调一次，驱动桌宠状态更新和主动建议。
        engine.tick() is synchronous (subprocess + LLM call) so it MUST run
        via asyncio.to_thread to avoid freezing the event loop for 2-10s,
        which would block session switching, message loading, and WS events.
        """
        try:
            body = await request.json() if request.headers.get("content-type") else {}
        except Exception:
            body = {}
        try:
            from pathlib import Path as _P

            from deskmate.proactivity.tick import ProactiveEngine

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            # Reuse a cached ProactiveEngine to preserve DisturbManager state
            # and avoid re-importing + re-initializing on every tick.
            global _deskmate_tick_engine
            if "_deskmate_tick_engine" not in globals():
                _deskmate_tick_engine = ProactiveEngine(base_dir=desk_dir)
            engine = _deskmate_tick_engine
            engine.disturb.persist(desk_dir / "memory" / "disturb_state.json")
            # Run the sync tick in a thread to avoid blocking the event loop.
            # The tick does: subprocess (osascript/pbpaste) + LLM call + evaluate.
            result = await asyncio.to_thread(
                engine.tick,
                user_message=body.get("user_message", ""),
                skip_activity_check=bool(body.get("skip_activity_check", False)),
            )
            return result.to_dict()
        except Exception:
            logging.exception("deskmate proactive tick failed")
            return JSONResponse({"error": "tick failed"}, status_code=500)

    @app.post("/v1/deskmate/style-profile")
    async def deskmate_style_profile(request: Request) -> dict[str, Any]:
        """从本地样本文件抽作者风格画像，存 ~/.deskmate/style_profile.json。

        Body: {"path": "/abs/path/to/sample.txt|.docx"}
        返回: {"ok": true, "profile": {...}, "chars": n}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        path = (body.get("path") or "").strip()
        content_b64 = (body.get("content_base64") or "").strip()
        filename = (body.get("filename") or "sample.txt").strip()
        if not path and not content_b64:
            return JSONResponse({"error": "missing path or content"}, status_code=400)
        try:
            from pathlib import Path as _P

            from deskmate.writing import (
                extract_style_profile,
                read_text_from_path,
                read_text_from_base64,
            )
            from datetime import datetime as _dt

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            if path:
                sample = read_text_from_path(path)
                source = path
            else:
                sample = read_text_from_base64(filename, content_b64)
                source = f"{filename}（上传）"
            if not sample:
                return JSONResponse({"error": "文件内容为空"}, status_code=400)
            profile = extract_style_profile(sample)
            profile_path = desk_dir / "style_profile.json"
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "profile": profile,
                        "source": source,
                        "chars": len(sample),
                        "updated_at": _dt.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return {"ok": True, "profile": profile, "chars": len(sample)}
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            logging.exception("deskmate style-profile failed")
            return JSONResponse({"error": "风格抽取失败"}, status_code=500)

    @app.get("/v1/deskmate/style-profile")
    async def get_deskmate_style_profile() -> dict[str, Any]:
        """读取已保存的作者风格画像（无则返回 {"profile": null}）。"""
        try:
            from pathlib import Path as _P

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            p = desk_dir / "style_profile.json"
            if not p.is_file():
                return {"profile": None}
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"profile": data.get("profile")}
        except Exception:
            logging.exception("get deskmate style-profile failed")
            return {"profile": None}

    @app.post("/v1/deskmate/chapters")
    async def deskmate_chapters(request: Request) -> dict[str, Any]:
        """返回文稿章节列表（按 第X章/回/节 切分），供前端章节下拉。

        Body: {"text": "...", "path": "本地路径", "content_base64"+ "filename": "..."}。
        """
        try:
            body = await request.json() if request.headers.get("content-type") else {}
        except Exception:
            body = {}
        try:
            from pathlib import Path as _P

            from deskmate.writing import split_into_chapters, read_text_from_path, read_text_from_base64

            raw_text = (body.get("text") or "").strip()
            path = (body.get("path") or "").strip()
            content_b64 = (body.get("content_base64") or "").strip()
            filename = (body.get("filename") or "draft.txt").strip()
            if not raw_text and path:
                raw_text = read_text_from_path(path)
            if not raw_text and content_b64:
                raw_text = read_text_from_base64(filename, content_b64)
            if not raw_text:
                return JSONResponse({"error": "missing text or path"}, status_code=400)
            return {"chapters": [
                {"idx": c["idx"], "title": c["title"], "char_count": len(c["text"]), "text": c["text"]}
                for c in split_into_chapters(raw_text)
            ]}
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            logging.exception("deskmate chapters failed")
            return JSONResponse({"error": "章节解析失败"}, status_code=500)

    @app.post("/v1/deskmate/proofread")
    async def deskmate_proofread(request: Request) -> dict[str, Any]:
        """对文稿做专业编辑审阅，逐章流式产出。

        逐章调 proofread_chapter，每章完成即把【原文】/【建议】/【理由】块作为助手消息
        落盘并进聊天；某章失败写提示并继续（不怕中间出问题）；最后补一张总览报告卡。

        Body: {"text": "...", "path": "...", "content_base64": "...", "filename": "...",
               "chapter": 可选 int/str, "session_id": "...", "title": "..."}
        """
        try:
            body = await request.json() if request.headers.get("content-type") else {}
        except Exception:
            body = {}
        try:
            import time as _t
            from pathlib import Path as _P

            from deskmate.writing import (
                proofread_chapter,
                format_chapter_block,
                format_report_card,
                generate_letter,
                split_into_chapters,
                _select_chapter,
                format_profile_for_prompt,
                read_text_from_path,
                read_text_from_base64,
            )

            raw_text = (body.get("text") or "").strip()
            path = (body.get("path") or "").strip()
            content_b64 = (body.get("content_base64") or "").strip()
            filename = (body.get("filename") or "draft.txt").strip()
            if not raw_text and path:
                raw_text = read_text_from_path(path)
            if not raw_text and content_b64:
                raw_text = read_text_from_base64(filename, content_b64)
            if not raw_text:
                return JSONResponse({"error": "missing text or path"}, status_code=400)

            # 章节选择（int 序号 / str 标题）；None 表示审整篇
            chapter = body.get("chapter")
            if isinstance(chapter, str):
                chapter = chapter.strip() or None

            # 载入风格画像（可选）
            profile = None
            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            sp = desk_dir / "style_profile.json"
            if sp.is_file():
                try:
                    data = json.loads(sp.read_text(encoding="utf-8"))
                    profile = data.get("profile")
                except Exception:
                    pass

            # 拆分 / 选章
            chapters = split_into_chapters(raw_text)
            target = _select_chapter(chapters, chapter)
            if target:
                work_units = [(target["idx"], target["title"], target["text"])]
            else:
                work_units = [(c["idx"], c["title"], c["text"]) for c in chapters]
            profile_text = format_profile_for_prompt(profile)

            session_id = (body.get("session_id") or "").strip()
            title = (body.get("title") or "").strip()
            label = f"《{title}》" if title else "草稿"

            # ---- 逐章流式：每章完成即落盘 + 推聊天 ----
            all_items: list[dict] = []
            per_chapter: list = []
            now = int(_t.time())

            if session_id:
                try:
                    await manager.broadcast_session(session_id, {"type": "turn_start", "data": {}})
                    manager.append_session_messages(
                        session_id, [{"role": "user", "content": f"📝 文稿编校：{label}", "ts": now}]
                    )
                except Exception:
                    logging.warning("proofread: 会话初始化失败（继续处理）")

            for _idx, ctitle, ctext in work_units:
                try:
                    res = await asyncio.to_thread(proofread_chapter, ctitle, ctext, profile_text)
                    items = res["items"]
                    block = format_chapter_block(ctitle, items)
                    all_items.extend(items)
                    per_chapter.append((ctitle, len(items)))
                    if session_id:
                        ts = int(_t.time())
                        manager.append_session_messages(
                            session_id, [{"role": "assistant", "content": block, "ts": ts}]
                        )
                        await manager.broadcast_session(
                            session_id, {"type": "assistant_message", "data": {"text": block}}
                        )
                except Exception as e:
                    logging.warning("proofread chapter %s failed: %s", ctitle, e)
                    if session_id:
                        note = f"⚠️ 《{label}》「{ctitle}」审阅失败：{e}，已跳过该章，其余继续。"
                        ts = int(_t.time())
                        manager.append_session_messages(
                            session_id, [{"role": "assistant", "content": note, "ts": ts}]
                        )
                        await manager.broadcast_session(
                            session_id, {"type": "assistant_message", "data": {"text": note}}
                        )

            # ---- 结尾总览报告卡 ----
            try:
                letter = await asyncio.to_thread(generate_letter, all_items)
            except Exception:
                letter = ""
            total = len(all_items)
            card = format_report_card(label, per_chapter, total, letter)
            if session_id:
                ts = int(_t.time())
                manager.append_session_messages(
                    session_id, [{"role": "assistant", "content": card, "ts": ts}]
                )
                await manager.broadcast_session(
                    session_id, {"type": "assistant_message", "data": {"text": card}}
                )
                await manager.broadcast_session(session_id, {"type": "turn_done", "data": {}})

            return {
                "letter": letter,
                "items": all_items,
                "report": card,
                "chapters": [{"idx": c["idx"], "title": c["title"], "char_count": len(c["text"])}
                             for c in chapters],
                "current_chapter": chapter if chapter is not None else (target["idx"] if target else None),
                "total_items": total,
            }
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            logging.exception("deskmate proofread failed")
            return JSONResponse({"error": "批改失败"}, status_code=500)

    @app.post("/v1/deskmate/manuscript")
    async def deskmate_save_manuscript(request: Request) -> dict[str, Any]:
        """保存整本书稿（含各章正文），供聊天「审阅第X章」直接调出。

        Body: {"name": "书名", "chapters": [{idx,title,text}, ...]}
        """
        try:
            import re as _re

            from deskmate.writing import save_manuscript

            body = await request.json()
            name = (body.get("name") or "").strip()
            chapters = body.get("chapters") or []
            if not name:
                return JSONResponse({"error": "missing name"}, status_code=400)
            if not isinstance(chapters, list) or not chapters:
                return JSONResponse({"error": "missing chapters"}, status_code=400)
            safe = _re.sub(r"[\\/]", "_", name)
            return save_manuscript(safe, chapters)
        except Exception:
            logging.exception("save manuscript failed")
            return JSONResponse({"error": "保存书稿失败"}, status_code=500)

    @app.get("/v1/deskmate/manuscripts")
    async def deskmate_list_manuscripts() -> dict[str, Any]:
        try:
            from deskmate.writing import list_manuscripts

            return {"manuscripts": list_manuscripts()}
        except Exception:
            logging.exception("list manuscripts failed")
            return {"manuscripts": []}

    @app.get("/v1/deskmate/manuscript/{name}")
    async def deskmate_load_manuscript(name: str) -> dict[str, Any]:
        try:
            from deskmate.writing import load_manuscript

            safe = name.strip()
            chapters = load_manuscript(safe)
            if chapters is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return {"name": safe, "chapters": chapters}
        except Exception:
            logging.exception("load manuscript failed")
            return JSONResponse({"error": "读取书稿失败"}, status_code=500)

    def _safe_report_name(name: str) -> str:
        s = (name or "").strip().replace("\x00", "").replace("/", "_").replace("\\", "_")
        while ".." in s:
            s = s.replace("..", "_")
        s = s.lstrip(". ")
        return s or "manuscript"

    @app.post("/v1/deskmate/report")
    async def deskmate_generate_report(request: Request) -> dict[str, Any]:
        """生成整书深度审阅报告（逐章细抠 + 总报告）—— 同步执行。

        注意：编辑专家 Agent 会话已改用原生 Agent Loop + review/generate_report 工具，
        本端点保留用于向后兼容（如直接 API 调用）。
        """
        try:
            body = await request.json() if request.headers.get("content-type") else {}
        except Exception:
            body = {}
        try:
            from pathlib import Path as _P

            from deskmate.report import generate_review_report
            from deskmate.writing import (
                load_manuscript,
                read_text_from_base64,
                read_text_from_path,
            )

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )

            name = (body.get("name") or "").strip()
            raw_text = (body.get("text") or "").strip()
            path = (body.get("path") or body.get("folder") or "").strip()
            content_b64 = (body.get("content_base64") or "").strip()
            filename = (body.get("filename") or "draft.txt").strip()
            bible: dict = {}

            if name:
                chapters = load_manuscript(name)
                if not chapters:
                    return JSONResponse({"error": f"未找到书稿：{name}"}, status_code=404)
                raw_text = "\n\n".join(
                    f"{c.get('title', '')}\n{c.get('text', '')}" for c in chapters
                )
            elif path:
                from deskmate.novel_memory.core import read_bible, scan_novel

                pp = _P(path).expanduser()
                if pp.is_dir():
                    try:
                        raw_text, _ = scan_novel(str(pp))
                    except FileNotFoundError as e:
                        return JSONResponse({"error": str(e)}, status_code=404)
                    if not raw_text.strip():
                        return JSONResponse(
                            {"error": f"该文件夹未找到可读的小说文本（.txt/.md 等）：{path}"},
                            status_code=400,
                        )
                    try:
                        bible = read_bible(str(pp))
                    except Exception:
                        bible = {}
                elif pp.is_file():
                    try:
                        raw_text = read_text_from_path(str(pp))
                    except FileNotFoundError as e:
                        return JSONResponse({"error": str(e)}, status_code=404)
                    except ValueError as e:
                        return JSONResponse({"error": str(e)}, status_code=400)
                    try:
                        bible = read_bible(str(pp.parent))
                    except Exception:
                        bible = {}
                else:
                    return JSONResponse({"error": f"路径不存在：{path}"}, status_code=404)
            elif content_b64:
                try:
                    raw_text = read_text_from_base64(filename, content_b64)
                except ValueError as e:
                    return JSONResponse({"error": str(e)}, status_code=400)
            if not raw_text:
                return JSONResponse(
                    {"error": "缺少文稿来源（name/text/path/folder/content_base64）"},
                    status_code=400,
                )

            profile = None
            sp = desk_dir / "style_profile.json"
            if sp.is_file():
                try:
                    profile = json.loads(sp.read_text(encoding="utf-8")).get("profile")
                except Exception:
                    pass

            src_name = name or (path.rsplit("/", 1)[-1] if path else "manuscript")
            safe = _safe_report_name(src_name)
            chapter_indices = body.get("chapter_indices")

            # 同步生成报告
            report = await asyncio.to_thread(
                generate_review_report,
                raw_text, profile, safe, bible or None, chapter_indices,
            )

            # 保存报告
            rdir = desk_dir / "reports"
            rdir.mkdir(parents=True, exist_ok=True)
            (rdir / f"{safe}.json").write_text(
                json.dumps({"name": safe, "report": report}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )

            return {"ok": True, "report": report, "report_name": safe}
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            logging.exception("deskmate report failed")
            return JSONResponse({"error": "生成审阅报告失败"}, status_code=500)

    @app.get("/v1/deskmate/reports")
    async def deskmate_list_reports() -> dict[str, Any]:
        """列出已生成的审阅报告（按生成时间倒序）。"""
        try:
            from pathlib import Path as _P

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            rdir = desk_dir / "reports"
            out: list[dict] = []
            if rdir.is_dir():
                for p in sorted(
                    rdir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True
                ):
                    try:
                        data = json.loads(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    rep = data.get("report", {})
                    meta = rep.get("meta", {})
                    out.append(
                        {
                            "name": data.get("name", p.stem),
                            "title": rep.get("title", ""),
                            "generated_at": rep.get("generated_at", ""),
                            "chapter_count": meta.get("chapter_count", 0),
                        }
                    )
            return {"reports": out}
        except Exception:
            logging.exception("list reports failed")
            return {"reports": []}

    @app.get("/v1/deskmate/report/{name}/docx")
    async def deskmate_report_docx(name: str) -> "Response":
        """下载指定审阅报告的 Word(.docx) 文档。"""
        try:
            from pathlib import Path as _P
            from urllib.parse import quote

            from fastapi.responses import Response as _Resp

            from deskmate.report import render_report_docx

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            p = desk_dir / "reports" / f"{name.strip()}.json"
            if not p.is_file():
                return JSONResponse({"error": "报告不存在"}, status_code=404)
            data = json.loads(p.read_text(encoding="utf-8"))
            report = data.get("report")
            if not report:
                return JSONResponse({"error": "报告内容为空"}, status_code=500)
            docx_bytes = render_report_docx(report)
            fname = f"{name.strip()}_审阅报告.docx"
            return _Resp(
                content=docx_bytes,
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                headers={
                    "Content-Disposition": (
                        f"attachment; filename=\"{quote(fname)}\"; "
                        f"filename*=UTF-8''{quote(fname)}"
                    )
                },
            )
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            logging.exception("deskmate report docx failed")
            return JSONResponse({"error": "生成 Word 失败"}, status_code=500)

    @app.websocket("/ws/session/{session_id}")
    async def ws_session(ws: WebSocket, session_id: str) -> None:
        if not _websocket_authenticated(ws):
            await ws.close(code=1008)
            return
        # CORS never gates WebSockets, so a cross-site page could otherwise open this socket
        # and drive the session into tool calls. Reject a disallowed browser Origin before
        # accepting the handshake (1008 = policy violation).
        if not _origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="openworker" if api_token else None)
        agent = ws.query_params.get("agent") or "code"

        # All four interactive prompts (approval / question / directory / plan) are parked as Inbox
        # items and awaited via inbox.wait — so they survive a dropped socket (redelivered on
        # reconnect) and can be resolved from any surface. `visibility` decides where they SHOW:
        # Unattended → the cross-session Inbox; attended → inline in this session only. The agent
        # stays blocked until the item is resolved (live WS response, REST, or a bound channel).
        def _visibility() -> str:
            return (
                VIS_INBOX
                if manager.unattended.is_unattended(session_id)
                else VIS_INLINE
            )

        async def _mirror(item) -> None:
            # Unattended items mirror to a bound channel as buttons (see mirror_inbox_item).
            await manager.mirror_inbox_item(item)

        def _route() -> str:
            return manager.inbox_routing.route_for(session_id, agent)

        async def approver(_request) -> ApprovalOutcome:
            # The engine has already emitted PERMISSION_REQUIRED (the live inline card). Park the
            # item so the answer can also come from the Inbox / a reconnect / after a restart.
            item = manager.inbox.add_approval(
                session_id,
                f"Run `{_request.tool_name}`?",
                body="\n".join(
                    p
                    for p in (
                        (getattr(_request, "reason", "") or "").strip(),
                        args_preview(getattr(_request, "arguments", None)),
                    )
                    if p
                ),
                inbox=_route(),
                visibility=_visibility(),
                # Automation-run context (manual "Run now" rides this socket): lets the
                # card offer the task-persistent "Allow every time" (§25). {} elsewhere.
                data=manager.approval_prompt_data(session_id, _request),
                tool_call_id=getattr(_request, "tool_call_id", None),
            )
            if (
                item.state == "pending"
            ):  # freshly raised (not a durable-resume re-raise)
                manager.persist_session(
                    session_id
                )  # the pending tool call is now on disk
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resolution = await manager.inbox.wait(item.id)
            # Accept every vocabulary: the live card sends once/always_tool/always_command/
            # always_task/deny; the Inbox / a channel send allow/always/deny.
            return manager.approval_outcome(resolution, _request, session_id)

        async def question_asker(args: dict, tool_call_id=None) -> dict:
            # ask_user (engine does NOT emit the event — we do, only when attended).
            item = manager.inbox.add_question(
                session_id,
                str(args.get("question", "")),
                inbox=_route(),
                visibility=_visibility(),
                options=list(args.get("options") or []),
                allow_text=bool(args.get("allow_text", True)),
                multi=bool(args.get("multi", False)),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
                else:
                    await ws.send_json(
                        {
                            "type": "question_requested",
                            "data": {
                                "question": item.title,
                                "options": item.options,
                                "allow_text": item.allow_text,
                                "multi": item.multi,
                                "header": str(args.get("header", "")),
                            },
                        }
                    )
            return {"answer": await manager.inbox.wait(item.id)}

        async def directory_requester(args: dict, tool_call_id=None) -> dict:
            # The engine has already emitted DIRECTORY_REQUESTED. Park, await, then apply the grant.
            item = manager.inbox.add_directory(
                session_id,
                "Grant access to a folder?",
                body=str(args.get("reason", "")),
                inbox=_route(),
                visibility=_visibility(),
                data={
                    "path": str(args.get("path", "")),
                    "writable": bool(args.get("writable", False)),
                },
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(
                await manager.inbox.wait(item.id)
            )  # {granted, path, writable}
            if not resp.get("granted"):
                return {"granted": False, "reason": "the user declined the request"}
            path = (resp.get("path") or args.get("path") or "").strip()
            if not path:
                return {"granted": False, "error": "no directory was provided"}
            writable = bool(resp.get("writable", args.get("writable", False)))
            res = manager.add_root(session_id, path, writable)
            if not res.get("ok"):
                return {
                    "granted": False,
                    "error": res.get("error", "could not grant access"),
                }
            primary = next(
                (
                    r
                    for r in res.get("roots", [])
                    if r.get("path")
                    and Path(r["path"]).expanduser().resolve()
                    == Path(path).expanduser().resolve()
                ),
                None,
            )
            return {
                "granted": True,
                "path": (primary or {}).get("path", path),
                "writable": writable,
            }

        async def plan_approver(_args: dict, tool_call_id=None) -> dict:
            # The engine has already emitted PLAN_PROPOSED. Park, await the verdict.
            item = manager.inbox.add_plan(
                session_id,
                "Approve the plan?",
                body=str(_args.get("plan", "")),
                inbox=_route(),
                visibility=_visibility(),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(
                await manager.inbox.wait(item.id)
            )  # {approved, mode, feedback}
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user rejected the plan",
                }
            return {"approved": True, "mode": resp.get("mode") or "interactive"}

        async def _apply_model(model: Optional[str]) -> None:
            # Mid-session rebind is allowed (roadmap item 3, supersedes the 2026-07-04
            # lock): history is canonical and providers convert per call. A real switch
            # appends a persisted notice; broadcast it so live views render the marker
            # and update their header. Never rebind mid-turn — the running loop reads
            # `engine.model` per iteration and a mixed turn is exactly the breakage the
            # old lock existed to prevent.
            if not model or manager.is_running(session_id):
                return
            notice = engine.switch_model(model)
            if notice is None:  # same model, or first bind on a fresh session
                return
            manager.persist_session(session_id)
            await manager.broadcast_session(
                session_id,
                {"type": "model_changed", "data": {"model": model, "text": notice}},
            )

        def _resolve_pending(resolution: str) -> None:
            # Live WS responses resolve THE session's single pending prompt (one at a time, since the
            # agent blocks). Reconnect / Inbox resolve by id via REST instead.
            pend = manager.inbox.pending(session_id)
            if pend:
                manager.inbox.resolve(pend[0].id, resolution)

        workspace = ws.query_params.get("workspace")
        mcp_tools = await manager.prepare_mcp_tools(
            session_id, workspace=workspace, agent=agent
        )
        engine = manager.get_engine(
            session_id,
            workspace=workspace,
            agent=agent,
            approver=approver,
            extra_tools=mcp_tools,
            directory_requester=directory_requester,
            plan_approver=plan_approver,
            question_asker=question_asker,
        )
        if engine is None:
            await ws.send_json(
                {
                    "type": "error",
                    "data": {
                        "error": "no valid workspace — choose a project folder first"
                    },
                }
            )
            await ws.close()
            return
        # Auto-compaction failure prompt (OPE-27): only an ATTENDED session may be asked
        # Retry/Trim — unattended runs auto-trim (the policy in engine._compact_now).
        engine.is_attended = lambda: _visibility() == VIS_INLINE
        await ws.send_json(
            {
                "type": "ready",
                "data": {
                    "session_id": session_id,
                    "agent": getattr(engine, "agent_name", "code"),
                    "model": engine.model,
                    "mode": engine.permissions.mode.value,
                    "workspace": (
                        str(getattr(engine, "executor").cwd)
                        if getattr(engine, "executor", None)
                        else None
                    ),
                    "command_trust": manager.workspace_command_trust(
                        str(getattr(engine, "audit_context", {}).get("workspace", ""))
                    ),
                },
            }
        )

        # Checkpoint events: persist mid-turn so a crash/quit can't eat the conversation.
        # turn_start = the user message just landed (a brand-new session gets its row here,
        # not at connect — empty never-used sessions shouldn't appear in Recents);
        # permission_required/directory_requested = parked indefinitely on the user;
        # iteration_end = a model response + its tool results completed.
        _CHECKPOINTS = {
            "turn_start",
            "permission_required",
            "directory_requested",
            "plan_proposed",
            "iteration_end",
        }

        async def _push_deskmate_memory() -> None:
            """把 DeskMate 记忆文件（SOUL/USER/MEMORY）实时推给当前会话的浮层。

            仅在 agent=="deskmate" 时执行；cowork 等其它 agent 不触发。记忆读取是纯文件 IO，
            失败不影响对话主流程。
            """
            if agent != "deskmate":
                return
            try:
                from pathlib import Path as _P

                from deskmate.memory.manager import DeskMateMemoryManager

                desk_dir = _P(
                    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
                )
                # Reuse cached manager to avoid creating a new ThreadPoolExecutor
                # on every turn_start (leaks threads + adds ~50ms init overhead)
                if not hasattr(ws_session, "_deskmate_mm_cache"):
                    ws_session._deskmate_mm_cache = {}
                mm_cache_key = str(desk_dir)
                mm = ws_session._deskmate_mm_cache.get(mm_cache_key)
                if mm is None:
                    mm = DeskMateMemoryManager(base_dir=desk_dir)
                    ws_session._deskmate_mm_cache[mm_cache_key] = mm
                files = []
                for fid, sec, name in (
                    ("soul", "soul", "SOUL.md"),
                    ("user", "user", "USER.md"),
                    ("memory", "memory", "MEMORY.md"),
                ):
                    path = desk_dir / name
                    content = await asyncio.to_thread(mm.read_memory, sec)
                    mtime = path.stat().st_mtime * 1000 if path.exists() else None
                    files.append(
                        {
                            "id": fid,
                            "name": name,
                            "path": str(path),
                            "content": content,
                            "lastModified": mtime,
                        }
                    )
                await manager.broadcast_session(
                    session_id,
                    {"type": "deskmate_memory_update", "data": {"files": files}},
                )
            except Exception:
                logging.exception("push deskmate memory failed")


        # -- DeskMate 学习历史（越用越懂你的时间线 + 了解度统计）-----------
        # _classify_learned_fact / _append_learned_history / _read_learned_history
        # 已提升到 create_app 顶层，供 @app.get 路由和此闭包共用。

        async def _deskmate_post_turn(
            session_id: str, agent: str, engine, manager
        ) -> None:
            """DeskMate post-turn processing, run as a fire-and-forget background task.

            All sync LLM calls and file I/O run via asyncio.to_thread so the event
            loop is never blocked. Broadcasts (memory_learned, context_injected)
            happen after the blocking work completes; a small delay is acceptable
            since these are informational UI updates, not turn-flow-critical events.
            """
            try:
                last_user = ""
                last_asst = ""
                for msg in reversed(engine.messages):
                    role = msg.get("role")
                    content = msg.get("content", "")
                    if not content:
                        continue
                    if role == "assistant" and not last_asst:
                        last_asst = content if isinstance(content, str) else str(content)
                    elif role == "user" and not last_user:
                        last_user = content if isinstance(content, str) else str(content)
                    if last_user and last_asst:
                        break
                if not (last_user or last_asst):
                    return

                from pathlib import Path as _P
                from deskmate.memory.manager import DeskMateMemoryManager

                desk_dir = _P(
                    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
                )
                # Reuse cached manager to avoid creating a new ThreadPoolExecutor each turn
                if not hasattr(ws_session, "_deskmate_mm_cache"):
                    ws_session._deskmate_mm_cache = {}
                mm_cache_key = str(desk_dir)
                mm = ws_session._deskmate_mm_cache.get(mm_cache_key)
                if mm is None:
                    mm = DeskMateMemoryManager(base_dir=desk_dir)
                    ws_session._deskmate_mm_cache[mm_cache_key] = mm

                # post_session_summary makes a sync LLM call - run in a thread
                summary = await asyncio.to_thread(
                    mm.post_session_summary, last_user, last_asst
                )
                learned = (summary or {}).get("learned") or []
                if learned:
                    try:
                        _append_learned_history(desk_dir, learned)
                    except Exception:
                        logging.exception("persist learned history failed")
                    try:
                        await manager.broadcast_session(
                            session_id,
                            {"type": "deskmate_memory_learned", "data": {"facts": learned}},
                        )
                    except Exception:
                        logging.exception("broadcast deskmate memory learned failed")

                # Context injection visibility: count injected memory blocks
                from deskmate.memory.llm import llm_available as _llm_ok
                _auto_inject = (
                    os.environ.get("DESKMATE_AUTO_INJECT") == "1" or _llm_ok()
                )
                if _auto_inject:
                    try:
                        ctx = await asyncio.to_thread(
                            mm.build_full_context_for_turn, last_user
                        )
                        if ctx:
                            inner = ctx
                            for tag in ("<memory-context>", "</memory-context>"):
                                inner = inner.replace(tag, "")
                            injected = inner.count("## ")
                        else:
                            injected = 0
                        if injected > 0:
                            await manager.broadcast_session(
                                session_id,
                                {"type": "deskmate_context_injected", "data": {"count": injected}},
                            )
                    except Exception:
                        logging.exception("broadcast context injected failed")
            except Exception:
                logging.exception("deskmate post-turn processing failed")

        async def run_turn(content, *, retry: bool = False) -> None:
            # The receive loop atomically claims this session before scheduling the task.
            # Keeping the claim outside prevents two back-to-back frames from both starting.
            try:
                events = engine.retry() if retry else engine.run(content)
                async for event in events:
                    # Broadcast to every socket viewing this session (this socket included — it's a
                    # registered client), so a second view of the same session stays in sync too.
                    await manager.broadcast_session(
                        session_id, {"type": event.type.value, "data": event.data}
                    )
                    if event.type.value in _CHECKPOINTS:
                        manager.save(session_id, engine)
                    if event.type.value == "turn_start":
                        await _push_deskmate_memory()
            finally:
                manager.mark_idle(session_id)
                manager.save(session_id, engine)
                # turn_done MUST be sent before any DeskMate post-processing:
                # the post-processing includes a sync LLM call (post_session_summary)
                # that would freeze the event loop for 2-10s, delaying turn_done
                # and blocking interrupt/session-switch/other WS events.
                await manager.broadcast_session(
                    session_id, {"type": "turn_done", "data": {}}
                )
                # DeskMate post-processing runs as a fire-and-forget background
                # task so it never blocks the event loop or the turn_done signal.
                if agent == "deskmate":
                    asyncio.create_task(
                        _deskmate_post_turn(session_id, agent, engine, manager)
                    )

        # This socket is now a live view of the session; background turns (channel delivery,
        # self-wake, durable resume) broadcast here too, not just locally driven run_turns.
        manager.register_session_client(session_id, ws.send_json)
        inbound_times: deque[float] = deque()

        async def reject_input(reason: str) -> None:
            # Input validation failures are not provider failures and must not offer "Retry"
            # or flush an in-progress assistant stream in the GUI.
            await ws.send_json({"type": "input_rejected", "data": {"error": reason}})

        async def claim_turn(*, retry: bool = False, content=None) -> None:
            if not manager.try_mark_running(session_id):
                await reject_input(
                    "This session is already running a turn. Wait for it to finish or stop it."
                )
                return
            asyncio.create_task(run_turn(content, retry=retry))

        try:
            while True:
                try:
                    message = await ws.receive_json()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await reject_input("Invalid WebSocket message: expected JSON.")
                    continue

                now = asyncio.get_running_loop().time()
                while (
                    inbound_times
                    and now - inbound_times[0] > _WS_RATE_LIMIT_WINDOW_SECONDS
                ):
                    inbound_times.popleft()
                if len(inbound_times) >= _WS_RATE_LIMIT_COUNT:
                    await reject_input("Too many WebSocket messages; reconnect and try again.")
                    await ws.close(code=1008)
                    return
                inbound_times.append(now)

                if not isinstance(message, dict):
                    await reject_input("Invalid WebSocket message: expected an object.")
                    continue
                kind = message.get("type")
                if not isinstance(kind, str):
                    await reject_input("Invalid WebSocket message: missing string type.")
                    continue
                if kind == "approval":
                    _resolve_pending(message.get("decision", "deny"))
                elif kind == "directory_response":
                    _resolve_pending(
                        json.dumps(
                            {
                                "granted": bool(message.get("granted")),
                                "path": message.get("path", ""),
                                "writable": bool(message.get("writable", False)),
                            }
                        )
                    )
                elif kind == "plan_response":
                    _resolve_pending(
                        json.dumps(
                            {
                                "approved": bool(message.get("approved")),
                                "mode": message.get("mode", "interactive"),
                                "feedback": message.get("feedback", ""),
                            }
                        )
                    )
                elif kind == "question_response":
                    _resolve_pending(str(message.get("answer", "")))
                elif kind == "interrupt":
                    engine.request_interrupt()
                elif kind == "retry":
                    # Re-run after a provider error (engine guards on the error-notice
                    # tail, so a stray frame is a no-op that still ends with turn_done).
                    await claim_turn(retry=True)
                elif kind == "set_mode":
                    try:
                        engine.permissions.mode = Mode(message.get("mode"))
                    except (TypeError, ValueError):
                        pass
                    # Governance execution level may travel alongside mode.
                    lvl = message.get("execution_level")
                    if lvl in ("off", "auto", "strict"):
                        engine.permissions.execution_level = lvl
                        # Rebuild the guard when toggling it on/off.
                        if lvl == "off":
                            engine.permissions.tool_guard = None
                        elif engine.permissions.tool_guard is None:
                            try:
                                from ..governance import RuleGuardian

                                engine.permissions.tool_guard = RuleGuardian()
                            except Exception:  # noqa: BLE001
                                pass
                elif kind == "set_model":
                    model = message.get("model")
                    if model is not None and not isinstance(model, str):
                        await reject_input("Invalid model: expected a string.")
                    else:
                        await _apply_model(model)
                elif kind == "user_message":
                    raw_text = message.get("text")
                    if raw_text is None:
                        raw_text = ""
                    if not isinstance(raw_text, str):
                        await reject_input("Invalid message text: expected a string.")
                        continue
                    text = raw_text.strip()
                    raw_attachments = message.get("attachments")
                    attachments = [] if raw_attachments is None else raw_attachments
                    # Reject an oversized frame instead of buffering it into a turn. Send a
                    # visible error so the surface can tell the user, and drop the message.
                    if not isinstance(attachments, list):
                        await reject_input("Invalid attachments: expected a list.")
                        continue
                    reject = None
                    if len(text) > _MAX_MESSAGE_TEXT_CHARS:
                        reject = (
                            f"Message too long ({len(text)} chars; "
                            f"limit {_MAX_MESSAGE_TEXT_CHARS})."
                        )
                    elif len(attachments) > _MAX_ATTACHMENTS:
                        reject = (
                            f"Too many attachments ({len(attachments)}; "
                            f"limit {_MAX_ATTACHMENTS})."
                        )
                    elif any(not isinstance(a, dict) for a in attachments):
                        reject = "Invalid attachment: expected an object."
                    elif _json_value_size(attachments) > _MAX_ATTACHMENTS_BYTES:
                        reject = "Attachments too large (limit 15 MB per message)."
                    else:
                        for attachment in attachments:
                            attachment_kind = attachment.get("kind")
                            name = attachment.get("name")
                            mime = attachment.get("mime")
                            if attachment_kind not in {"image", "pdf", "text"}:
                                reject = "Invalid attachment kind."
                            elif name is not None and (
                                not isinstance(name, str) or len(name) > 1024
                            ):
                                reject = "Invalid attachment name."
                            elif mime is not None and (
                                not isinstance(mime, str) or len(mime) > 255
                            ):
                                reject = "Invalid attachment MIME type."
                            elif attachment_kind == "image":
                                data = attachment.get("data_url")
                                if (
                                    not isinstance(data, str)
                                    or not data.startswith("data:image/")
                                    or ";base64," not in data
                                    or len(data) > MAX_IMAGE_CHARS
                                ):
                                    reject = "Invalid or oversized image attachment."
                            elif attachment_kind == "pdf":
                                data = attachment.get("data_url")
                                if (
                                    not isinstance(data, str)
                                    or not data.startswith(
                                        "data:application/pdf;base64,"
                                    )
                                    or len(data) > MAX_PDF_CHARS
                                ):
                                    reject = "Invalid or oversized PDF attachment."
                            else:
                                body = attachment.get("text")
                                if (
                                    not isinstance(body, str)
                                    or len(body) > MAX_TEXT_CHARS
                                ):
                                    reject = "Invalid or oversized text attachment."
                            if reject is not None:
                                break
                    if reject is not None:
                        await reject_input(reject)
                        continue
                    # The composer sends its visible model with every message — the FIRST
                    # one binds the session (race-proof across reconnects; see api.ts
                    # Session.userMessage), later ones may switch it (notice persisted).
                    model = message.get("model")
                    if model is not None and not isinstance(model, str):
                        await reject_input("Invalid model: expected a string.")
                        continue
                    await _apply_model(model)
                    if text or attachments:
                        content = build_user_content(text, attachments)
                        await claim_turn(content=content)
                else:
                    await reject_input(f"Unknown WebSocket message type: {kind}.")
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister_session_client(session_id, ws.send_json)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        """App-wide event stream (session-independent): the GUI keeps one open for
        pushes like automation_run_started (the UX-026 toast). Read-only — inbound
        frames are ignored; the receive loop just detects disconnect."""
        if not _websocket_authenticated(ws):
            await ws.close(code=1008)
            return
        if not _origin_allowed(ws.headers.get("origin")):
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="openworker" if api_token else None)
        manager.register_event_client(ws.send_json)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.unregister_event_client(ws.send_json)

    return app


def _parse_json(s: str) -> dict[str, Any]:
    """Parse a structured Inbox resolution (directory/plan carry their reply as a JSON string)."""
    try:
        v = json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _openai_response(model: str, turn: AssistantTurn) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": turn.text or ""}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in turn.tool_calls
        ]
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:12],
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": turn.finish_reason or "stop",
            }
        ],
    }
