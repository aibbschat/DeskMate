/**
 * MemoryGraph — 「我们的记忆」星尘夜空（canvas 实现，2026-11-03 改版）。
 *
 * 旧版力导向在 1298 条时退化成标签打架的毛线球（每个 hub 无条件画药丸 +
 * 逐帧 O(n²) 斥力）。星尘模式的规则（mock：ui-mocks/我们的记忆_星尘夜空_2026-11-03.html）：
 * - 每条记忆 = 一颗星（类别着色，轻轻呼吸）；一本作品 = 一片星群
 *   （成员星高斯环绕一颗亮主星）；没作品归属的记忆是四团类别星砂云。
 * - 默认全空皆星、只有大星群挂名（n≥26 / 聚焦 / 悬停 / 放大到 1.8x 才出名字），
 *   连线只在点进星群时才亮 —— 平时天空是安静的。
 * - 布局是确定性的（mulberry32 定种随机），不再跑逐帧力导向，1300 星也满帧；
 *   滚轮缩放 + 拖拽平移负责「凑近看」，点主星/成员星负责「聚焦 + 看清单」。
 *
 * 交互契约（与外层 MemoryGraphPage 对齐）：
 * - onSelect：点成员星 → 该条记忆 + 同群相关；点主星 → 星群摘要（group 字段）；
 *   点空白 → null（退出聚焦）。
 * - onHover：悬停文本（外层做 tooltip）。
 * - resetSignal 变化 → 复位缩放/平移/聚焦（外层「复位星空」与面包屑「整片夜空」）。
 */
import { useEffect, useRef } from "react";

export interface MemItem {
  ts: string;
  fact: string;
  category: string;
}

/* 类别色 = 暮色底上的柔光版（2026-11-03 星尘改版；图例/抽屉共用这一真源）。 */
export const CATEGORY_META: Record<string, { name: string; color: string }> = {
  preference: { name: "偏好", color: "#ff9ec4" },
  habit: { name: "习惯", color: "#ffc36b" },
  tool: { name: "工具/项目", color: "#8fe3a8" },
  other: { name: "其他", color: "#b9a8ff" },
};
const CAT_KEYS = Object.keys(CATEGORY_META);

export interface SelectInfo {
  item: MemItem;
  related: MemItem[];
  /** 点主星进来的星群摘要（外层抽屉据此显示群清单而非单条）。 */
  group?: { title: string; count: number };
}

interface Props {
  items: MemItem[];
  hiddenCats?: Set<string>;
  selectedIndex?: number | null;
  onSelect?: (info: SelectInfo | null) => void;
  onHover?: (text: string | null) => void;
  resetSignal?: number;
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

/* 定种随机（每次重建布局一致，星空不会刷新就变样） */
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Star {
  x: number; y: number; r: number; cat: string;
  item: number;   // items 下标（散星也指回原条）
  group: number;  // 所属星群下标；-1 = 散星
}
interface Group {
  title: string; x: number; y: number; mainR: number;
  members: number[]; // stars 下标
}

export function MemoryGraph({ items, hiddenCats, onSelect, onHover, resetSignal }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const hiddenRef = useRef<Set<string>>(hiddenCats ?? new Set());
  const onSelectRef = useRef(onSelect);
  const onHoverRef = useRef(onHover);

  // 用 ref 持有最新 props，避免重建星空
  hiddenRef.current = hiddenCats ?? new Set();
  onSelectRef.current = onSelect;
  onHoverRef.current = onHover;

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    const W = () => cv.clientWidth;
    const H = () => cv.clientHeight;

    /* ── 建天：作品→星群，散记忆→星砂云 ── */
    let stars: Star[] = [];
    let groups: Group[] = [];
    const phase = new Float32Array(4096);

    function buildSky() {
      const rnd = mulberry32(20261103);
      const gauss = () => (rnd() + rnd() + rnd() - 1.5) / 1.5;
      stars = []; groups = [];
      for (let i = 0; i < phase.length; i++) phase[i] = rnd() * Math.PI * 2;

      // 主归属 = fact 里第一个《书名》
      const byTitle = new Map<string, number[]>();
      const primary = new Map<number, string>();
      items.forEach((d, i) => {
        const ts = extractTitles(d.fact);
        if (!ts.length) return;
        const p = ts[0];
        primary.set(i, p);
        if (!byTitle.has(p)) byTitle.set(p, []);
        byTitle.get(p)!.push(i);
      });

      const cx = W() * 0.5, cy = H() * 0.54;
      const maxR = Math.min(W(), H()) * 0.42;
      const sorted = [...byTitle.entries()].sort((a, b) => b[1].length - a[1].length);
      sorted.forEach(([title, memberItems], k) => {
        const ang = (k + 1) * 2.399963;                    // 黄金角螺旋
        const rr = maxR * Math.sqrt((k + 1) / sorted.length);
        const gx = cx + Math.cos(ang) * rr * 1.32;
        const gy = cy + Math.sin(ang) * rr * 0.92;
        const spread = 14 + Math.sqrt(memberItems.length) * 4.2;
        const gi = groups.length;
        const members: number[] = [];
        for (const ii of memberItems) {
          const a2 = rnd() * Math.PI * 2, d = Math.abs(gauss()) * spread;
          members.push(stars.length);
          stars.push({
            x: gx + Math.cos(a2) * d, y: gy + Math.sin(a2) * d * 0.86,
            r: 1.1 + rnd() * 1.5, cat: items[ii].category || "other",
            item: ii, group: gi,
          });
        }
        groups.push({
          title, x: gx, y: gy,
          mainR: 2.6 + Math.min(4.5, Math.sqrt(memberItems.length) * 0.42),
          members,
        });
      });

      // 散星云：无书名归属的记忆，按类别聚在四角（低密度星砂）
      const spots = [
        { x: W() * 0.12, y: H() * 0.30 }, { x: W() * 0.88, y: H() * 0.24 },
        { x: W() * 0.10, y: H() * 0.78 }, { x: W() * 0.90, y: H() * 0.72 },
      ];
      items.forEach((d, i) => {
        if (primary.has(i)) return;
        const ci = Math.max(0, CAT_KEYS.indexOf(d.category || "other"));
        const p = spots[ci];
        const a = rnd() * Math.PI * 2, dd = Math.abs(gauss()) * 120;
        stars.push({
          x: p.x + Math.cos(a) * dd, y: p.y + Math.sin(a) * dd * 0.7,
          r: 0.8 + rnd() * 1.1, cat: CAT_KEYS[ci], item: i, group: -1,
        });
      });
    }
    buildSky();

    /* ── 视图（缩放/平移/聚焦） ── */
    const view = { x: 0, y: 0, k: 1 };
    let focus = -1;
    let hoverStar = -1, hoverGroup = -1;
    const drag = { active: false, x: 0, y: 0, ox: 0, oy: 0 };
    let moved = false;

    const toS = (p: { x: number; y: number }) => ({ x: p.x * view.k + view.x, y: p.y * view.k + view.y });
    const toW = (sx: number, sy: number) => ({ x: (sx - view.x) / view.k, y: (sy - view.y) / view.k });

    function setFocus(i: number) {
      focus = i;
      if (i < 0) onSelectRef.current?.(null);
      else {
        const g = groups[i];
        onSelectRef.current?.({
          item: { ts: "", fact: `《${g.title}》 · 这片星群 ${g.members.length} 颗星`, category: "other" },
          related: g.members.slice(0, 30).map((si) => items[stars[si].item]),
          group: { title: g.title, count: g.members.length },
        });
      }
    }
    // 外层复位信号入口（挂在 canvas，避免多余 state）
    (cv as HTMLCanvasElement & { __dmReset?: () => void }).__dmReset = () => {
      view.x = 0; view.y = 0; view.k = 1;
      if (focus >= 0) setFocus(-1);
    };

    /* ── 渲染（每帧：呼吸闪烁） ── */
    function draw(t: number) {
      const w = W(), h = H();
      ctx!.clearRect(0, 0, w, h);
      const fg = focus >= 0;

      // 星群连线：只在聚焦时亮（平时天空是安静的）
      if (fg) {
        const g = groups[focus];
        const gp = toS(g);
        ctx!.strokeStyle = "rgba(255,220,170,0.30)"; ctx!.lineWidth = 1;
        for (const si of g.members) {
          const s = stars[si];
          if (hiddenRef.current.has(s.cat)) continue;
          const p = toS(s);
          ctx!.beginPath(); ctx!.moveTo(gp.x, gp.y); ctx!.lineTo(p.x, p.y); ctx!.stroke();
        }
      }

      // 星
      const sizeK = Math.min(2, 0.7 + view.k * 0.5);
      for (let i = 0; i < stars.length; i++) {
        const s = stars[i];
        if (hiddenRef.current.has(s.cat)) continue;
        const inFocus = fg && s.group === focus;
        if (fg && !inFocus) continue;                        // 聚焦：他群星隐去
        if (fg && s.group === -1 && view.k < 1.6) continue;  // 散星只在近看时出现
        const p = toS(s);
        if (p.x < -8 || p.x > w + 8 || p.y < -8 || p.y > h + 8) continue;
        const a = (0.82 + 0.18 * Math.sin(t * 0.0011 + phase[i % 4096])) * (s.group === -1 ? 0.7 : 1);
        ctx!.save();
        ctx!.globalAlpha = a;
        ctx!.shadowColor = CATEGORY_META[s.cat]?.color ?? "#b9a8ff";
        ctx!.shadowBlur = i === hoverStar ? 10 : 5;
        ctx!.fillStyle = CATEGORY_META[s.cat]?.color ?? "#b9a8ff";
        ctx!.beginPath(); ctx!.arc(p.x, p.y, s.r * sizeK, 0, Math.PI * 2); ctx!.fill();
        ctx!.restore();
      }

      // 主星 + 星群名（标签只在「够大 / 聚焦 / 悬停 / 放大」时出 —— 不再打架的根）
      groups.forEach((g, i) => {
        if (fg && i !== focus) return;
        const p = toS(g);
        if (p.x < -60 || p.x > w + 60 || p.y < -60 || p.y > h + 60) return;
        ctx!.save();
        const gr = ctx!.createRadialGradient(p.x, p.y, 1, p.x, p.y, g.mainR * 4);
        gr.addColorStop(0, "rgba(255,236,200,0.95)");
        gr.addColorStop(1, "rgba(255,236,200,0)");
        ctx!.fillStyle = gr;
        ctx!.beginPath(); ctx!.arc(p.x, p.y, g.mainR * 4, 0, Math.PI * 2); ctx!.fill();
        ctx!.fillStyle = "#fff3dd";
        ctx!.shadowColor = "#ffcf8a"; ctx!.shadowBlur = i === focus ? 18 : 12;
        ctx!.beginPath(); ctx!.arc(p.x, p.y, g.mainR * sizeK, 0, Math.PI * 2); ctx!.fill();
        ctx!.restore();
        if (g.members.length >= 26 || i === focus || view.k >= 1.8 || i === hoverGroup) {
          ctx!.font = "600 12px PingFang SC, sans-serif";
          ctx!.textAlign = "center"; ctx!.textBaseline = "top";
          ctx!.fillStyle = i === focus ? "#ffd9a0" : "rgba(246,234,214,0.82)";
          ctx!.shadowColor = "rgba(20,14,40,0.9)"; ctx!.shadowBlur = 6;
          ctx!.fillText(`《${g.title}》 · ${g.members.length}`, p.x, p.y + g.mainR * 2 + 8);
          ctx!.shadowBlur = 0;
        } else {
          ctx!.font = "500 10px PingFang SC, sans-serif";
          ctx!.textAlign = "center"; ctx!.textBaseline = "middle";
          ctx!.fillStyle = "rgba(255,243,221,0.85)";
          ctx!.fillText(String(g.members.length), p.x, p.y);
        }
      });
    }

    let raf = 0;
    let running = true;
    const loop = (t: number) => { if (!running) return; draw(t); raf = requestAnimationFrame(loop); };

    function resize() {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      cv!.width = cv!.clientWidth * dpr;
      cv!.height = cv!.clientHeight * dpr;
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
      buildSky();
    }
    resize();
    window.addEventListener("resize", resize);
    raf = requestAnimationFrame(loop);

    /* ── 命中 ── */
    function pick(sx: number, sy: number): { kind: "group" | "star"; i: number } | null {
      const wpt = toW(sx, sy);
      for (let i = groups.length - 1; i >= 0; i--) {
        const g = groups[i];
        if (Math.hypot(g.x - wpt.x, g.y - wpt.y) < (g.mainR + 6) / view.k) return { kind: "group", i };
      }
      let best = -1, bd = 1e9;
      for (let i = 0; i < stars.length; i++) {
        const s = stars[i];
        if (hiddenRef.current.has(s.cat)) continue;
        if (focus >= 0 && s.group !== focus) continue;
        const d = Math.hypot(s.x - wpt.x, s.y - wpt.y);
        if (d < 7 / view.k && d < bd) { bd = d; best = i; }
      }
      return best >= 0 ? { kind: "star", i: best } : null;
    }

    function relPos(e: MouseEvent) {
      const rect = cv!.getBoundingClientRect();
      return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }
    const onMove = (e: MouseEvent) => {
      const { x, y } = relPos(e);
      if (drag.active) {
        view.x += x - drag.x; view.y += y - drag.y;
        drag.x = x; drag.y = y;
        if (Math.hypot(x - drag.ox, y - drag.oy) > 3) moved = true;
        return;
      }
      const h = pick(x, y);
      hoverStar = h?.kind === "star" ? h.i : -1;
      hoverGroup = h?.kind === "group" ? h.i : -1;
      cv!.style.cursor = h ? "pointer" : "grab";
      if (h && h.kind === "star") {
        const f = items[stars[h.i].item].fact;
        onHoverRef.current?.(f.length > 90 ? f.slice(0, 90) + "…" : f);
      } else if (h && h.kind === "group") {
        const g = groups[h.i];
        onHoverRef.current?.(`《${g.title}》星群 · ${g.members.length} 颗星，${focus === h.i ? "已展开" : "点开看连线"}`);
      } else onHoverRef.current?.(null);
    };
    const onLeave = () => { hoverStar = hoverGroup = -1; onHoverRef.current?.(null); };
    const onDown = (e: MouseEvent) => {
      const { x, y } = relPos(e);
      drag.active = true; drag.x = x; drag.y = y; drag.ox = x; drag.oy = y;
      cv!.style.cursor = "grabbing";
    };
    const onUp = () => { drag.active = false; cv!.style.cursor = "grab"; };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const { x: sx, y: sy } = relPos(e);
      const wpt = toW(sx, sy);
      const k2 = Math.max(0.5, Math.min(4, view.k * (e.deltaY < 0 ? 1.16 : 0.86)));
      view.k = k2; view.x = sx - wpt.x * k2; view.y = sy - wpt.y * k2;
    };
    const onClick = (e: MouseEvent) => {
      if (moved) { moved = false; return; }
      const { x, y } = relPos(e);
      const h = pick(x, y);
      if (!h) { if (focus >= 0) setFocus(-1); return; }
      if (h.kind === "group") {
        setFocus(focus === h.i ? -1 : h.i);
      } else {
        const s = stars[h.i];
        if (s.group >= 0 && focus !== s.group) setFocus(s.group);
        const related = s.group >= 0
          ? groups[s.group].members.filter((si) => si !== h.i).slice(0, 30).map((si) => items[stars[si].item])
          : [];
        onSelectRef.current?.({ item: items[s.item], related });
      }
    };
    cv.addEventListener("mousemove", onMove);
    cv.addEventListener("mouseleave", onLeave);
    cv.addEventListener("mousedown", onDown);
    window.addEventListener("mouseup", onUp);
    cv.addEventListener("wheel", onWheel, { passive: false });
    cv.addEventListener("click", onClick);

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      window.removeEventListener("mouseup", onUp);
      cv.removeEventListener("mousemove", onMove);
      cv.removeEventListener("mouseleave", onLeave);
      cv.removeEventListener("mousedown", onDown);
      cv.removeEventListener("wheel", onWheel);
      cv.removeEventListener("click", onClick);
      delete (cv as HTMLCanvasElement & { __dmReset?: () => void }).__dmReset;
    };
  }, [items]);

  // 复位信号：外层（面包屑「整片夜空」/复位按钮）递增即触发
  useEffect(() => {
    const cv = canvasRef.current as (HTMLCanvasElement & { __dmReset?: () => void }) | null;
    if (resetSignal && cv?.__dmReset) cv.__dmReset();
  }, [resetSignal]);

  return <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block", cursor: "grab" }} />;
}
