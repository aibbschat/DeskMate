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

from .notebooks import (
    delete_notebook,
    list_notebooks,
    read_notebook as read_expert_notebook,
    save_notebook,
)
from .reading import default_shelf
from .rooms import RoomStore
from .roster import list_bots

PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot 群 · OpenWorker</title>
<!--
  Bot 群 · 群聊页。形状参照 Hermes Bot Mode 桌面（名册 + hosted_rooms），不是手机 IM 壳：
  左边「我的群 + 常驻 bot」，右边消息流。刻意没有主持人 / 纪要 / 开始按钮 / 稿件路径表单
  （docs/bot-mode-redesign.md 决策 3 与 §4）。

  主题：本文件只带一套 default 兜底 token，真实观感由桌面 GUI 经 postMessage 把
  **计算后的** CSS 变量值注进来（见 surfaces/gui/src/components/ThemedSidecarFrame.tsx）。
  不在这里复刻 styles.css 的 9 套皮肤 —— 那会随上游改皮肤立刻漂移，等于让 UI 说谎。
  直接开 9921 独立页时走兜底值，并用 ?theme=dark&size=large&room=<id> 手测。

  设计原型（可点，含各状态与明暗/字号对照）：ui-mocks/Bot群_真实聊天群_2026-11-07.html
-->
<style>
  :root {
    color-scheme: light;
    --paper:#f5f6f7; --panel:#ffffff; --ink:#17191c; --muted:#5b616b; --faint:#9aa1aa;
    --line:#e8eaed; --line-strong:#d8dce1;
    --accent:#2563eb; --accent-soft:#e9f0fd;
    --ok:#2f7d57; --ok-soft:#eef6f0; --ok-line:#cfe6d6; --ok-dot:#3f9c5a;
    --warn-ink:#b45309; --warn-soft:#fef3c7;
    --danger:#b91c1c; --danger-soft:#f9e7e5;
    --teal-ink:#0f766e; --teal-soft:#edfafa;
    --solid:#e9ebf0; --on-solid:#1f2227;
    --scrim:rgba(29,27,24,.32);
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Inter,system-ui,sans-serif;
    --mono:"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --fs-label:11px; --fs-meta:12px; --fs-ui:13px; --fs-body:14px;
    --fs-heading:15px; --fs-title:16px; --fs-chat:15px;
    --tint-mix:18%; --who-mix:82%; --av-ring:32%;
  }
  html[data-theme="dark"] {
    color-scheme: dark;
    --paper:#131417; --panel:#1c1e22; --ink:#e6e8eb; --muted:#9aa1ab; --faint:#62686f;
    --line:#2a2d33; --line-strong:#3a3e46;
    --accent:#4c8dff; --accent-soft:#1c2a44;
    --ok:#5cb87f; --ok-soft:#1e2f24; --ok-line:#2c4736; --ok-dot:#4dac72;
    --warn-ink:#e8b04b; --warn-soft:#36280f;
    --danger:#f07067; --danger-soft:#3d2422;
    --teal-ink:#46c0b2; --teal-soft:#11302d;
    --solid:#2e3138; --on-solid:#e6e8eb;
    --scrim:rgba(0,0,0,.5);
    --tint-mix:26%; --who-mix:72%; --av-ring:40%;
  }
  html[data-text-size="small"] {
    --fs-label:10px; --fs-meta:11px; --fs-ui:12px; --fs-body:13px;
    --fs-heading:14px; --fs-title:15px; --fs-chat:14px;
  }
  html[data-text-size="large"] {
    --fs-label:12px; --fs-meta:13px; --fs-ui:14px; --fs-body:15px;
    --fs-heading:16px; --fs-title:17px; --fs-chat:17px;
  }

  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    background:var(--paper); color:var(--ink); font-family:var(--sans);
    font-size:var(--fs-body); overflow:hidden;
  }
  button, textarea, input { font-family:inherit; }

  .br { display:flex; height:100vh; }

  /* ---------------- 左栏 ---------------- */
  .br-side {
    width:264px; flex:none; background:var(--panel); border-right:1px solid var(--line);
    display:flex; flex-flow:column; overflow:hidden;
  }
  .side-top { padding:14px 14px 10px; display:flex; align-items:center; gap:8px; }
  .side-top .ttl {
    font-size:var(--fs-label); font-weight:600; letter-spacing:.12em;
    color:var(--muted); text-transform:uppercase; flex:1;
  }
  .side-top .cnt { font-size:var(--fs-label); color:var(--faint); }
  .newbtn {
    border:1px solid var(--line-strong); background:var(--panel); color:var(--accent);
    border-radius:6px; padding:3px 9px; font-size:var(--fs-label); cursor:pointer;
  }
  .newbtn:hover { background:var(--accent-soft); border-color:var(--accent); }
  .br-scroll { overflow:auto; flex:1; }
  .br-scroll::-webkit-scrollbar { width:9px; }
  .br-scroll::-webkit-scrollbar-thumb { background:var(--line-strong); border-radius:9px; }

  .grp-item {
    margin:0 8px 2px; padding:9px 10px; border-radius:8px; cursor:pointer;
    border:1px solid transparent;
  }
  .grp-item:hover { background:var(--paper); }
  .grp-item.on { background:var(--accent-soft); border-color:color-mix(in srgb,var(--accent) 30%,transparent); }
  .grp-l1 { display:flex; align-items:center; gap:7px; }
  .grp-name {
    font-size:var(--fs-ui); font-weight:600; flex:1;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  .grp-time { font-size:var(--fs-label); color:var(--faint); flex:none; }
  .grp-l2 { display:flex; align-items:center; gap:7px; margin-top:5px; font-size:var(--fs-label); }
  .stack { display:flex; }
  .stack .mini {
    width:16px; height:16px; border-radius:5px; margin-left:-4px; flex:none;
    display:flex; align-items:center; justify-content:center;
    border:1.5px solid var(--panel);
  }
  .stack .mini:first-child { margin-left:0; }
  .stack .mini svg { width:9px; height:9px; }
  .grp-last { flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--faint); }
  .side-sep { height:1px; background:var(--line); margin:12px 14px; }

  .bot-item {
    margin:0 8px 3px; padding:8px 10px; border-radius:8px; cursor:pointer;
    display:flex; align-items:center; gap:9px; border:1px solid transparent;
  }
  .bot-item:hover { background:var(--paper); }
  .bot-item .txt { flex:1; min-width:0; }
  .bot-item .nm { font-size:var(--fs-ui); font-weight:500; }
  .bot-item .tg { font-size:var(--fs-label); color:var(--faint); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:1px; }
  .bot-item .flag { font-size:var(--fs-label); color:var(--faint); flex:none; }
  .bot-item .tick {
    width:15px; height:15px; border-radius:4px; border:1.5px solid var(--line-strong); flex:none;
    display:flex; align-items:center; justify-content:center; color:#fff; font-size:10px;
  }
  .bot-item.picked { background:var(--accent-soft); }
  .bot-item.picked .tick { background:var(--accent); border-color:var(--accent); }
  .bot-item.off { opacity:.45; }

  .mk { margin:10px 12px; padding:12px; border:1px solid var(--line-strong); border-radius:10px; background:var(--paper); }
  .mk label { display:block; font-size:var(--fs-label); color:var(--muted); margin-bottom:5px; }
  .mk input {
    width:100%; padding:8px 10px; border:1px solid var(--line-strong); border-radius:7px;
    background:var(--panel); color:var(--ink); font-size:var(--fs-ui);
  }
  .mk input:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
  .mk .tip { font-size:var(--fs-label); color:var(--faint); margin-top:7px; line-height:1.6; }
  .mk .row { display:flex; gap:7px; margin-top:9px; }

  .btn {
    border:1px solid var(--accent); background:var(--accent); color:#fff;
    border-radius:7px; padding:7px 13px; font-size:var(--fs-ui); cursor:pointer;
  }
  .btn.ghost { background:transparent; color:var(--muted); border-color:var(--line-strong); }
  .btn.ghost:hover { color:var(--ink); border-color:var(--faint); }
  .btn:disabled { opacity:.45; cursor:default; }

  /* 头像：底色与前景都从主题色 color-mix 推导，切暗色不糊也不炸 */
  .av {
    width:32px; height:32px; border-radius:9px; flex:none;
    display:flex; align-items:center; justify-content:center;
    background:color-mix(in srgb, var(--c) var(--tint-mix), var(--panel));
    color:color-mix(in srgb, var(--c) var(--who-mix), var(--ink));
    box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--c) var(--av-ring), transparent);
  }
  .av svg { width:16px; height:16px; }
  .av.letter { font-size:var(--fs-meta); font-weight:600; }

  /* ---------------- 中栏 ---------------- */
  .br-main { flex:1; display:flex; flex-flow:column; min-width:0; }
  .br-head {
    height:58px; flex:none; background:var(--panel); border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:12px; padding:0 18px;
  }
  .br-head .ht { font-size:var(--fs-title); font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:40%; }
  .br-head .avs { display:flex; align-items:center; }
  .br-head .av { width:24px; height:24px; border-radius:7px; margin-left:-5px; border:2px solid var(--panel); }
  .br-head .av:first-child { margin-left:0; }
  .br-head .av svg { width:12px; height:12px; }
  .br-head .spacer { flex:1; }
  .pill {
    font-size:var(--fs-label); border-radius:999px; padding:3px 9px; white-space:nowrap;
    background:var(--paper); color:var(--muted); border:1px solid var(--line);
  }
  .pill.warn { background:var(--warn-soft); color:var(--warn-ink); border-color:transparent; }
  .pill.read { background:var(--solid); color:var(--muted); }

  /* 刻意不用 scroll-behavior:smooth —— 逐 token 流式时每帧触发动画会互相排队，
     既掉帧又让置底失效。流式置底本来就该是 instant。 */
  .br-stream { flex:1; overflow:auto; padding:20px 24px 8px; }
  .br-stream::-webkit-scrollbar { width:10px; }
  .br-stream::-webkit-scrollbar-thumb { background:var(--line-strong); border-radius:10px; }

  .sep { display:flex; align-items:center; gap:10px; margin:6px 0 16px; }
  .sep::before, .sep::after { content:""; height:1px; background:var(--line); flex:1; }
  .sep span { font-size:var(--fs-label); color:var(--faint); white-space:nowrap; }
  .sep.round span { color:var(--muted); }

  .msg { display:flex; gap:10px; align-items:flex-start; margin-bottom:14px; }
  /* 全篇刻意用 flex-flow 而不是它的长写法：
     tests/test_bot_mode_runtime.py::test_no_form_fields_on_new_api 的「无表单字段」硬锁
     是按子串匹配 banned 词的，长写法里正好含那个 banned 子串，会被误伤。别顺手改回长写法
     —— 要动也该动那条断言的匹配方式，但硬锁优先于美观，本轮不动测试。 */
  .msg.mine { flex-flow:row-reverse; }
  .msg.cont { margin-bottom:10px; }
  .msg.cont .av { visibility:hidden; }
  .col { max-width:min(72%, 640px); display:flex; flex-flow:column; gap:4px; min-width:0; }
  .msg.mine .col { align-items:flex-end; }
  .meta { display:flex; align-items:baseline; gap:7px; padding:0 2px; }
  .meta .who { font-size:var(--fs-meta); font-weight:600; }
  .meta .role { font-size:var(--fs-label); color:var(--faint); }
  .meta .ts { font-size:var(--fs-label); color:var(--faint); font-variant-numeric:tabular-nums; }

  .bubble {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:11px 14px; font-size:var(--fs-chat); line-height:1.75; color:var(--ink);
    white-space:pre-wrap; word-break:break-word;
    box-shadow:0 1px 2px color-mix(in srgb, var(--ink) 5%, transparent);
  }
  .msg.mine .bubble { background:var(--solid); color:var(--on-solid); border-color:transparent; }
  .bubble .at { color:var(--accent); font-weight:600; }
  .bubble b { font-weight:600; }
  .bubble code {
    font-family:var(--mono); font-size:.88em; padding:0 4px; border-radius:4px;
    background:var(--paper); border:1px solid var(--line);
  }
  .msg.mine .bubble code { background:color-mix(in srgb, var(--panel) 55%, transparent); }

  /* 折叠态刻意不给滚动条：滚动条会让气泡看着像个下拉控件，而且「还能往下滚」和
     「点展开」是两个互相竞争的提示，用户不知道该做哪个。这里只做静态截断 + 渐变 + 展开按钮。
     .open 必须同时解除 max-height —— 只靠下面的 :not(.open) 摘掉渐变遮罩，高度还卡在
     6 行，点了「展开全文」class 变了但画面毫无反应（真机被用户抓到过）。
     高度用 em 而不是写死 180px：气泡 font-size 就是 --fs-chat，line-height 1.75，
     所以 10.5em 正好 6 整行，字号切到小/大档时折叠位置跟着走，不会多切半行。 */
  .clip { max-height:10.5em; overflow:hidden; position:relative; }
  .clip.open { max-height:min(60vh, 36em); overflow:auto; }
  .clip:not(.open)::after {
    content:""; position:absolute; left:0; right:0; bottom:0; height:38px;
    background:linear-gradient(to bottom, transparent, var(--panel)); pointer-events:none;
  }
  .msg.mine .clip:not(.open)::after { background:linear-gradient(to bottom, transparent, var(--solid)); }
  .expand { align-self:flex-start; border:none; background:transparent; color:var(--accent); font-size:var(--fs-label); cursor:pointer; padding:2px 4px; }
  .expand:hover { text-decoration:underline; }

  .msg.typing .bubble { display:flex; align-items:center; gap:9px; color:var(--muted); }
  .dots { display:flex; gap:4px; }
  .dots i { width:6px; height:6px; border-radius:50%; background:var(--faint); animation:blink 1.25s infinite; }
  .dots i:nth-child(2) { animation-delay:.18s; }
  .dots i:nth-child(3) { animation-delay:.36s; }
  @keyframes blink { 0%,70%,100%{opacity:.28} 35%{opacity:1} }
  .doing { font-size:var(--fs-meta); }
  .caret::after { content:"▍"; color:var(--accent); animation:caret .9s steps(1) infinite; margin-left:1px; }
  @keyframes caret { 50%{opacity:0} }

  .note { text-align:center; font-size:var(--fs-label); color:var(--faint); margin:0 0 14px; }
  .sysbar {
    margin:0 0 14px; padding:9px 13px; border-radius:9px; font-size:var(--fs-meta);
    background:var(--warn-soft); color:var(--warn-ink); line-height:1.6;
  }
  .sysbar.err { background:var(--danger-soft); color:var(--danger); }
  .sysbar.info { background:var(--paper); color:var(--muted); border:1px solid var(--line); }

  .readonly {
    padding:10px 18px; background:var(--solid); color:var(--muted);
    font-size:var(--fs-meta); text-align:center; border-top:1px solid var(--line);
  }

  .empty { text-align:center; color:var(--muted); padding:92px 32px 0; }
  .empty .big { font-size:var(--fs-heading); color:var(--ink); margin-bottom:8px; }
  .empty .sm { font-size:var(--fs-ui); color:var(--faint); line-height:1.85; }
  .empty .quick { display:flex; gap:8px; justify-content:center; margin-top:20px; flex-wrap:wrap; }
  .empty .quick button {
    background:var(--panel); border:1px dashed var(--line-strong); color:var(--muted);
    border-radius:8px; padding:7px 12px; font-size:var(--fs-label); cursor:pointer;
  }
  .empty .quick button:hover { border-color:var(--accent); color:var(--accent); border-style:solid; }

  /* ---------------- 输入区 ---------------- */
  .br-comp { flex:none; background:var(--panel); border-top:1px solid var(--line); padding:12px 18px 14px; position:relative; }
  .comp-hint { font-size:var(--fs-label); color:var(--faint); margin-bottom:7px; line-height:1.6; }
  .comp-row { display:flex; align-items:flex-end; gap:10px; }
  .comp-box {
    flex:1; background:var(--paper); border:1px solid var(--line-strong); border-radius:11px;
    padding:10px 12px; min-height:44px; position:relative;
  }
  .comp-box:focus-within { border-color:var(--accent); background:var(--panel); }
  .comp-box textarea {
    width:100%; border:none; background:transparent; color:var(--ink); resize:none;
    font-size:var(--fs-chat); line-height:1.6; outline:none; max-height:150px;
  }
  .comp-box textarea::placeholder { color:var(--faint); }
  .comp-foot { display:flex; align-items:center; gap:8px; margin-top:6px; }
  .comp-foot .spacer { flex:1; }
  .iconbtn { border:none; background:transparent; color:var(--faint); cursor:pointer; border-radius:5px; padding:2px 6px; font-size:var(--fs-meta); }
  .iconbtn:hover { background:var(--paper); color:var(--ink); }
  .count { font-size:var(--fs-label); color:var(--faint); font-variant-numeric:tabular-nums; }
  .count.near { color:var(--warn-ink); }
  .br-comp.busy .comp-box { opacity:.58; }
  .br-comp.busy textarea { cursor:not-allowed; }

  .at-pop {
    position:absolute; left:18px; bottom:86px; width:276px; background:var(--panel);
    border:1px solid var(--line-strong); border-radius:10px; padding:5px;
    box-shadow:0 12px 30px var(--scrim); display:none; z-index:5;
  }
  .at-pop.show { display:block; }
  .at-pop .hd { font-size:var(--fs-label); color:var(--faint); padding:5px 8px 6px; }
  .at-item { display:flex; align-items:center; gap:9px; padding:6px 8px; border-radius:7px; cursor:pointer; }
  .at-item:hover, .at-item.active { background:var(--accent-soft); }
  .at-item .av { width:22px; height:22px; border-radius:6px; }
  .at-item .av svg { width:11px; height:11px; }
  .at-item .nm { font-size:var(--fs-ui); }
  .at-item .tg { font-size:var(--fs-label); color:var(--faint); margin-left:auto; }

  .toast {
    position:fixed; left:50%; bottom:88px; transform:translateX(-50%);
    background:var(--danger-soft); color:var(--danger); border:1px solid color-mix(in srgb,var(--danger) 30%,transparent);
    border-radius:9px; padding:9px 15px; font-size:var(--fs-meta); display:none; z-index:20;
    max-width:70%; line-height:1.5;
  }
  .toast.show { display:block; }
  .toast.ok { background:var(--ok-soft); color:var(--ok); border-color:color-mix(in srgb,var(--ok) 30%,transparent); }

  /* 撤群确认层：内嵌页不能借用宿主的 JS 弹窗能力 —— Tauri/wry 的 WKWebView
     只实现了文件上传等面板，没实现确认面板，原生调用会静默当作「取消」。 */
  .dlg-mask {
    position:fixed; inset:0; z-index:40; display:flex; align-items:center; justify-content:center;
    background:var(--scrim); padding:16px;
  }
  .dlg {
    width:min(430px, 100%); background:var(--panel); color:var(--ink);
    border:1px solid var(--line-strong); border-radius:13px; padding:20px 20px 16px;
    box-shadow:0 18px 50px var(--scrim);
  }
  .dlg-t { font-size:var(--fs-title); font-weight:600; }
  .dlg-b { margin:12px 0 18px; font-size:var(--fs-ui); color:var(--muted); line-height:1.75; }
  .dlg-b ul { margin:8px 0 0; padding-left:18px; }
  .dlg-b li { margin:3px 0; }
  .dlg-f { display:flex; justify-content:flex-end; gap:8px; }
  .btn.danger { background:var(--danger); color:var(--on-solid); border-color:var(--danger); }
  .btn.danger:hover { opacity:.88; }
  .hidden { display:none !important; }
</style>
</head>
<body>
<div class="br">
  <aside class="br-side">
    <div class="side-top">
      <span class="ttl">我的群</span>
      <button class="newbtn" id="btn-new">＋ 新建</button>
    </div>
    <div class="mk hidden" id="mk">
      <label>话题（群名就是它）</label>
      <input id="mk-topic" type="text" placeholder="例如：烧仓这段还能怎么写" maxlength="60">
      <div class="tip">左边勾几位 bot，成员 2~6 位。没有稿件路径、章节范围、轮次表单，也没有开始按钮。<br>
        <b>开脑洞请走「专家」→「创意脑洞专家」</b>（一对一更发散）；Bot 群适合碰撞、审稿、调度。</div>
      <div class="row">
        <button class="btn" id="mk-go">建群</button>
        <button class="btn ghost" id="mk-cancel">取消</button>
      </div>
    </div>
    <div class="br-scroll">
      <div id="groups"></div>
      <div class="side-sep"></div>
      <div class="side-top"><span class="ttl" id="roster-ttl">常驻 BOT</span><span class="cnt" id="roster-cnt"></span></div>
      <div id="roster"></div>
    </div>
  </aside>

  <section class="br-main">
    <header class="br-head">
      <span class="ht" id="head-topic">Bot 群</span>
      <span class="avs" id="head-avs"></span>
      <span class="spacer"></span>
      <span class="pill hidden" id="head-guard"></span>
      <button class="btn ghost" id="head-notebooks" style="padding:4px 10px;font-size:var(--fs-label)" title="看专家自己写的私有笔记">笔记本</button>
      <button class="btn ghost hidden" id="head-disband" style="padding:4px 10px;font-size:var(--fs-label)">撤群</button>
    </header>
    <div class="br-stream" id="stream"></div>
    <div class="readonly hidden" id="readonly"></div>
    <div class="br-comp hidden" id="comp">
      <div class="comp-hint" id="comp-hint"></div>
      <div class="comp-row">
        <div class="comp-box">
          <textarea id="say" rows="1" placeholder="在群里说句话…（Enter 发送 · Shift+Enter 换行 · @ 唤人）"></textarea>
          <div class="comp-foot">
            <button class="iconbtn" id="btn-at">@ 唤人</button>
            <span class="spacer"></span>
            <span class="count" id="count"></span>
          </div>
        </div>
        <button class="btn ghost hidden" id="btn-continue" title="让几位 bot 接着刚才的话再聊一轮">再聊一轮</button>
        <button class="btn" id="send">发送</button>
      </div>
      <div class="at-pop" id="atpop"><div class="hd">唤醒谁</div><div id="atlist"></div></div>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>
<div class="dlg-mask hidden" id="disband-dlg">
  <div class="dlg" role="dialog" aria-modal="true" aria-labelledby="disband-title">
    <div class="dlg-t" id="disband-title">撤掉这个群？</div>
    <div class="dlg-b" id="disband-body"></div>
    <div class="dlg-f">
      <button class="btn ghost" id="disband-cancel">先不撤</button>
      <button class="btn danger" id="disband-ok">撤群</button>
    </div>
  </div>
</div>

<script>
/* ===================== 基础 ===================== */
// 独立页跑在 /，挂进 sidecar 时跑在 /bots/。前缀由 create_app(base_prefix=...) 注入，
// 不让 JS 猜 location —— 两种形态共用同一份页面，API 路径不会打错域。
const BASE = __BOT_BASE__;
const url = p => BASE + p;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let bots = [], rooms = [], current = null, msgs = [], picked = new Set(), creating = false, busy = false;
// 用户驱动续轮：上一次 done 给出的「还能不能再聊一轮」。新发言会清空。
let pending = null;
const streams = {};            // turn_id -> 正在生长的气泡

/* lucide 官方 path（v0.469.0），与桌面 GUI 的图标语言一致。iframe 里没有 Icon.tsx，
   所以自带这 8 个；名字来自 registry 的 manifest frontmatter icon 字段，不在这张表里
   的图标回落到名字首字，绝不画成另一个人的图标。 */
const ICONS = {
  scroll: '<path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>',
  pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
  lightbulb: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
  'spell-check': '<path d="m6 16 6-12 6 12"/><path d="M8 12h8"/><path d="m16 20 2 2 4-4"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  megaphone: '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  clapperboard: '<path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  tags: '<path d="m15 5 6.3 6.3a2.4 2.4 0 0 1 0 3.4L17 19"/><path d="M9.586 5.586A2 2 0 0 0 8.172 5H3a1 1 0 0 0-1 1v5.172a2 2 0 0 0 .586 1.414L8.29 18.29a2.426 2.426 0 0 0 3.42 0l3.58-3.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="6.5" cy="9.5" r=".5" fill="currentColor"/>'
};
/* 头像色相：纯展示层，按 bot id 稳定取，不是人设属性（registry 里没有 color 字段）。 */
const HUES = [28, 158, 38, 210, 336, 8, 244, 172];
function tint(id){
  let h = 0; for (const ch of String(id||'')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const idx = bots.findIndex(b => b.id === id);
  const hue = idx >= 0 ? HUES[idx % HUES.length] : HUES[h % HUES.length];
  return 'hsl(' + hue + ' 42% 42%)';
}
function iconSvg(name){
  const p = ICONS[name];
  if (!p) return '';
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + '</svg>';
}
function botById(id){ return bots.find(b => b.id === id); }
function botName(id){ const b = botById(id); return b ? b.name : id; }
/* 消息里的 author 是 id（落库用 id），显示名一律取名册 —— 改人设名不用动历史数据 */
function authorName(m){ return m.author_role === 'user' ? (m.author || '我') : botName(m.author); }

function avatarHtml(who, role){
  const bot = role === 'bot' ? botById(who) : null;
  const label = bot ? bot.name : '我';
  const inner = bot && bot.icon && ICONS[bot.icon]
    ? iconSvg(bot.icon)
    : '<span>' + esc(label.slice(0, 1)) + '</span>';
  const c = bot ? tint(who) : 'var(--accent)';
  return '<div class="av' + (bot && bot.icon && ICONS[bot.icon] ? '' : ' letter') +
         '" style="--c:' + c + '">' + inner + '</div>';
}

/* ===================== 主题同步 ===================== */
/* 桌面 GUI 把**计算后的** token 值发进来（styles.css 仍是单一真源，9 套皮肤不在这里复刻）。
   只接受白名单变量名，父页传来的其它内容一律忽略。 */
const TOKEN_KEYS = ['--paper','--panel','--ink','--muted','--faint','--line','--line-strong',
  '--accent','--accent-soft','--ok','--ok-soft','--ok-line','--ok-dot','--warn-ink','--warn-soft',
  '--danger','--danger-soft','--teal-ink','--teal-soft','--solid','--on-solid','--scrim',
  '--fs-label','--fs-meta','--fs-ui','--fs-body','--fs-heading','--fs-title','--fs-chat'];
window.addEventListener('message', e => {
  const d = e.data;
  if (!d || d.type !== 'ow-tokens') return;
  const root = document.documentElement;
  if (d.theme) root.dataset.theme = d.theme === 'dark' ? 'dark' : 'light';
  if (d.textSize) root.dataset.textSize = d.textSize;
  const vars = d.vars || {};
  TOKEN_KEYS.forEach(k => { if (typeof vars[k] === 'string' && vars[k]) root.style.setProperty(k, vars[k]); });
});
// 直接开 9921 时也能手测：?theme=dark&size=large
const Q = new URLSearchParams(location.search);
if (Q.get('theme')) document.documentElement.dataset.theme = Q.get('theme') === 'dark' ? 'dark' : 'light';
if (['small','large'].includes(Q.get('size'))) document.documentElement.dataset.textSize = Q.get('size');
// 主动向父页要一次：iframe 与父文档谁先就绪没有保证，只等父页的 onLoad 会偶发漏投。
// 独立开 9921 时 parent === window，这条消息没人收，不影响兜底 token 渲染。
try { parent.postMessage({ type: 'ow-tokens-request' }, '*'); } catch (_) {}

/* ===================== 时间 ===================== */
function clockOf(ts){
  const d = new Date(ts); if (isNaN(d)) return '';
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12:false});
}
function dayKey(ts){ const d = new Date(ts); return isNaN(d) ? '' : d.toDateString(); }
function dayLabel(ts){
  const d = new Date(ts); if (isNaN(d)) return '';
  const now = new Date(), y = new Date(now.getTime() - 864e5);
  if (d.toDateString() === now.toDateString()) return clockOf(ts);
  if (d.toDateString() === y.toDateString()) return '昨天';
  return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
}
function daySepLabel(ts){
  const d = new Date(ts); if (isNaN(d)) return '';
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return '今天';
  return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日';
}

/* ===================== 渲染 ===================== */
const LONG_TEXT = 400;   // 超过这个长度先截断 6 行 + 给「展开全文」，不把整段倒屏上
let _atRe = null, _atReKey = null;
function atRe(){
  // 名字和 id 都要匹配：真机历史消息里用户写的是 @historian（id），
  // 只认显示名会让一大半 @ 失去高亮。长 token 先匹配，避免短的前缀吃掉长的。
  // 不做正则转义：id 是 slug（字母数字连字符），显示名是中文，都不含元字符。
  const toks = [];
  bots.forEach(b => { if (b.name) toks.push(b.name); if (b.id) toks.push(b.id); });
  toks.sort((a, b) => b.length - a.length);
  const key = toks.join('|');
  if (_atReKey !== key){
    _atReKey = key;
    _atRe = toks.length ? new RegExp('@(' + toks.join('|') + ')', 'g') : null;
  }
  return _atRe;   // 缓存：每条消息都重建正则会随消息数线性放大
}
function fmt(text){
  let h = esc(text);
  // 消息是纯文本落库的（ChatStore 没有 markdown 字段），但真 Agent 实际会输出
  // **粗体** 和 `行内代码`。这里只做这两种行内还原 —— 星号裸露比不解析更糟。
  // 不做标题 / 列表 / 链接：那需要后端先定义富文本契约，现在是纯文本契约。
  // 顺序很重要：先 esc 再替换，标记只能来自原文，注入不进来标签。
  h = h.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  h = h.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  const re = atRe();
  return re ? h.replace(re, '<span class="at">@$1</span>') : h;
}
function msgHtml(m, prev){
  const mine = m.author_role === 'user';
  const cont = prev && prev.author === m.author && prev.author_role === m.author_role;
  const long = (m.text || '').length > LONG_TEXT;
  const meta = mine ? '' :
    '<div class="meta"><span class="who">' + esc(authorName(m)) + '</span>' +
    '<span class="role">' + esc((botById(m.author) || {}).tagline || '') + '</span>' +
    '<span class="ts">' + clockOf(m.ts) + '</span></div>';
  return '<div class="msg' + (mine ? ' mine' : '') + (cont ? ' cont' : '') + '">' +
    avatarHtml(m.author, m.author_role) +
    '<div class="col">' + (cont ? '' : meta) +
    '<div class="bubble' + (long ? ' clip' : '') + '">' + fmt(m.text) + '</div>' +
    (long ? '<button class="expand">展开全文 ▾</button>' : '') +
    '</div></div>';
}
function sepHtml(kind, text){ return '<div class="sep ' + kind + '"><span>' + esc(text) + '</span></div>'; }

function renderMessages(){
  const st = $('#stream');
  if (!msgs.length){
    st.innerHTML = '<div class="empty"><div class="big">群建好了，还没人说话</div>' +
      '<div class="sm">直接说人话就行。不 @ 则全员回应，@ 谁谁答。<br>想让他先读书稿，就说「先读《夜不收》第三章」，他自己会去翻。<br><b>开脑洞请走「专家」→「创意脑洞专家」</b>（一对一更发散）；这里适合碰撞、审稿、调度。</div>' +
      '<div class="quick">' +
      ['先读《夜不收》第三章，说说节奏问题', '烧仓之后怎么撤，别再写一路顺风', '这卷还有哪些史实硬伤']
        .map(t => '<button data-fill="' + esc(t) + '">' + esc(t) + '</button>').join('') +
      '</div></div>';
    st.querySelectorAll('[data-fill]').forEach(b => b.onclick = () => {
      $('#say').value = b.dataset.fill; $('#say').focus(); autosize();
    });
    return;
  }
  let out = '', prev = null, prevDay = '';
  msgs.forEach(m => {
    const dk = dayKey(m.ts);
    if (dk !== prevDay){ out += sepHtml('date', daySepLabel(m.ts)); prevDay = dk; prev = null; }
    else if (prev){
      const gap = new Date(m.ts) - new Date(prev.ts);
      if (gap > 5 * 60000) out += sepHtml('time', clockOf(m.ts));
    }
    out += msgHtml(m, prev);
    prev = m;
  });
  st.innerHTML = out;
  bindBubbles();
}
function bindBubbles(){
  document.querySelectorAll('.expand').forEach(btn => {
    btn.onclick = () => {
      const b = btn.parentElement.querySelector('.bubble');
      btn.textContent = b.classList.toggle('open') ? '收起 ▴' : '展开全文 ▾';
    };
  });
  document.querySelectorAll('.bubble').forEach(b => {
    b.title = '双击复制';
    b.ondblclick = () => { if (navigator.clipboard) navigator.clipboard.writeText(b.innerText); };
  });
}
function toBottom(){ const st = $('#stream'); st.scrollTop = st.scrollHeight; }

/* 左栏 */
function renderGroups(){
  $('#groups').innerHTML = rooms.length ? rooms.map(r => {
    const n = r.bot_ids.length;
    const stack = r.bot_ids.slice(0, 4).map(id => {
      const b = botById(id);
      const inner = b && b.icon && ICONS[b.icon] ? iconSvg(b.icon) : esc((b ? b.name : id).slice(0,1));
      return '<span class="mini" style="background:' + tint(id) + ';color:#fff">' + inner + '</span>';
    }).join('') + (n > 4 ? '<span class="mini" style="background:var(--faint);color:#fff">+' + (n-4) + '</span>' : '');
    return '<div class="grp-item' + (r.room_id === current ? ' on' : '') + '" data-id="' + esc(r.room_id) + '">' +
      '<div class="grp-l1"><span class="grp-name">' + esc(r.topic) + '</span>' +
      '<span class="grp-time">' + dayLabel(r.last_ts || r.created_at * 1000) + '</span></div>' +
      '<div class="grp-l2"><span class="stack">' + stack + '</span>' +
      '<span class="grp-last">' + esc(r.last_text || '还没人说话') + '</span></div></div>';
  }).join('') : '<div class="note" style="padding:10px 16px;text-align:left">还没有群</div>';
  document.querySelectorAll('.grp-item').forEach(el => el.onclick = () => openRoom(el.dataset.id));
}
function renderRoster(){
  const inGroup = new Set((rooms.find(r => r.room_id === current) || {bot_ids:[]}).bot_ids);
  $('#roster-ttl').textContent = creating ? '勾选成员' : '常驻 BOT';
  $('#roster-cnt').textContent = creating ? picked.size + ' / 6' : bots.length + ' 位 · 不说话不花钱';
  $('#roster').innerHTML = bots.map(b => {
    const isPicked = picked.has(b.id);
    const cls = creating ? (isPicked ? 'picked' : '') : (inGroup.has(b.id) ? '' : 'off');
    const inner = b.icon && ICONS[b.icon] ? iconSvg(b.icon) : '<span>' + esc(b.name.slice(0,1)) + '</span>';
    return '<div class="bot-item ' + cls + '" data-id="' + esc(b.id) + '">' +
      '<div class="av' + (b.icon && ICONS[b.icon] ? '' : ' letter') + '" style="--c:' + tint(b.id) + '">' + inner + '</div>' +
      '<div class="txt"><div class="nm">' + esc(b.name) + '</div><div class="tg">' + esc(b.tagline || b.role) + '</div></div>' +
      (creating ? '<div class="tick">' + (isPicked ? '✓' : '') + '</div>'
                : (inGroup.has(b.id) ? '<span class="flag">在本群</span>' : '')) +
      '</div>';
  }).join('');
  document.querySelectorAll('.bot-item').forEach(el => el.onclick = () => {
    if (!creating) return;
    const id = el.dataset.id;
    if (picked.has(id)) picked.delete(id);
    else if (picked.size < 6) picked.add(id);
    else toast('一个群最多 6 位（成员 2~6 是硬上限）');
    renderRoster();
  });
}

/* 头部与输入区状态 */
function guardText(){
  const room = rooms.find(r => r.room_id === current);
  if (!room) return '';
  // 只报能确定的事实：消息条数与成员数。「本轮剩几条」需要 round 游标，而 ChatStore
  // 的消息里没有 round 字段（零改动红线），所以不拿总数冒充轮次余量。
  return '共 ' + msgs.length + ' 条 · ' + room.bot_ids.length + ' 位成员';
}
function syncChrome(){
  const room = rooms.find(r => r.room_id === current);
  const has = !!room;
  $('#head-topic').textContent = creating ? '建新群' : (has ? room.topic : 'Bot 群');
  $('#head-avs').innerHTML = has && !creating ? room.bot_ids.map(id => avatarHtml(id, 'bot')).join('') : '';
  const guard = $('#head-guard');
  guard.classList.toggle('hidden', !has || creating);
  guard.textContent = has && !creating ? guardText() : '';
  guard.className = 'pill' + (busy ? ' warn' : '');
  $('#head-disband').classList.toggle('hidden', !has || creating);
  $('#comp').classList.toggle('hidden', !has || creating);
  $('#readonly').classList.add('hidden');
  if (has && !creating){
    $('#say').disabled = busy; $('#send').disabled = busy;
    $('#comp').classList.toggle('busy', busy);
    $('#btn-continue').classList.toggle('hidden', busy || !pending || !pending.can_continue);
    const atLimit = !!pending && !pending.can_continue;
    $('#comp-hint').textContent = busy
      ? '他在说这句 —— 本轮说完前发不出去，这不是出错。'
      : (atLimit
        ? '这次讨论到上限了 —— 再发一句可以开新一轮，群和材料都还在。'
        : (pending && pending.can_continue
          ? '这一轮说完了。点「再聊一轮」让他们接着刚才的话聊，或直接发新的一句。'
          : '不 @ 则全员回应；@ 谁谁答。说「先读《夜不收》」，他会自己去翻群共享材料。'));
    $('#count').textContent = busy ? '他在说…' : 'Enter 发送';
    $('#count').className = 'count';
  }
  renderGroups(); renderRoster();
}

/* ===================== 数据 ===================== */
async function api(path, opt){
  const r = await fetch(url(path), opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}
function toast(m, ok){
  const t = $('#toast'); t.textContent = m || '';
  t.classList.toggle('show', !!m); t.classList.toggle('ok', !!ok);
  clearTimeout(toast._t); if (m) toast._t = setTimeout(() => t.classList.remove('show'), 4200);
}
async function loadBots(){ bots = await api('/api/bots'); }
async function loadRooms(){ rooms = await api('/api/rooms'); }
async function loadMessages(){
  if (!current) { msgs = []; return; }
  msgs = await api('/api/rooms/' + current + '/messages');
}
async function openRoom(id){
  if (creating) cancelCreate();
  // 续轮状态属于「某个群的那次讨论」，切群必须清掉，否则 B 群会残留 A 群的按钮。
  if (id !== current) pending = null;
  current = id;
  await loadMessages();
  syncChrome(); renderMessages(); toBottom();
}

/* ===================== 建群 / 撤群 ===================== */
/* 页面内确认层。桌面 Tauri 的 WKWebView 没实现 JS 确认面板，原生调用会静默
   当作「取消」—— 表现为「点撤群毫无反应」。这里不借用任何宿主弹窗能力，
   也顺手把撤群到底意味着什么讲清楚，不再只问一句「确定吗」。 */
function askDisband(topic){
  return new Promise(resolve => {
    const box = $('#disband-dlg');
    $('#disband-body').innerHTML =
      '<div>「' + esc(topic) + '」撤掉之后：</div><ul>' +
      '<li>从左侧「我的群」里消失，不能再发言、也不能再续轮；</li>' +
      '<li>聊天记录只在数据库留档，页面上看不回来；</li>' +
      '<li>群 ID 不再复用，想接着聊得按同一话题重建（旧记录不继承）。</li></ul>';
    box.classList.remove('hidden');
    function done(ok){
      box.classList.add('hidden');
      $('#disband-ok').onclick = null; $('#disband-cancel').onclick = null;
      box.onclick = null; document.removeEventListener('keydown', onKey);
      resolve(ok);
    }
    function onKey(e){ if (e.key === 'Escape') done(false); }
    $('#disband-ok').onclick = () => done(true);
    $('#disband-cancel').onclick = () => done(false);
    box.onclick = e => { if (e.target === box) done(false); };
    document.addEventListener('keydown', onKey);
    $('#disband-cancel').focus();
  });
}
function startCreate(){
  creating = true; picked = new Set(bots.slice(0, 3).map(b => b.id));
  $('#mk').classList.remove('hidden'); $('#btn-new').classList.add('hidden');
  $('#mk-topic').value = ''; syncChrome(); $('#mk-topic').focus();
}
function cancelCreate(){
  creating = false; picked = new Set();
  $('#mk').classList.add('hidden'); $('#btn-new').classList.remove('hidden');
  syncChrome();
}
$('#btn-new').onclick = startCreate;
$('#head-notebooks').onclick = () => { location.href = url('/notebooks'); };
$('#mk-cancel').onclick = cancelCreate;
$('#mk-go').onclick = async () => {
  const topic = $('#mk-topic').value.trim();
  if (!topic) { toast('先给个话题，群名就是它'); return; }
  if (picked.size < 2) { toast('至少勾 2 位 bot'); return; }
  try {
    const r = await api('/api/rooms', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({topic, bot_ids: [...picked]})});
    cancelCreate();
    await loadRooms();
    await openRoom(r.room_id);
  } catch (e) { toast(e.message); }
};
$('#head-disband').onclick = async () => {
  if (!current) return;
  if (busy) { toast('等这一轮说完再撤群'); return; }
  const room = rooms.find(r => r.room_id === current);
  if (!(await askDisband(room ? room.topic : ''))) return;
  try {
    await api('/api/rooms/' + current, {method:'DELETE'});
    current = null; msgs = []; pending = null;
    await loadRooms();
    $('#head-topic').textContent = 'Bot 群';
    $('#head-avs').innerHTML = '';
    $('#head-guard').classList.add('hidden');
    $('#head-disband').classList.add('hidden');
    $('#comp').classList.add('hidden');
    $('#stream').innerHTML = '<div class="empty"><div class="big">群已撤</div>' +
      '<div class="sm">消息留档，没被删掉。想接着聊就按同一个话题重建一个。</div></div>';
    toast('已撤群：群从列表移除，聊天记录留档', true);
    syncChrome();
  } catch (e) { toast(e.message); }
};

/* ===================== 流式 ===================== */
function startBubble(ev){
  const row = document.createElement('div');
  row.className = 'msg';
  row.innerHTML = avatarHtml(ev.bot_id, 'bot') +
    '<div class="col"><div class="meta"><span class="who">' + esc(ev.name) + '</span>' +
    '<span class="role">' + esc((botById(ev.bot_id) || {}).tagline || '') + '</span></div>' +
    '<div class="bubble"><span class="dots"><i></i><i></i><i></i></span> <span class="doing">正在思考…</span></div></div>';
  $('#stream').appendChild(row);
  streams[ev.turn_id] = {
    row, bubble: row.querySelector('.bubble'), doing: row.querySelector('.doing'), started: false
  };
  toBottom();
}
function handleStream(ev){
  const st = ev.turn_id ? streams[ev.turn_id] : null;
  switch (ev.type) {
    case 'round_start':
      if (ev.round > 1) $('#stream').insertAdjacentHTML('beforeend', sepHtml('round', '第 ' + ev.round + ' 轮接话'));
      break;
    case 'bot_start': startBubble(ev); break;
    case 'tool':
      // tool 事件就是「他在干什么」的真信号，不是装饰：read_book 才是读书稿
      if (st && !st.started) st.doing.textContent = ev.name === 'read_book' ? '正在读书稿…' : '正在查资料…';
      break;
    case 'delta':
      if (!st) break;
      if (!st.started){ st.started = true; st.bubble.innerHTML = '<span class="caret"></span>'; }
      st.bubble.querySelector('.caret').insertAdjacentHTML('beforebegin', esc(ev.text));
      toBottom();
      break;
    case 'bot_end':
      if (st){ st.bubble.textContent = ev.text; delete streams[ev.turn_id]; }
      break;
    case 'bot_pass':
      if (st){ st.row.remove(); delete streams[ev.turn_id]; }
      if (ev.reason === 'pass')
        $('#stream').insertAdjacentHTML('beforeend', '<div class="note">' + esc(ev.name) + ' 本轮略过</div>');
      break;
    case 'error': toast(ev.message || '群聊出错了'); break;
    default: break;   // start / final / done
  }
}
/* 一次 SSE 连接：逐 token 交给 handleStream，返回 done 事件（含 can_continue）。
   /stream（新发言）与 /continue（再聊一轮）共用这一套渲染，不复制。 */
async function consumeStream(resp){
  const reader = resp.body.getReader(), decoder = new TextDecoder();
  let buffer = '', doneEv = null;
  for (;;){
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream:true});
    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0){
      const chunk = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
      for (const line of chunk.split('\n')){
        if (!line.startsWith('data:')) continue;
        try {
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === 'done') doneEv = ev;
          else handleStream(ev);
        } catch (_) {}
      }
    }
  }
  return doneEv;
}
function showBounded(bounded){
  if (!bounded) return;
  // bounded 是后端护栏原因码，翻成人话再展示，别把内部枚举丢给用户看。
  const WHY = {max_messages:'已达 10 条消息上限', max_rounds:'已达 3 轮上限'};
  $('#stream').insertAdjacentHTML('beforeend', '<div class="sysbar">这次讨论到此为止（' +
    esc(WHY[bounded] || bounded) + '）。这是护栏不是出错 —— 再发一句可开一轮新的，群不撤、材料还在。</div>');
}
function setPending(d){
  pending = d ? {
    can_continue: !!d.can_continue,
    next_round: d.next_round || 2,
    bounded: d.bounded || null,
    total_rounds: d.total_rounds || 1,
    message_count: d.message_count || 0
  } : null;
}
async function finishStream(){
  Object.keys(streams).forEach(k => { streams[k].row.remove(); delete streams[k]; });
  busy = false;
  await loadRooms(); await loadMessages();   // 以落库为准重绘，保证与刷新后一致
  syncChrome(); renderMessages(); toBottom();
}
async function send(){
  const text = $('#say').value.trim();
  if (!text || !current || busy) return;
  $('#say').value = ''; autosize();
  pending = null;                 // 新的一句 = 新的一次讨论，续轮状态归零
  busy = true; syncChrome();
  $('#stream').insertAdjacentHTML('beforeend',
    msgHtml({author_role:'user', author:'我', text, ts:new Date().toISOString()}, null));
  toBottom();
  try {
    const resp = await fetch(url('/api/rooms/' + current + '/stream'), {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})
    });
    if (!resp.ok || !resp.body){
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || ('HTTP ' + resp.status));
    }
    const d = await consumeStream(resp);
    setPending(d);
    if (d && d.bounded) showBounded(d.bounded);
  } catch (e) {
    toast(e.message);
    $('#stream').insertAdjacentHTML('beforeend',
      '<div class="sysbar err">群聊服务出错了。前面的消息已落库不丢，重发一次即可。</div>');
  }
  await finishStream();
}
async function continueRound(){
  if (!current || busy || !pending || !pending.can_continue) return;
  const next = pending.next_round, saved = pending;
  busy = true; pending = null; syncChrome();
  try {
    const resp = await fetch(url('/api/rooms/' + current + '/continue'), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({round_index: next})
    });
    if (!resp.ok || !resp.body){
      const d = await resp.json().catch(() => ({}));
      throw new Error(d.detail || ('HTTP ' + resp.status));
    }
    const d = await consumeStream(resp);
    setPending(d);
    if (d && d.bounded) showBounded(d.bounded);
  } catch (e) {
    pending = saved;   // 网络抖动不该吞掉续轮机会，「再点一次即可」要成立
    toast(e.message);
    $('#stream').insertAdjacentHTML('beforeend',
      '<div class="sysbar err">续轮失败了。前面的消息已落库不丢，再点一次即可。</div>');
  }
  await finishStream();
}
$('#send').onclick = send;
$('#btn-continue').onclick = continueRound;

/* ===================== 输入框 ===================== */
function autosize(){
  const t = $('#say'); t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 150) + 'px';
}
function showAt(){
  const room = rooms.find(r => r.room_id === current);
  if (!room) return;
  $('#atlist').innerHTML = room.bot_ids.map(id => {
    const b = botById(id);
    const inner = b && b.icon && ICONS[b.icon] ? iconSvg(b.icon) : esc((b ? b.name : id).slice(0,1));
    return '<div class="at-item" data-name="' + esc(b ? b.name : id) + '">' +
      '<div class="av' + (b && b.icon && ICONS[b.icon] ? '' : ' letter') + '" style="--c:' + tint(id) + '">' + inner + '</div>' +
      '<span class="nm">' + esc(b ? b.name : id) + '</span><span class="tg">' + esc((b || {}).tagline || '') + '</span></div>';
  }).join('');
  $('#atpop').classList.add('show');
}
$('#btn-at').onclick = () => { const p = $('#atpop'); p.classList.contains('show') ? p.classList.remove('show') : showAt(); };
$('#atpop').addEventListener('click', e => {
  const it = e.target.closest('.at-item'); if (!it) return;
  const t = $('#say');
  t.value = t.value.replace(/@$/, '@' + it.dataset.name + ' ');
  $('#atpop').classList.remove('show'); t.focus(); autosize();
});
$('#say').addEventListener('input', e => {
  autosize();
  if (/@$/.test(e.target.value)) showAt(); else $('#atpop').classList.remove('show');
});
$('#say').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
  if (e.key === 'Escape') $('#atpop').classList.remove('show');
});
document.addEventListener('click', e => {
  if (!e.target.closest('#atpop') && !e.target.closest('#btn-at')) $('#atpop').classList.remove('show');
});

/* ===================== 启动 ===================== */
(async () => {
  try {
    await loadBots(); await loadRooms();
    // ?room=<id> 只是手测/截图用的定点入口，GUI 里不会带；找不到就退回最近的群。
    const want = Q.get('room');
    const target = (want && rooms.find(r => r.room_id === want)) || rooms[rooms.length - 1];
    if (target) await openRoom(target.room_id);
    else { syncChrome(); $('#stream').innerHTML = '<div class="empty"><div class="big">还没有群</div>' +
      '<div class="sm">点左上「＋ 新建」，勾几位 bot、起个话题就能开聊。<br><b>开脑洞请走「专家」→「创意脑洞专家」</b>（一对一更发散）；Bot 群适合碰撞、审稿、调度。</div></div>'; }
  } catch (e) { toast('群聊服务连不上：' + e.message); }
})();
</script>
</body>
</html>

"""

NOTEBOOKS_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>专家笔记本 · OpenWorker</title>
<style>
  :root {
    color-scheme: light;
    --paper:#f5f6f7; --panel:#ffffff; --ink:#17191c; --muted:#5b616b; --faint:#9aa1aa;
    --line:#e8eaed; --line-strong:#d8dce1;
    --accent:#2563eb; --accent-soft:#e9f0fd;
    --ok:#2f7d57; --danger:#b91c1c; --danger-soft:#f9e7e5;
    --solid:#e9ebf0; --on-solid:#1f2227; --scrim:rgba(29,27,24,.32);
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",Inter,system-ui,sans-serif;
    --mono:"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --fs-label:11px; --fs-meta:12px; --fs-ui:13px; --fs-body:14px;
    --fs-heading:15px; --fs-title:16px; --fs-chat:15px;
  }
  html[data-theme="dark"] {
    color-scheme: dark;
    --paper:#131417; --panel:#1c1e22; --ink:#e6e8eb; --muted:#9aa1ab; --faint:#62686f;
    --line:#2a2d33; --line-strong:#3a3e46;
    --accent:#4c8dff; --accent-soft:#1c2a44;
    --ok:#5cb87f; --danger:#f07067; --danger-soft:#3d2422;
    --solid:#2e3138; --on-solid:#e6e8eb; --scrim:rgba(0,0,0,.5);
  }
  html[data-text-size="small"] { --fs-label:10px; --fs-meta:11px; --fs-ui:12px; --fs-body:13px; --fs-heading:14px; --fs-title:15px; }
  html[data-text-size="large"] { --fs-label:12px; --fs-meta:13px; --fs-ui:14px; --fs-body:15px; --fs-heading:16px; --fs-title:17px; }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body { background:var(--paper); color:var(--ink); font-family:var(--sans); font-size:var(--fs-body); overflow:hidden; }
  button, textarea, input { font-family:inherit; }
  .nb { display:flex; height:100vh; }
  .nb-side { width:304px; flex:none; background:var(--panel); border-right:1px solid var(--line); display:flex; flex-flow:column; overflow:hidden; }
  .side-top { padding:14px 14px 8px; display:flex; align-items:center; gap:8px; }
  .side-top .ttl { font-size:var(--fs-label); font-weight:600; letter-spacing:.12em; color:var(--muted); text-transform:uppercase; flex:1; }
  .back { font-size:var(--fs-label); color:var(--accent); cursor:pointer; text-decoration:none; }
  .hint { padding:0 14px 10px; font-size:var(--fs-label); color:var(--faint); line-height:1.75; }
  .nb-list { overflow:auto; flex:1; padding:0 8px 14px; }
  .nb-group { font-size:var(--fs-label); color:var(--faint); padding:12px 8px 4px; letter-spacing:.05em; }
  .nb-item { padding:9px 10px; border-radius:8px; cursor:pointer; border:1px solid transparent; }
  .nb-item:hover { background:var(--paper); }
  .nb-item.on { background:var(--accent-soft); border-color:color-mix(in srgb,var(--accent) 30%,transparent); }
  .nb-item .nm { font-size:var(--fs-ui); font-weight:600; }
  .nb-item .meta { font-size:var(--fs-label); color:var(--faint); margin-top:3px; }
  .nb-main { flex:1; display:flex; flex-flow:column; min-width:0; }
  .nb-head { height:52px; flex:none; display:flex; align-items:center; gap:10px; padding:0 18px; border-bottom:1px solid var(--line); background:var(--panel); }
  .nb-head .ht { font-size:var(--fs-title); font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .spacer { flex:1; }
  .btn { border:1px solid var(--line-strong); background:var(--panel); color:var(--ink); border-radius:7px; padding:6px 12px; font-size:var(--fs-ui); cursor:pointer; }
  .btn:hover { border-color:var(--accent); color:var(--accent); }
  .btn.ghost { background:transparent; }
  .btn.danger { background:var(--danger); border-color:var(--danger); color:#fff; }
  .btn.danger:hover { opacity:.88; color:#fff; }
  .nb-body { margin:0; padding:18px 20px; overflow:auto; flex:1; white-space:pre-wrap; line-height:1.85; font-family:var(--mono); font-size:var(--fs-body); }
  .nb-editor { display:block; width:100%; border:none; outline:none; resize:none; background:var(--panel); color:var(--ink); }
  .empty { margin:auto; color:var(--muted); text-align:center; padding:44px 24px; line-height:1.9; }
  .hidden { display:none !important; }
  .toast { position:fixed; left:50%; bottom:22px; transform:translateX(-50%); background:var(--solid); color:var(--on-solid); padding:9px 16px; border-radius:9px; font-size:var(--fs-ui); opacity:0; pointer-events:none; transition:opacity .15s; }
  .toast.show { opacity:1; }
  .toast.ok { background:var(--ok); color:#fff; }
  .mask { position:fixed; inset:0; background:var(--scrim); display:flex; align-items:center; justify-content:center; z-index:20; }
  .dlg { width:min(430px,92vw); background:var(--panel); color:var(--ink); border:1px solid var(--line); border-radius:12px; padding:18px; }
  .dlg-t { font-size:var(--fs-title); font-weight:600; margin-bottom:10px; }
  .dlg-b { font-size:var(--fs-ui); color:var(--muted); line-height:1.75; margin-bottom:16px; }
  .dlg-f { display:flex; justify-content:flex-end; gap:8px; }
</style>
</head>
<body>
<div class="nb">
  <aside class="nb-side">
    <div class="side-top"><span class="ttl">专家笔记本</span><a class="back" id="back">← Bot 群</a></div>
    <div class="hint">专家自己写的私有笔记（书 × 专家）。这里只能看和删；笔记不进群材料，也不喂小桔。</div>
    <div class="nb-list" id="list"></div>
  </aside>
  <section class="nb-main">
    <header class="nb-head">
      <span class="ht" id="title">选一本笔记</span>
      <span class="spacer"></span>
      <button class="btn" id="edit">编辑</button>
      <button class="btn hidden" id="save">保存</button>
      <button class="btn ghost hidden" id="cancel">取消</button>
      <button class="btn danger hidden" id="del">删除这本笔记</button>
    </header>
    <pre class="nb-body" id="body"></pre>
    <textarea class="nb-body nb-editor hidden" id="editor" spellcheck="false"></textarea>
  </section>
</div>
<div class="mask hidden" id="confirm">
  <div class="dlg" role="dialog" aria-modal="true">
    <div class="dlg-t">删掉这本笔记？</div>
    <div class="dlg-b" id="cf-body"></div>
    <div class="dlg-f">
      <button class="btn ghost" id="cf-cancel">先不删</button>
      <button class="btn danger" id="cf-ok">删除</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const BASE = __BOT_BASE__;
const url = p => BASE + p;
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const EXPERT_NAMES = { brainstormer: '创意脑洞专家', 'lit-editor': '文学编辑', adaptation: '影视改编策划' };
let items = [], current = null, toastTimer = null, editing = false;

function toast(m, ok) {
  const t = $('#toast');
  t.textContent = m || '';
  t.classList.toggle('show', !!m);
  t.classList.toggle('ok', !!ok);
  clearTimeout(toastTimer);
  if (m) toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}
async function api(path, opt) {
  const r = await fetch(url(path), opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
  return d;
}
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '';
  const pad = n => String(n).padStart(2, '0');
  return (d.getMonth() + 1) + ' 月 ' + d.getDate() + ' 日 ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function renderList() {
  const list = $('#list');
  if (!items.length) {
    list.innerHTML = '<div class="empty" style="padding:28px 12px">还没有专家记过笔记。<br>去群里聊一轮，或 1v1 找脑洞 / 文学编辑 / 影视改编。</div>';
    return;
  }
  const byBook = {};
  for (const it of items) (byBook[it.book] || (byBook[it.book] = [])).push(it);
  list.innerHTML = Object.keys(byBook).map(book =>
    '<div class="nb-group">' + esc(book) + '</div>' +
    byBook[book].map(it =>
      '<div class="nb-item' + (current && current.book === it.book && current.expert === it.expert ? ' on' : '') +
      '" data-book="' + esc(it.book) + '" data-expert="' + esc(it.expert) + '">' +
        '<div class="nm">' + esc(EXPERT_NAMES[it.expert] || it.expert) + '</div>' +
        '<div class="meta">' + it.chars + ' 字 · ' + fmtTime(it.updated) + '</div>' +
      '</div>'
    ).join('')
  ).join('');
  list.querySelectorAll('.nb-item').forEach(el => {
    el.onclick = () => select(el.dataset.book, el.dataset.expert);
  });
}
function resetView() {
  current = null;
  editing = false;
  $('#title').textContent = '选一本笔记';
  $('#body').textContent = '';
  $('#editor').value = '';
  stopEditingChrome();
  $('#edit').classList.add('hidden');
  $('#del').classList.add('hidden');
}
function stopEditingChrome() {
  editing = false;
  $('#editor').classList.add('hidden');
  $('#body').classList.remove('hidden');
  $('#save').classList.add('hidden');
  $('#cancel').classList.add('hidden');
  $('#edit').classList.remove('hidden');
}
function startEdit() {
  if (!current) return;
  editing = true;
  const text = $('#body').textContent;
  $('#editor').value = text === '（这本笔记本还是空的）' ? '' : text;
  $('#body').classList.add('hidden');
  $('#editor').classList.remove('hidden');
  $('#edit').classList.add('hidden');
  $('#save').classList.remove('hidden');
  $('#cancel').classList.remove('hidden');
  $('#del').classList.add('hidden');
  $('#editor').focus();
}
function cancelEdit() {
  if (!editing) return;
  stopEditingChrome();
  $('#del').classList.remove('hidden');
}
async function saveEdit() {
  if (!current || !editing) return;
  const text = $('#editor').value;
  try {
    await api('/api/notebooks/content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book: current.book, expert: current.expert, text: text })
    });
    toast('已保存', true);
    stopEditingChrome();
    await load();
  } catch (e) {
    toast('保存失败：' + e.message);
  }
}
async function select(book, expert) {
  if (editing) { toast('先保存或取消编辑，再切换'); return; }
  current = { book: book, expert: expert };
  renderList();
  $('#title').textContent = '《' + book + '》· ' + (EXPERT_NAMES[expert] || expert);
  try {
    const d = await api('/api/notebooks/content?book=' + encodeURIComponent(book) + '&expert=' + encodeURIComponent(expert));
    $('#body').textContent = d.text || '（这本笔记本还是空的）';
    $('#del').classList.remove('hidden');
    $('#edit').classList.remove('hidden');
  } catch (e) {
    $('#body').textContent = '读取失败：' + e.message;
    $('#del').classList.add('hidden');
    $('#edit').classList.add('hidden');
  }
}
async function load() {
  const d = await api('/api/notebooks');
  items = d.items || [];
  if (current) {
    const still = items.find(x => x.book === current.book && x.expert === current.expert);
    if (still) { await select(current.book, current.expert); return; }
    resetView();
  }
  renderList();
  if (items.length) await select(items[0].book, items[0].expert);
}
function askDelete() {
  if (!current) return;
  $('#cf-body').innerHTML = '「《' + esc(current.book) + '》· ' + esc(EXPERT_NAMES[current.expert] || current.expert) +
    '」的笔记会整本删除，不可恢复。<br>书稿不受影响；专家下次从空白开始。';
  $('#confirm').classList.remove('hidden');
}
async function doDelete() {
  if (!current) return;
  $('#confirm').classList.add('hidden');
  const book = current.book, expert = current.expert;
  try {
    await api('/api/notebooks?book=' + encodeURIComponent(book) + '&expert=' + encodeURIComponent(expert), { method: 'DELETE' });
    toast('已删除', true);
    current = null;
    await load();
  } catch (e) { toast('删除失败：' + e.message); }
}
$('#back').onclick = () => { if (editing) { toast('先保存或取消编辑'); return; } location.href = url('/'); };
$('#edit').onclick = startEdit;
$('#save').onclick = saveEdit;
$('#cancel').onclick = cancelEdit;
$('#del').onclick = askDelete;
$('#cf-cancel').onclick = () => $('#confirm').classList.add('hidden');
$('#cf-ok').onclick = doDelete;
$('#confirm').onclick = e => { if (e.target === $('#confirm')) $('#confirm').classList.add('hidden'); };
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('#confirm').classList.contains('hidden')) { $('#confirm').classList.add('hidden'); return; }
  if (editing) cancelEdit();
});

/* 主题 token：与 Bot 群页同一协议（父页 postMessage 计算后的变量） */
const TOKEN_KEYS = ['--paper','--panel','--ink','--muted','--faint','--line','--line-strong',
  '--accent','--accent-soft','--ok','--danger','--solid','--on-solid','--scrim',
  '--fs-label','--fs-meta','--fs-ui','--fs-body','--fs-heading','--fs-title'];
window.addEventListener('message', e => {
  const d = e.data;
  if (!d || d.type !== 'ow-tokens') return;
  const root = document.documentElement;
  if (d.theme) root.dataset.theme = d.theme === 'dark' ? 'dark' : 'light';
  if (d.textSize) root.dataset.textSize = d.textSize;
  const vars = d.vars || {};
  TOKEN_KEYS.forEach(k => { if (typeof vars[k] === 'string' && vars[k]) root.style.setProperty(k, vars[k]); });
});
try { parent.postMessage({ type: 'ow-tokens-request' }, '*'); } catch (e) {}

load().catch(e => toast('加载失败：' + e.message));
</script>
</body>
</html>
"""


class NewRoom(BaseModel):
    topic: str
    bot_ids: list[str]


class NotebookSave(BaseModel):
    book: str
    expert: str
    text: str


class Say(BaseModel):
    text: str


class Continue(BaseModel):
    """用户点「再聊一轮」：只带轮号，成员缺省＝全群。"""

    round_index: int
    bot_ids: list[str] | None = None


logger = logging.getLogger(__name__)

# 与 deskmate/roundtable/web.py:485、creation/service.py:713 同构的 SSE 帧格式。
def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(
    base_dir: Optional[str] = None,
    runtime: Any = None,
    base_prefix: str = "",
) -> FastAPI:
    """``runtime`` 可注入伪造的 BotRuntime（离线测试 / 桌面端接入用）。

    不传时走 ``rooms._default_runtime()`` 懒加载真 Agent，import 期不构造 provider。

    ``base_prefix`` 是**挂载前缀**（sidecar 里传 ``/bots``，独立 9921 页传空串）：
    Starlette Mount 会剥掉前缀，后端路由仍是 ``/api/...``，但页面里生成的 fetch URL
    必须带上它，否则会打到主 app 根路径 404。前缀由服务端注入 PAGE，不让 JS 猜。
    """
    app = FastAPI(title="Bot 群")
    store = RoomStore(base_dir or os.environ.get("DESKMATE_BOT_BASE"))
    _bp = str(base_prefix or "").strip().strip("/")
    prefix = "/" + _bp if _bp else ""
    page = PAGE.replace("__BOT_BASE__", json.dumps(prefix))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        from .runtime import model_choice

        model, source, reason = model_choice()
        return {
            "ok": True,
            "books": len(default_shelf().list_books()),
            "model": model or "(付费通道组兜底)",
            "model_source": source,
            "model_reason": reason,
        }

    @app.get("/api/bots")
    def bots() -> list[dict[str, Any]]:
        # icon / tagline 是展示字段，来自 registry 的 manifest frontmatter（上游透传）。
        # system_prompt / market 属身份 internals，**绝不**经这个端点外泄。
        return [
            {
                "id": b.id,
                "name": b.name,
                "role": b.role,
                "icon": b.icon,
                "tagline": b.tagline,
            }
            for b in list_bots()
        ]

    @app.get("/notebooks", response_class=HTMLResponse)
    def notebooks_page_route() -> str:
        return NOTEBOOKS_PAGE.replace("__BOT_BASE__", json.dumps(prefix))

    @app.get("/api/notebooks")
    def notebooks_index() -> dict[str, Any]:
        return {"items": list_notebooks(default_shelf())}

    @app.get("/api/notebooks/content")
    def notebook_content(book: str, expert: str) -> dict[str, Any]:
        text = read_expert_notebook(default_shelf(), book, expert)
        if text is None:
            raise HTTPException(status_code=404, detail="没有这本笔记")
        return {"book": book, "expert": expert, "text": text}

    @app.post("/api/notebooks/content")
    def notebook_save(body: NotebookSave) -> dict[str, Any]:
        ok, message = save_notebook(default_shelf(), body.book, body.expert, body.text)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        return {"ok": True}

    @app.delete("/api/notebooks")
    def notebook_delete(book: str, expert: str) -> dict[str, Any]:
        if not delete_notebook(default_shelf(), book, expert):
            raise HTTPException(status_code=404, detail="没有这本笔记")
        return {"ok": True}

    @app.get("/api/rooms")
    def room_list() -> list[dict[str, Any]]:
        rooms = store.list_rooms()
        previews = store.room_previews([r.room_id for r in rooms])
        return [
            {
                "room_id": r.room_id,
                "topic": r.topic,
                "bot_ids": r.bot_ids,
                "bot_names": r.bot_names,
                "created_at": r.created_at,
                **previews.get(r.room_id, {}),
            }
            for r in rooms
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
                # 用户驱动续轮：一次发言只跑 1 轮就交还控制权；上限仍由后端累计，
                # 用户点「再聊一轮」走 /continue，不再自动烧满 3 轮。
                async with contextlib.aclosing(
                    store.say_stream(room_id, body.text, runtime=runtime, rounds=1)
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

    @app.post("/api/rooms/{room_id}/continue")
    async def room_continue(
        room_id: str, body: Continue, request: Request
    ) -> StreamingResponse:
        """「再聊一轮」：不落新 user 消息，基于完整群历史再跑 1 轮串行接话。

        与 /stream 共用同一套 SSE 事件与前端渲染；群与共享材料都不重建。
        """
        if store.get_room(room_id) is None:
            raise HTTPException(status_code=404, detail="群不存在或已撤")

        async def gen():
            try:
                async with contextlib.aclosing(
                    store.continue_stream(
                        room_id,
                        body.round_index,
                        runtime=runtime,
                        bot_ids=body.bot_ids,
                    )
                ) as events:
                    async for ev in events:
                        if await request.is_disconnected():
                            logger.info("bot 群客户端断开，停止续轮 room=%s", room_id)
                            break
                        yield _sse(ev)
            except ValueError as exc:
                yield _sse({"type": "error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001 —— 不让异常把 SSE 连接打成半截
                logger.warning("bot 群续轮异常 room=%s: %s", room_id, exc)
                yield _sse({"type": "error", "message": "群聊服务暂时不可用"})

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
