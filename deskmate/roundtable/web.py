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
import asyncio
import json
import logging
import os
from pathlib import Path
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


class ReadBody(BaseModel):
    """POST /api/read 的请求体：触发「阅读全书」（生成圣经 + FTS5 索引）。"""

    path: str = ""
    chapter_range: str = ""


def _read_html() -> str:
    try:
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>roundtable.html 模板缺失</h1>"


def _run_read_book(mp: str, emit) -> None:
    """阻塞执行：阅读全书（带 up-to-date 跳过）+ 建 FTS5 索引。

    emit(dict) 推进度事件，由 SSE 端点流式传给前端。
    """
    from deskmate.novel_memory import core as nb
    import time

    # up-to-date 判断：若四份圣经已存在且新于源文件，直接跳过（避免每改几章就全量重读）
    try:
        bible_dir = nb._bible_root(mp) / nb._BIBLE_DIR
        bible_files = [bible_dir / fn for fn in nb._BIBLE_FILES.values()]
        if all(p.is_file() for p in bible_files):
            bible_mtime = max(p.stat().st_mtime for p in bible_files)
            scan_root, _ = nb._resolve_root(mp)
            src_mtime = 0.0
            for sp in nb._iter_text_files(scan_root):
                try:
                    src_mtime = max(src_mtime, sp.stat().st_mtime)
                except OSError:
                    pass
            if bible_mtime >= src_mtime - 1.0:
                emit({"type": "status", "stage": "skip",
                      "message": "✅ 该书稿已读且为最新，无需重读。"})
                emit({"type": "done", "ok": True})
                return
    except Exception as exc:  # noqa: BLE001
        logger.warning("阅读全书 up-to-date 检查失败（将重新生成）: %s", exc)

    def cb(stage, data):
        if stage == "extract_progress":
            emit({"type": "progress", "stage": "extract",
                  "idx": data.get("idx"), "total": data.get("total")})
        elif stage == "scanning":
            emit({"type": "status", "stage": "scanning", "message": "🔍 扫描全书文本…"})
        elif stage == "extracting":
            emit({"type": "status", "stage": "extracting",
                  "message": f"📝 逐段抽取摘要（共 {data.get('total')} 段）…"})
        elif stage == "merging":
            emit({"type": "status", "stage": "merging",
                  "message": "🧩 综合全书，生成人物 / 情节 / 时间线 / 伏笔…"})
        elif stage == "completed":
            emit({"type": "status", "stage": "merged",
                  "message": f"📚 圣经生成完成：{', '.join(data.get('docs', []))}"})

    emit({"type": "status", "stage": "start", "message": "📖 开始阅读全书…"})
    docs = nb.build_bible(mp, progress_callback=cb)
    # 落盘四份圣经（否则圆桌 read_bible 读不到宏观摘要）+ upsert 进 workspace 记忆
    try:
        nb.inject_bible(mp, docs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("圣经落盘失败: %s", exc)
        emit({"type": "status", "stage": "inject_error",
              "message": f"⚠️ 圣经生成成功但落盘失败（圆桌读不到摘要）：{exc}"})
    try:
        n = nb.build_fts5_index(mp)
        emit({"type": "status", "stage": "fts5",
              "message": f"🔎 全文检索索引已建立（{n} 个文件）。"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("FTS5 索引失败: %s", exc)
        emit({"type": "status", "stage": "fts5_error",
              "message": f"⚠️ 索引建立失败（不影响宏观摘要）：{exc}"})
    emit({"type": "done", "ok": True})


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

    @app.post("/api/read")
    async def read_book(request: Request, body: ReadBody):
        """SSE：阅读全书（生成圣经 + 建立 FTS5 索引），带实时进度。

        请求体 {"path": "<稿件路径>", "chapter_range": "可选"}。
        进度事件：{"type":"progress"|"status"|"done"|"error", ...}
        """
        mp = (body.path or "").strip()
        if not mp:
            return JSONResponse({"error": "missing manuscript path"}, status_code=400)
        if not Path(mp).expanduser().exists():
            return JSONResponse({"error": f"路径不存在: {mp}"}, status_code=400)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def emit(d: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, d)

        async def gen():
            worker = asyncio.ensure_future(
                asyncio.to_thread(_run_read_book, mp, emit)
            )
            while True:
                try:
                    d = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if worker.done():
                        break
                    continue
                yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
                if d.get("type") in ("done", "error"):
                    break
            # 排空残留事件
            while not queue.empty():
                try:
                    d = queue.get_nowait()
                    yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
                except Exception:  # noqa: BLE001
                    break
            try:
                await worker
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
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
