"""FastAPI app — OpenAI-compatible endpoint + WS session API + REST.

The control plane every surface (GUI/IDE/messaging) rides on. The WS carries the engine
event stream and the approval channel; `/v1/chat/completions` is the OpenAI-compatible
proxy so any OpenAI-format client can use the runtime as a backend.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import threading
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

# Thread-safe locks for module-level singleton initialization
_deskmate_mm_lock = threading.Lock()
_deskmate_tick_lock = threading.Lock()


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


def _persona_mcp_refs(manager: "SessionManager", persona_id: str) -> set[str]:
    """The ``<server>:<tool>`` pairs a persona explicitly declares via
    ``tools: [mcp:<server>:<tool>, …]``. Empty set ⇒ no pinning (servers expose
    their full toolset). Returns ``set()`` for unknown personas (don't pin)."""
    from ..catalog import is_mcp_ref, parse_mcp_ref

    try:
        entry = manager.personas.get(persona_id)
    except Exception:
        return set()
    if entry is None:
        return set()
    refs: set[str] = set()
    for t in getattr(entry, "tools", []) or []:
        if not is_mcp_ref(t):
            continue
        try:
            server, tool = parse_mcp_ref(t)
        except ValueError:
            continue
        refs.add(f"{server}:{tool}")
    return refs


import contextlib
from typing import Iterator


@contextlib.contextmanager
def _temp_plugin_file(payload: Any) -> Iterator[str]:
    """Write an inline plugin payload to a temp file so the package loader (which
    reads from disk) can consume it. Supports JSON text or an already-parsed dict."""
    import tempfile

    suffix = ".json"
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False)
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as f:
        f.write(text)
        path = f.name
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

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
from .. import toolchain
from ..teams.model import AuthorityError as TeamsAuthorityError
from ..teams.model import BoardError as TeamsBoardError
from .manager import SessionManager


def create_app(manager: SessionManager) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            # Background mirror of the OpenRouter catalog (6h TTL) — keeps the picker's
            # OpenRouter section fresh without blocking any request path.
            from ..providers.openrouter_catalog import start_catalog_refresh

            start_catalog_refresh()
        except Exception:  # never let the catalog mirror block startup
            pass
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
            or request.url.path.startswith("/roundtable/")
            # `/v1/board` carries its own, stronger auth: per-actor board tokens
            # (identity + access), designed to be handed to external harnesses and
            # other machines — which can never hold the machine-local sidecar token.
            or request.url.path.startswith("/v1/board/")
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
        from ..personas.registry import include_unshipped

        # `internal` tells the GUI it may show internal-build affordances (the
        # "Not in this release" group, the Gallery entry point).
        return {"personas": manager.personas.list_all(), "internal": include_unshipped()}

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

    @app.get("/v1/sessions/{session_id}/reviewer-stats")
    def get_reviewer_stats(session_id: str) -> dict[str, Any]:
        # Auto-Approve metering (§1.7): checks/verdicts/tokens from the durable audit rows.
        # Drives the composer's "Auto-Approve · N checks" badge and the mode-menu summary.
        return manager.audit_store.reviewer_stats(session_id)

    @app.post("/v1/sessions/{session_id}/unattended")
    def set_unattended(session_id: str, body: dict) -> dict[str, Any]:
        # The GUI gates the on-transition behind a one-tap confirm; the manager records the
        # transition either way, so the change is answerable from the audit store.
        return manager.set_unattended(session_id, bool(body.get("unattended")))

    @app.get("/v1/sessions/{session_id}/skills")
    def session_skills(session_id: str, workspace: str = "") -> dict[str, Any]:
        # The rail's Skills group + the composer popup both read this (SKILLS-SPEC §4.1).
        return manager.session_skills_view(session_id, workspace or None)

    @app.post("/v1/sessions/{session_id}/skills")
    def set_session_skill(session_id: str, body: dict) -> dict[str, Any]:
        # A session mute. `clear` drops the override (inherit again); otherwise explicit
        # on/off. Nothing on disk changes — Settings owns permanent state.
        body = body or {}
        skill = str(body.get("skill", "")).strip()
        if not skill:
            return {"ok": False, "error": "skill required"}
        if body.get("clear"):
            manager.session_skills.clear(session_id, skill)
        else:
            manager.session_skills.set(
                session_id, skill, bool(body.get("enabled", False))
            )
        return manager.session_skills_view(
            session_id, str(body.get("workspace", "")) or None
        )

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
            elif body.get("zip_b64"):
                # Sharing v1 (OPE-7): a bundle zip — the export format — round-trips
                # through the same dir installer + consent path.
                try:
                    data = base64.b64decode(str(body["zip_b64"]), validate=True)
                except (ValueError, binascii.Error):
                    return {"ok": False, "error": "Invalid archive encoding."}
                summaries = reg.install_from_zip(
                    data, str(body.get("filename", ""))
                )
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
                    "error": "provide a `dir`, `git_url`, `zip_b64`, or `gallery_slug`",
                }
        except Exception as e:  # surface manifest/clone errors to the caller
            return {"ok": False, "error": str(e)}
        return {"ok": True, "consent": summaries, "personas": reg.list_all()}

    @app.post("/v1/personas/{persona_id}/export")
    def export_persona(persona_id: str, body: dict) -> dict[str, Any]:
        # Sharing v1 (OPE-7): zip the persona's bundle into the chosen folder. The zip
        # is the import format — send it to a teammate, they import it from the picker.
        return manager.personas.export_persona(
            persona_id, str((body or {}).get("dir", ""))
        )

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

    # -- plugin packages (server + persona bundles) ------------------------------
    @app.post("/v1/plugins/validate")
    def plugin_validate(body: dict) -> dict[str, Any]:
        # Pre-check a plugin package (local path or inline payload) before install.
        from ..mcp import validate_plugin_package

        path = body.get("path")
        payload = body.get("payload")
        if path:
            return validate_plugin_package(path)
        if payload:
            with _temp_plugin_file(payload) as tp:
                return validate_plugin_package(tp)
        return {"ok": False, "error": "need `path` or `payload`"}

    @app.post("/v1/plugins/install")
    def plugin_install(body: dict) -> dict[str, Any]:
        # Install a plugin package: its MCP servers land in the global config and its
        # personas are snapshotted (disabled pending consent). Local-only operation.
        from ..mcp import install_plugin_package

        path = body.get("path")
        payload = body.get("payload")
        try:
            if path:
                result = install_plugin_package(path)
            elif payload:
                with _temp_plugin_file(payload) as tp:
                    result = install_plugin_package(tp)
            else:
                return {"ok": False, "error": "need `path` or `payload`"}
        except Exception as exc:
            return {"ok": False, "error": f"install failed: {exc}"}
        if result.get("ok"):
            result["personas"] = manager.personas.list_all()
        return result

    @app.get("/v1/personas/{persona_id}/media/{name}")
    def persona_media(persona_id: str, name: str) -> Any:
        # Screenshots from the persona bundle's media/ folder. The name is confined
        # to that folder: no separators, resolved path must stay inside it.
        from fastapi.responses import FileResponse, Response

        media_dir = manager.personas.media_dir(persona_id)
        if media_dir is None or "/" in name or "\\" in name or name.startswith("."):
            return Response(status_code=404)
        f = (media_dir / name).resolve()
        try:
            inside = f.is_relative_to(media_dir.resolve())
        except AttributeError:  # pragma: no cover — py<3.9 has no is_relative_to
            inside = str(f).startswith(str(media_dir.resolve()))
        if not inside or not f.is_file():
            return Response(status_code=404)
        return FileResponse(f)
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
    def skills(workspace: str = "") -> dict[str, Any]:
        return {"skills": manager.list_skills(workspace or None)}

    @app.post("/v1/skills")
    def create_skill(body: dict) -> dict[str, Any]:
        return manager.create_skill(body or {})

    @app.patch("/v1/skills/{name}")
    def update_skill(name: str, body: dict) -> dict[str, Any]:
        return manager.update_skill(name, body or {})

    @app.delete("/v1/skills/{name}")
    def delete_skill(name: str, workspace: str = "") -> dict[str, Any]:
        return manager.delete_skill(name, workspace or None)

    @app.post("/v1/skills/{name}/move")
    def move_skill(name: str, body: dict) -> dict[str, Any]:
        return manager.move_skill(name, body or {})

    @app.post("/v1/skills/{name}/reveal")
    def reveal_skill(name: str, body: dict) -> dict[str, Any]:
        # §6 "Show folder": open the skill's folder in the OS file manager (local machine).
        return manager.reveal_skill(name, str((body or {}).get("workspace", "")) or None)

    @app.post("/v1/skills/upload")
    def stage_skill_upload(body: dict) -> dict[str, Any]:
        # Stage → preview; nothing is installed until /upload/confirm (SKILLS-SPEC §4.2).
        data_b64 = str((body or {}).get("data_b64", ""))
        if not data_b64:
            return {"ok": False, "error": "No archive supplied."}
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error):
            return {"ok": False, "error": "Invalid archive encoding."}
        return manager.stage_skill_upload(data, str((body or {}).get("filename", "")))

    @app.post("/v1/skills/upload/confirm")
    def confirm_skill_upload(body: dict) -> dict[str, Any]:
        return manager.confirm_skill_upload(body or {})

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

    @app.post("/v1/workspaces/temp")
    def provision_temp_workspace(body: dict) -> dict[str, Any]:
        # UX-029: a code-family session starting "in a temporary folder" — created only
        # at send time, with git ready. Knowledge families keep their auto-provisioned dir.
        return manager.provision_temp_workspace(
            str((body or {}).get("session_id", "")),
            git=bool((body or {}).get("git", True)),
        )

    @app.post("/v1/sessions/{session_id}/save-as-project")
    def save_session_as_project(session_id: str, body: dict) -> dict[str, Any]:
        # UX-029 "Save as project…": move the temporary folder somewhere real. The GUI
        # reconnects afterwards so the engine rebinds to the new path.
        return manager.save_temp_as_project(session_id, str((body or {}).get("path", "")))

    @app.post("/v1/workspaces/pick")
    async def pick_workspace() -> dict[str, Any]:
        # Native folder picker opened by the LOCAL sidecar (browser GUIs can't get absolute
        # paths from web file dialogs). Off the event loop: blocks until pick/cancel.
        return await asyncio.to_thread(manager.pick_native_folder)

    @app.post("/v1/files/pick")
    async def pick_file() -> dict[str, Any]:
        # Native single-file picker (创作流水线「浏览…」选原著文件用)。
        return await asyncio.to_thread(manager.pick_native_file)

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

    # Agent teams (OPE-96): the session's board (workspace-keyed space) + journal
    # overview. Mutations act as the USER — the human side of the gates.
    @app.get("/v1/sessions/{session_id}/board")
    def session_board(session_id: str) -> dict[str, Any]:
        return manager.session_board(session_id)

    @app.get("/v1/sessions/{session_id}/board/item")
    def session_board_item(session_id: str, id: int) -> dict[str, Any]:
        return manager.board_item_detail(session_id, int(id))

    @app.get("/v1/sessions/{session_id}/board/attachment")
    def session_board_attachment(session_id: str, name: str):
        from fastapi.responses import Response

        try:
            path = manager.attachment_store.path_for(name)
        except TeamsBoardError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        return Response(
            content=path.read_bytes(),
            media_type=manager.attachment_store.mime_for(name),
        )

    @app.post("/v1/sessions/{session_id}/board/comment")
    def session_board_comment(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.board_comment(
            session_id, int(body.get("item", 0)), str(body.get("body", ""))
        )

    @app.post("/v1/sessions/{session_id}/board/transition")
    def session_board_transition(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.board_transition(
            session_id,
            int(body.get("item", 0)),
            str(body.get("to", "")),
            comment=str(body.get("comment", "")),
        )

    @app.get("/v1/teams/{team_id}/chat")
    def team_chat(team_id: str) -> dict[str, Any]:
        return manager.team_chat(team_id)

    @app.post("/v1/teams/{team_id}/chat")
    def team_chat_post(team_id: str, body: dict) -> dict[str, Any]:
        return manager.post_team_chat(team_id, str((body or {}).get("text", "")))

    @app.get("/v1/teams/journal")
    def teams_journal() -> dict[str, Any]:
        return {"cases": manager.journal_overview()}

    # ---- The open board surface (OPE-100): token-authenticated `/v1/board` API.
    # Identity is the TOKEN (actor+role bound at mint, resolved per request, never
    # client-asserted); authority is the STORE — the same double gate in-app agents
    # get. This is the one wire protocol every external front door rides:
    # RemoteDialect (the `ocw` CLI, the team-board MCP server, headless instances)
    # today, a hosted board service later. Tokens are required even on loopback —
    # they carry identity, not just access.

    def _board_actor(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        return manager.board_tokens.resolve(token)

    def _board(request: Request, handler):
        actor = _board_actor(request)
        if actor is None:
            return JSONResponse(
                {"error": "board token required (Authorization: Bearer …) — mint"
                          " one with `ocw board token` on the serving machine"},
                status_code=401,
            )
        try:
            return handler(actor)
        except TeamsAuthorityError as error:
            return JSONResponse({"error": str(error)}, status_code=403)
        except (TeamsBoardError, ValueError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    @app.get("/v1/board/whoami")
    def board_whoami(request: Request):
        return _board(
            request, lambda actor: {"actor": actor.id, "role": actor.role.value}
        )

    @app.get("/v1/board/spaces")
    def board_spaces(request: Request):
        return _board(request, lambda actor: {"spaces": manager.team_store.spaces()})

    @app.get("/v1/board/items")
    def board_list_items(
        request: Request, space: str, state: str = "", assignee: str = ""
    ):
        return _board(
            request,
            lambda actor: {
                "items": manager.team_store.list_items(
                    space, actor, state=state or None, assignee=assignee or None
                )
            },
        )

    @app.get("/v1/board/item")
    def board_get_item(request: Request, space: str, id: int):
        return _board(
            request, lambda actor: manager.team_store.get_item(space, int(id))
        )

    @app.post("/v1/board/items")
    def board_create_item(request: Request, body: dict):
        body = body or {}

        def run(actor):
            item = manager.team_store.create_item(
                str(body.get("space", "")),
                actor,
                title=str(body.get("title", "")),
                criteria=str(body.get("criteria", "")),
                description=str(body.get("description", "")),
                parent=(
                    int(body["parent"]) if body.get("parent") is not None else None
                ),
                case=str(body.get("case") or "") or None,
            )
            manager.kick_team_tick()  # a new filing is lead-subscription news
            return item

        return _board(request, run)

    @app.post("/v1/board/items/transition")
    def board_transition_item(request: Request, body: dict):
        body = body or {}

        def run(actor):
            item = manager.team_store.transition(
                str(body.get("space", "")),
                actor,
                int(body.get("id", 0)),
                str(body.get("to", "")),
                comment=str(body.get("comment", "")),
                refs=[str(ref) for ref in body.get("refs") or []],
            )
            manager.kick_team_tick()  # review/blocked should reach the lead now
            return item

        return _board(request, run)

    @app.post("/v1/board/items/comment")
    def board_comment_item(request: Request, body: dict):
        body = body or {}
        return _board(
            request,
            lambda actor: manager.team_store.comment(
                str(body.get("space", "")),
                actor,
                int(body.get("id", 0)),
                str(body.get("body", "")),
                refs=[str(ref) for ref in body.get("refs") or []],
            ),
        )

    @app.post("/v1/board/items/assign")
    def board_assign_item(request: Request, body: dict):
        body = body or {}

        def run(actor):
            item = manager.team_store.assign(
                str(body.get("space", "")),
                actor,
                int(body.get("id", 0)),
                str(body.get("assignee", "")),
            )
            manager.kick_team_tick()  # the assignee's queue has news
            return item

        return _board(request, run)

    @app.post("/v1/board/items/claim")
    def board_claim_item(request: Request, body: dict):
        body = body or {}

        def run(actor):
            item = manager.team_store.claim(
                str(body.get("space", "")), actor, int(body.get("id", 0))
            )
            manager.kick_team_tick()  # claims land in the lead's feed
            return item

        return _board(request, run)

    @app.post("/v1/board/link")
    def board_link_items(request: Request, body: dict):
        body = body or {}
        return _board(
            request,
            lambda actor: manager.team_store.link(
                str(body.get("space", "")),
                actor,
                int(body.get("src", 0)),
                str(body.get("kind", "")),
                int(body.get("dst", 0)),
            ),
        )

    @app.post("/v1/board/items/attach")
    def board_attach(request: Request, body: dict):
        body = body or {}

        def run(actor):
            raw = str(body.get("data_b64", ""))
            # Cheap pre-decode bound: base64 is ~4/3 of the payload, so anything
            # multiples over the cap is refused before allocating the decode.
            if len(raw) > 15 * 1024 * 1024:
                return JSONResponse(
                    {"error": "attachment exceeds 10MB"}, status_code=400
                )
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return JSONResponse(
                    {"error": "data_b64 is not valid base64"}, status_code=400
                )
            ref = manager.attachment_store.put(
                data, str(body.get("filename", ""))
            )
            filename = str(body.get("filename", ""))
            event = manager.team_store.comment(
                str(body.get("space", "")),
                actor,
                int(body.get("id", 0)),
                str(body.get("caption", "")) or f"attached {filename}",
                refs=[ref],
            )
            return {"ref": ref, "seq": event["seq"]}

        return _board(request, run)

    @app.get("/v1/board/attachment")
    def board_attachment(request: Request, name: str):
        def run(actor):
            from fastapi.responses import Response

            path = manager.attachment_store.path_for(name)
            return Response(
                content=path.read_bytes(),
                media_type=manager.attachment_store.mime_for(name),
            )

        return _board(request, run)

    @app.get("/v1/board/policy")
    def board_get_policy(request: Request, space: str):
        return _board(request, lambda actor: manager.team_store.policy(space))

    @app.post("/v1/board/policy")
    def board_set_policy(request: Request, body: dict):
        body = body or {}
        return _board(
            request,
            lambda actor: manager.team_store.set_policy(
                str(body.get("space", "")), actor, claims=str(body.get("claims", ""))
            ),
        )

    @app.get("/v1/board/pending")
    def board_pending(request: Request, space: str, limit: int = 200):
        # The actor's FEED: events on its slice since its cursor — interest
        # follows the assignment relation, same projection in-app workers use.
        return _board(
            request,
            lambda actor: {
                "events": manager.team_store.feed_for(
                    space, actor.id, limit=int(limit)
                )
            },
        )

    @app.post("/v1/board/consume")
    def board_consume(request: Request, body: dict):
        body = body or {}

        def run(actor):
            manager.team_store.consume_feed(
                str(body.get("space", "")), actor.id, int(body.get("upto_seq", 0))
            )
            return {"ok": True}

        return _board(request, run)

    @app.get("/v1/board/journal/cases")
    def board_journal_cases(request: Request):
        return _board(
            request, lambda actor: {"cases": manager.journal_store.overview(actor)}
        )

    @app.get("/v1/board/journal")
    def board_journal_read(
        request: Request,
        case: str,
        item: Optional[int] = None,
        author: str = "",
        kind: str = "",
        entity: str = "",
        include_raw: str = "",
        limit: int = 100,
    ):
        return _board(
            request,
            lambda actor: {
                "entries": manager.journal_store.read(
                    actor,
                    case,
                    item=item,
                    author=author or None,
                    kind=kind or None,
                    entity=entity or None,
                    include_raw=bool(include_raw),
                    limit=int(limit),
                )
            },
        )

    @app.post("/v1/board/journal")
    def board_journal_append(request: Request, body: dict):
        body = body or {}
        return _board(
            request,
            lambda actor: manager.journal_store.append(
                actor,
                str(body.get("case", "")),
                str(body.get("body", "")),
                kind=str(body.get("kind") or "note"),
                space=str(body.get("space") or "") or None,
                item=int(body["item"]) if body.get("item") is not None else None,
                entities=[str(e) for e in body.get("entities") or []],
                refs=[str(ref) for ref in body.get("refs") or []],
            ),
        )

    @app.get("/v1/memory")
    def memory() -> dict[str, Any]:
        return {"memory": manager.list_memory()}

    @app.post("/v1/memory")
    def add_memory(body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.add_memory(
            str(body.get("content", "")), str(body.get("scope", "workspace"))
        )

    # Declared before the /{item_id} routes so "settings" can never be parsed as an id.
    @app.get("/v1/memory/settings")
    def memory_settings() -> dict[str, Any]:
        return manager.get_memory_settings()

    @app.put("/v1/memory/settings")
    def memory_settings_put(body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.set_memory_settings(
            enabled=bool(body["enabled"]) if "enabled" in body else None,
            user_rules=str(body["user_rules"]) if "user_rules" in body else None,
            auto_inject=bool(body["auto_inject"]) if "auto_inject" in body else None,
        )

    @app.patch("/v1/memory/{item_id}")
    def memory_patch(item_id: int, body: dict) -> dict[str, Any]:
        return manager.update_memory(item_id, str((body or {}).get("content", "")))

    @app.delete("/v1/memory/{item_id}")
    def memory_delete(item_id: int) -> dict[str, Any]:
        return manager.delete_memory(item_id)

    @app.delete("/v1/memory")
    def memory_delete_all() -> dict[str, Any]:
        return manager.delete_all_memory()

    # -- project bindings (pass 20 / UX-044) --------------------------------------

    @app.get("/v1/sessions/{session_id}/project-menu")
    def project_menu(session_id: str, kind: str = "memory") -> dict[str, Any]:
        return manager.project_menu(session_id, kind)

    @app.put("/v1/sessions/{session_id}/bindings")
    def put_binding(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        name = body.get("name")
        return manager.set_binding(
            session_id, str(body.get("kind", "")), str(name) if name else None
        )

    @app.post("/v1/sessions/{session_id}/project-name")
    def name_project(session_id: str, body: dict) -> dict[str, Any]:
        body = body or {}
        return manager.name_current_project(
            session_id, str(body.get("kind", "")), str(body.get("name", ""))
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
        manager.begin_mcp_connect(name)  # authorizing shows on the very next poll
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
    async def mcp_reload(body: dict | None = None) -> dict[str, Any]:
        """Reload MCP connections. With ``session_id`` this hot-plugs the new tool
        set into a *running* session (roundtable/agent) without a restart; without
        it, all live connections are build-before-swapped for new sessions."""
        body = body or {}
        session_id = (body.get("session_id") or "").strip()
        agent = (body.get("agent") or "code").strip()
        if session_id:
            return await manager.hot_reload_mcp(session_id, agent=agent)
        return await manager.reload_mcp()

    # -- skill store (one-click MCP install) ------------------------------------
    # NOTE: routes live under /v1/skill-store to avoid colliding with the
    # plugin-skill catalog at GET /v1/skills (manager.list_skills).
    @app.get("/v1/skill-store")
    async def skill_list() -> dict[str, Any]:
        """Return the skill catalog with install status for each skill."""
        from ..mcp.store import get_builtin_skills, get_skill_status

        skills = []
        for s in get_builtin_skills():
            skills.append({
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "icon": s.icon,
                "version": s.version,
                "tags": s.tags,
                "author": s.author,
                "homepage": s.homepage,
                "config_type": s.config.type,
                "config_key_name": s.config.key_name,
                "config_key_hint": s.config.key_hint,
                "config_env_key": s.config.env_key,
                "status": get_skill_status(s.id),
            })
        return {"skills": skills}

    @app.post("/v1/skill-store/install")
    async def skill_install(body: dict) -> dict[str, Any]:
        """Install a skill by its ID. Writes config to mcp.json, then hot-reloads."""
        from ..mcp.store import get_builtin_skills, install_skill

        skill_id = body.get("skill_id", "").strip()
        if not skill_id:
            return {"ok": False, "error": "skill_id required"}

        catalog = {s.id: s for s in get_builtin_skills()}
        skill = catalog.get(skill_id)
        if skill is None:
            return {"ok": False, "error": f"unknown skill: {skill_id}"}

        try:
            install_skill(skill)
            await manager.reload_mcp()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/v1/skill-store/configure")
    async def skill_configure(body: dict) -> dict[str, Any]:
        """Configure an installed skill (set API key, etc.)."""
        from ..mcp.store import configure_skill

        skill_id = body.get("skill_id", "").strip()
        config = body.get("config", {})
        if not skill_id:
            return {"ok": False, "error": "skill_id required"}
        if not config:
            return {"ok": False, "error": "config required"}

        try:
            configure_skill(skill_id, config)
            await manager.reload_mcp()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/v1/skill-store/{skill_id}")
    async def skill_uninstall(skill_id: str) -> dict[str, Any]:
        """Uninstall a skill by removing it from mcp.json."""
        from ..mcp.store import uninstall_skill

        try:
            uninstall_skill(skill_id)
            await manager.reload_mcp()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    @app.post("/v1/providers/openai-codex/signin")
    async def codex_signin() -> dict[str, Any]:
        # Opens the system browser and waits on the loopback callback — that can
        # take minutes, so it runs as a background task; the GUI polls the status
        # route for the flip (authorizing → signed_in | last_error). Same shape as
        # the MCP OAuth connect route.
        manager.begin_codex_signin()
        asyncio.create_task(manager.codex_signin())
        return {"ok": True, "started": True}

    @app.get("/v1/providers/openai-codex/status")
    def codex_status() -> dict[str, Any]:
        return manager.codex_status()

    @app.post("/v1/providers/openai-codex/signout")
    def codex_signout() -> dict[str, Any]:
        return manager.codex_signout()

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

    @app.post("/v1/settings/auto-approve")
    def settings_set_auto_approve(body: dict) -> dict[str, Any]:
        # Auto-Approve feature flag (spec §1.5): when on, Mode.AUTO_APPROVE gets an LLM
        # reviewer. Takes effect on the next session build. Turning it off leaves any
        # shadow-eval setting alone (they are independent switches).
        return manager.set_auto_approve((body or {}).get("auto_approve", False))

    @app.post("/v1/settings/auto-approve-shadow")
    def settings_set_auto_approve_shadow(body: dict) -> dict[str, Any]:
        # Shadow evaluation (Part 6 step 3): the reviewer records what it WOULD decide on
        # every approval card while the human still decides. Independent of the live flag.
        return manager.set_auto_approve_shadow((body or {}).get("auto_approve_shadow", False))

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
                with _deskmate_mm_lock:
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

    @app.get("/v1/deskmate/memory/trace")
    async def get_deskmate_memory_trace(
        kind: str = "both", limit: int = 10,
    ) -> dict[str, Any]:
        """溯源 + 遗忘可见（P0-4 + 8.3）：返回最近「注入了哪些记忆」与「忘了哪些记忆」。

        - kind=inject → 每轮注入了哪些记忆（溯源：可查「这条回复基于哪条记忆」）
        - kind=forgot → 归档/删除了哪些记忆（遗忘可见：遗忘与记忆一样透明）
        - kind=both   → 两者都返回（默认）
        """
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import (
                DeskMateMemoryManager,
                FORGOT_TRACE_FILE,
                INJECT_TRACE_FILE,
            )

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            global _deskmate_mm_rest
            if "_deskmate_mm_rest" not in globals():
                with _deskmate_mm_lock:
                    if "_deskmate_mm_rest" not in globals():
                        _deskmate_mm_rest = DeskMateMemoryManager(base_dir=desk_dir)
            mm = _deskmate_mm_rest
            n = max(1, min(int(limit), 50))
            out: dict[str, Any] = {}
            if kind in ("both", "inject"):
                out["inject"] = mm.read_trace(INJECT_TRACE_FILE, n)
            if kind in ("both", "forgot"):
                out["forgot"] = mm.read_trace(FORGOT_TRACE_FILE, n)
            return out
        except Exception:
            logging.exception("get deskmate memory trace failed")
            return JSONResponse({"error": "read trace failed"}, status_code=500)

    @app.get("/v1/deskmate/memory/user-dedup")
    async def get_deskmate_user_dedup() -> dict[str, Any]:
        """P0-5 记忆去重：返回 USER.md 近重复率与条目数（8.0 红线：前端可感可看）。"""
        try:
            from pathlib import Path as _P

            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            global _deskmate_mm_rest
            if "_deskmate_mm_rest" not in globals():
                with _deskmate_mm_lock:
                    if "_deskmate_mm_rest" not in globals():
                        _deskmate_mm_rest = DeskMateMemoryManager(base_dir=desk_dir)
            mm = _deskmate_mm_rest
            return {
                "dup_rate": mm.user_dup_rate(),
                "entries": len(mm._user_entries()),
            }
        except Exception:
            logging.exception("get deskmate user dedup failed")
            return JSONResponse({"error": "read user dedup failed"}, status_code=500)

    @app.get("/v1/deskmate/memory/layering")
    async def get_deskmate_memory_layering() -> dict[str, Any]:
        """P0-6 记忆分层净化 + 时间感知：返回「书的记忆 vs 你的记忆」分离状态与陈旧率。

        8.0 红线：让分层与时间感知在前端可感可看。
        """
        try:
            from pathlib import Path as _P

            from deskmate.memory import project_memory as _pm
            from deskmate.memory.manager import DeskMateMemoryManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            global _deskmate_mm_rest
            if "_deskmate_mm_rest" not in globals():
                with _deskmate_mm_lock:
                    if "_deskmate_mm_rest" not in globals():
                        _deskmate_mm_rest = DeskMateMemoryManager(base_dir=desk_dir)
            mm = _deskmate_mm_rest

            global_entries = mm._global_memory_entries()
            # 全局是否仍残留书稿/项目分节（分层未净化的标志）
            _, sections = mm._parse_sections(mm._load_global_memory())
            book_sections_left = [s["id"] for s in sections
                                  if s["id"].lower().startswith("project")]

            projects = _pm.list_projects(desk_dir)
            project_entries = 0
            for p in projects:
                for content in _pm.load_project_memory(desk_dir, p).values():
                    project_entries += len([b for b in re.split(r"\n\n+", content) if b.strip()])

            age = mm.memory_age_stats()
            return {
                "global_entries": len(global_entries),
                "project_entries": project_entries,
                "project_names": projects,
                "book_sections_left": book_sections_left,
                "layered": len(book_sections_left) == 0,
                "stale_rate": age["stale_rate"],
                "stale_days": age["stale_days"],
                "stale_entries": age["stale"],
            }
        except Exception:
            logging.exception("get deskmate memory layering failed")
            return JSONResponse({"error": "read memory layering failed"}, status_code=500)

    @app.get("/v1/deskmate/memory/dream")
    async def get_deskmate_memory_dream() -> dict[str, Any]:
        """P2-1 后台自动提炼（Dreaming）：返回上次记忆提炼状态与是否到期可跑。

        8.0 红线：让「记忆自我进化 / 矛盾消解 / 过时更新」在前端可感可看。
        """
        try:
            from pathlib import Path as _P

            from deskmate.memory import dream as _dream

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            last = _dream.last_run(desk_dir)
            due = _dream._should_run(desk_dir)
            report_path = desk_dir / _dream._REPORT_FILE
            merged = contradictions_resolved = stale_superseded = 0
            files_touched: list[str] = []
            if report_path.is_file():
                for line in report_path.read_text(encoding="utf-8").splitlines():
                    m = re.search(r"去重[:：]\s*(\d+)", line)
                    if m:
                        merged = int(m.group(1))
                    m = re.search(r"矛盾消解[:：]\s*(\d+)", line)
                    if m:
                        contradictions_resolved = int(m.group(1))
                    m = re.search(r"超龄迁 vault[（(]可恢复[)）][:：]\s*(\d+)", line)
                    if m:
                        stale_superseded = int(m.group(1))
                    m = re.search(r"触及文件[:：]\s*(.+)", line)
                    if m:
                        files_touched = [f.strip() for f in m.group(1).split(",") if f.strip()]
            return {
                "last_run": last,
                "due": due,
                "merged": merged,
                "contradictions_resolved": contradictions_resolved,
                "stale_superseded": stale_superseded,
                "files_touched": files_touched,
                "report_path": str(report_path),
            }
        except Exception:
            logging.exception("get deskmate memory dream failed")
            return JSONResponse({"error": "read memory dream failed"}, status_code=500)

    @app.get("/v1/deskmate/skills/curator")
    async def get_deskmate_skills_curator() -> dict[str, Any]:
        """P0-7 curator 自控清理：返回自动归档开关状态与当前整理候选（非破坏性巡检）。

        8.0 红线：让「技能库自整理 / 归档」在前端可感可看。
        """
        try:
            from pathlib import Path as _P

            from deskmate.skills import curator as _curator
            from deskmate.skills.manager import SkillManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            store = SkillManager(base_dir=desk_dir).store
            result = _curator.curate_skills(desk_dir, auto_archive=False)
            # 当前 archive/ 已归档存量（还原前的状态，可感）
            archive_dir = store.skills_dir / "archive"
            archived_files = list(archive_dir.glob("*.md")) if archive_dir.is_dir() else []
            archived_now = len(archived_files)
            archived_names = [f.stem for f in archived_files]
            return {
                "auto_archive_enabled": _curator.auto_archive_enabled(),
                "total": result.get("total", 0),
                "merge_candidates": result.get("merge_candidates", []),
                "archive_candidates": result.get("archive_candidates", []),
                "auto_archive_candidates": result.get("auto_archive_candidates", []),
                "archived_now": archived_now,
                "archived_names": archived_names,
                "report_path": result.get("report_path", ""),
            }
        except Exception:
            logging.exception("get deskmate skills curator failed")
            return JSONResponse({"error": "read skills curator failed"}, status_code=500)

    @app.post("/v1/deskmate/skills/restore")
    async def post_deskmate_skills_restore(request: Request) -> dict[str, Any]:
        """P0-7 一键还原：把 archive/ 中的技能还原回技能库（归档不删除，可还原）。"""
        try:
            from pathlib import Path as _P

            from deskmate.skills.manager import SkillManager

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            body = await request.json()
            name = (body or {}).get("name", "")
            if not name:
                return JSONResponse(
                    {"status": "error", "message": "缺少 name"}, status_code=400
                )
            store = SkillManager(base_dir=desk_dir).store
            return store.restore_skill(name)
        except Exception:
            logging.exception("post deskmate skills restore failed")
            return JSONResponse({"status": "error", "message": "restore failed"}, status_code=500)

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

    @app.post("/v1/deskmate/vault/update")
    async def update_vault_entry(request: Request) -> dict[str, Any]:
        """更新 vault 中的单个文件内容（用于人生档案等条目的在线编辑/校准）。"""
        try:
            body = await request.json()
            rel_path = str(body.get("path", "")).strip()
            content = body.get("content")
            if not rel_path or content is None:
                return JSONResponse({"error": "path and content required"}, status_code=400)
            # 防路径穿越：不允许 ..，必须是相对路径
            if ".." in rel_path.split("/") or rel_path.startswith("/"):
                return JSONResponse({"error": "invalid path"}, status_code=400)
            from pathlib import Path as _P

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            vault_dir = desk_dir / "vault"
            target = (vault_dir / rel_path).resolve()
            # 二次校验：解析后必须仍在 vault_dir 内
            if not str(target).startswith(str(vault_dir.resolve())):
                return JSONResponse({"error": "path outside vault"}, status_code=400)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {"ok": True, "path": rel_path, "bytes": len(content.encode("utf-8"))}
        except Exception:
            logging.exception("update vault entry failed")
            return JSONResponse({"error": "write failed"}, status_code=500)

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
            return _read_learned_history(desk_dir, limit=max(1, min(limit, 5000)))
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

    @app.get("/v1/deskmate/badges")
    async def get_deskmate_badges() -> dict[str, Any]:
        """获取勋章墙数据（全量勋章 + 获得状态 + 进度 + 解锁时间 + 统计摘要）。

        本端点会顺带执行一次勋章判定：新达成的勋章在此刻被记录解锁时间并落盘，
        并通过 newlyUnlocked 返回给前端做「桌宠报喜」。判定是幂等的，
        没有新解锁就不写盘。

        隐藏勋章（奇遇门）在未解锁时名称与条件均被遮蔽，只返回占位内容。
        """
        try:
            from pathlib import Path as _P

            from deskmate.growth.engine import GrowthEngine

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            return GrowthEngine(base_dir=desk_dir).badge_snapshot()
        except Exception:
            logging.exception("get deskmate badges failed")
            return JSONResponse({"error": "badges failed"}, status_code=500)

    @app.get("/v1/deskmate/learning-graph")
    async def get_deskmate_learning_graph() -> dict[str, Any]:
        """「学习可视化」图数据（记忆↔技能连边，对标 Hermes learning_graph）。

        纯文件扫描，零 LLM：技能节点（SkillStore 全量）+ 记忆卡片
        （USER.md/MEMORY.md/longterm.md 按 ## 块切）+ 技能↔技能（共享 tags）
        与记忆↔技能（id 命中强边 / 中文 bigram 重叠弱边）两类边。
        前端「学习面板」展开时调用。
        """
        try:
            from pathlib import Path as _P

            from deskmate.memory.learning_graph import (
                _growth_snapshot,
                build_learning_graph,
            )

            desk_dir = _P(
                os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            )
            # P1-8：图随成长动态化——连接强度由信任/默契/亲密度调制（越用越连）
            return build_learning_graph(
                base_dir=desk_dir, growth=_growth_snapshot(desk_dir)
            )
        except Exception:
            logging.exception("get deskmate learning graph failed")
            return JSONResponse({"error": "learning graph failed"}, status_code=500)

    @app.get("/v1/deskmate/digest")
    async def get_deskmate_digest(force: bool = False) -> dict[str, Any]:
        """获取 DeskMate 首页资讯卡片（文化 / 网文 / AI / 开源）。

        数据通过 web_search 抓取并由 LLM 提炼成卡片；缓存 4 小时，过期自动刷新。
        点击卡片后前端把 query 交给 AI 搜索总结，而不是跳转到源站。
        """
        try:
            from deskmate.digest.engine import get_digest

            return await get_digest(force=force)
        except Exception:
            logging.exception("get deskmate digest failed")
            return {"items": [], "updated_at": 0, "fresh": False}

    @app.post("/v1/deskmate/emote-pack")
    async def set_deskmate_emote_pack(request: Request) -> dict[str, Any]:
        """前端切换激活表情包时上报，让桌搭（assistant）回复里的表情冻结到该包画风。"""
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "bad json"}
        pack = data.get("pack") if isinstance(data, dict) else None
        from ..emotes import set_active_pack, get_active_pack

        ok = set_active_pack(pack)
        return {"ok": ok, "pack": get_active_pack()}

    @app.get("/v1/deskmate/emote-pack")
    async def get_deskmate_emote_pack() -> dict[str, Any]:
        from ..emotes import get_active_pack, list_packs

        return {"pack": get_active_pack(), "packs": list_packs()}

    @app.get("/v1/deskmate/sync")
    async def get_deskmate_sync_status() -> dict[str, Any]:
        """P2-7 云端记忆同步状态（本地优先，默认关闭）。"""
        try:
            from deskmate.memory.sync import MemorySyncService

            return MemorySyncService().status()
        except Exception:
            logging.exception("get deskmate sync status failed")
            return {"enabled": False, "backend": "noop", "local_files": 0, "remote_files": 0}

    @app.post("/v1/deskmate/sync/configure")
    async def post_deskmate_sync_configure(request: Request) -> dict[str, Any]:
        """启用/禁用同步，或指定同步目录（指向 iCloud/Dropbox/USB 即真实跨设备）。

        Body: {"enabled": true/false, "dir": "/path/optional"}
        """
        try:
            data = await request.json()
        except Exception:
            return {"ok": False, "error": "bad json"}
        enabled = bool(data.get("enabled")) if isinstance(data, dict) else False
        sync_dir = data.get("dir") if isinstance(data, dict) else None
        try:
            from deskmate.memory.sync import MemorySyncService

            return {"ok": True, **MemorySyncService().configure(enabled, sync_dir)}
        except Exception:
            logging.exception("configure deskmate sync failed")
            return {"ok": False, "error": "configure failed"}

    @app.post("/v1/deskmate/sync/trigger")
    async def post_deskmate_sync_trigger() -> dict[str, Any]:
        """立即执行一次双向同步（push + pull）。fail-open。"""
        try:
            from deskmate.memory.sync import MemorySyncService

            return {"ok": True, **MemorySyncService().sync()}
        except Exception:
            logging.exception("trigger deskmate sync failed")
            return {"ok": False, "error": "sync failed"}

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

    # ---- auto_tasks：统一自动任务沉淀框架（每日读书/进度跟踪/未来新任务）----

    @app.get("/v1/deskmate/auto-tasks/stats")
    async def get_deskmate_auto_tasks_stats() -> dict[str, Any]:
        """获取自动任务沉淀的统计数据（总记录数/按类型/按分类/任务列表）。"""
        try:
            from deskmate.auto_tasks import get_recorder
            return get_recorder().get_stats()
        except Exception:
            logging.exception("get auto_tasks stats failed")
            return JSONResponse({"error": "stats failed"}, status_code=500)

    @app.get("/v1/deskmate/auto-tasks/timeline")
    async def get_deskmate_auto_tasks_timeline(
        limit: int = 50, task_type: str | None = None
    ) -> dict[str, Any]:
        """获取自动任务的时间线（倒序）。

        Query: limit（默认50）, task_type（可选过滤）
        """
        try:
            from deskmate.auto_tasks import get_recorder
            from dataclasses import asdict
            records = get_recorder().get_timeline(limit=limit, task_type=task_type)
            return {"records": [asdict(r) for r in records]}
        except Exception:
            logging.exception("get auto_tasks timeline failed")
            return JSONResponse({"error": "timeline failed"}, status_code=500)

    @app.get("/v1/deskmate/auto-tasks/insights")
    async def get_deskmate_auto_tasks_insights(
        task_type: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """获取自动任务沉淀的结构化洞察。"""
        try:
            from deskmate.auto_tasks import get_recorder
            insights = get_recorder().get_insights(task_type=task_type, limit=limit)
            return {"insights": insights}
        except Exception:
            logging.exception("get auto_tasks insights failed")
            return JSONResponse({"error": "insights failed"}, status_code=500)

    @app.post("/v1/deskmate/auto-tasks/record")
    async def post_deskmate_auto_tasks_record(request: Request) -> dict[str, Any]:
        """记录一条自动任务的沉淀。供定时任务执行完后调用。

        Body: {
          "task_type": "daily_reading",
          "target": "17-明朝的裂变",
          "action": "finish",
          "summary": "...",
          "insights": [{"text": "...", "tags": ["风格"], "push_to_vault": true}],
          "tags": ["..."],
          "detail_content": "...",
          "task_name": "每日读书"
        }
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            from deskmate.auto_tasks import record_task_result
            from deskmate.auto_tasks.models import Insight
            insights = [
                i if isinstance(i, Insight) else Insight(**i)
                for i in body.get("insights", [])
            ]
            rec = record_task_result(
                task_type=body["task_type"],
                target=body.get("target", ""),
                action=body.get("action", "finish"),
                summary=body.get("summary", ""),
                insights=insights,
                tags=body.get("tags"),
                detail_content=body.get("detail_content"),
                task_name=body.get("task_name"),
                extract_cognitive=body.get("extract_cognitive", False),
            )
            from dataclasses import asdict
            return {"ok": True, "record": asdict(rec)}
        except Exception:
            logging.exception("post auto_tasks record failed")
            return JSONResponse({"error": "record failed"}, status_code=500)

    # ---- cognitive：认知层（从"懂我"到"像我"的关键跃迁）----

    @app.get("/v1/deskmate/cognitive/profile")
    async def get_cognitive_profile() -> dict[str, Any]:
        """获取认知画像（五个维度：立场/论证模式/价值观/思维框架/认知盲区）。"""
        try:
            from deskmate.cognitive import get_profile_manager
            from dataclasses import asdict
            profile = get_profile_manager().get()
            return asdict(profile)
        except Exception:
            logging.exception("get cognitive profile failed")
            return JSONResponse({"error": "profile failed"}, status_code=500)

    @app.get("/v1/deskmate/cognitive/stats")
    async def get_cognitive_stats() -> dict[str, Any]:
        """获取认知画像统计摘要（用于前端栏目展示）。"""
        try:
            from deskmate.cognitive import get_profile_manager
            return get_profile_manager().get().stats()
        except Exception:
            logging.exception("get cognitive stats failed")
            return JSONResponse({"error": "stats failed"}, status_code=500)

    @app.post("/v1/deskmate/cognitive/extract")
    async def post_cognitive_extract(request: Request) -> dict[str, Any]:
        """
        从文本中提取认知特征，增量更新到认知画像。

        Body: {"text": "...", "source_title": "书名/对话日期", "source_type": "book/conversation/article", "source_date": "2026-08-30"}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            from deskmate.cognitive import get_extractor
            result = get_extractor().extract_from_text(
                text=body.get("text", ""),
                source_title=body.get("source_title", "未知来源"),
                source_type=body.get("source_type", "book"),
                source_date=body.get("source_date", ""),
            )
            return result
        except Exception:
            logging.exception("cognitive extract failed")
            return JSONResponse({"error": "extract failed"}, status_code=500)

    @app.post("/v1/deskmate/cognitive/calibrate")
    async def post_cognitive_calibrate(request: Request) -> dict[str, Any]:
        """
        用户校准——纠正认知画像中的错误。

        Body: {"item_id": "stance_xxx", "corrected_content": "...", "feedback": "..."}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            from deskmate.cognitive import get_profile_manager
            get_profile_manager().calibrate(
                item_id=body["item_id"],
                corrected_content=body.get("corrected_content", ""),
                feedback=body.get("feedback", ""),
            )
            return {"ok": True}
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except Exception:
            logging.exception("cognitive calibrate failed")
            return JSONResponse({"error": "calibrate failed"}, status_code=500)

    @app.get("/v1/deskmate/cognitive/gaps")
    async def get_cognitive_gaps() -> dict[str, Any]:
        """获取认知盲区/待澄清列表（供第二期深度访谈引擎使用）。"""
        try:
            from deskmate.cognitive import get_profile_manager
            from dataclasses import asdict
            gaps = get_profile_manager().get().open_gaps()
            return {"gaps": [asdict(g) for g in gaps]}
        except Exception:
            logging.exception("get cognitive gaps failed")
            return JSONResponse({"error": "gaps failed"}, status_code=500)

    @app.post("/v1/deskmate/cognitive/cold-start")
    async def post_cognitive_cold_start(request: Request) -> dict[str, Any]:
        """
        触发冷启动——从已有 vault/MEMORY/style_profile 中提取初始认知画像。

        仅在画像为空时执行；已有数据时跳过。
        Body: {"use_llm": false}（默认 false，用规则快速提取）
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            from deskmate.cognitive import cold_start
            result = cold_start(use_llm=body.get("use_llm", False))
            return result
        except Exception:
            logging.exception("cognitive cold start failed")
            return JSONResponse({"error": "cold start failed"}, status_code=500)

    @app.post("/v1/deskmate/cognitive/interview/start")
    async def post_cognitive_interview_start(request: Request) -> dict[str, Any]:
        """发起一轮深度访谈：基于认知薄弱点生成苏格拉底式问题。

        Body: {"limit": 3}（默认 3）。返回 {session_id, questions, total}；
        暂无可问薄弱点时 session_id=null。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            from deskmate.cognitive import start_interview
            return start_interview(limit=body.get("limit", 3))
        except Exception:
            logging.exception("cognitive interview start failed")
            return JSONResponse({"error": "interview start failed"}, status_code=500)

    @app.post("/v1/deskmate/cognitive/interview/answer")
    async def post_cognitive_interview_answer(request: Request) -> dict[str, Any]:
        """提交一个访谈回答：回灌认知画像、落库、判断是否问完。

        Body: {"session_id": "...", "question_id": "...", "response": "..."}
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        sid = body.get("session_id", "")
        qid = body.get("question_id", "")
        resp = body.get("response", "")
        if not sid or not qid:
            return JSONResponse({"error": "missing session_id/question_id"}, status_code=400)
        try:
            from deskmate.cognitive import answer_interview
            return answer_interview(session_id=sid, question_id=qid, response=resp)
        except Exception:
            logging.exception("cognitive interview answer failed")
            return JSONResponse({"error": "interview answer failed"}, status_code=500)

    @app.get("/v1/deskmate/cognitive/interview/should")
    async def get_cognitive_interview_should() -> dict[str, Any]:
        """是否应主动发起访谈（保守阈值：open gap>=2 且距上次>=3天）。供前端/调度判断。"""
        try:
            from deskmate.cognitive import get_profile_manager, get_interview_engine
            pm = get_profile_manager().get()
            ok = get_interview_engine().should_interview_now(pm)
            return {"should_interview": ok, "open_gaps": len(pm.open_gaps())}
        except Exception:
            logging.exception("cognitive interview should failed")
            return JSONResponse({"error": "interview should failed"}, status_code=500)

    @app.get("/v1/deskmate/cognitive/interview/sessions")
    async def get_cognitive_interview_sessions() -> dict[str, Any]:
        """历史访谈会话列表（含问题数、状态、摘要）。"""
        try:
            from deskmate.cognitive.store import CognitiveStore
            return {"sessions": CognitiveStore().list_sessions()}
        except Exception:
            logging.exception("cognitive interview sessions failed")
            return JSONResponse({"error": "interview sessions failed"}, status_code=500)

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
                with _deskmate_tick_lock:
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

    @app.get("/v1/deskmate/diary")
    async def get_deskmate_diary() -> dict[str, Any]:
        """P1-3：小桔日记——日期列表（DeskHome 可翻）。"""
        try:
            from deskmate.growth.diary import DiaryWriter

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            w = DiaryWriter(base_dir=base)
            return {"dates": w.list_diaries()}
        except Exception:
            logging.exception("get deskmate diary list failed")
            return {"dates": []}

    @app.get("/v1/deskmate/diary/recap")
    async def get_deskmate_diary_recap(days: int = 7) -> dict[str, Any]:
        """P1-3：每周/每月「我们的回忆」。"""
        try:
            from deskmate.growth.diary import DiaryWriter

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            w = DiaryWriter(base_dir=base)
            return {"days": max(1, min(days, 90)), "recap": w.weekly_recap(days)}
        except Exception:
            logging.exception("get deskmate diary recap failed")
            return {"days": days, "recap": ""}

    @app.post("/v1/deskmate/diary/today")
    async def post_deskmate_diary_today() -> dict[str, Any]:
        """P1-3：生成/刷新今日小桔日记（幂等）。"""
        try:
            from deskmate.growth.diary import DiaryWriter

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            w = DiaryWriter(base_dir=base)
            path = w.ensure_today()
            date = Path(str(path)).stem
            return {"path": str(path), "content": w.read(date)}
        except Exception:
            logging.exception("post deskmate diary today failed")
            return JSONResponse({"error": "生成日记失败"}, status_code=500)

    @app.get("/v1/deskmate/diary/{date}")
    async def get_deskmate_diary_date(date: str) -> dict[str, Any]:
        """P1-3：读取某天小桔日记。"""
        try:
            from deskmate.growth.diary import DiaryWriter

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            w = DiaryWriter(base_dir=base)
            content = w.read(date)
            if content is None:
                return JSONResponse({"error": "no diary"}, status_code=404)
            return {"date": date, "content": content}
        except Exception:
            logging.exception("get deskmate diary date failed")
            return JSONResponse({"error": "读取日记失败"}, status_code=500)

    # ---- MomentFeed：私密双人日记（用户侧数据 + 🩷 落记忆 + 小桔回复）----
    @app.get("/v1/deskmate/moments")
    async def get_deskmate_moments(cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """用户日记 / 留言 / 🩷 沉淀状态。小桔日记另走 /v1/deskmate/diary。
        分页：cursor(上一页末条 createdAt|id) + limit → {items, next_cursor, comments}；
        cursor 为 None 时返回第一页（同样的 {items, next_cursor, comments} 形态）。
        items 为 user_posts（内联 comments、带 loved 标记），comments 为全量留言映射
        （含 xj-* 小桔日记留言，供前端按 moment_id 查找）。next_cursor 为 None 表示到底。"""
        try:
            from deskmate.growth.moments import MomentsStore

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            page = MomentsStore(base_dir=base).get_posts(
                limit=max(1, min(limit, 200)), cursor=cursor
            )
            return {
                "items": page["items"],
                "next_cursor": page["next_cursor"],
                "comments": page.get("comments", {}),
            }
        except Exception:
            logging.exception("get deskmate moments failed")
            return {"items": [], "next_cursor": None, "comments": {}}

    @app.post("/v1/deskmate/moments/post")
    async def post_deskmate_moment(request: Request) -> dict[str, Any]:
        """用户发一条日记；后端顺带生成小桔的回复（LLM，失败回落规则）。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        content = (body or {}).get("content", "")
        if not content or not content.strip():
            return JSONResponse({"error": "内容为空"}, status_code=400)
        try:
            from deskmate.growth.moments import MomentsStore

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            store = MomentsStore(base_dir=base)
            post = store.add_user_post(content)
            reply = store.generate_reply(content)
            return {"post": post, "reply": reply}
        except Exception:
            logging.exception("post deskmate moment failed")
            return JSONResponse({"error": "发布失败"}, status_code=500)

    @app.post("/v1/deskmate/moments/comment")
    async def post_deskmate_moment_comment(request: Request) -> dict[str, Any]:
        """给任意一条日记留言（互动层，不落记忆）。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        moment_id = (body or {}).get("moment_id", "")
        author = (body or {}).get("author", "me")
        text = (body or {}).get("text", "")
        if not moment_id or not text.strip():
            return JSONResponse({"error": "参数缺失"}, status_code=400)
        try:
            from deskmate.growth.moments import MomentsStore

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            store = MomentsStore(base_dir=base)
            c = store.add_comment(moment_id, author, text)
            return {"comment": c}
        except Exception:
            logging.exception("post deskmate moment comment failed")
            return JSONResponse({"error": "留言失败"}, status_code=500)

    @app.post("/v1/deskmate/moments/{moment_id}/love")
    async def post_deskmate_moment_love(moment_id: str, request: Request) -> dict[str, Any]:
        """🩷 沉淀：把这条日记落 SQLite（loved_entries），并导出检索视图供聊天 prefetch 检索。

        不再写入全局 MEMORY.md（避免 2 万字符截断 + compress/purify 改写丢失）。见 diary_store.py。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        content = (body or {}).get("content", "")
        try:
            from deskmate.growth.moments import MomentsStore

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            return MomentsStore(base_dir=base).love(moment_id, content)
        except Exception:
            logging.exception("post deskmate moment love failed")
            return JSONResponse({"error": "沉淀失败"}, status_code=500)

    @app.post("/v1/deskmate/moments/{moment_id}/archive")
    async def post_deskmate_moment_archive(moment_id: str, request: Request) -> dict[str, Any]:
        """每条日记独立归档 / 取消归档（与🩷、回复并列的卡片操作）。

        body: {"archived": true|false}（缺省 true）。归档仅影响时间线展示，
        不影响已🩷的记忆检索（🩷 与 归档 是两个独立维度）。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        archived = bool((body or {}).get("archived", True))
        try:
            from deskmate.growth.moments import MomentsStore

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            ok = MomentsStore(base_dir=base).archive(moment_id, archived=archived)
            return {"ok": ok, "archived": archived}
        except Exception:
            logging.exception("archive moment failed")
            return JSONResponse({"error": "归档失败"}, status_code=500)

    @app.get("/v1/deskmate/behavior-patterns")
    async def get_deskmate_behavior_patterns(days: int = 120) -> dict[str, Any]:
        """P2-3：从长期行为推断出的偏好模式（越用越懂你 最高阶，8.0 可感可看）。"""
        try:
            from deskmate.proactivity.behavior_patterns import mine_patterns

            base = os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
            res = mine_patterns(base_dir=base, days=max(7, min(days, 365)))
            return {
                "window_days": res["window_days"],
                "event_count": res["event_count"],
                "kinds": res.get("kinds", []),
                "patterns": res["patterns"],
                "context": res["context"],
            }
        except Exception:
            logging.exception("get deskmate behavior patterns failed")
            return {"window_days": days, "event_count": 0, "patterns": [], "context": ""}

    @app.get("/v1/deskmate/positioning")
    async def get_deskmate_positioning() -> dict[str, Any]:
        """P2-8：合规轻陪伴桌宠定位卡位——明确「工具属性 + 可爱外形 + 可控记忆」，非虚拟伴侣。"""
        try:
            from deskmate.compliance import get_positioning

            return get_positioning()
        except Exception:
            logging.exception("get deskmate positioning failed")
            return {}

    @app.get("/v1/deskmate/alter-ego")
    async def get_deskmate_alter_ego() -> dict[str, Any]:
        """P2-6 个人 AI 分身模式状态 + 「它眼中的我」完整画像（6 栏目聚合）。"""
        try:
            from deskmate.alter_ego.config import is_enabled
            from deskmate.alter_ego.decision_profile import load_profile
            from deskmate.alter_ego.persona import has_alter_ego_data

            enabled = is_enabled()
            profile = load_profile()
            result = {
                "enabled": enabled,
                "has_data": has_alter_ego_data(),
                "total_signals": profile.total_signals,
                "mode": "full",
            }
            # 「它眼中的我」6 栏目聚合画像（fail-open，失败不阻断基础状态）
            try:
                from deskmate.alter_ego.portrait import build_portrait

                result["portrait"] = build_portrait()
            except Exception:
                logging.exception("alter-ego portrait build failed")
                result["portrait"] = None
            return result
        except Exception:
            logging.exception("get deskmate alter-ego failed")
            return {"enabled": False, "has_data": False, "total_signals": 0, "mode": "full", "portrait": None}

    @app.post("/v1/deskmate/alter-ego")
    async def post_deskmate_alter_ego(request: Request) -> dict[str, Any]:
        """启用/禁用分身模式。Body: {"enabled": true/false}。

        开启后：每轮对话学习决策偏好 + 注入「另一个我」画像。fail-open。
        """
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        enabled = bool(data.get("enabled")) if isinstance(data, dict) else False
        try:
            from deskmate.alter_ego.config import set_enabled

            set_enabled(enabled)
            return {"ok": True, "enabled": enabled}
        except Exception:
            logging.exception("set deskmate alter-ego failed")
            return {"ok": False, "error": "set failed"}

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
            done_chapters = 0   # 实际审完的章节数（跳过失败章），供勋章统计
            done_words = 0      # 实际过手的字数
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
                    done_chapters += 1
                    done_words += len(ctext)
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

            # 勋章统计：记录本次编校产出（失败不影响审阅结果返回）
            try:
                from deskmate.growth.engine import GrowthEngine as _GEng

                await asyncio.to_thread(
                    _GEng(base_dir=desk_dir).record_writing,
                    done_chapters,
                    done_words,
                )
            except Exception as e:
                logging.debug("record_writing 失败（不影响审阅）: %s", e)

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
            result = save_manuscript(safe, chapters)

            # 勋章统计：以书稿总数为准（幂等，重存同一部不虚增）
            try:
                from pathlib import Path as _P

                from deskmate.growth.engine import GrowthEngine as _GEng
                from deskmate.writing import list_manuscripts as _lsm

                _dd = _P(
                    os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate"))
                )
                await asyncio.to_thread(
                    _GEng(base_dir=_dd).record_manuscript, len(_lsm())
                )
            except Exception as e:
                logging.debug("record_manuscript 失败（不影响保存）: %s", e)
            return result
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
                from deskmate.memory.after_action import refresh_bible_if_novel

                pp = _P(path).expanduser()
                # 读稿前先增量刷新记忆（修"读稿不更新记忆"）：已建圣经的书会自动纠正陈旧实体
                try:
                    # P2-10：刷新圣经内含多批 LLM 抽取（秒到分钟级），若在 async 端点
                    # 内同步调用会阻塞整个 sidecar。包进线程池，不挡其他请求。
                    await asyncio.to_thread(refresh_bible_if_novel, str(pp))
                except Exception:
                    logging.warning("报告流刷新圣经失败（不影响报告生成）：%s", pp)
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

            # 勋章统计：报告生成计数（失败不影响报告返回）
            try:
                from deskmate.growth.engine import GrowthEngine as _GEng

                await asyncio.to_thread(_GEng(base_dir=desk_dir).record_report)
            except Exception as e:
                logging.debug("record_report 失败（不影响报告）: %s", e)

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
            from ..tools.ask import answer_result, question_item_fields

            fields = question_item_fields(args)
            if fields is None:  # engine guards too; belt-and-braces
                return {"answer": "", "error": "no question"}
            item = manager.inbox.add_question(
                session_id,
                inbox=_route(),
                visibility=_visibility(),
                tool_call_id=tool_call_id,
                **fields,
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
                                "header": item.header,
                                "questions": item.questions,
                            },
                        }
                    )
            return answer_result(item.questions, await manager.inbox.wait(item.id))

        async def tool_requester(args: dict, tool_call_id=None) -> dict:
            """Park a TOOL_REQUESTED prompt, then install the PINNED build if approved.

            Declining is a first-class outcome: the agent is told to fall back and disclose
            the gap rather than drop the check (OPE-85). Installs only ever come from the
            pinned registry with its digest verified — an approval is consent to install
            THAT artifact, not licence to fetch whatever a prompt asked for.
            """
            name = str(args.get("name", "")).strip()
            info = toolchain.describe(name)
            if not info:
                # Not in the pinned catalog: never show an install card that can only
                # end in "no pinned build" AFTER approval (owner-hit 2026-08-20 — agents
                # routed ordinary brew/pip installs through the card). Steer to the
                # shell, which has its own approval flow.
                return {
                    "installed": False,
                    "error": (
                        f"'{name}' is not in the pinned tool catalog "
                        f"({', '.join(sorted(toolchain.MANAGED))}). Install it yourself "
                        "with the shell (brew/pip/…, subject to the normal command "
                        "approval), or proceed without it and note the gap."
                    ),
                }
            item = manager.inbox.add_tool_request(
                session_id,
                f"Install {name}?" if name else "Install a tool?",
                body=str(args.get("reason", "")),
                inbox=_route(),
                visibility=_visibility(),
                data={
                    "tool": name,
                    "installable": bool(info),
                    "version": (info or {}).get("version", ""),
                    "summary": (info or {}).get("summary", ""),
                    "url": (info or {}).get("url", ""),
                    "source": (info or {}).get("source", ""),
                },
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(await manager.inbox.wait(item.id))  # {approved}
            if not resp.get("approved"):
                return {
                    "installed": False,
                    "reason": "the user declined to install it",
                }
            if not info:
                return {
                    "installed": False,
                    "error": f"no pinned build of {name} is available for this platform",
                }
            try:
                path = await asyncio.to_thread(toolchain.install, name)
            except Exception as exc:  # noqa: BLE001 - surfaced to the agent verbatim
                return {"installed": False, "error": str(exc)}
            return {"installed": True, "path": path, "version": info["version"]}

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
                    "primary": bool(args.get("primary", False)),
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
            if bool(args.get("primary", False)):
                # Root promotion (workspace-scratch-design.md §5) — the shell cd inside
                # is blocking, keep it off the event loop.
                promo = await asyncio.to_thread(
                    manager.promote_workspace, session_id, path
                )
                if promo.get("ok"):
                    return {
                        "granted": True,
                        "path": promo["path"],
                        "writable": True,
                        "primary": True,
                        "note": (
                            "This folder is now the session's workspace. For the rest "
                            "of this turn, address it by absolute path."
                        ),
                    }
                # Promotion refused (e.g. the session already has a workspace): still
                # honor the grant as a plain additional folder.
                res = manager.add_root(session_id, path, writable)
                if not res.get("ok"):
                    return {
                        "granted": False,
                        "error": promo.get("error", "could not promote"),
                    }
                return {
                    "granted": True,
                    "path": path,
                    "writable": writable,
                    "primary": False,
                    "note": promo.get("error", "")
                    + " — granted as an additional folder instead",
                }
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

        async def team_approver(_args: dict, tool_call_id=None) -> dict:
            # The staffing gate. The engine already emitted TEAM_PROPOSED; park an
            # Inbox item as the durable resolution vehicle, wait for the verdict, and
            # on approval PRE-SPAWN the team (create_team fails closed on non-worker
            # personas, so a bad roster reads as a rejection with the reason).
            members = _args.get("members") or []
            roster = "\n".join(
                f"- {m.get('persona', '?')}"
                + (f" · {m['model']}" if m.get("model") else "")
                + (f" — {m['reason']}" if m.get("reason") else "")
                for m in members
                if isinstance(m, dict)
            )
            item = manager.inbox.add_plan(
                session_id,
                "Create this team?",
                body=roster,
                inbox=_route(),
                visibility=_visibility(),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(await manager.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user declined this roster",
                }
            # The gate checkbox is the USER's call: an explicit enable_chat in the
            # response overrides whatever the lead proposed.
            enable_chat = bool(
                resp["enable_chat"]
                if "enable_chat" in resp
                else _args.get("enable_chat", False)
            )
            return manager.create_team(
                session_id,
                [m for m in members if isinstance(m, dict)],
                enable_chat=enable_chat,
            )

        async def items_approver(_args: dict, tool_call_id=None) -> dict:
            # The decomposition gate. TEAMS-flavored sibling of plan_approver:
            # park a durable Inbox item, wait, and on approval create the items.
            items = _args.get("items") or []
            body = "\n".join(
                f"- {i.get('title', '?')} — Done when: {i.get('criteria', '?')}"
                for i in items
                if isinstance(i, dict)
            )
            item = manager.inbox.add_plan(
                session_id,
                "Approve the proposed work items?",
                body=body,
                inbox=_route(),
                visibility=_visibility(),
                tool_call_id=tool_call_id,
            )
            if item.state == "pending":
                manager.persist_session(session_id)
                if item.visibility == VIS_INBOX:
                    await _mirror(item)
            resp = _parse_json(await manager.inbox.wait(item.id))
            if not resp.get("approved"):
                return {
                    "approved": False,
                    "feedback": resp.get("feedback") or "the user declined the split",
                }
            return manager.board_create_items(
                session_id, [i for i in items if isinstance(i, dict)]
            )

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
            session_id,
            workspace=workspace,
            agent=agent,
            mcp_refs=_persona_mcp_refs(manager, agent),
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
            tool_requester=tool_requester,
            team_approver=team_approver,
            items_approver=items_approver,
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
        # MCP servers that failed to start while preparing this session's tools:
        # leave a quiet, persistent notice instead of the session silently lacking
        # them (drill 2026-08-20: three silent startup failures in a row).
        for name, err in manager.pop_mcp_failures(session_id):
            detail = f": {err}" if err else ""
            # `server` makes the notice structured: the GUI renders one quiet line
            # with the full error behind a disclosure + an Open-Connectors action
            # (owner ruling 2026-08-21) instead of a wall of stderr.
            engine._append_notice(
                "mcp_error",
                f"MCP server “{name}” failed to start{detail}"[:500],
                server=name,
            )
        # Auto-compaction failure prompt (OPE-27): only an ATTENDED session may be asked
        # Retry/Trim — unattended runs auto-trim (the policy in engine._compact_now).
        engine.is_attended = lambda: _visibility() == VIS_INLINE
        await ws.send_json(
            {
                "type": "ready",
                "data": {
                    "session_id": session_id,
                    # A reconnect can land MID-TURN (sidebar revisit, app relaunch, WS
                    # drop). Without server truth the GUI never learns a turn is live —
                    # no Stop button, no waiting row (owner catch 2026-08-24).
                    "running": manager.is_running(session_id),
                    "agent": getattr(engine, "agent_name", "code"),
                    "model": engine.model,
                    "mode": engine.permissions.mode.value,
                    "workspace": (
                        str(getattr(engine, "executor").cwd)
                        if getattr(engine, "executor", None)
                        else None
                    ),
                    # UX-029: the GUI never shows a temporary folder's raw path — this flag
                    # is how it knows to say "Temporary folder" (and offer Save as project).
                    "temp_workspace": manager.is_temp_workspace(
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
        # P0-1：创作流水线相关 agent → 走「创作」默契子域；其余 →「陪伴」子域。
        #（此前只有 deskmate 会计数，创作场景的「被懂得」完全不进成长数值）
        _CREATION_AGENTS = frozenset({
            "editor", "lit-editor", "proofreader", "publisher", "reader",
            "genre", "adaptation", "screenwriter", "outline-writer",
            "script-reviewer", "editor-in-chief", "historian",
        })
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

                # 全局 after-action 记忆钩子：每轮记一笔项目日志 + 刷新小说圣经
                try:
                    from deskmate.memory.after_action import reflect_and_persist_async

                    ws = None
                    try:
                        ws = manager.engine_workspace(session_id)
                    except Exception:
                        logging.debug("engine_workspace 取不到，after_action 仅记项目日志")
                    await reflect_and_persist_async(
                        ws,
                        last_user,
                        last_asst,
                        active_project=getattr(mm, "active_project", None),
                        learned=learned,
                    )
                except Exception:
                    logging.exception("after_action reflect failed")

                # Context injection visibility: 仅当记忆注入开启时播报（P0-1 默认开启）
                _auto_inject = os.environ.get("DESKMATE_AUTO_INJECT") == "1"
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

        async def run_turn(content, *, retry: bool = False, display=None) -> None:            # The receive loop atomically claims this session before scheduling the task.
            # Keeping the claim outside prevents two back-to-back frames from both starting.
            try:
                events = (
                    engine.retry()
                    if retry
                    else engine.run(content, display=display)
                )
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
                        # 累计对话数与活跃天数（可视化面板的 streakDays/totalChats 真实数据源）
                        # P0-1：此前被 `if agent == "deskmate"` 挡住，导致**创作流水线**
                        # （萧哥主场景）的「被懂得」完全不进成长数值——total_chats 长期
                        # 卡在 155 不涨、默契子域恒空。现放开到所有 agent，并按场景归属
                        # 到对应默契子域（创作类 → creation，其余 → companion）。
                        try:
                            from pathlib import Path as _P

                            from deskmate.growth.engine import (
                                DOMAIN_COMPANION,
                                DOMAIN_CREATION,
                            )
                            from deskmate.growth.engine import GrowthEngine as _GE

                            _ge_dir = _P(
                                os.environ.get(
                                    "DESKMATE_DIR", os.path.expanduser("~/.deskmate")
                                )
                            )
                            _domain = (
                                DOMAIN_CREATION
                                if agent in _CREATION_AGENTS
                                else DOMAIN_COMPANION
                            )
                            await asyncio.to_thread(
                                _GE(base_dir=_ge_dir).record_chat, _domain
                            )
                            # P2-10：聊天每轮 → 行为日志（供「常在何时聊天/聊什么域」规律积累）
                            from deskmate.proactivity.behavior_patterns import log_event
                            log_event("chat_turn", {"agent": agent, "domain": _domain}, base_dir=_ge_dir)
                        except Exception:
                            logging.exception("record chat growth failed")
                        # Title on the user's words the moment they land — never behind
                        # a long agentic turn (owner catch 2026-08-24).
                        manager._maybe_autotitle(session_id)
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
                    _task = asyncio.create_task(
                        _deskmate_post_turn(session_id, agent, engine, manager)
                    )

                    def _on_post_turn_done(t: asyncio.Task) -> None:
                        if not t.cancelled() and t.exception():
                            logging.warning("deskmate post_turn failed: %s", t.exception())

                    _task.add_done_callback(_on_post_turn_done)

        # This socket is now a live view of the session; background turns (channel delivery,
        # self-wake, durable resume) broadcast here too, not just locally driven run_turns.
        manager.register_session_client(session_id, ws.send_json)
        if engine.permissions.mode is Mode.AUTO_APPROVE and not any(
            m.get("kind") == "mode_notice" for m in engine.messages
        ):
            from coworker.permissions import AUTO_APPROVE_NOTICE

            engine._append_notice(
                "mode_notice", AUTO_APPROVE_NOTICE, title="Auto-approve is on."
            )
            manager.save(session_id, engine, touch=False)  # migration ≠ activity
            await ws.send_json(
                {
                    "type": "mode_notice",
                    "data": {
                        "title": "Auto-approve is on.",
                        "text": AUTO_APPROVE_NOTICE,
                    },
                }
            )
        inbound_times: deque[float] = deque()

        async def reject_input(reason: str) -> None:
            # Input validation failures are not provider failures and must not offer "Retry"
            # or flush an in-progress assistant stream in the GUI.
            await ws.send_json({"type": "input_rejected", "data": {"error": reason}})

        async def claim_turn(*, retry: bool = False, content=None, display=None) -> None:
            if not manager.try_mark_running(session_id):
                await reject_input(
                    "This session is already running a turn. Wait for it to finish or stop it."
                )
                return
            asyncio.create_task(run_turn(content, retry=retry, display=display))

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
                elif kind == "tool_response":
                    _resolve_pending(
                        json.dumps({"approved": bool(message.get("approved"))})
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
                elif kind in ("team_response", "items_response"):
                    _resolve_pending(
                        json.dumps(
                            {
                                "approved": bool(message.get("approved")),
                                "feedback": message.get("feedback", ""),
                                **(
                                    {"enable_chat": bool(message.get("enable_chat"))}
                                    if "enable_chat" in message
                                    else {}
                                ),
                            }
                        )
                    )
                elif kind == "question_response":
                    _resolve_pending(str(message.get("answer", "")))
                elif kind == "allow_anyway":
                    # §8.4: the user clicked "Allow anyway" on a reviewer-denied tool card.
                    # Registers a ONE-SHOT exact-action approval on the engine; the GUI then
                    # sends its canned retry message through the normal user_message path,
                    # and the re-proposed identical action runs without the reviewer/card.
                    name = message.get("name")
                    arguments = message.get("arguments")
                    if not isinstance(name, str) or not name:
                        await reject_input("Invalid allow_anyway: missing tool name.")
                    elif arguments is not None and not isinstance(arguments, dict):
                        await reject_input("Invalid allow_anyway: arguments must be an object.")
                    else:
                        engine.approve_action_once(name, arguments or {})
                elif kind == "interrupt":
                    engine.request_interrupt()
                elif kind == "retry":
                    # Re-run after a provider error (engine guards on the error-notice
                    # tail, so a stray frame is a no-op that still ends with turn_done).
                    await claim_turn(retry=True)
                elif kind == "set_mode":
                    try:
                        new_mode = Mode(message.get("mode"))
                    except (TypeError, ValueError):
                        pass
                    else:
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
                        previous = engine.permissions.mode
                        engine.permissions.mode = new_mode
                        if previous is not new_mode:
                            manager.audit_autonomy_change(
                                session_id, "mode", previous.value, new_mode.value
                            )
                            from coworker.permissions import (
                                AUTO_APPROVE_NOTICE,
                                MODE_LABELS,
                            )
                            if new_mode is Mode.AUTO_APPROVE and not any(
                                m.get("kind") == "mode_notice"
                                for m in engine.messages
                            ):
                                engine._append_notice(
                                    "mode_notice",
                                    AUTO_APPROVE_NOTICE,
                                    title="Auto-approve is on.",
                                )
                                notice_data = {
                                    "title": "Auto-approve is on.",
                                    "text": AUTO_APPROVE_NOTICE,
                                }
                            else:
                                label = MODE_LABELS.get(
                                    new_mode.value, new_mode.value
                                )
                                engine._append_notice(
                                    "mode_switch", f"{label} is on."
                                )
                                notice_data = {"text": f"{label} is on."}
                            manager.save(session_id, engine, touch=False)
                            await manager.broadcast_session(
                                session_id,
                                {"type": "mode_notice", "data": notice_data},
                            )
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
                    # Force-run (SKILLS-SPEC §4.1 #3): the composer's `/skill` pick rides as a
                    # separate field. Validated against the session's effective menu — a muted
                    # or unknown skill is a visible error, never a silent no-op (§4.6 #15).
                    # The model-facing framing goes into `content`; the transcript shows the
                    # user's literal "/name …" line via the `_display` sidecar (one bubble).
                    skill = message.get("skill")
                    display = None
                    if skill is not None:
                        if not isinstance(skill, str) or not skill.strip():
                            await reject_input("Invalid skill: expected a name.")
                            continue
                        skill = skill.strip()
                        menu = manager.effective_skill_names(session_id, workspace)
                        if skill not in menu:
                            await reject_input(
                                f"Skill '{skill}' is not available in this session."
                            )
                            continue
                        display = f"/{skill}" + (f" {text}" if text else "")
                        text = (
                            f'Use the skill "{skill}" for this request: first call '
                            f'load_skill("{skill}") and follow its instructions.'
                            + (f"\n\n{text}" if text else "")
                        )
                    await _apply_model(model)
                    if text or attachments:
                        content = build_user_content(text, attachments)
                        await claim_turn(content=content, display=display)
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

    # A2A 多租户 Mount('/{tenant}') 是贪婪的前缀挂载，会把 /v1/agents、
    # /v1/providers 等 /v1/* 业务路由当作 tenant=v1 拦截掉（返回 404，前端
    # 把错误体当数组用而白屏崩溃）。把它移到路由表末尾作为兜底，确保所有
    # 具体业务路由（/v1/*、/roundtable）优先匹配；真正的多租户路径
    # （如 /acme/message:send）仍会落到这里。
    try:
        _routes = app.router.routes
        for _i in range(len(_routes) - 1, -1, -1):
            if getattr(_routes[_i], "path", "") == "/{tenant}":
                _tenant_mount = _routes.pop(_i)
                _routes.append(_tenant_mount)
                break
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("调整 /{tenant} 挂载顺序失败：%s", exc)

    # 创作圆桌：挂到 /roundtable（独立页面，本地可信，免鉴权）。
    # 圆桌内部自带 /opencode-a2a 适配层，挂载后完整路径为 /roundtable/opencode-a2a。
    try:
        from deskmate.roundtable.web import create_app as _create_roundtable_app
        from starlette.routing import Mount as _Mount

        # 把主 app 的会话库 base 与当前 workspace 传给圆桌，使其落盘的纪要
        # 复用同一份 coworker.db，并能在左侧历史对话栏（按当前 workspace）被看到。
        _rt_base = str(getattr(manager.session_store, "base", "")) or None
        _rt_mount = _Mount(
            "/roundtable",
            _create_roundtable_app(base_dir=_rt_base, workspace=manager.default_workspace),
        )
        # 必须排在 /{tenant} 通配挂载之前，否则 /roundtable/ 会被 A2A 多租户路由抢匹配返回 404。
        _routes = app.router.routes
        _idx = next(
            (i for i, r in enumerate(_routes) if getattr(r, "path", "") == "/{tenant}"),
            len(_routes),
        )
        _routes.insert(_idx, _rt_mount)
        logging.getLogger(__name__).info("创作圆桌已挂载于 /roundtable")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("创作圆桌挂载失败（功能不可用）：%s", exc)

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
