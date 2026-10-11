"""划词适配器服务器：OpenAI 兼容代理 + 搜索动作注入。

对外固定端口（默认 18795），CORS 放开（仅本地浏览器扩展调用）。划词是偶发单轮任务，
**默认走 OmniRoute 本机免费模型**（免密钥、不烧付费额度）；仅当用户显式指定
``deepseek``/``mimo`` 等付费短名时才转发到 sidecar 付费模型。搜索动作通过哨兵前缀
识别后注入联网检索结果。
"""
from __future__ import annotations

import json
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .discovery import resolve_sidecar

# 客户端自定义 prompt 以该前缀开头时，视为「联网搜索」动作。
SEARCH_SENTINEL = "【联网搜索】"

# 模型路由：划词是偶发单轮任务，默认走 OmniRoute 本机免费模型（免密钥、不烧付费额度）。
# 仅当用户显式指定 deepseek/mimo 等付费短名时才走 sidecar 付费模型。
OMNIROUTE_BASE = os.environ.get(
    "DESKMATE_OMNIROUTE_BASE", "http://127.0.0.1:20128/v1"
)
OMNIROUTE_KEY = "omniroute"  # OmniRoute 免密钥占位 key
FREE_MODEL = "auto"  # OmniRoute 免费档 model

# 短名别名 -> "provider:model" 全名（走 sidecar 付费）或 "omniroute:*" 标记（走免费网关）。
# 未知短名（如 gpt-4）回落免费模型，避免误烧付费额度。
_MODEL_ALIASES = {
    "free": "omniroute:auto",
    "auto": "omniroute:auto",
    "omniroute": "omniroute:auto",
    "deepseek-chat": "deepseek:deepseek-v4-flash",
    "deepseek": "deepseek:deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek:deepseek-v4-flash",
    "mimo": "xiaomi:mimo-v2.6",
    "mimo-v2.6": "xiaomi:mimo-v2.6",
    "mimo-v2.6-pro": "xiaomi:mimo-v2.6-pro",
}


def _resolve_target(model):
    """返回 ``(target, model_name)``。

    - ``target="free"``    -> 直连 OmniRoute 免费网关（model=auto，免密钥）
    - ``target="sidecar"`` -> 转发到 sidecar（provider:model 全名或回落其默认模型）
    """
    if not model:
        return ("free", FREE_MODEL)
    s = str(model).strip().lower()
    if ":" in s:
        prov, _, m = s.partition(":")
        if prov == "omniroute":
            return ("free", m or FREE_MODEL)
        return ("sidecar", s)
    full = _MODEL_ALIASES.get(s)
    if full is None:
        return ("free", FREE_MODEL)  # 未知短名 -> 免费兜底
    if full.startswith("omniroute:"):
        return ("free", full.split(":", 1)[1])
    return ("sidecar", full)

# 注入点：单元测试用 httpx.MockTransport 替换，避免依赖真实 sidecar。
_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))
    return _client


def build_app() -> FastAPI:
    app = FastAPI(title="DeskMate Selection Adapter")
    # 仅本地使用：放开 CORS，让浏览器扩展在任意页面源下都能调用 localhost 适配器。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/v1/health")
    async def health():
        try:
            resolve_sidecar()
            return {"ok": True, "service": "deskmate-selection-adapter"}
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    @app.post("/v1/chat/completions")
    async def chat_completions(req: Request):
        return await _proxy_chat(req)

    @app.post("/v1/deskmate/selection")
    async def selection(req: Request):
        body = await req.json()
        action = (body.get("action") or "explain").lower()
        text = body.get("text", "")
        target_lang = body.get("target_lang", "中文")
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        if action == "search":
            results = _run_web_search(text)
            ctx = _format_results(results)
            sys_prompt = (
                "你是萧盛（历史小说作家）的写作助手。基于下方联网检索结果，"
                f"用{target_lang}回答用户的问题，并标注信息来源。"
            )
            payload = {
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"检索背景：\n{ctx}\n\n问题：{text}"},
                ],
            }
        else:
            payload = {
                "messages": _build_messages(action, text, target_lang),
            }
        norm = _resolve_target(body.get("model"))
        if norm[0] != "free":
            payload["model"] = norm[1]
        out = await _complete_text(payload)
        return {"action": action, "result": out}

    return app


# --------------------------------------------------------------------------- #
# 代理 / 动作实现
# --------------------------------------------------------------------------- #

async def _proxy_chat(req: Request) -> Response:
    try:
        payload = json.loads(await req.body())
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    if _detect_search_sentinel(payload):
        payload = await _inject_web_search(payload)

    target, model = _resolve_target(payload.get("model"))
    payload["model"] = model
    want_stream = bool(payload.get("stream"))

    if target == "free":
        # OmniRoute 免费档不真正流式；适配器统一取全量再包标准 SSE 帧。
        upstream = f"{OMNIROUTE_BASE}/chat/completions"
        headers = {
            "authorization": f"Bearer {OMNIROUTE_KEY}",
            "content-type": "application/json",
        }
    else:
        port, token = resolve_sidecar()
        upstream = f"http://127.0.0.1:{port}/v1/chat/completions"
        headers = {"x-openworker-token": token, "content-type": "application/json"}

    # 免费档网关与 sidecar 都不真正流式（无论 stream 都回完整 JSON），而 OpenAI Translator
    # 等客户端开启流式时期待 SSE 帧。这里统一取全量再包成标准 OpenAI SSE 帧（代价是失去
    # 逐字增量显示），保证任何客户端都能解析。
    payload["stream"] = False
    body = json.dumps(payload, ensure_ascii=False)

    if not want_stream:
        r = await get_client().post(upstream, content=body, headers=headers)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def gen():
        r = await get_client().post(upstream, content=body, headers=headers)
        if r.status_code != 200:
            yield ("data: " + json.dumps({"error": r.text[:500]}) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"
            return
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except Exception:
            content = r.text
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
            ]
        }
        yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _complete_text(payload: dict) -> str:
    """非流式调用，从 OpenAI 格式响应里抽出纯文本。默认走免费模型。"""
    target, model = _resolve_target(payload.get("model"))
    payload = {**payload, "stream": False, "model": model}
    if target == "free":
        upstream = f"{OMNIROUTE_BASE}/chat/completions"
        headers = {
            "authorization": f"Bearer {OMNIROUTE_KEY}",
            "content-type": "application/json",
        }
    else:
        port, token = resolve_sidecar()
        upstream = f"http://127.0.0.1:{port}/v1/chat/completions"
        headers = {"x-openworker-token": token, "content-type": "application/json"}
    r = await get_client().post(
        upstream, content=json.dumps(payload, ensure_ascii=False), headers=headers
    )
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception:
        return r.text


def _build_messages(action: str, text: str, target_lang: str) -> list[dict]:
    if action == "translate":
        sys_prompt = (
            f"你是萧盛（历史小说作家）的写作助手。把用户内容准确翻译为{target_lang}，"
            "保留人名、地名、官职等专有名词原文并在括号中标注，语气符合文学写作。"
        )
    elif action == "summarize":
        sys_prompt = (
            f"你是萧盛（历史小说作家）的写作助手。用{target_lang}概括用户内容的核心要点，"
            "条理清晰、不添油加醋。"
        )
    else:  # explain
        sys_prompt = (
            "你是萧盛（历史小说作家）的写作助手。请解释用户选中的词语或段落的准确含义、"
            "适用语境与常见误用，必要时举例；若涉及史实典故，务必严谨并点明出处。"
        )
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text},
    ]


# --------------------------------------------------------------------------- #
# 联网搜索（仅搜索动作使用）
# --------------------------------------------------------------------------- #

def _detect_search_sentinel(payload: dict) -> bool:
    for m in payload.get("messages", []):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str) and c.startswith(SEARCH_SENTINEL):
                return True
    return False


async def _inject_web_search(payload: dict) -> dict:
    query = None
    for m in payload.get("messages", []):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str) and c.startswith(SEARCH_SENTINEL):
                query = c[len(SEARCH_SENTINEL):].strip()
                m["content"] = query  # 清掉哨兵前缀
                break
    if not query:
        return payload

    results = _run_web_search(query)
    ctx = _format_results(results)
    injection = f"以下是联网检索到的结果，请基于它们回答用户问题：\n{ctx}"
    msgs = payload.get("messages", [])
    sys_msg = next((m for m in msgs if m.get("role") == "system"), None)
    if sys_msg:
        sys_msg["content"] = (sys_msg.get("content", "") + "\n\n" + injection).strip()
    else:
        msgs.insert(0, {"role": "system", "content": injection})
    payload["messages"] = msgs
    return payload


def _run_web_search(query: str) -> dict:
    try:
        from coworker.web.tool import make_web_search_tool

        return make_web_search_tool()(query, max_results=5)
    except Exception as e:  # 未配置搜索 provider / 网络异常
        return {"error": str(e)}


def _format_results(results: dict) -> str:
    if not results or results.get("error"):
        return f"（联网搜索暂不可用：{results.get('error', '未知错误')}）"
    lines = []
    for i, r in enumerate(results.get("results", []), 1):
        title = r.get("title", "")
        url = r.get("url", "")
        snippet = r.get("snippet") or r.get("body") or ""
        lines.append(f"[{i}] {title}\n{url}\n{snippet}")
    return "\n\n".join(lines) if lines else "（无检索结果）"
