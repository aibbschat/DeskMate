"""OpenCode Zen 免费通道 provider — 按 opencode2dsh/dsh-plugin 移植的线协议。

免费通道（``https://opencode.ai/zen/v1`` 的 ``*-free`` 模型）不是普通的 OpenAI 兼容端点，
它对匿名流量做了两层校验（2026-09-16 起实测）：

1. 请求头必须是 opencode CLI 的形状：``Authorization: Bearer public``（字面量 "public"）
   加 ``x-opencode-client/project/session/request`` 伪装头与 CLI 的 User-Agent；
2. 请求体必须带 agent 形状 —— ``tools`` 数组里要有名为 ``bash`` 和 ``read`` 的 function
   工具（网关不检查参数与描述，仅查名字）；上下文里没有真实工具时补 ``tool_choice:"none"``。
   且**只支持流式**（``stream:false`` 会被拒）。

会话/请求 id 的派生与该插件逐字节一致（``_canonical_session_id`` 有 JS 平价测试锁住）：
网关用 session id 形状做匿名额度记账，形状不对直接 403 FreeTierError。

协议来源：~/.dsh/profiles/desktop/node_modules/@opencode2dsh/dsh-plugin/lib/*.js
（v0.3.3），2026-10-06 逆向并实测验证后移植。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from typing import Any, Optional

from .base import AssistantTurn
from .openai_provider import OpenAIProvider

ANONYMOUS_KEY = "public"
ZEN_BASE_URL = "https://opencode.ai/zen/v1"
# 探针模型：2026-10-06 实测免费通道可达且响应最快（TTFT ~950ms）的 *-free 模型。
PROBE_MODEL = "space-bunny-free"

# 免费通道默认窗口/输出上限（插件 DEFAULT_CONTEXT_WINDOW / DEFAULT_MAX_TOKENS）。
DEFAULT_CONTEXT_WINDOW = 262_144

# -- 匿名 CLI 伪装 -------------------------------------------------------------
# 网关不校验 UA 内容（实测），但保持与插件一致的形状最稳。
_CLI_VERSION = "1.18.31"
_NODE_VERSION = "24.18.0"


def _opencode_user_agent() -> str:
    plat = {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(
        sys.platform, sys.platform
    )
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    return f"opencode/{_CLI_VERSION} ({plat} {arch}; node{_NODE_VERSION})"


# -- 会话/请求 id 派生（与插件 lib/catalog.js 逐字节一致）-----------------------

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _base62_fixed(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_B62[value % 62])
        value //= 62
    return "".join(reversed(chars))


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256((prefix + "\0" + value).encode("utf-8")).digest()
    return f"{prefix}_{digest[:12].hex()}"


def _canonical_session_id(seed: str) -> str:
    digest = hashlib.sha256(("ses\0" + seed).encode("utf-8")).digest()
    head = digest[:6].hex()
    tail = _base62_fixed(int.from_bytes(digest[6:16], "big"), 14)
    return f"ses_{head}{tail}"


_PROJECT_ID = _stable_id("prj", "opencode2dsh:default-project")


def _derive_ids(messages: Optional[list[dict[str, Any]]]) -> dict[str, str]:
    """session id 从首条 user 消息内容派生（同对话同 id → 同一免费额度桶），request id
    每次随机 —— 与插件行为一致。"""
    seed: Optional[str] = None
    for message in messages or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            seed = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        elif content is not None:
            seed = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        if seed:
            break
    if not seed:
        seed = os.urandom(16).hex()
    return {
        "session": _canonical_session_id(seed),
        "request": "req_" + os.urandom(16).hex(),
        "project": _PROJECT_ID,
    }


# -- 免费通道的 bash/read 门面工具 ----------------------------------------------

GATE_TOOL_NAMES = ("bash", "read")
GATE_TOOL_DESCRIPTION = "Reserved for the host runtime; do not call it."


def _gate_tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": GATE_TOOL_DESCRIPTION,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _apply_free_lane_gate(
    tools: Optional[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """返回（补充门面工具后的 tools, tool_choice）。tool_choice 仅在调用方没带任何真实
    工具时置 "none"（防止模型调用门面桩）；有真实工具时保持网关默认，不干预。"""
    body = list(tools or [])
    had_real_tools = bool(body)
    names = {
        t.get("function", {}).get("name") for t in body if isinstance(t, dict)
    }
    for name in GATE_TOOL_NAMES:
        if name not in names:
            body.append(_gate_tool(name))
    return body, ("none" if not had_real_tools else None)


def _lane_headers(ids: dict[str, str]) -> dict[str, str]:
    # Authorization 不在这里设置：SDK 从 api_key（默认 ANONYMOUS_KEY）自动带 Bearer。
    return {
        "user-agent": _opencode_user_agent(),
        "x-opencode-client": "cli",
        "x-opencode-project": ids["project"],
        "x-opencode-session": ids["session"],
        "x-opencode-request": ids["request"],
    }


class OpencodeProvider(OpenAIProvider):
    """免费通道的 OpenAI Chat Completions provider。

    与基类的差别只在出站请求的伪装：extra_headers（CLI 头）+ 门面工具 + 空工具时的
    tool_choice。api_key 为空时用匿名 "public"；用户填了付费 key 则原样转发（同一网关
    的付费模型）。绝不回退 OPENAI_API_KEY / SecretStore 的 OpenAI 配置。
    """

    def __init__(
        self,
        client: Any = None,
        *,
        default_model: str = PROBE_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        secrets: Any = None,
    ):
        super().__init__(
            client,
            default_model=default_model,
            api_key=(api_key or "").strip() or ANONYMOUS_KEY,
            base_url=(base_url or "").strip() or ZEN_BASE_URL,
            secrets=secrets,
        )

    def stream(self, *, model: str, messages: list[dict[str, Any]], tools=None, **settings):
        settings.setdefault("extra_headers", _lane_headers(_derive_ids(messages)))
        body_tools, tool_choice = _apply_free_lane_gate(tools)
        if tool_choice is not None:
            settings.setdefault("tool_choice", tool_choice)
        yield from super().stream(model=model, messages=messages, tools=body_tools, **settings)

    def complete(self, *, model: str, messages: list[dict[str, Any]], tools=None, **settings) -> AssistantTurn:
        # 免费通道只收流式请求：非流式 complete 聚合流式结果，行为对引擎透明。
        turn: Optional[AssistantTurn] = None
        for chunk in self.stream(model=model, messages=messages, tools=tools, **settings):
            if chunk.turn is not None:
                turn = chunk.turn
        if turn is None:
            raise RuntimeError("OpenCode 免费通道未返回任何内容（流提前结束）。")
        return turn


def probe_free_lane(base_url: Optional[str] = None, timeout: float = 10.0) -> dict[str, Any]:
    """Test 按钮的存活探针：发一个门面形状的流式请求，读首块即返回。

    200 = 可用；429 = 可达但匿名额度耗尽/限流（算 ok，让用户知道网关本身通）；
    401/403 = 被网关拒绝（常见于区域封锁或协议校验失败）。代理按环境变量放行
    （trust_env 默认开）—— 对区域受限的用户反而是救命的。
    """
    import httpx

    base = ((base_url or "").strip().rstrip("/") or ZEN_BASE_URL).rstrip("/")
    headers = {
        "authorization": f"Bearer {ANONYMOUS_KEY}",
        **_lane_headers(_derive_ids([{"role": "user", "content": "hi"}])),
        "accept": "text/event-stream",
    }
    body = {
        "model": PROBE_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 16,
        "tools": [_gate_tool(n) for n in GATE_TOOL_NAMES],
        "tool_choice": "none",
    }
    try:
        with httpx.stream(
            "POST", base + "/chat/completions", headers=headers, json=body, timeout=timeout
        ) as resp:
            if resp.status_code < 300:
                return {"ok": True}
            if resp.status_code == 429:
                return {"ok": True, "note": "免费通道可达，但匿名额度当前被限流，稍后重试。"}
            if resp.status_code in (401, 403):
                return {
                    "ok": False,
                    "error": f"OpenCode 免费通道拒绝请求（HTTP {resp.status_code}）。"
                    "多为区域封锁或风控，可尝试配置代理后重测。",
                }
            return {"ok": False, "error": f"OpenCode 免费通道返回 HTTP {resp.status_code}。"}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"连接 OpenCode 免费通道失败（{exc.__class__.__name__}）。请检查网络或网关地址。",
        }
