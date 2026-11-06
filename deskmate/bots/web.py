"""Bot Mode · 独立群聊页（阶段 2 可点可测，不必重打包桌面 App）。

运行：
    python -m deskmate.bots.web --port 9921
浏览器打开 http://localhost:9921

形态对照 Grok Bot / Hermes Bot Mode：bot 常驻在左侧名册，群按话题随建随撤；
开群后直接在群里说人话（如「先读《夜不收》，聊聊烧仓还能怎么写」），bot 自己
决定要不要读稿。**没有任何表单字段、没有开始按钮、没有纪要**。

本服务只读挂载：不改圆桌、不改主编、不改 teams.json；群消息走官方 ChatStore，
群元数据走独立 bots.db，书稿全程只读。
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .reading import default_shelf
from .rooms import RoomStore
from .roster import list_bots

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot 群 · OpenWorker</title>
<style>
  :root { --bg:#f5f4f1; --panel:#fff; --ink:#262521; --sub:#7c786f;
          --line:#e3e0d9; --accent:#4c5b52; --mine:#e8ede8; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.7 -apple-system,"PingFang SC","Helvetica Neue",sans-serif; }
  header { height:52px; padding:0 22px; display:flex; align-items:center;
           gap:14px; border-bottom:1px solid var(--line); background:var(--panel); }
  header b { font-weight:600; letter-spacing:.08em; }
  header span { color:var(--sub); font-size:12px; }
  .wrap { display:grid; grid-template-columns:270px 1fr; height:calc(100vh - 52px); }
  aside { border-right:1px solid var(--line); background:var(--panel);
          padding:18px; overflow:auto; }
  h3 { margin:0 0 10px; font-size:12px; font-weight:600; color:var(--sub);
       letter-spacing:.12em; }
  .roster { margin-bottom:24px; }
  label.bot { display:flex; gap:9px; align-items:center; padding:7px 9px;
              border:1px solid var(--line); border-radius:7px; margin-bottom:6px;
              cursor:pointer; }
  label.bot:hover { border-color:var(--accent); }
  label.bot small { color:var(--sub); margin-left:auto; font-size:11px; }
  input[type=text] { width:100%; padding:9px 11px; border:1px solid var(--line);
                     border-radius:7px; background:#fff; color:var(--ink); font:inherit; }
  button { border:1px solid var(--accent); background:var(--accent); color:#fff;
           padding:8px 13px; border-radius:7px; cursor:pointer; font:inherit; }
  button.ghost { background:transparent; color:var(--accent); }
  button:disabled { opacity:.45; cursor:default; }
  .row { display:flex; gap:8px; margin-top:9px; }
  .room { padding:9px 11px; border:1px solid var(--line); border-radius:7px;
          margin-bottom:6px; cursor:pointer; display:flex; gap:6px; align-items:center; }
  .room.on { border-color:var(--accent); background:var(--mine); }
  .room small { color:var(--sub); margin-left:auto; }
  main { display:grid; grid-template-rows:1fr auto; min-width:0; }
  #log { overflow:auto; padding:22px 26px; }
  .msg { max-width:74%; margin:0 0 15px; padding:10px 14px; border-radius:10px;
         background:var(--panel); border:1px solid var(--line); white-space:pre-wrap; }
  .msg.me { margin-left:auto; background:var(--mine); }
  .msg .who { display:block; font-size:11px; color:var(--sub); margin-bottom:4px; }
  .msg .hint2 { display:block; font-size:12px; color:var(--sub); }
  .msg.typing { opacity:.88; }
  .round-sep { text-align:center; color:var(--sub); font-size:11px; margin:6px 0 14px; }
  .pass-note { color:var(--sub); font-size:12px; margin:0 0 12px; }
  .empty { color:var(--sub); text-align:center; margin-top:80px; }
  .composer { border-top:1px solid var(--line); background:var(--panel);
              padding:14px 22px; display:flex; gap:10px; }
  .composer input { flex:1; }
  .hint { color:var(--sub); font-size:12px; padding:0 22px 10px; background:var(--panel); }
  #err { color:#9b4a42; font-size:12px; min-height:18px; }
</style>
</head>
<body>
<header><b>BOT 群</b><span>bot 常驻 · 群随建随撤 · 说人话即可</span>
  <span id="err" style="margin-left:auto"></span></header>
<div class="wrap">
  <aside>
    <div class="roster">
      <h3>常驻 BOT</h3>
      <div id="bots"></div>
    </div>
    <h3>按话题建群</h3>
    <input id="topic" type="text" placeholder="话题，例如：烧仓怎么往下写">
    <div class="row"><button id="mk">建群</button><button id="del" class="ghost" disabled>撤群</button></div>
    <h3 style="margin-top:24px">我的群</h3>
    <div id="rooms"></div>
  </aside>
  <main>
    <div id="log"><div class="empty">勾选几位 bot，起个话题，就可以开始聊了。</div></div>
    <div>
      <div class="hint">不 @ 则全员回应；@某位只唤醒他。说「先读《夜不收》」，bot 会自己去读。</div>
      <div class="composer">
        <input id="say" type="text" placeholder="在群里说句话…" disabled>
        <button id="send" disabled>发送</button>
      </div>
    </div>
  </main>
</div>
<script>
let current = null, rooms = [], bots = [];
const streams = {};   // turn_id -> 正在生长的气泡
const $ = s => document.querySelector(s);
const err = m => { $('#err').textContent = m || ''; };
function botName(id) {
  const b = bots.find(x => x.id === id);
  return b ? b.name : id;
}
async function api(url, opt) {
  const r = await fetch(url, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}
function esc(s) { return (s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function loadBots() {
  bots = await api('/api/bots');
  $('#bots').innerHTML = bots.map(b =>
    `<label class="bot"><input type="checkbox" value="${esc(b.id)}" checked>` +
    `<span>${esc(b.name)}</span><small>${esc(b.role)}</small></label>`).join('');
}
async function loadRooms() {
  rooms = await api('/api/rooms');
  $('#rooms').innerHTML = rooms.length ? rooms.map(r =>
    `<div class="room ${r.room_id === current ? 'on' : ''}" data-id="${esc(r.room_id)}">` +
    `<span>${esc(r.topic)}</span><small>${r.bot_ids.length} 位</small></div>`).join('')
    : '<div style="color:var(--sub);font-size:12px">还没有群</div>';
  document.querySelectorAll('.room').forEach(el => el.onclick = () => openRoom(el.dataset.id));
  syncButtons();
}
function syncButtons() {
  const on = !!current;
  $('#del').disabled = !on; $('#say').disabled = !on; $('#send').disabled = !on;
}
async function openRoom(id) {
  current = id; await loadRooms(); await loadMessages();
}
async function loadMessages() {
  if (!current) return;
  const ms = await api('/api/rooms/' + current + '/messages');
  $('#log').innerHTML = ms.length ? ms.map(m =>
    `<div class="msg ${m.author_role === 'user' ? 'me' : ''}">` +
    `<span class="who">${esc(m.author_role === 'user' ? m.author : botName(m.author))}${m.author_role === 'user' ? ' · 我' : ' · bot'}</span>` +
    `${esc(m.text)}</div>`).join('')
    : '<div class="empty">群里还没人说话。</div>';
  $('#log').scrollTop = $('#log').scrollHeight;
}
$('#mk').onclick = async () => {
  err('');
  const ids = [...document.querySelectorAll('#bots input:checked')].map(i => i.value);
  const topic = $('#topic').value.trim();
  if (!topic || ids.length < 2) { err('需要一个话题，并至少勾选 2 位 bot'); return; }
  try {
    const r = await api('/api/rooms', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({topic, bot_ids: ids})});
    $('#topic').value = '';
    current = r.room_id; await loadRooms(); await loadMessages();
  } catch (e) { err(e.message); }
};
$('#del').onclick = async () => {
  if (!current) return;
  try { await api('/api/rooms/' + current, {method:'DELETE'}); current = null;
        await loadRooms(); $('#log').innerHTML = '<div class="empty">群已撤，消息留档。</div>';
  } catch (e) { err(e.message); }
};
function startBubble(ev) {
  const row = document.createElement('div');
  row.className = 'msg typing';
  row.innerHTML = `<span class="who">${esc(ev.name)} · bot</span>` +
                  `<span class="hint2">正在思考…</span><span class="body"></span>`;
  $('#log').appendChild(row);
  streams[ev.turn_id] = {
    row, body: row.querySelector('.body'), hint: row.querySelector('.hint2'),
    started: false
  };
  $('#log').scrollTop = $('#log').scrollHeight;
}
function handleStream(ev) {
  const st = ev.turn_id ? streams[ev.turn_id] : null;
  switch (ev.type) {
    case 'round_start':
      if (ev.round > 1) {
        const sep = document.createElement('div');
        sep.className = 'round-sep';
        sep.textContent = '— 第 ' + ev.round + ' 轮接话 —';
        $('#log').appendChild(sep);
      }
      break;
    case 'bot_start': startBubble(ev); break;
    case 'tool':
      if (st && !st.started) st.hint.textContent =
        ev.name === 'read_book' ? '正在读书稿…' : '正在查资料…';
      break;
    case 'delta':
      if (!st) break;
      if (!st.started) { st.started = true; st.hint.textContent = ''; st.body.textContent = ''; }
      st.body.textContent += ev.text;                 // 逐 token 生长
      $('#log').scrollTop = $('#log').scrollHeight;
      break;
    case 'bot_end':
      if (st) {
        st.body.textContent = ev.text;               // 以落库的权威文本覆盖
        st.hint.textContent = ''; st.row.classList.remove('typing');
        delete streams[ev.turn_id];
      }
      break;
    case 'bot_pass':
      if (st) { st.row.remove(); delete streams[ev.turn_id]; }
      if (ev.reason === 'pass') {
        const note = document.createElement('div');
        note.className = 'pass-note';
        note.textContent = ev.name + ' 略过';
        $('#log').appendChild(note);
      }
      break;
    case 'error': err(ev.message || '群聊出错了'); break;
    default: break;                                 // start / final / done
  }
}
async function send() {
  const text = $('#say').value.trim();
  if (!text || !current || $('#send').disabled) return;
  $('#say').value = ''; err(''); $('#send').disabled = true;
  $('#log').insertAdjacentHTML('beforeend',
    `<div class="msg me"><span class="who">我</span>${esc(text)}</div>`);
  $('#log').scrollTop = $('#log').scrollHeight;
  try {
    const resp = await fetch('/api/rooms/' + current + '/stream', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text})
    });
    if (!resp.ok || !resp.body) {
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || ('HTTP ' + resp.status));
    }
    const reader = resp.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream:true});
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue;
          try { handleStream(JSON.parse(line.slice(5).trim())); } catch (_) {}
        }
      }
    }
  } catch (e) { err(e.message); }
  // 清掉残留的打字占位，再用落库消息重绘一次，保证与刷新后完全一致。
  Object.keys(streams).forEach(k => { streams[k].row.remove(); delete streams[k]; });
  await loadMessages();
  $('#send').disabled = false;
}
$('#send').onclick = send;
$('#say').onkeydown = e => { if (e.key === 'Enter') send(); };
loadBots().then(loadRooms).catch(e => err(e.message));
</script>
</body></html>
"""


class NewRoom(BaseModel):
    topic: str
    bot_ids: list[str]


class Say(BaseModel):
    text: str


logger = logging.getLogger(__name__)

# 与 deskmate/roundtable/web.py:485、creation/service.py:713 同构的 SSE 帧格式。
def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(base_dir: Optional[str] = None, runtime: Any = None) -> FastAPI:
    """``runtime`` 可注入伪造的 BotRuntime（离线测试 / 未来桌面端接入用）。

    不传时走 ``rooms._default_runtime()`` 懒加载真 Agent，import 期不构造 provider。
    """
    app = FastAPI(title="Bot 群")
    store = RoomStore(base_dir or os.environ.get("DESKMATE_BOT_BASE"))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "books": len(default_shelf().list_books())}

    @app.get("/api/bots")
    def bots() -> list[dict[str, str]]:
        return [
            {"id": b.id, "name": b.name, "role": b.role} for b in list_bots()
        ]

    @app.get("/api/rooms")
    def room_list() -> list[dict[str, Any]]:
        return [
            {
                "room_id": r.room_id,
                "topic": r.topic,
                "bot_ids": r.bot_ids,
                "bot_names": r.bot_names,
                "created_at": r.created_at,
            }
            for r in store.list_rooms()
        ]

    @app.post("/api/rooms")
    def room_create(body: NewRoom) -> dict[str, Any]:
        try:
            r = store.create_room(body.topic, body.bot_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "room_id": r.room_id, "topic": r.topic,
            "bot_ids": r.bot_ids, "bot_names": r.bot_names,
            "created_at": r.created_at,
        }

    @app.delete("/api/rooms/{room_id}")
    def room_disband(room_id: str) -> dict[str, Any]:
        try:
            return store.disband_room(room_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/rooms/{room_id}/messages")
    def room_messages(room_id: str) -> list[dict[str, Any]]:
        if store.get_room(room_id) is None:
            raise HTTPException(status_code=404, detail="群不存在或已撤")
        return store.chat_store.messages(room_id)

    @app.post("/api/rooms/{room_id}/say")
    async def room_say(room_id: str, body: Say) -> dict[str, Any]:
        """旧契约：一次性 JSON。保留给现有调用方与回归测试，前端已改用 /stream。"""
        try:
            return await run_in_threadpool(
                functools.partial(store.say, room_id, body.text, runtime=runtime)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/rooms/{room_id}/stream")
    async def room_stream(room_id: str, body: Say, request: Request) -> StreamingResponse:
        """原生流式群聊：TurnEngine 的 ASSISTANT_DELTA 经 SSE 逐 token 推给前端。

        不是新建一套 LLM 流 —— 同一 engine / 同一 prompt / 同一份常驻记忆，只是
        不再丢弃事件。首个事件先发 ``start``，让响应头立刻落地（SSE 经反代时
        首个 yield 才真正下发，见 creation/service.py:728-730 的事故记录）。
        """
        if store.get_room(room_id) is None:
            raise HTTPException(status_code=404, detail="群不存在或已撤")
        if not (body.text or "").strip():
            raise HTTPException(status_code=400, detail="群里不能发空话")

        async def gen():
            try:
                # 不额外发 start：say_stream 的第一个事件就是 start，避免重复。
                # StreamingResponse 在进入迭代前已发响应头；X-Accel-Buffering: no
                # 负责挡住反代缓冲，无需靠一个假事件把头「顶」出去。
                # aclosing：客户端断开时 GeneratorExit 级联到 rooms.say_stream →
                # runtime.speak_stream → engine.request_interrupt()，停止烧 token。
                async with contextlib.aclosing(
                    store.say_stream(room_id, body.text, runtime=runtime)
                ) as events:
                    async for ev in events:
                        if await request.is_disconnected():
                            logger.info("bot 群客户端断开，停止流式发言 room=%s", room_id)
                            break
                        yield _sse(ev)
            except ValueError as exc:
                yield _sse({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 —— 不让异常把 SSE 连接打成半截
                logger.warning("bot 群流式发言异常 room=%s: %s", room_id, exc)
                yield _sse({"type": "error", "message": "群聊服务暂时不可用"})

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # 反代不缓冲，否则逐字变一次性
                "Connection": "keep-alive",
            },
        )

    return app


# 故意不留模块级 ``app``：RoomStore 会打开 sqlite / 建表，属于 import 期副作用
# （红线：DESKMATE_DIR 须调用时解析）。外部要挂进别的服务请用 ``create_app()``；
# uvicorn 工厂模式：uvicorn deskmate.bots.web:create_app --factory


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot 群独立服务")
    parser.add_argument("--port", type=int, default=9921)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-dir", default=None, help="群库目录（默认 ~/.config/coworker）")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(args.base_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
