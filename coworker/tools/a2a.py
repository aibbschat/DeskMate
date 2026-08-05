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

_DEFAULT_TIMEOUT = 120

# ---------------------------------------------------------------------------
# A2A JSON-RPC helpers
# ---------------------------------------------------------------------------

_PROTOCOL_VERSION = "1.0"

_ROLE_USER = "ROLE_USER"


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


def _send_message(url: str, message: str, context_id: str = "", timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """Send an A2A JSON-RPC message and return the parsed response."""
    payload = {
        "jsonrpc": "2.0",
        "id": _new_id(),
        "method": "SendMessage",
        "params": {
            "message": _text_message(message, context_id=context_id),
        },
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "A2A-Version": _PROTOCOL_VERSION,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
) -> str:
    """Send a task to an A2A agent and return its reply.

    Use this to delegate work to another agent. The default URL points to
    a local Hermes A2A server. Pass context_id from a previous reply to
    continue a multi-turn conversation.

    Args:
        url: The A2A endpoint URL of the target agent (e.g. http://localhost:9900)
        message: The task or message to send, in natural language
        context_id: Optional context id from a prior reply, to continue the conversation
    """
    if not message:
        return "Error: 'message' is required."

    try:
        resp = _send_message(url, message, context_id=context_id)
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