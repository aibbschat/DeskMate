"""A2A 服务端 — 让桌搭（openworker/deskmate）可被其它 agent 发现与调用。

基于官方 `a2a-sdk`（canonical A2A v1.0）实现，而不是手写 JSON-RPC：
- 用 `AgentExecutor` 把收到的新消息路由到桌搭真实能力（文稿编校 / 编辑专家 / 通用问答）；
- 用 `DefaultRequestHandler` + `InMemoryTaskStore` 管理 Task 生命周期；
- 用 `add_a2a_routes_to_fastapi` 把 Agent Card、JSON-RPC、REST 路由直接并入现有 FastAPI app。

⚠️ 方法名约定（重要）：A2A v1.0 的权威 JSON-RPC method 名取自官方 SDK 的 proto
`A2AService` RPC 名，即 `SendMessage` / `SendStreamingMessage`（大写、无斜杠）。
规范 markdown 老写法 `message/send` / `message/stream` 在官方 SDK 里只存在于
`a2a/compat/v0_3/`（老 v0.3 方言）。本服务端为**纯 v1.0**，只认 `SendMessage`。
调用方（`coworker/tools/a2a.py`）做了双语：主发 `SendMessage`，遇 -32601 回退 `message/send`。

数据模型与状态枚举严格遵循 ProtoJSON 规范（如 `TASK_STATE_COMPLETED` 而非小写 `completed`），
入站还需带 `A2A-Version: 1.0` 头（缺省按 0.3 处理、主版本不符会被拒）。
流式（`SendStreamingMessage`）由 SDK 根据我们入队的 `TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent` 自动转为 SSE。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from a2a.helpers.proto_helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)

from deskmate.memory.llm import chat, llm_available
from deskmate.tools.review import review_chapter
from deskmate.writing import (
    format_chapter_block,
    format_profile_for_prompt,
    format_report_card,
    generate_letter,
    proofread_chapter,
)

logger = logging.getLogger(__name__)

_ASK_SYSTEM = (
    "你是「小桔」，一位历史小说作家的专属 AI 搭档。回答要简洁、专业、"
    "带史料严谨性：涉及史实、官职、典制、年代时务必准确，拿不准要明说出处或存疑，"
    "不要编造。语气像一位靠谱的老搭档，不卑不亢。"
)

# 章节切分：命中「第X章/回/节」「Chapter N」或「N. 标题」这类行时切分
_CHAPTER_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千0-9]+[章回节卷部篇]|Chapter\s*\d+|[0-9]+[.、])",
    re.MULTILINE,
)


def _split_chapters(text: str) -> list[tuple[str, str]]:
    """把一篇长文按章节标题切成 [(标题, 正文), ...]；找不到标题则整体作为「全文」一章。"""
    lines = (text or "").splitlines()
    chapters: list[tuple[str, str]] = []
    title, buf = "全文", []
    for ln in lines:
        stripped = ln.strip()
        if _CHAPTER_RE.match(ln) and len(stripped) < 40:
            if buf:
                chapters.append((title, "\n".join(buf).strip()))
            title, buf = stripped, []
        else:
            buf.append(ln)
    if buf:
        chapters.append((title, "\n".join(buf).strip()))
    return chapters or [("全文", (text or "").strip())]


def _detect_skill(text: str) -> str:
    """按请求内容判定走哪个桌搭能力。"""
    t = (text or "").strip()
    if "[editorial]" in t or "评析" in t or "编辑专家" in t:
        return "editorial"
    if len(t) > 300:
        return "proofread"
    return "ask"


def build_agent_card(base_url: str) -> AgentCard:
    """构造桌搭的 Agent Card（canonical v1.0，ProtoJSON 字段名）。"""
    skills = [
        AgentSkill(
            id="deskmate.proofread",
            name="文稿编校",
            description="逐章流式校改：语病、节奏、用词、史实一致性，输出【原文】/【建议】/【理由】。",
            tags=["writing", "proofread", "编辑"],
            examples=["（粘贴一章小说正文，让小桔校一遍）"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="deskmate.editorial",
            name="编辑专家",
            description="文学性深度评析：单章概括、亮点、问题清单与全书一致性判断。",
            tags=["writing", "editorial", "评析"],
            examples=["[editorial] 评析这一章的节奏与人物塑造"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
        AgentSkill(
            id="deskmate.ask",
            name="通用问答",
            description="基于小桔历史/写作知识回答作者的问题，史料严谨、带出处。",
            tags=["qa", "history", "writing"],
            examples=["明代卫所和营兵制有什么区别？"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
    ]
    return AgentCard(
        name="小桔",
        description="openworker 小桔 —— 历史小说作家的 AI 搭档，提供文稿编校、编辑专家评析与史料问答。",
        version="1.0.0",
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=False,
        ),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=base_url.rstrip("/") or "http://localhost",
                protocol_version="1.0",
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=skills,
    )


class DeskmateAgentExecutor(AgentExecutor):
    """把 A2A 请求路由到桌搭的真实能力。"""

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id or "",
        )
        await updater.start_work(message=new_text_message("小桔正在处理你的请求…"))

        query = get_message_text(context.message) or ""
        try:
            reply = await self._dispatch(query)
        except Exception as exc:  # noqa: BLE001 — 任何能力内异常都转成失败态，不拖垮连接
            logger.exception("A2A dispatch failed")
            await updater.failed(message=new_text_message(f"处理失败：{exc}"))
            return

        await updater.add_artifact(
            parts=[new_text_part(text=reply, media_type="text/plain")]
        )
        await updater.complete(message=new_text_message("小桔已完成"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if not task:
            return
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task.id,
            context_id=task.context_id or "",
        )
        await updater.update_status(
            state=TaskState.TASK_STATE_CANCELED,
            message=new_text_message("任务已取消"),
        )

    async def _dispatch(self, query: str) -> str:
        # LLM 未就绪时仍给出友好提示，证明通道已通（便于联调）
        if not llm_available():
            return (
                "⚠️ 小桔后端 LLM 未就绪（未在 OpenWorker 配置可用模型）。\n"
                "A2A 通道已连通，配置模型后即可产出文稿编校 / 编辑专家 / 史料问答内容。"
            )

        skill = _detect_skill(query)
        if skill == "proofread":
            return await self._run_proofread(query)
        if skill == "editorial":
            return await self._run_editorial(query)
        return await asyncio.to_thread(
            chat, _ASK_SYSTEM, query, temperature=0.3, max_tokens=2000
        )

    async def _run_proofread(self, query: str) -> str:
        chapters = _split_chapters(query)
        blocks: list[str] = []
        per_chapter: list[tuple[str, int]] = []
        all_items: list[dict] = []
        for title, body in chapters:
            if not body:
                continue
            res = await asyncio.to_thread(
                proofread_chapter, title, body, "", self.workspace
            )
            items = res.get("items", []) if isinstance(res, dict) else []
            all_items.extend(items)
            blocks.append(format_chapter_block(title, items))
            per_chapter.append((title, len(items)))
        letter = await asyncio.to_thread(generate_letter, all_items)
        report = format_report_card("A2A 文稿编校", per_chapter, len(all_items), letter)
        return "\n".join([*blocks, report]).strip() + "\n"

    async def _run_editorial(self, query: str) -> str:
        chapters = _split_chapters(query)
        parts: list[str] = []
        # 加载全局风格画像（~/.deskmate/style_profile.json），提升编辑专家分析质量
        profile_text = ""
        try:
            _desk = os.path.expanduser(os.environ.get("DESKMATE_DIR", "~/.deskmate"))
            _sp = os.path.join(_desk, "style_profile.json")
            if os.path.isfile(_sp):
                _p = json.loads(open(_sp, encoding="utf-8").read()).get("profile")
                profile_text = format_profile_for_prompt(_p)
        except Exception:  # noqa: BLE001
            pass
        for title, body in chapters:
            if not body:
                continue
            r = await asyncio.to_thread(review_chapter, title, body, profile_text, "")
            parts.append(f"## {title}")
            if isinstance(r, dict) and r.get("error"):
                parts.append(f"⚠️ {r['error']}")
                continue
            if isinstance(r, dict):
                if r.get("summary"):
                    parts.append(f"**概括**：{r['summary']}")
                if r.get("analysis"):
                    parts.append(f"**评析**：{r['analysis']}")
                for it in r.get("issues", []) or []:
                    parts.append(
                        f"- [{it.get('type', '')}] {it.get('excerpt', '')} —— {it.get('comment', '')}"
                    )
        return "\n".join(parts).strip() + "\n"


def build_request_handler(card: AgentCard, workspace: str = "") -> DefaultRequestHandler:
    return DefaultRequestHandler(
        agent_executor=DeskmateAgentExecutor(workspace=workspace),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )


def register_a2a(app, manager=None) -> None:
    """把 A2A 路由并入现有 FastAPI app。在 create_app 内调用。"""
    workspace = getattr(manager, "default_workspace", "") or ""
    base_url = os.environ.get("OPENWORKER_A2A_URL", "http://localhost")
    card = build_agent_card(base_url)
    handler = build_request_handler(card, workspace=workspace)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        rest_routes=create_rest_routes(handler),
    )
