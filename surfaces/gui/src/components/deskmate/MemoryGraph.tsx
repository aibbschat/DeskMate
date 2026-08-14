/**
 * MemoryGraph — 「我们的记忆」力导向知识图谱（canvas 实现）。
 *
 * 数据由 props.items 传入（来自后端 /v1/deskmate/learned-history 实时拉取）。
 * 把每条记忆做成节点，按 4 类（偏好/习惯/工具/其他）分色成暖色区域；
 * 抽取《书名/项目》作为「作品枢纽」，相关记忆以星团方式环绕——一张有语义的
 * 知识图谱，而非随机散点。悬停看内容、点击触发 onSelect 给外层详情面板。
 *
 * 移植自 ui-mocks/memory-graph.html（已在 Node + DOM 桩真跑验证无 TDZ）。
 */
import { useEffect, useRef } from "react";

export interface MemItem {
  ts: string;
  fact: string;
  category: string;
}

export const CATEGORY_META: Record<string, { name: string; color: string }> = {
  preference: { name: "偏好", color: "#ef7d9b" },
  habit: { name: "习惯", color: "#f4a261" },
  tool: { name: "工具/项目", color: "#7fb069" },
  other: { name: "其他", color: "#b9a7d6" },
};

// 四类的温和区域（相对画布比例）
const REGIONS: Record<string, { x: number; y: number }> = {
  preference: { x: 0.26, y: 0.3 },
  habit: { x: 0.74, y: 0.3 },
  tool: { x: 0.26, y: 0.74 },
  other: { x: 0.74, y: 0.74 },
};

interface Node {
  id: number;
  type: "mem" | "hub";
  cat: string;
  fact: string;
  ts: string;
  titles: string[];
  members?: number[];
  deg: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface Props {
  items: MemItem[];
  hiddenCats?: Set<string>;
  selectedIndex?: number | null;
  onSelect?: (info: { item: MemItem; related: MemItem[] } | null) => void;
  onHover?: (text: string | null) => void;
}

const titleRe = /《([^》]+)》/g;
function normTitle(t: string): string {
  t = (t || "").trim();
  const ci = t.indexOf("：");
  return ci > -1 ? t.slice(0, ci) : t;
}
function extractTitles(fact: string): string[] {
  const set = new Set<string>();
  let m: RegExpExecArray | null;
  titleRe.lastIndex = 0;
  while ((m = titleRe.exec(fact)) !== null) set.add(normTitle(m[1]));
  return [...set];
}

export function MemoryGraph({ items, hiddenCats, selectedIndex, onSelect, onHover }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const hiddenRef = useRef<Set<string>>(hiddenCats ?? new Set());
  const selRef = useRef<number | null>(selectedIndex ?? null);
  const onSelectRef = useRef(onSelect);
  const onHoverRef = useRef(onHover);

  // 用 ref 持有最新 props，避免重建物理模拟
  hiddenRef.current = hiddenCats ?? new Set();
  selRef.current = selectedIndex ?? null;
  onSelectRef.current = onSelect;
  onHoverRef.current = onHover;

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    const W = () => cv.clientWidth;
    const H = () => cv.clientHeight;

    // ---- 构建节点 ----
    const nodes: Node[] = items.map((d, idx) => ({
      id: idx,
      type: "mem",
      cat: d.category || "other",
      fact: d.fact,
      ts: d.ts,
      titles: extractTitles(d.fact),
      deg: 0,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
    }));
    const hubs: Node[] = [];
    const titleMap: Record<string, number> = {};
    nodes.forEach((n) => {
      n.titles.forEach((t) => {
        if (!(t in titleMap)) {
          hubs.push({
            id: -1 - hubs.length,
            type: "hub",
            cat: n.cat,
            fact: "",
            ts: "",
            titles: [t],
            members: [],
            deg: 0,
            x: 0,
            y: 0,
            vx: 0,
            vy: 0,
          });
          titleMap[t] = hubs.length - 1;
        }
        hubs[titleMap[t]].members!.push(n.id);
      });
    });
    const all = nodes.concat(hubs);
    const edges: { a: number; b: number }[] = [];
    hubs.forEach((h) => {
      h.members!.forEach((mid) => {
        edges.push({ a: mid, b: h.id });
        nodes[mid].deg++;
        h.deg++;
      });
    });
    const byId = new Map<number, Node>();
    all.forEach((n) => byId.set(n.id, n));

    const rand = (a: number, b: number) => a + Math.random() * (b - a);
    function seed() {
      nodes.forEach((n) => {
        const r = REGIONS[n.cat] || REGIONS.other;
        n.x = W() * r.x + rand(-120, 120);
        n.y = H() * r.y + rand(-90, 90);
        n.vx = 0;
        n.vy = 0;
      });
      hubs.forEach((h) => {
        const first = nodes[h.members![0]];
        const r = REGIONS[first ? first.cat : "other"];
        h.x = W() * r.x + rand(-40, 40);
        h.y = H() * r.y + rand(-40, 40);
        h.vx = 0;
        h.vy = 0;
      });
    }
    seed();

    function nodeR(n: Node): number {
      if (n.type === "hub") return 13 + Math.min(16, (n.members?.length ?? 0) * 2.2);
      return 4.2 + Math.min(7, n.titles.length * 3);
    }

    function isHidden(n: Node): boolean {
      if (n.type === "hub") return false;
      return hiddenRef.current.has(n.cat);
    }

    function neighbors(n: Node): Set<number> {
      const s = new Set<number>();
      if (n.type === "hub") n.members!.forEach((m) => s.add(m));
      else
        edges.forEach((e) => {
          if (e.a === n.id) s.add(e.b);
          if (e.b === n.id) s.add(e.a);
        });
      return s;
    }

    // ---- 物理模拟 ----
    const K_REP = 1400;
    const SPRING_K = 0.035;
    const REST = 58;
    const CENTER_K = 0.0006;
    const REGION_K = 0.0022;
    const DAMP = 0.86;

    function step() {
      const fxArr = new Map<number, number>();
      const fyArr = new Map<number, number>();
      all.forEach((n) => {
        fxArr.set(n.id, 0);
        fyArr.set(n.id, 0);
      });
      for (let i = 0; i < all.length; i++) {
        const a = all[i];
        if (isHidden(a)) continue;
        for (let j = i + 1; j < all.length; j++) {
          const b = all[j];
          if (isHidden(b)) continue;
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 1) d2 = 1;
          if (d2 > 90000) continue;
          const d = Math.sqrt(d2);
          const f = K_REP / d2;
          const fx = (dx / d) * f;
          const fy = (dy / d) * f;
          fxArr.set(a.id, fxArr.get(a.id)! + fx);
          fyArr.set(a.id, fyArr.get(a.id)! + fy);
          fxArr.set(b.id, fxArr.get(b.id)! - fx);
          fyArr.set(b.id, fyArr.get(b.id)! - fy);
        }
      }
      for (const e of edges) {
        const a = byId.get(e.a)!;
        const b = byId.get(e.b)!;
        if (isHidden(a) || isHidden(b)) continue;
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const f = SPRING_K * (d - REST);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        fxArr.set(a.id, fxArr.get(a.id)! + fx);
        fyArr.set(a.id, fyArr.get(a.id)! + fy);
        fxArr.set(b.id, fxArr.get(b.id)! - fx);
        fyArr.set(b.id, fyArr.get(b.id)! - fy);
      }
      for (const n of all) {
        if (isHidden(n)) continue;
        if (n.type === "mem") {
          const r = REGIONS[n.cat] || REGIONS.other;
          fxArr.set(n.id, fxArr.get(n.id)! + (W() * r.x - n.x) * REGION_K);
          fyArr.set(n.id, fyArr.get(n.id)! + (H() * r.y - n.y) * REGION_K);
        }
        fxArr.set(n.id, fxArr.get(n.id)! + (W() * 0.5 - n.x) * CENTER_K);
        fyArr.set(n.id, fyArr.get(n.id)! + (H() * 0.5 - n.y) * CENTER_K);
      }
      for (const n of all) {
        if (isHidden(n)) {
          n.vx *= 0.9;
          n.vy *= 0.9;
          continue;
        }
        n.vx = (n.vx + fxArr.get(n.id)!) * DAMP;
        n.vy = (n.vy + fyArr.get(n.id)!) * DAMP;
        const sp = Math.hypot(n.vx, n.vy);
        if (sp > 14) {
          n.vx *= 14 / sp;
          n.vy *= 14 / sp;
        }
        n.x += n.vx;
        n.y += n.vy;
        const pad = 40;
        n.x = Math.max(pad, Math.min(W() - pad, n.x));
        n.y = Math.max(pad, Math.min(H() - pad, n.y));
      }
    }

    // ---- 渲染 ----
    let hoverNode: Node | null = null;
    let dragNode: Node | null = null;
    let selNode: Node | null = selRef.current != null ? byId.get(selRef.current) ?? null : null;

    function roundRect(x: number, y: number, w: number, h: number, r: number) {
      ctx!.beginPath();
      ctx!.moveTo(x + r, y);
      ctx!.arcTo(x + w, y, x + w, y + h, r);
      ctx!.arcTo(x + w, y + h, x, y + h, r);
      ctx!.arcTo(x, y + h, x, y, r);
      ctx!.arcTo(x, y, x + w, y, r);
      ctx!.closePath();
    }

    function draw() {
      const w = W();
      const h = H();
      ctx!.clearRect(0, 0, w, h);
      // 边
      for (const e of edges) {
        const a = byId.get(e.a)!;
        const b = byId.get(e.b)!;
        if (isHidden(a) || isHidden(b)) continue;
        const active =
          (selNode && (selNode === a || selNode === b)) ||
          (hoverNode && (hoverNode === a || hoverNode === b));
        ctx!.beginPath();
        ctx!.moveTo(a.x, a.y);
        ctx!.lineTo(b.x, b.y);
        ctx!.strokeStyle = active ? "rgba(214,140,70,0.55)" : "rgba(210,170,130,0.20)";
        ctx!.lineWidth = active ? 1.8 : 1.1;
        ctx!.stroke();
      }
      // 节点
      for (const n of all) {
        if (isHidden(n)) continue;
        const dim = !!(
          selNode &&
          n !== selNode &&
          !neighbors(selNode).has(n.id) &&
          n.type === "mem"
        );
        const r = nodeR(n) * (n === hoverNode ? 1.35 : 1);
        if (n.type === "hub") {
          ctx!.save();
          ctx!.shadowColor = "rgba(220,160,70,0.55)";
          ctx!.shadowBlur = 16;
          ctx!.beginPath();
          ctx!.arc(n.x, n.y, r, 0, Math.PI * 2);
          ctx!.fillStyle = "#ffd27a";
          ctx!.fill();
          ctx!.restore();
          ctx!.lineWidth = 2;
          ctx!.strokeStyle = "rgba(200,130,50,0.9)";
          ctx!.stroke();
          ctx!.font = "600 12px PingFang SC, sans-serif";
          ctx!.textAlign = "center";
          ctx!.textBaseline = "middle";
          const label = "《" + n.titles[0] + "》";
          const tw = ctx!.measureText(label).width;
          const bw = tw + 14;
          ctx!.fillStyle = "rgba(255,250,243,0.92)";
          roundRect(n.x - bw / 2, n.y - r - 22, bw, 18, 9);
          ctx!.fill();
          ctx!.fillStyle = "#7a4f23";
          ctx!.fillText(label, n.x, n.y - r - 13);
        } else {
          const c = CATEGORY_META[n.cat]?.color ?? "#b9a7d6";
          ctx!.save();
          if (!dim) {
            ctx!.shadowColor = c;
            ctx!.shadowBlur = 8;
          }
          ctx!.beginPath();
          ctx!.arc(n.x, n.y, r, 0, Math.PI * 2);
          ctx!.globalAlpha = dim ? 0.18 : 1;
          ctx!.fillStyle = c;
          ctx!.fill();
          ctx!.restore();
          if (n === selNode || n === hoverNode) {
            ctx!.beginPath();
            ctx!.arc(n.x, n.y, r + 3, 0, Math.PI * 2);
            ctx!.strokeStyle = "rgba(120,80,50,0.8)";
            ctx!.lineWidth = 2;
            ctx!.stroke();
          }
        }
      }
    }

    function pickNode(mx: number, my: number): Node | null {
      let best: Node | null = null;
      let bd = 1e9;
      for (const n of all) {
        if (isHidden(n)) continue;
        const r = nodeR(n) + 5;
        const d = Math.hypot(n.x - mx, n.y - my);
        if (d < r && d < bd) {
          bd = d;
          best = n;
        }
      }
      return best;
    }

    let raf = 0;
    let running = true;
    function loop() {
      if (running) step();
      draw();
      raf = requestAnimationFrame(loop);
    }

    function resize() {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      cv!.width = cv!.clientWidth * dpr;
      cv!.height = cv!.clientHeight * dpr;
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    window.addEventListener("resize", resize);
    loop();

    // 交互
    function relPos(e: MouseEvent) {
      const rect = cv!.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }
    cv.addEventListener("mousemove", (e) => {
      const { x, y } = relPos(e);
      if (dragNode) {
        dragNode.x = x;
        dragNode.y = y;
        dragNode.vx = 0;
        dragNode.vy = 0;
        return;
      }
      const n = pickNode(x, y);
      hoverNode = n;
      cv.style.cursor = n ? "pointer" : "default";
      if (n) {
        onHoverRef.current?.(
          n.type === "hub"
            ? `《${n.titles[0]}》 · 围绕它的记忆 ${n.members!.length} 条`
            : (n.fact.length > 90 ? n.fact.slice(0, 90) + "…" : n.fact),
        );
      } else {
        onHoverRef.current?.(null);
      }
    });
    cv.addEventListener("mouseleave", () => {
      hoverNode = null;
      onHoverRef.current?.(null);
    });
    cv.addEventListener("mousedown", (e) => {
      const { x, y } = relPos(e);
      dragNode = pickNode(x, y);
    });
    window.addEventListener("mouseup", () => {
      dragNode = null;
    });
    cv.addEventListener("click", (e) => {
      const { x, y } = relPos(e);
      const n = pickNode(x, y);
      if (n && n.type === "mem") {
        selNode = n;
        const related = [...neighbors(n)]
          .map((id) => byId.get(id)!)
          .filter((m) => m.type === "mem") as Node[];
        onSelectRef.current?.({
          item: { ts: n.ts, fact: n.fact, category: n.cat },
          related: related.map((m) => ({ ts: m.ts, fact: m.fact, category: m.cat })),
        });
      } else if (n && n.type === "hub") {
        selNode = n;
        onSelectRef.current?.({
          item: { ts: "", fact: `《${n.titles[0]}》 · 围绕它的记忆 ${n.members!.length} 条`, category: "other" },
          related: n.members!.map((id) => {
            const m = byId.get(id)!;
            return { ts: m.ts, fact: m.fact, category: m.cat };
          }),
        });
      } else {
        selNode = null;
        onSelectRef.current?.(null);
      }
    });

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
    };
  }, [items]);

  return <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block" }} />;
}
