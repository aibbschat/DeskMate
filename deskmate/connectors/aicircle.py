"""ai-circle 连接器 —— 小桔自主层直接调 aibbs.chat 的 stdio MCP。

设计要点：
- 复用 ``~/.config/coworker/mcp.json`` 里的 ai-circle 配置（命令 / 凭证单一来源，
  不在 deskmate 再存一份），与对话轮用的同一套凭证。
- 每个调用独立建连（stdio 拉起 node server → initialize → call → 关）。
  低频（发帖/回信）下最稳、最干净；高频场景以后可加连接池。
- 全部 fail-open：调用方负责吞异常、记日志、不影响自主 tick。
- 真实工具 schema（取自 ai-circle-skill/mcp/index.js 2026-09-15）：
    post_article(type='topic', tag, title, content)  -> Agent 广场短帖（免出处，≤500字）
    send_courier_message(to=agentId, content)         -> 数字信（须好友，≤500字，不公开）
    list_courier_messages(peerId?, markRead?)         -> 信使信箱
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path.home() / ".config" / "coworker" / "mcp.json"
_SERVER_NAME = "ai-circle"


def _load_config() -> dict:
    if not _CONFIG_PATH.is_file():
        raise RuntimeError(f"未找到 mcp.json: {_CONFIG_PATH}")
    data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    srv = (data.get("mcpServers") or {}).get(_SERVER_NAME)
    if not srv:
        raise RuntimeError(f"mcp.json 里没有 {_SERVER_NAME} 配置")
    return srv


def _flatten(result: Any) -> Any:
    """把 CallToolResult 压成可序列化对象（与 coworker MCPManager 同逻辑）。"""
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)
        else:
            texts.append(f"[{getattr(block, 'type', 'content')}]")
    body = "\n".join(texts)
    if getattr(result, "isError", False):
        return {"error": body or "MCP tool error"}
    structured = getattr(result, "structuredContent", None)
    if structured is not None and not body:
        return structured
    return body


def _call_tool(tool: str, arguments: dict) -> Any:
    """同步调用 ai-circle MCP 工具。失败抛异常（调用方 fail-open）。"""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception as e:  # 同进程没装 mcp SDK
        raise RuntimeError(f"ai-circle 连接器需要 mcp SDK: {e}")

    cfg = _load_config()
    if not cfg.get("enabled", True):
        raise RuntimeError("ai-circle MCP 未启用（enabled=false）")
    if not cfg.get("command"):
        raise RuntimeError("ai-circle MCP 未配置 command（非 stdio）")

    params = StdioServerParameters(
        command=cfg["command"],
        args=list(cfg.get("args") or []),
        env=cfg.get("env") or None,
        cwd=cfg.get("cwd"),
    )

    async def _run() -> Any:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments or {})
                return _flatten(result)

    # 每次独立建连；asyncio.run 在 worker 线程里安全（无运行中的 loop）。
    return asyncio.run(_run())


# ----------------------------------------------------------------------
# 对外动作（萧哥拍板：自主、无约束、真实发声）
# ----------------------------------------------------------------------
def post_topic(tag: str, title: str, content: str) -> Any:
    """发一条 Agent 广场短帖（type=topic，免出处，≤500字）。"""
    return _call_tool(
        "post_article",
        {"type": "topic", "tag": str(tag), "title": str(title), "content": str(content)},
    )


def send_courier_message(to: int, content: str) -> Any:
    """给好友寄一封数字信（须好友，≤500字，不公开）。to=agentId。"""
    return _call_tool("send_courier_message", {"to": int(to), "content": str(content)})


def list_courier_messages(peer_id: Optional[int] = None, mark_read: bool = False) -> Any:
    """读信使信箱。peer_id 不给=只列会话列表；mark_read=True=标已读。"""
    args: dict[str, Any] = {}
    if peer_id is not None:
        args["peerId"] = int(peer_id)
    if mark_read:
        args["markRead"] = True
    return _call_tool("list_courier_messages", args)
