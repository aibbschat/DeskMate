"""A2A (Agent-to-Agent) client tools — call other A2A agents from OpenWorker.

Uses Python stdlib only (urllib). Each tool is a plain callable registered
via the ToolRegistry, gated by the PermissionEngine like any other tool.

Usage in a prompt:
  "Ask Hermes (at localhost:9900) to summarize today's arXiv postings."
  "Discover what agents are available at http://localhost:9900."
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300

# ---------------------------------------------------------------------------
# A2A JSON-RPC helpers
# ---------------------------------------------------------------------------

# A2A v1.0 权威 JSON-RPC method 名（取自官方 a2a-sdk v1.x 的 proto A2AService RPC 名）。
# 注意：规范 markdown 老写法 `message/send` 在官方 SDK 里已被归到 `a2a/compat/v0_3`
# （老 v0.3 方言），v1.0 服务端（含官方 SDK 与我们这套）只认 `SendMessage` / `SendStreamingMessage`。
_PRIMARY_METHOD = "SendMessage"
_STREAM_METHOD = "SendStreamingMessage"
_FALLBACK_METHOD = "message/send"

_PROTOCOL_VERSION = "1.0"

# v1.0 ProtoJSON 用大写枚举值 ROLE_USER；老 v0.3 方言用小写 user。
_ROLE_USER = "ROLE_USER"
_ROLE_USER_V03 = "user"


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _text_message(text: str, role: str = _ROLE_USER, context_id: str = "") -> dict:
    msg: dict[str, Any] = {
        "messageId": _new_id(),
        "role": role,
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }
    if context_id:
        msg["contextId"] = context_id
    return msg


def _unwrap_send_message_response(result: Any) -> dict:
    """Drill into the v1.0 JSON-RPC result envelope."""
    if isinstance(result, dict):
        task = result.get("task") or result
        if isinstance(task, dict):
            return task.get("status", task)
    return result if isinstance(result, dict) else {}


def _reply_text(result: Any) -> str:
    """Extract the reply text from an A2A SendMessage response."""
    task = result.get("task") if isinstance(result, dict) else result
    if not isinstance(task, dict):
        return str(task)
    # Artifacts first (final output)
    for artifact in task.get("artifacts", []) or []:
        for part in artifact.get("parts", []) or []:
            text = (part.get("text") or "").strip()
            if text:
                return text
    # Then status message (interim / clarify)
    status = task.get("status", {}) or {}
    msg = status.get("message") or {}
    for part in msg.get("parts", []) or []:
        text = (part.get("text") or "").strip()
        if text:
            return text
    # Fallback: bare message
    for part in task.get("parts", []) or []:
        text = (part.get("text") or "").strip()
        if text:
            return text
    return "(no text reply)"


def _input_required(result: Any) -> bool:
    task = result.get("task") if isinstance(result, dict) else result
    if not isinstance(task, dict):
        return False
    state = ((task.get("status") or {}) or {}).get("state", "")
    return "INPUT_REQUIRED" in state


def _send_message(url: str, message: str, context_id: str = "", timeout: int = _DEFAULT_TIMEOUT, method: str = _PRIMARY_METHOD, role: str = _ROLE_USER) -> dict:
    """Send an A2A JSON-RPC message and return the parsed response.

    method 默认走规范 v1.0 权威名 `SendMessage`（官方 a2a-sdk 即如此）。
    对只认老 v0.3 方言（`message/send` + 小写 role）的 peer，由调用方在 -32601 时回退（见 a2a_call）。
    """
    payload = {
        "jsonrpc": "2.0",
        "id": _new_id(),
        "method": method,
        "params": {
            "message": _text_message(message, role=role, context_id=context_id),
        },
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "A2A-Version": _PROTOCOL_VERSION,
    }
    # 规范化尾斜杠：A2A JSON-RPC 端点挂在服务根 `/`，而 Starlette 的 submount
    # （如 /opencode-a2a）会把无尾斜杠的 POST 307 重定向到 `/opencode-a2a/`，
    # 标准库 urlopen 对 307 不一定跟随，直接带上尾斜杠规避。
    post_url = url if url.endswith("/") else url + "/"
    req = urllib.request.Request(post_url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send_streaming(url: str, message: str, context_id: str = "",
                    timeout: int = _DEFAULT_TIMEOUT, role: str = _ROLE_USER) -> dict:
    """Send an A2A SendStreamingMessage and parse the SSE response.

    Returns a synthetic JSON-RPC response dict with the final result, so the
    caller can use the same _reply_text / _input_required helpers.

    SSE format from Hermes:
      data: {"jsonrpc":"2.0","id":...,"result":{StreamResponse}}\\n\\n
      : keepalive\\n\\n          (every 5s, resets socket timeout)
      : done\\n\\n               (stream closed = terminal state)
    """
    payload = {
        "jsonrpc": "2.0",
        "id": _new_id(),
        "method": _STREAM_METHOD,
        "params": {
            "message": _text_message(message, role=role, context_id=context_id),
        },
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "A2A-Version": _PROTOCOL_VERSION,
    }
    post_url = url if url.endswith("/") else url + "/"

    # Use a generous socket timeout — each SSE line (including keepalives)
    # resets the timer, so this only fires if the server goes silent for
    # `timeout` seconds straight.
    req = urllib.request.Request(post_url, data=data, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)

    # Parse SSE stream line by line
    collected_text = ""
    last_result: dict = {}
    context_id_out = context_id
    state = ""

    try:
        buf = ""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")

            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")

                # Skip empty lines and SSE comments (keepalives, done marker)
                if not line or line.startswith(":"):
                    continue

                # Parse SSE data frames
                if line.startswith("data:"):
                    payload_str = line[5:].strip()
                    if not payload_str:
                        continue
                    try:
                        frame = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue

                    # Unwrap JSON-RPC envelope
                    result = frame.get("result", frame)

                    # Extract task status
                    task = result.get("task", result)
                    if isinstance(task, dict):
                        ctx = (task.get("status") or {}).get("contextId", "")
                        if ctx:
                            context_id_out = ctx
                        s = (task.get("status") or {}).get("state", "")
                        if s:
                            state = s

                    # Extract artifact text (final output)
                    for artifact in (task.get("artifacts") if isinstance(task, dict) else []) or []:
                        for part in artifact.get("parts", []) or []:
                            t = (part.get("text") or "").strip()
                            if t:
                                collected_text = t  # last artifact wins

                    # Also check status message (interim / INPUT_REQUIRED)
                    if not collected_text:
                        status = (task.get("status") or {}) if isinstance(task, dict) else {}
                        msg = status.get("message") or {}
                        for part in msg.get("parts", []) or []:
                            t = (part.get("text") or "").strip()
                            if t:
                                collected_text = t

                    last_result = result
    finally:
        resp.close()

    # Build a synthetic response that looks like a synchronous SendMessage reply
    # so _reply_text / _input_required work unchanged.
    if last_result:
        task = last_result.get("task", last_result)
        if isinstance(task, dict):
            if collected_text and state == "TASK_STATE_COMPLETED":
                # Put text in artifacts for _reply_text to find
                task.setdefault("artifacts", [])
                if not any(
                    (p.get("text") or "").strip()
                    for a in task.get("artifacts", [])
                    for p in a.get("parts", [])
                ):
                    task["artifacts"] = [{"parts": [{"text": collected_text}]}]
            elif collected_text:
                # Put in status message for _reply_text
                task.setdefault("status", {}).setdefault("message", {})
                if not any(
                    (p.get("text") or "").strip()
                    for p in task["status"]["message"].get("parts", [])
                ):
                    task["status"]["message"] = {"parts": [{"text": collected_text}]}
            # Ensure context_id is set
            task.setdefault("status", {})["contextId"] = context_id_out

    return {"result": last_result} if last_result else {}


def _fetch_card(url: str, timeout: int = 30) -> dict:
    """Fetch an A2A Agent Card."""
    base = url.rstrip("/")
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        try:
            req = urllib.request.Request(f"{base}{path}", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    return {}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def a2a_discover(url: str = "http://localhost:9900") -> str:
    """Fetch and summarize another agent's A2A Agent Card.

    Use this to find out what a remote agent can do before calling it.
    The default URL points to a local Hermes A2A server.

    Args:
        url: Base URL of the remote A2A agent (e.g. http://localhost:9900)
    """
    try:
        card = _fetch_card(url)
    except urllib.error.HTTPError as e:
        return f"Error: discovery failed — HTTP {e.code} from {url}."
    except Exception as e:
        return f"Error: could not reach {url} — {e}."

    if not card:
        return f"No Agent Card found at {url}."

    name = card.get("name", "?")
    desc = card.get("description", "")
    skills = card.get("skills", []) or []
    ifaces = card.get("supportedInterfaces", []) or []
    proto = ", ".join(
        f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')}"
        for i in ifaces if isinstance(i, dict)
    ) or f"v{card.get('protocolVersion', '?')}"

    lines = [
        f"Agent: {name}",
        f"Description: {desc}",
        f"URL: {url}",
        f"Protocol: {proto}",
        f"Skills ({len(skills)}):",
    ]
    for s in skills[:20]:
        lines.append(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')}")
    return "\n".join(lines)


def a2a_call(
    url: str = "http://localhost:9900",
    message: str = "",
    context_id: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
) -> str:
    """Send a task to an A2A agent and return its reply.

    Use this to delegate work to another agent. The default URL points to
    a local Hermes A2A server. Pass context_id from a previous reply to
    continue a multi-turn conversation.

    Uses SSE streaming by default (SendStreamingMessage) so that long-running
    tasks don't trigger socket timeouts — keepalive bytes from the server
    reset the timer. Falls back to synchronous SendMessage if the peer
    doesn't support streaming.

    Args:
        url: The A2A endpoint URL of the target agent (e.g. http://localhost:9900)
        message: The task or message to send, in natural language
        context_id: Optional context id from a prior reply, to continue the conversation
        timeout: Seconds of server silence before giving up (default 300)
    """
    if not message:
        return "Error: 'message' is required."

    # ── Try streaming first (preferred: keepalives prevent false timeouts) ──
    try:
        resp = _send_streaming(url, message, context_id=context_id, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"Error: peer at {url} rejected auth (HTTP {e.code})."
        if e.code == 429:
            return f"Error: peer at {url} rate limited us (HTTP 429)."
        # Streaming not supported or other error — fall through to sync
        resp = {}
    except Exception:
        # Streaming failed (e.g. server doesn't support it) — fall through
        resp = {}

    # ── Fallback: synchronous SendMessage ──
    if not resp or "error" in resp:
        err_code = (resp.get("error") or {}).get("code") if resp else None
        try:
            resp = _send_message(url, message, context_id=context_id,
                                 method=_PRIMARY_METHOD, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return f"Error: peer at {url} rejected auth (HTTP {e.code})."
            if e.code == 429:
                return f"Error: peer at {url} rate limited us (HTTP 429)."
            return f"Error: call to {url} failed — HTTP {e.code}."
        except Exception as e:
            return f"Error: call to {url} failed — {e}."

    if "error" in resp:
        err = resp["error"]
        # 老方言（v0.3）peer 只认 message/send（且 role 用小写）：自动回退一次再试
        if err.get("code") == -32601:
            try:
                resp = _send_message(
                    url,
                    message,
                    context_id=context_id,
                    method=_FALLBACK_METHOD,
                    role=_ROLE_USER_V03,
                    timeout=timeout,
                )
            except Exception as e:
                return f"Error: call to {url} failed — {e}."
            if "error" in resp:
                return f"Error: peer returned an error — {resp['error'].get('message', resp['error'])}"
        else:
            return f"Error: peer returned an error — {err.get('message', err)}"

    result = resp.get("result", {})
    reply = _reply_text(result)
    ctx = ((result.get("task") or {}).get("status") or {}).get("contextId", "") or context_id

    header = f"[A2A · {url}"
    if ctx:
        header += f" · context {ctx}"
    header += "]"

    body = reply or "(no reply)"
    if _input_required(result):
        body += (
            f"\n\n(The peer needs more input — call a2a_call again with "
            f"context_id='{ctx}'.)"
        )

    return f"{header}\n{body}"
