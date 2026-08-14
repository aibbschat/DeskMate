#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「记忆图谱」独立页面（ui-mocks/memory-graph.html）。

数据源：~/.deskmate/memory/learned_history.jsonl（了解度 / 「我们的记忆」真实数据）。
把每条记忆做成节点，按 4 类（偏好 / 习惯 / 工具 / 其他）分色成暖色区域；
抽取出《书名》作为「作品 / 项目枢纽」，相关记忆以星团方式环绕——
这是一张真正有语义的「记忆知识图谱」，而非随机散点。

用法：python3 ui-mocks/gen_memory_graph.py
输出：ui-mocks/memory-graph.html（自包含，可直接双击打开或嵌入 App）。
"""

import json
import os
import re

SRC = os.path.expanduser("~/.deskmate/memory/learned_history.jsonl")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory-graph.html")

items = []
if os.path.exists(SRC):
    with open(SRC, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if "fact" in d:
                    items.append({
                        "ts": d.get("ts", ""),
                        "fact": d["fact"],
                        "cat": d.get("category", "other"),
                    })
            except Exception:
                pass

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>我们的记忆 · 墨灵记忆图谱</title>
<style>
  *{box-sizing:border-box;}
  html,body{height:100%;margin:0;}
  body{
    font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei",-apple-system,system-ui,sans-serif;
    color:#5b4636;
    background:
      radial-gradient(120% 90% at 22% 12%, #fff8ef 0%, #ffe9d4 38%, #f6dcc6 72%, #efcdb4 100%);
    overflow:hidden;
    -webkit-font-smoothing:antialiased;
  }
  #graph{position:fixed;inset:0;display:block;}

  .head{
    position:fixed;left:26px;top:22px;z-index:5;pointer-events:none;
    text-shadow:0 1px 0 rgba(255,255,255,.55);
  }
  .head h1{margin:0;font-size:26px;font-weight:800;letter-spacing:2px;color:#6b4a32;}
  .head .sub{margin-top:6px;font-size:13px;color:#9a7b5f;max-width:430px;line-height:1.6;}
  .head .count{margin-top:10px;font-size:13px;color:#b07a4e;font-weight:600;}
  .head .count b{font-size:20px;color:#c2662f;}

  .legend{
    position:fixed;left:26px;bottom:24px;z-index:5;
    background:rgba(255,250,243,.72);backdrop-filter:blur(8px);
    border:1px solid rgba(214,188,160,.55);border-radius:14px;
    padding:12px 14px;font-size:12.5px;color:#7a5f48;line-height:1.7;
    box-shadow:0 8px 26px rgba(180,130,90,.16);
  }
  .legend .row{display:flex;align-items:center;gap:8px;}
  .legend .dot{width:12px;height:12px;border-radius:50%;flex:0 0 auto;
    box-shadow:0 0 0 3px rgba(255,255,255,.5);}
  .legend .hub-note{margin-top:7px;color:#9a7b5f;font-size:11.5px;max-width:220px;}

  .controls{position:fixed;right:22px;top:22px;z-index:6;display:flex;gap:8px;flex-wrap:wrap;
    justify-content:flex-end;max-width:46vw;}
  .controls button{font-family:inherit;font-size:12px;color:#6b4a32;cursor:pointer;
    background:rgba(255,250,243,.82);border:1px solid rgba(214,188,160,.6);
    border-radius:10px;padding:7px 12px;transition:.18s;backdrop-filter:blur(6px);
    box-shadow:0 4px 14px rgba(180,130,90,.12);}
  .controls button:hover{background:#fff;transform:translateY(-1px);}
  .controls button.off{opacity:.4;}
  .controls .chip{display:flex;align-items:center;gap:6px;}
  .controls .chip .dot{width:10px;height:10px;border-radius:50%;}

  #tip{position:fixed;z-index:8;pointer-events:none;max-width:280px;
    background:rgba(60,42,30,.92);color:#fff7ec;font-size:12.5px;line-height:1.55;
    padding:9px 12px;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.25);
    opacity:0;transition:opacity .12s;transform:translate(-50%,-115%);}
  #tip .t-cat{font-size:11px;opacity:.72;margin-bottom:3px;}

  #panel{position:fixed;top:0;right:0;height:100%;width:360px;max-width:86vw;z-index:9;
    background:rgba(255,250,243,.96);backdrop-filter:blur(14px);
    border-left:1px solid rgba(214,188,160,.6);
    box-shadow:-14px 0 40px rgba(160,110,70,.18);
    transform:translateX(105%);transition:transform .32s cubic-bezier(.22,.9,.3,1);
    display:flex;flex-direction:column;padding:22px 22px 16px;}
  #panel.open{transform:translateX(0);}
  #panel .pcat{display:inline-flex;align-items:center;gap:7px;font-size:12px;
    font-weight:600;padding:4px 11px;border-radius:20px;color:#fff;}
  #panel .pdate{font-size:12px;color:#a38568;margin:14px 0 8px;}
  #panel .pfact{font-size:15px;line-height:1.75;color:#4d3a2a;}
  #panel .rel-title{margin:18px 0 8px;font-size:13px;font-weight:700;color:#6b4a32;}
  #panel .rel{display:flex;flex-direction:column;gap:7px;overflow-y:auto;padding-right:4px;}
  #panel .rel .item{font-size:12.5px;line-height:1.6;color:#6a5340;
    background:rgba(255,255,255,.6);border:1px solid rgba(214,188,160,.4);
    border-radius:9px;padding:8px 10px;cursor:pointer;transition:.15s;}
  #panel .rel .item:hover{background:#fff;border-color:#e0b98c;}
  #panel .close{position:absolute;top:16px;right:16px;width:30px;height:30px;
    border-radius:50%;border:none;background:rgba(214,188,160,.25);color:#6b4a32;
    font-size:16px;cursor:pointer;}
  #panel .close:hover{background:rgba(214,188,160,.45);}

  .mascot{position:fixed;right:24px;bottom:24px;z-index:5;width:56px;height:56px;
    border-radius:50%;background:radial-gradient(circle at 35% 30%,#ffe39c,#ffcf6b);
    box-shadow:0 8px 22px rgba(220,160,70,.4);display:flex;align-items:center;
    justify-content:center;font-size:26px;pointer-events:none;
    animation:bob 3.4s ease-in-out infinite;}
  @keyframes bob{0%,100%{transform:translateY(0);}50%{transform:translateY(-7px);}}-
</style>
</head>
<body>
<canvas id="graph"></canvas>

<div class="head">
  <h1>我们的记忆</h1>
  <div class="sub">这是墨灵一点一点，记住的你。<br/>散落在四个温柔的角落，被一本本作品轻轻串起。</div>
  <div class="count">共 <b id="cnt">0</b> 条记忆</div>
</div>

<div class="controls" id="controls"></div>

<div class="legend">
  <div class="row"><span class="dot" style="background:#ef7d9b"></span>偏好 · 你喜欢与不喜欢</div>
  <div class="row"><span class="dot" style="background:#f4a261"></span>习惯 · 你做事的方式</div>
  <div class="row"><span class="dot" style="background:#7fb069"></span>工具/项目 · 你在做的事</div>
  <div class="row"><span class="dot" style="background:#b9a7d6"></span>其他 · 零散的片段</div>
  <div class="hub-note">● 较大的带字节点 = 一部作品 / 项目，<br/>周围环绕着围绕它的记忆。</div>
</div>

<div id="tip"></div>

<div id="panel">
  <button class="close" id="panelClose">×</button>
  <div id="panelBody"></div>
</div>

<div class="mascot">🐾</div>

<script>
const DATA = /*__DATA__*/[];

const CATS = {
  preference:{name:"偏好", color:"#ef7d9b"},
  habit:{name:"习惯", color:"#f4a261"},
  tool:{name:"工具/项目", color:"#7fb069"},
  other:{name:"其他", color:"#b9a7d6"},
};
const REGIONS = {
  preference:{x:0.26,y:0.30},
  habit:{x:0.74,y:0.30},
  tool:{x:0.26,y:0.74},
  other:{x:0.74,y:0.74},
};

const titleRe = /《([^》]+)》/g;
function normTitle(t){
  t=(t||"").trim();
  const ci=t.indexOf("：");
  if(ci>-1) t=t.slice(0,ci);
  return t;
}
function extractTitles(fact){
  const set=new Set(); let m;
  titleRe.lastIndex=0;
  while((m=titleRe.exec(fact))){ set.add(normTitle(m[1])); }
  return [...set];
}

const nodes=[];
DATA.forEach((d,idx)=>{
  nodes.push({id:idx,type:"mem",cat:d.cat,fact:d.fact,ts:d.ts,
    titles:extractTitles(d.fact),deg:0,x:0,y:0,vx:0,vy:0});
});
const hubs=[]; const titleMap={};
nodes.forEach(n=>{
  n.titles.forEach(t=>{
    if(!(t in titleMap)){
      hubs.push({id:"h"+hubs.length,type:"hub",title:t,members:[],x:0,y:0,vx:0,vy:0,deg:0});
      titleMap[t]=hubs.length-1;
    }
    hubs[titleMap[t]].members.push(n.id);
  });
});
const all = nodes.concat(hubs);
const edges=[];
hubs.forEach(h=>{
  h.members.forEach(mid=>{
    edges.push({a:mid,b:h.id,kind:"title"});
    nodes[mid].deg++; h.deg++;
  });
});

document.getElementById("cnt").textContent = nodes.length;

function rand(a,b){return a+Math.random()*(b-a);}
const W=()=>window.innerWidth, H=()=>window.innerHeight;
function seed(){
  nodes.forEach(n=>{
    const r=REGIONS[n.cat]||REGIONS.other;
    n.x=W()*r.x+rand(-120,120); n.y=H()*r.y+rand(-90,90);
    n.vx=0; n.vy=0;
  });
  hubs.forEach(h=>{
    const first=nodes[h.members[0]];
    const r=REGIONS[first?first.cat:"other"];
    h.x=W()*r.x+rand(-40,40); h.y=H()*r.y+rand(-40,40);
    h.vx=0; h.vy=0;
  });
}
seed();

const cv=document.getElementById("graph");
const ctx=cv.getContext("2d");
let DPR=Math.min(2,window.devicePixelRatio||1);
function resize(){
  DPR=Math.min(2,window.devicePixelRatio||1);
  cv.width=W()*DPR; cv.height=H()*DPR;
  cv.style.width=W()+"px"; cv.style.height=H()+"px";
  ctx.setTransform(DPR,0,0,DPR,0,0);
}
resize();
window.addEventListener("resize",resize);

function nodeR(n){
  if(n.type==="hub") return 13+Math.min(16,n.members.length*2.2);
  return 4.2 + Math.min(7, n.titles.length*3);
}

let running=true;
const K_REP=1400, SPRING_K=0.035, REST=58, CENTER_K=0.0006, REGION_K=0.0022, DAMP=0.86;
const hidden=new Set();

const byIdMap={}; all.forEach(n=>byIdMap[n.id]=n);
function byId(id){return byIdMap[id];}
function hiddenKey(n){ return n.type==="mem" ? n.cat : "__hub"; }

function step(){
  for(const n of all){ if(hidden.has(hiddenKey(n))){ n.vx*=0.9; n.vy*=0.9; continue; } n.fx=0; n.fy=0; }
  for(let i=0;i<all.length;i++){
    const a=all[i]; if(hidden.has(hiddenKey(a))) continue;
    for(let j=i+1;j<all.length;j++){
      const b=all[j]; if(hidden.has(hiddenKey(b))) continue;
      let dx=a.x-b.x, dy=a.y-b.y; let d2=dx*dx+dy*dy;
      if(d2<1) d2=1; if(d2>90000) continue;
      const d=Math.sqrt(d2); const f=K_REP/d2;
      const fx=dx/d*f, fy=dy/d*f;
      a.fx+=fx; a.fy+=fy; b.fx-=fx; b.fy-=fy;
    }
  }
  for(const e of edges){
    const a=byId(e.a), b=byId(e.b);
    if(!a||!b) continue;
    if(hidden.has(hiddenKey(a))||hidden.has(hiddenKey(b))) continue;
    let dx=b.x-a.x, dy=b.y-a.y; const d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=SPRING_K*(d-REST);
    const fx=dx/d*f, fy=dy/d*f;
    a.fx+=fx; a.fy+=fy; b.fx-=fx; b.fy-=fy;
  }
  for(const n of all){
    if(hidden.has(hiddenKey(n))) continue;
    if(n.type==="mem"){
      const r=REGIONS[n.cat]||REGIONS.other;
      n.fx+=(W()*r.x-n.x)*REGION_K; n.fy+=(H()*r.y-n.y)*REGION_K;
    }
    n.fx+=(W()*0.5-n.x)*CENTER_K; n.fy+=(H()*0.5-n.y)*CENTER_K;
  }
  for(const n of all){
    if(n===dragNode) continue;
    if(hidden.has(hiddenKey(n))) continue;
    n.vx=(n.vx+n.fx)*DAMP; n.vy=(n.vy+n.fy)*DAMP;
    const sp=Math.hypot(n.vx,n.vy); if(sp>14){n.vx*=14/sp;n.vy*=14/sp;}
    n.x+=n.vx; n.y+=n.vy;
    const pad=40;
    n.x=Math.max(pad,Math.min(W()-pad,n.x));
    n.y=Math.max(pad,Math.min(H()-pad,n.y));
  }
}

let hoverNode=null, selNode=null, dragNode=null;
function roundRect(x,y,w,h,r){
  ctx.beginPath();
  ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r);
  ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath();
}
function neighbors(n){
  const s=new Set();
  if(!n) return s;
  if(n.type==="hub"){ n.members.forEach(m=>s.add(m)); }
  else { edges.forEach(e=>{ if(e.a===n.id)s.add(e.b); if(e.b===n.id)s.add(e.a); }); }
  return s;
}
function draw(){
  ctx.clearRect(0,0,W(),H());
  for(const e of edges){
    const a=byId(e.a), b=byId(e.b); if(!a||!b) continue;
    if(hidden.has(hiddenKey(a))||hidden.has(hiddenKey(b))) continue;
    const active=(selNode&&(selNode===a||selNode===b))||(hoverNode&&(hoverNode===a||hoverNode===b));
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y);
    ctx.strokeStyle = active? "rgba(214,140,70,.55)" : "rgba(210,170,130,.20)";
    ctx.lineWidth = active? 1.8 : 1.1;
    ctx.stroke();
  }
  for(const n of all){
    if(hidden.has(hiddenKey(n))) continue;
    const isNeighbor = selNode && (selNode===n || neighbors(selNode).has(n.id));
    const dim = (selNode && !isNeighbor);
    const r=nodeR(n)*(n===hoverNode?1.35:1);
    if(n.type==="hub"){
      ctx.save();
      ctx.shadowColor="rgba(220,160,70,.55)"; ctx.shadowBlur=16;
      ctx.beginPath(); ctx.arc(n.x,n.y,r,0,Math.PI*2);
      ctx.fillStyle="rgba(255,210,122,"+(dim?0.2:1)+")"; ctx.fill();
      ctx.restore();
      ctx.lineWidth=2; ctx.strokeStyle="rgba(200,130,50,"+(dim?0.3:0.9)+")"; ctx.stroke();
      ctx.font="600 12px PingFang SC, sans-serif";
      ctx.textAlign="center"; ctx.textBaseline="middle";
      const label="《"+n.title+"》";
      const tw=ctx.measureText(label).width;
      const bw=tw+14;
      ctx.fillStyle="rgba(255,250,243,"+(dim?0.4:0.92)+")";
      roundRect(n.x-bw/2,n.y-r-22,bw,18,9); ctx.fill();
      ctx.fillStyle=dim?"rgba(122,79,35,.4)":"#7a4f23"; ctx.fillText(label,n.x,n.y-r-13);
    }else{
      const c=CATS[n.cat].color;
      ctx.save();
      if(!dim){ctx.shadowColor=c; ctx.shadowBlur=8;}
      ctx.beginPath(); ctx.arc(n.x,n.y,r,0,Math.PI*2);
      ctx.globalAlpha = dim?0.18:1;
      ctx.fillStyle=c; ctx.fill();
      ctx.restore();
      if(n===selNode||n===hoverNode){
        ctx.beginPath(); ctx.arc(n.x,n.y,r+3,0,Math.PI*2);
        ctx.strokeStyle="rgba(120,80,50,.8)"; ctx.lineWidth=2; ctx.stroke();
      }
    }
  }
}
function loop(){
  if(running) step();
  draw();
  requestAnimationFrame(loop);
}
loop();

const tip=document.getElementById("tip");
function pickNode(mx,my){
  let best=null,bd=1e9;
  for(const n of all){
    if(hidden.has(hiddenKey(n))) continue;
    const r=nodeR(n)+5; const d=Math.hypot(n.x-mx,n.y-my);
    if(d<r&&d<bd){bd=d;best=n;}
  }
  return best;
}
cv.addEventListener("mousemove",e=>{
  const mx=e.clientX,my=e.clientY;
  if(dragNode){ dragNode.x=mx; dragNode.y=my; dragNode.vx=0; dragNode.vy=0; return; }
  const n=pickNode(mx,my);
  hoverNode=n;
  cv.style.cursor=n?"pointer":"default";
  if(n){
    tip.style.opacity="1";
    tip.style.left=mx+"px"; tip.style.top=my+"px";
    if(n.type==="hub"){
      tip.innerHTML='<div class="t-cat">作品 / 项目枢纽</div>《'+n.title+'》<br/>围绕它的记忆 '+n.members.length+' 条';
    }else{
      const c=CATS[n.cat];
      tip.innerHTML='<div class="t-cat" style="color:'+c.color+'">'+c.name+'</div>'+
        (n.fact.length>90?n.fact.slice(0,90)+"…":n.fact);
    }
  }else{ tip.style.opacity="0"; }
});
cv.addEventListener("mouseleave",()=>{tip.style.opacity="0";hoverNode=null;});

cv.addEventListener("mousedown",e=>{ dragNode=pickNode(e.clientX,e.clientY); });
window.addEventListener("mouseup",()=>{dragNode=null;});
cv.addEventListener("click",e=>{
  const n=pickNode(e.clientX,e.clientY);
  if(n){ openPanel(n); } else { closePanel(); }
});

const panel=document.getElementById("panel");
const panelBody=document.getElementById("panelBody");
function fmtDate(ts){
  if(!ts) return "";
  const m=/^(\d{4}-\d{2}-\d{2})T?(\d{2}:\d{2})?/.exec(ts);
  if(m) return (m[1]||"")+(m[2]?" "+m[2]:"");
  return ts;
}
function relatedTo(n){
  const out=[];
  if(n.type==="hub"){
    n.members.forEach(mid=>{ out.push(byId(mid)); });
  }else{
    const hubIds=edges.filter(e=>e.a===n.id||e.b===n.id).map(e=>e.a===n.id?e.b:e.a);
    hubIds.forEach(hid=>{ const h=byId(hid); if(h) h.members.forEach(mid=>{ if(mid!==n.id) out.push(byId(mid)); }); });
    const seen=new Set([n.id]);
    return out.filter(x=>x&&!seen.has(x.id)&&seen.add(x.id));
  }
  return out;
}
function openPanel(n){
  selNode=n;
  const c = n.type==="hub" ? {name:"作品 / 项目枢纽",color:"#c8923a"} : CATS[n.cat];
  let html='<span class="pcat" style="background:'+c.color+'">'+c.name+'</span>';
  if(n.type==="hub"){
    html+='<div class="pdate">围绕它的记忆 '+n.members.length+' 条</div>';
    html+='<div class="pfact">《'+n.title+'》</div>';
  }else{
    html+='<div class="pdate">'+fmtDate(n.ts)+'</div>';
    html+='<div class="pfact">'+escapeHtml(n.fact)+'</div>';
    const rel=relatedTo(n);
    if(rel.length){
      html+='<div class="rel-title">与它相关的记忆（'+rel.length+'）</div><div class="rel">';
      rel.slice(0,12).forEach(r=>{
        html+='<div class="item" data-id="'+r.id+'">· '+escapeHtml(r.fact.length>60?r.fact.slice(0,60)+"…":r.fact)+'</div>';
      });
      html+='</div>';
    }
  }
  panelBody.innerHTML=html;
  panel.classList.add("open");
  panelBody.querySelectorAll(".item").forEach(el=>{
    el.addEventListener("click",()=>{ const id=parseInt(el.getAttribute("data-id")); const t=byId(id); if(t) openPanel(t); });
  });
}
function escapeHtml(s){ return (s||"").replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m])); }
function closePanel(){ selNode=null; panel.classList.remove("open"); }
document.getElementById("panelClose").addEventListener("click",closePanel);

// ---- 控制条：分类筛选 + 重置 ----
const controls=document.getElementById("controls");
function makeBtn(label,color,key){
  const b=document.createElement("button");
  b.className="chip";
  if(color) b.innerHTML='<span class="dot" style="background:'+color+'"></span>'+label;
  else b.textContent=label;
  b.dataset.key=key;
  b.addEventListener("click",()=>{
    if(key==="reset"){ hidden.clear(); document.querySelectorAll(".controls button").forEach(x=>x.classList.remove("off")); return; }
    if(hidden.has(key)){ hidden.delete(key); b.classList.remove("off"); }
    else { hidden.add(key); b.classList.add("off"); }
  });
  return b;
}
controls.appendChild(makeBtn("偏好","#ef7d9b","preference"));
controls.appendChild(makeBtn("习惯","#f4a261","habit"));
controls.appendChild(makeBtn("工具","#7fb069","tool"));
controls.appendChild(makeBtn("其他","#b9a7d6","other"));
controls.appendChild(makeBtn("全部显示","","reset"));
</script>
</body>
</html>
"""

html = TEMPLATE.replace("/*__DATA__*/[]", json.dumps(items, ensure_ascii=False))
with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)

title_re = re.compile(r"《([^》]+)》")
def _norm(t):
    t = t.strip(); ci = t.find("：")
    return t[:ci] if ci > -1 else t
_hubs = set()
for it in items:
    _hubs.update(_norm(m) for m in title_re.findall(it["fact"]))
print(f"已生成 {OUT}")
print(f"  记忆节点: {len(items)} 条")
print(f"  作品/项目枢纽: {len(_hubs)} 个（运行时按《》动态抽取）")
