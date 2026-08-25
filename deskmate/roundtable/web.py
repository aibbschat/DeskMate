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
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from deskmate.roundtable.coordinator import PeerSpec, Roundtable
from deskmate.roundtable.personas import load_personas

from coworker.conversations import ConversationStore
from coworker.sessions import SessionRecord

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = (
    "一段明代背景小说开篇：主角以锦衣卫身份出入诏狱，借『东厂』之名缉拿官绅，"
    "文中写『万历年间，东厂提督冯保秉笔直入内廷』。请各位从各自角度评审这一段。"
)

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "roundtable.html")


def _resolve_store(base_dir: Optional[str]) -> Optional["ConversationStore"]:
    """定位主 app 会话库。base_dir 由主 app 挂载时传入（manager.session_store.base），
    保证读写与主 app 同一份 coworker.db；独立运行时回退 COWORKER_DATA_DIR 或默认 ~/.config/coworker。"""
    base = (
        base_dir
        or os.environ.get("COWORKER_DATA_DIR")
        or os.path.join(os.path.expanduser("~"), ".config", "coworker")
    )
    try:
        return ConversationStore(base)
    except Exception as exc:  # noqa: BLE001
        logger.warning("圆桌会话库初始化失败（纪要不落盘）：%s", exc)
        return None


def _save_roundtable_session(store, workspace, topic, markdown, traces) -> Optional[str]:
    """把一次圆桌的完整纪要写入主 app 会话库，使其出现在左侧历史对话栏。

    落一条 session：user=议题配置，assistant=RoundtableResult.to_markdown() 完整纪要
    （含各轮各人格/peer 发言 + 检索轨迹）。origin=roundtable 便于历史栏识别与将来回链还原。
    失败静默（不影响圆桌本身）。
    """
    if store is None or not markdown:
        return None
    import uuid

    sid = "rt-" + uuid.uuid4().hex[:12]
    messages = [
        {"role": "user", "content": f"【创作圆桌议题】\n{topic}"},
        {"role": "assistant", "content": markdown},
    ]
    title = "🪑 圆桌：" + ((topic or "").strip()[:40] or "未命名议题")
    rec = SessionRecord(
        session_id=sid,
        workspace=workspace or "",
        model="deskmate-roundtable",
        mode="interactive",
        messages=messages,
        title=title,
        agent="deskmate-roundtable",
        origin="roundtable",
        origin_label="创作圆桌",
    )
    try:
        store.save(rec)
        return sid
    except Exception as exc:  # noqa: BLE001
        logger.warning("圆桌纪要落盘失败（不影响圆桌）：%s", exc)
        return None


# 神圣稿件硬隔离：圆桌报告回流圣经时同样不得触碰（与 after_action._safe_book 同红线）
_SACRED_MARKERS = ("国子监秘闻录",)


def _upsert_report_to_bible(mp: str, synthesis_md: str) -> None:
    """圆桌收尾后，把《综合研判报告》回流进稿件圣经（复用 A 的内容侧闭环 upsert_bible_from_text）。

    仅在稿件已建圣经时生效（upsert_bible_from_text 自带 has_data 守门，绝不会擅自全量建圣经）；
    神圣稿件被硬隔离；任何异常仅记日志，绝不阻断圆桌本身。本函数在后台守护线程调用。
    """
    if not mp or not synthesis_md or not synthesis_md.strip():
        return
    if any(m in mp for m in _SACRED_MARKERS):
        logger.warning("圆桌报告回流跳过神圣稿件（硬隔离）：%s", mp)
        return
    try:
        from deskmate.novel_memory import core as nb

        docs = nb.upsert_bible_from_text(mp, synthesis_md, source_label="圆桌综合研判报告")
        if docs:
            logger.info("圆桌《综合研判报告》已回流圣经：%s", mp)
        else:
            logger.info("圆桌报告未回流（该书尚无关联网经/无新增事实）：%s", mp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("圆桌报告回流圣经失败（不影响圆桌）：%s", exc)


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


class TrendSaveBody(BaseModel):
    """POST /api/trends 与 /api/trends/format 的请求体：沉淀 / 整理潮流简报。"""

    markdown: str = ""
    genre: str = ""
    title: str = ""
    llm_format: bool = False


class TrendActiveBody(BaseModel):
    """POST /api/trends/active 的请求体：切换当前生效简报。"""

    id: str = ""


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
        bible_files = nb.bible_file_paths(mp)
        if all(p.is_file() for p in bible_files):
            bible_mtime = max(p.stat().st_mtime for p in bible_files)
            scan_root, _ = nb.resolve_root(mp)
            src_mtime = 0.0
            for sp in nb.iter_text_files(scan_root):
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


def create_app(rt_port: int = 9920, base_dir: Optional[str] = None, workspace: Optional[str] = None) -> FastAPI:
    # 挂入主 app 时 rt_port 默认 9920，但实际端口与挂载前缀均由 COWORKER_PORT 语境决定：
    # - 主 app（run.py 起 sidecar）会设置 COWORKER_PORT，并把本 app 整块挂到 /roundtable 下，
    #   此时 opencode-a2a 的完整路径是 /roundtable/opencode-a2a，Agent Card 的
    #   supportedInterfaces 必须填真实可连地址，否则 A2A 客户端按 card 里的地址去连会撞上空端口。
    # - 独立运行（python -m deskmate.roundtable.web）时 COWORKER_PORT 未设置，挂在根 /，
    #   opencode-a2a 就在 /opencode-a2a，地址不带 /roundtable 前缀。
    # - host 固定用 127.0.0.1：sidecar 只绑定 IPv4 回环，advertise localhost 会被解析成 ::1
    #   导致 A2A 客户端（IPv6 优先）连不上。
    actual_port = int(os.environ.get("COWORKER_PORT") or rt_port)
    mount = "/roundtable" if os.environ.get("COWORKER_PORT") else ""
    opencode_peer_url = f"http://127.0.0.1:{actual_port}{mount}/opencode-a2a"
    app = FastAPI(title="创作圆桌")

    # 复用主 app 会话库：圆桌结束后把纪要落盘，使其进入左侧历史对话栏。
    _store = _resolve_store(base_dir)
    _rt_workspace = workspace

    # 把 opencode A2A 适配层挂到 /opencode-a2a：单进程即可让 OpenCode 成为 peer。
    try:
        from coworker.server.opencode_a2a_server import build_app as build_opencode_app

        oc_app = build_opencode_app(opencode_peer_url)
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

    @app.get("/api/personas")
    async def api_personas() -> JSONResponse:
        # 圆桌人格来自官方 persona registry（与普通聊天选择器共用同一份），动态返回。
        return JSONResponse(
            [
                {"id": p.id, "name": p.name, "role": p.role, "market": p.market}
                for p in load_personas()
            ]
        )

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
                    # 支持章节范围选择（如 "1-3" 或 "2"）；未指定时默认仅评第 1 章，
                    # 避免静默截断到前 5 章却让用户误以为在评其指定的章节。
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
                            selected = chapters[:1]
                    else:
                        selected = chapters[:1]
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
            else:
                name, url = part, part
            name, url = name.strip(), url.strip()
            # OpenCode 适配层只能消费传入的 prompt 文本、无法读取本地文件，
            # 必须传完整正文（见 coordinator._peer_speak）。
            can_read = "opencode" not in name.lower() and "opencode" not in url.lower()
            peer_list.append(PeerSpec(name, url, can_read_files=can_read))

        selected_personas = None
        ids = [p.strip() for p in (personas or "").split(",") if p.strip()]
        if ids:
            by_id = {p.id: p for p in load_personas()}
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

        # 勋章统计：记一次圆桌召集。_make_stream 是同步函数，这里直接同步写盘
        # （一次小 JSON 落盘，与上面同步读取文稿同量级），失败不影响议事。
        try:
            from deskmate.growth.engine import GrowthEngine as _GEng

            _GEng().record_roundtable()
        except Exception as e:
            logger.debug("record_roundtable 失败（不影响圆桌）: %s", e)

        _topic = topic
        _markdown = None
        _traces = None
        _synthesis = None

        async def gen():
            nonlocal _topic, _markdown, _traces, _synthesis
            async for ev in rt.run_stream():
                if await request.is_disconnected():
                    logger.info("客户端断开，停止圆桌流")
                    break
                et = ev.get("type")
                if et == "meta":
                    _topic = ev.get("topic") or _topic
                elif et == "done":
                    _markdown = ev.get("markdown")
                    _traces = ev.get("traces")
                    _synthesis = ev.get("synthesis")
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            # 流正常结束（收到 done）：把圆桌纪要落盘进主 app 会话库。
            # 中途断开（未收到 done）则不落盘——未完成的圆桌不入库。
            if _markdown:
                sid = await asyncio.to_thread(
                    _save_roundtable_session, _store, _rt_workspace, _topic, _markdown, _traces
                )
                if sid:
                    logger.info("圆桌纪要已存入历史对话栏 session=%s", sid)
            # B：圆桌《综合研判报告》回流进稿件圣经（A 的内容侧闭环）。
            # 后台守护线程跑，避免真实 LLM 抽取拖住 SSE 关闭；失败仅记日志。
            if mp and _synthesis:
                try:
                    threading.Thread(
                        target=_upsert_report_to_bible, args=(mp, _synthesis), daemon=True
                    ).start()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("启动圆桌报告回流线程失败：%s", exc)

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

    @app.get("/api/trends")
    async def list_trends():
        """列出各期潮流简报 + 当前生效 id。"""
        from deskmate.roundtable import trends as _trends

        briefs = _trends.list_briefs()
        latest = _trends.get_latest()
        return JSONResponse({"briefs": briefs, "latest": latest.get("id") if latest else None})

    @app.get("/api/trends/{bid}")
    async def get_trend(bid: str):
        """查看某一期潮流简报（含正文）。"""
        from deskmate.roundtable import trends as _trends

        b = _trends.get_brief(bid)
        if not b:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(b)

    @app.post("/api/trends")
    async def save_trend(body: TrendSaveBody):
        """沉淀一期潮流简报（可选让 LLM 先结构化）。"""
        from deskmate.roundtable import trends as _trends

        md = (body.markdown or "").strip()
        if not md:
            return JSONResponse({"error": "empty markdown"}, status_code=400)
        if body.llm_format:
            md = _trends.build_via_llm(md, body.genre)
        b = _trends.save_brief(md, genre=body.genre, title=body.title)
        return JSONResponse(b)

    @app.post("/api/trends/active")
    async def set_trend_active(body: TrendActiveBody):
        """切换当前生效的潮流简报。"""
        from deskmate.roundtable import trends as _trends

        if not body.id:
            return JSONResponse({"error": "missing id"}, status_code=400)
        if not _trends.set_active(body.id):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True, "active": body.id})

    @app.post("/api/trends/format")
    async def format_trend(body: TrendSaveBody):
        """把零散观察交给 LLM 整理成标准格式（不落盘，仅返回结构化文本）。"""
        from deskmate.roundtable import trends as _trends

        md = (body.markdown or "").strip()
        if not md:
            return JSONResponse({"error": "empty markdown"}, status_code=400)
        out = _trends.build_via_llm(md, body.genre)
        return JSONResponse({"markdown": out})

    @app.put("/api/trends/{bid}")
    async def update_trend(bid: str, body: TrendSaveBody):
        """覆盖更新某一期潮流简报（保留 id/日期，重写正文与 meta）。"""
        from deskmate.roundtable import trends as _trends

        md = (body.markdown or "").strip()
        if not md:
            return JSONResponse({"error": "empty markdown"}, status_code=400)
        if body.llm_format:
            md = _trends.build_via_llm(md, body.genre)
        b = _trends.update_brief(bid, md, genre=body.genre, title=body.title)
        if not b:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(b)

    @app.delete("/api/trends/{bid}")
    async def delete_trend(bid: str):
        """删除某一期潮流简报（若删的是当前生效，自动改指剩余最新一期）。"""
        from deskmate.roundtable import trends as _trends

        if not _trends.delete_brief(bid):
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"ok": True})

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
