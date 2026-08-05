"""A2A (Agent-to-Agent) server for DeskMate.

把桌搭的高价值能力暴露给其它 A2A agent（如 Hermes、opencode），采用与
coworker/tools/a2a.py 这个纯标准库客户端完全一致的契约：

  GET  /.well-known/agent-card.json   -> 桌搭 Agent Card
  POST /a2a  (以及 POST /)            -> JSON-RPC 2.0，method="SendMessage"

响应信封对齐 a2a.py 的 _reply_text 期望：
  {"jsonrpc":"2.0","id":<id>,"result":{"task":{
      "status":{"state":"COMPLETED","message":{...}},
      "artifacts":[{"parts":[{"text":<reply>}]}]
  }}}

说明：当前端点放进了 tokenless_paths（本地可信网络放行），企业级鉴权
（A2A enterprise readiness）按 a2a_integration_eval.md 规划后续补。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "1.0"

# 桌搭对外暴露的能力（与 a2a_integration_eval.md 规划一致）
SKILLS = [
    {
        "id": "deskmate.proofread",
        "name": "文稿编校",
        "description": "对中文书稿逐章审阅，返回【原文】/【建议】/【理由】校改块与总览报告卡。适合长文稿润色。",
        "tags": ["proofread", "writing", "edit", "manuscript"],
        "examples": ["[proofread] 标题\n正文…"],
    },
    {
        "id": "deskmate.editorial",
        "name": "编辑专家",
        "description": "以文学编辑视角做一次性深度评析：结构、节奏、人物弧光、主题。",
        "tags": ["editorial", "literary", "review"],
        "examples": ["[editorial] 贴一段正文，请评析"],
    },
    {
        "id": "deskmate.ask",
        "name": "通用问答",
        "description": "以桌搭（历史小说创作助手）人设回答作者关于写作、史料、结构的问题。",
        "tags": ["chat", "ask", "writing"],
    },
]


def build_agent_card(base_url: str) -> dict[str, Any]:
    """构造桌搭的 A2A Agent Card。base_url 取请求的实际访问地址。"""
    base = (base_url or "").rstrip("/")
    rpc_url = f"{base}/a2a"
    return {
        "name": "DeskMate (openworker)",
        "description": "桌搭 —— 面向历史小说作家的 AI 创作搭档，提供文稿编校、编辑专家评析与写作问答。",
        "url": base + "/",
        "version": "1.0.0",
        "protocolVersion": _PROTOCOL_VERSION,
        "provider": {"organization": "openworker", "url": base + "/"},
        "supportedInterfaces": [
            {"url": rpc_url, "protocolBinding": "JSONRPC", "protocolVersion": _PROTOCOL_VERSION}
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": SKILLS,
    }


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------

def _detect_skill(text: str, skill_hint: str = "") -> str:
    t = (text or "").strip()
    hint = (skill_hint or "").strip().lower()
    if hint in ("proofread", "deskmate.proofread"):
        return "deskmate.proofread"
    if hint in ("editorial", "deskmate.editorial"):
        return "deskmate.editorial"
    if hint in ("ask", "deskmate.ask"):
        return "deskmate.ask"
    low = t.lower()
    if low.startswith("[proofread]") or low.startswith("【proofread】"):
        return "deskmate.proofread"
    if low.startswith("[editorial]") or low.startswith("【editorial】"):
        return "deskmate.editorial"
    # 启发式：长散文（>300 字且无问号）倾向文稿编校
    if len(t) > 300 and "?" not in t and "？" not in t:
        return "deskmate.proofread"
    return "deskmate.ask"


# ---------------------------------------------------------------------------
# 各能力处理器（同步；调用方用 asyncio.to_thread 包一层）
# ---------------------------------------------------------------------------

def _strip_prefix(text: str, prefixes) -> str:
    for pre in prefixes:
        if text.lower().startswith(pre.lower()):
            return text[len(pre):].strip()
    return text.strip()


def _run_proofread(text: str) -> str:
    from deskmate.writing import (
        proofread_chapter,
        format_chapter_block,
        format_report_card,
        generate_letter,
        split_into_chapters,
    )
    text = _strip_prefix(text, ("[proofread]", "【proofread】"))
    chapters = split_into_chapters(text)
    if not chapters:
        chapters = [{"idx": 0, "title": "全文", "text": text}]
    all_items: list = []
    per_chapter: list = []
    for c in chapters:
        res = proofread_chapter(c["title"], c["text"], "")
        items = res["items"]
        all_items.extend(items)
        per_chapter.append((c["title"], len(items)))
    letter = generate_letter(all_items)
    card = format_report_card("投稿", per_chapter, len(all_items), letter)
    blocks = "\n\n".join(format_chapter_block(t, its) for t, its in per_chapter)
    return f"{blocks}\n\n{card}"


def _run_editorial(text: str) -> str:
    from deskmate.memory.llm import chat, llm_available
    text = _strip_prefix(text, ("[editorial]", "【editorial】"))
    if not llm_available():
        return "（编辑专家暂不可用：未配置可用模型）"
    sys = (
        "你是一位资深文学编辑，擅长历史小说的结构、节奏、人物弧光与主题评析。"
        "请给出简洁、可操作的一次性评析，分点列出，避免空话。"
    )
    return chat(sys, f"请评析下面这段文稿：\n\n{text}", temperature=0.5, max_tokens=1200).strip()


def _run_ask(text: str) -> str:
    from deskmate.memory.llm import chat, llm_available
    if not llm_available():
        return "（桌搭暂不可用：未配置可用模型）"
    sys = (
        "你是桌搭，一位服务于历史小说作家的 AI 创作搭档，严谨、懂史料、说话利落。"
        "用中文回答，必要时给出可落地的写作建议。"
    )
    return chat(sys, text, temperature=0.6, max_tokens=1200).strip()


_HANDLERS = {
    "deskmate.proofread": _run_proofread,
    "deskmate.editorial": _run_editorial,
    "deskmate.ask": _run_ask,
}


def dispatch(text: str, skill_hint: str = "") -> str:
    """把一条 A2A 消息路由到对应能力，返回纯文本回复。"""
    skill = _detect_skill(text, skill_hint)
    handler = _HANDLERS.get(skill, _run_ask)
    try:
        return handler(text) or "（无内容返回）"
    except Exception as e:  # 不让单能力失败拖垮整个 A2A 调用
        logger.warning("A2A skill %s failed: %s", skill, e)
        return f"⚠️ 桌搭处理「{skill}」时出错：{e}"


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 分发
# ---------------------------------------------------------------------------

def _jsonrpc(id_, result=None, error=None) -> dict:
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


def _task_result(reply_text: str, context_id: str = "") -> dict:
    status: dict[str, Any] = {
        "state": "COMPLETED",
        "message": {"role": "agent", "parts": [{"text": reply_text, "mediaType": "text/plain"}]},
    }
    if context_id:
        status["contextId"] = context_id
    return {
        "task": {
            "status": status,
            "artifacts": [{"parts": [{"text": reply_text, "mediaType": "text/plain"}]}],
        }
    }


def handle_a2a_request(payload: dict) -> dict:
    """处理一个 JSON-RPC 请求 dict，返回 JSON-RPC 响应 dict。"""
    if not isinstance(payload, dict):
        return _jsonrpc(None, error={"code": -32600, "message": "Invalid Request"})
    rpc_id = payload.get("id")
    if payload.get("jsonrpc") != "2.0":
        return _jsonrpc(rpc_id, error={"code": -32600, "message": "Invalid Request"})
    method = payload.get("method")
    if method != "SendMessage":
        return _jsonrpc(rpc_id, error={"code": -32601, "message": f"Method not found: {method}"})
    params = payload.get("params", {}) or {}
    message = params.get("message", {}) or {}
    parts = message.get("parts", []) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    context_id = message.get("contextId", "") or ""
    skill_hint = (params.get("skill") or "").strip()
    if not text:
        return _jsonrpc(rpc_id, result=_task_result("（未收到有效文本内容）", context_id))
    reply = dispatch(text, skill_hint)
    return _jsonrpc(rpc_id, result=_task_result(reply, context_id))
