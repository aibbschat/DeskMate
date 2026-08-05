"""创作圆桌 · Web UI 服务端。

把圆桌协调器通过 SSE 实时推到浏览器，呈现微信风群聊（成员头像气泡实时冒出）。

设计要点（对应"做完善、以真实为目的"）：
- 单进程即可跑通完整链路：本服务把 opencode 的 A2A 适配层挂载在 /opencode-a2a，
  因此无需另起服务即可让 OpenCode 作为外部 peer 参与圆桌（走真实 A2A：发现 + SendMessage）。
- /api/stream 用 SSE 边跑边推协调器事件（speaking / turn / round_start / done…），
  前端据此实时冒出"思考中"气泡与发言气泡，且严格保持协调器的【到达序】。
- 外部 peer（OpenCode / Hermes）由前端勾选，经协调器 a2a_discover 探活、离线则跳过。

运行：
    python -m deskmate.roundtable.web --port 9920
浏览器打开 http://localhost:9920
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from deskmate.roundtable.coordinator import PeerSpec, Roundtable
from deskmate.roundtable.personas import PERSONAS

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = (
    "一段明代背景小说开篇：主角以锦衣卫身份出入诏狱，借『东厂』之名缉拿官绅，"
    "文中写『万历年间，东厂提督冯保秉笔直入内廷』。请各位从各自角度评审这一段。"
)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "roundtable.html")


class StreamBody(BaseModel):
    """POST /api/stream 的请求体（context 放 body 避免 URL 超长）。"""

    topic: str = DEFAULT_TOPIC
    max_rounds: int = 2
    peers: str = ""
    personas: str = ""
    context: str = ""
    manuscript_path: str = ""
    chapter_range: str = ""


def _read_html() -> str:
    try:
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>roundtable.html 模板缺失</h1>"


def create_app(rt_port: int = 9920) -> FastAPI:
    opencode_peer_url = f"http://localhost:{rt_port}/opencode-a2a"
    app = FastAPI(title="创作圆桌")

    # 把 opencode A2A 适配层挂到 /opencode-a2a：单进程即可让 OpenCode 成为 peer。
    try:
        from coworker.server.opencode_a2a_server import build_app as build_opencode_app

        oc_app = build_opencode_app(f"http://localhost:{rt_port}/opencode-a2a")
        app.mount("/opencode-a2a", oc_app)
        logger.info("opencode A2A 已挂载于 /opencode-a2a (peer=%s)", opencode_peer_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("opencode A2A 挂载失败（圆桌仍可运行，OpenCode 将离线）：%s", exc)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_read_html())

    @app.get("/api/health")
    async def health():
        from deskmate.memory.llm import llm_available

        return JSONResponse({"llm": llm_available()})

    def _make_stream(
        request: Request,
        topic: str,
        max_rounds: int,
        peers: str,
        personas: str,
        context: str,
        manuscript_path: str = "",
        chapter_range: str = "",
    ) -> StreamingResponse:
        """构造圆桌 SSE 响应：peer/persona/context/manuscript 解析 + 协调器流式事件。"""
        # 加载待评审文稿（文件路径/书名），注入 prompt 让人格基于真实文本发言
        manuscript_text = ""
        mp = (manuscript_path or "").strip()
        if mp:
            try:
                from deskmate.tools.review import load_manuscript_file

                result = load_manuscript_file(mp)
                if result.get("error"):
                    logger.warning("加载文稿失败(%s): %s", mp, result["error"])
                else:
                    chapters = result.get("chapters") or []
                    # 支持章节范围选择（如 "1-3" 或 "2"），默认全部（限前 5 章防超 context）
                    selected = chapters
                    cr = (chapter_range or "").strip()
                    if cr:
                        try:
                            if "-" in cr:
                                lo, hi = cr.split("-", 1)
                                lo_i, hi_i = int(lo.strip()), int(hi.strip())
                                selected = [c for c in chapters if lo_i <= c.get("idx", 0) <= hi_i]
                            else:
                                idx = int(cr)
                                selected = [c for c in chapters if c.get("idx", 0) == idx]
                        except (ValueError, IndexError):
                            selected = chapters[:5]
                    else:
                        selected = chapters[:5]
                    parts = []
                    for c in selected:
                        parts.append(f"### {c.get('title', '')}\n{c.get('text', '')}")
                    manuscript_text = "\n\n".join(parts)
                    # 注意：圣经宏观摘要与全书检索片段改由协调器统一注入
                    # （deskmate.roundtable.coordinator.Roundtable._build_story_context），
                    # 此处不再预插，避免与协调器的完整摘要重复。
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载文稿异常(%s): %s", mp, exc)

        peer_list: list[PeerSpec] = []
        for part in (peers or "").split(","):
            part = part.strip()
            if not part:
                continue
            if "|" in part:
                name, url = part.split("|", 1)
                peer_list.append(PeerSpec(name.strip(), url.strip()))
            else:
                peer_list.append(PeerSpec(part, part))

        selected_personas = None
        ids = [p.strip() for p in (personas or "").split(",") if p.strip()]
        if ids:
            by_id = {p.id: p for p in PERSONAS}
            selected_personas = [by_id[i] for i in ids if i in by_id] or None

        rt = Roundtable(
            topic=topic,
            peers=peer_list or None,
            personas=selected_personas,
            max_rounds=int(max_rounds),
            context=context or "",
            manuscript_text=manuscript_text,
            manuscript_path=mp,
            chapter_range=chapter_range,
        )

        async def gen():
            async for ev in rt.run_stream():
                if await request.is_disconnected():
                    logger.info("客户端断开，停止圆桌流")
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/stream")
    async def stream(
        request: Request,
        topic: str = DEFAULT_TOPIC,
        max_rounds: int = 2,
        peers: str = "",
        personas: str = "",
        context: str = "",
        manuscript_path: str = "",
        chapter_range: str = "",
    ):
        """SSE（GET）。manuscript_path: 稿件路径/书名；chapter_range: 如 1-3 指定评审章节。"""
        return _make_stream(request, topic, max_rounds, peers, personas, context, manuscript_path, chapter_range)

    @app.post("/api/stream")
    async def stream_post(request: Request, body: StreamBody):
        """SSE（POST，context 放 body，避免续聊累积超 URL 长度限制）。"""
        return _make_stream(
            request, body.topic, body.max_rounds, body.peers, body.personas,
            body.context, body.manuscript_path, body.chapter_range,
        )

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="创作圆桌 Web UI")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RT_PORT", "9920")))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(create_app(args.port), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
