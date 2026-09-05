/**
 * GardenPanels — 花园里的面板（点园中物件才浮现，游戏式漫游）。
 *
 * 「花园的门」是功能总入口：点它弹出功能菜单（它眼中的我 / 长期记忆 / 统计默契 /
 * 访客模式 / 退出），点哪项才展开哪项——各项功能收敛在门后，花园本身保持纯净。
 * 树 → 树的详情（长势/浇水/年花）｜花丛 → 花圃（真花/点亮/回响）。
 *
 * 所有面板数据来自后端真实接口，失败静默降级；零 LLM、官方小桔。
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { BondPanel } from "../deskmate/BondPanel";
import { BadgeWall } from "../deskmate/BadgeWall";
import { GardenBed } from "./GardenBed";
import type { GardenP2 } from "./useGarden";
import { treeProgressPct } from "./useGarden";
import type { GardenFloraItem } from "./GardenFlora";
import type { StoryBook } from "./GardenStoryBook";
import type { GardenFlower, GardenStats } from "./GardenFlowers";

/* ================= 通用面板容器 ================= */

interface PanelProps {
  title: string;
  icon?: string;
  sub?: string;
  onClose: () => void;
  children: React.ReactNode;
  width?: "md" | "lg" | "xl";
}

export function GardenPanel({ title, icon, sub, onClose, children, width = "lg" }: PanelProps) {
  return (
    <div className="garden-panel-mask" onClick={onClose}>
      <div
        className={`garden-panel garden-panel-${width}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={title}
      >
        <div className="garden-panel-head">
          <div className="garden-panel-title">
            {icon && <span className="garden-panel-icon">{icon}</span>}
            <span>{title}</span>
          </div>
          {sub && <div className="garden-panel-sub">{sub}</div>}
          <button type="button" className="garden-panel-close" onClick={onClose} title="收起">
            ✕
          </button>
        </div>
        <div className="garden-panel-body">{children}</div>
      </div>
    </div>
  );
}

/* ================= 花园的门 · 功能菜单 ================= */

export type GateFeature = "portrait" | "memory" | "stats" | "badges";

interface GateMenuProps {
  onOpen: (f: GateFeature) => void;
  onClose: () => void;
  onToggleGuest: (on: boolean) => void;
  guestMode: boolean;
}

export function GardenGateMenu({ onOpen, onClose, onToggleGuest, guestMode }: GateMenuProps) {
  const [portraitReady, setPortraitReady] = useState<number | null>(null);
  const [memoryCount, setMemoryCount] = useState<number | null>(null);
  const [badgeCount, setBadgeCount] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/growth`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive || !d) return;
        const unlocked = Array.isArray(d.achievements) ? d.achievements.filter((a: { unlocked?: boolean }) => a.unlocked).length : null;
        if (unlocked !== null) setBadgeCount(unlocked);
      })
      .catch(() => { /* 静默降级 */ });
    authedFetch(`${httpBase()}/v1/deskmate/learned-history?limit=200`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d && Array.isArray(d.items)) setMemoryCount(d.items.length); })
      .catch(() => { /* 静默降级 */ });
    authedFetch(`${httpBase()}/v1/deskmate/alter-ego`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (alive && d && d.portrait && Array.isArray(d.portrait.sections)) {
          setPortraitReady(d.portrait.sections.filter((s: { status?: string }) => s.status === "ready").length);
        }
      })
      .catch(() => { /* 静默降级 */ });
    return () => { alive = false; };
  }, []);
  return (
    <GardenPanel title="花园的门" icon="🚪" sub="这里的每一样，都只为你留着" onClose={onClose} width="md">
      <div className="garden-gate-menu">
        <button type="button" className="garden-gate-menu-item" onClick={() => onOpen("portrait")}>
          <span className="garden-gate-menu-icon">🌱</span>
          <span className="garden-gate-menu-text">
            <b>它眼中的我</b>
            <i>{portraitReady !== null ? `${portraitReady} 个角落已就绪` : "小桔正在慢慢认识你"}</i>
          </span>
          <span className="garden-gate-menu-arrow">›</span>
        </button>
        <button type="button" className="garden-gate-menu-item" onClick={() => onOpen("memory")}>
          <span className="garden-gate-menu-icon">📝</span>
          <span className="garden-gate-menu-text">
            <b>长期记忆</b>
            <i>{memoryCount !== null ? `记住了 ${memoryCount} 件事` : "它记得的你，全在这"}</i>
          </span>
          <span className="garden-gate-menu-arrow">›</span>
        </button>
        <button type="button" className="garden-gate-menu-item" onClick={() => onOpen("stats")}>
          <span className="garden-gate-menu-icon">📊</span>
          <span className="garden-gate-menu-text">
            <b>统计与默契</b>
            <i>长势 · 勋章 · 默契深度</i>
          </span>
          <span className="garden-gate-menu-arrow">›</span>
        </button>
        <button type="button" className="garden-gate-menu-item" onClick={() => onOpen("badges")}>
          <span className="garden-gate-menu-icon">🏅</span>
          <span className="garden-gate-menu-text">
            <b>勋章墙</b>
            <i>{badgeCount !== null ? `已点亮 ${badgeCount} 枚` : "攒下的勋章都在这"}</i>
          </span>
          <span className="garden-gate-menu-arrow">›</span>
        </button>

        <label className="garden-gate-menu-guest">
          <span className="garden-gate-menu-text">
            <b>访客模式</b>
            <i>开启后，花园不展示长势与花园数据（留白）</i>
          </span>
          <input
            type="checkbox"
            checked={guestMode}
            onChange={(e) => onToggleGuest(e.target.checked)}
            className="garden-gate-menu-check"
          />
        </label>
      </div>
    </GardenPanel>
  );
}

/* ================= 树的详情（长势/浇水/年花） ================= */

interface TreePanelProps {
  p2?: GardenP2 | null;
  water?: { watered: boolean; streak: number; today: string } | null;
  flowerCount: number;
  onClose: () => void;
}


export function GardenTreePanel({ p2, water, flowerCount, onClose }: TreePanelProps) {
  const tree = p2?.tree;
  const w = water ?? p2?.water;
  return (
    <GardenPanel title="花园的树" icon="🌳" sub={tree?.title ?? "花园的树"} onClose={onClose} width="md">
      <div className="garden-tree-panel">
        <div className="garden-tree-panel-stage">
          <span className="garden-tree-panel-emoji">{tree?.key === "bloom" ? "🌸" : tree?.key === "canopy" ? "🌳" : tree?.key === "sapling" ? "🌿" : tree?.key === "sprout" ? "🌱" : "🌰"}</span>
          <div>
            <b>{tree?.title ?? "一粒种子"}</b>
            <i>{tree?.desc ?? "你的花园正在长大"}</i>
          </div>
        </div>
        {tree?.count !== undefined && tree?.threshold !== undefined && (
          <div className="garden-tree-panel-progress">
            <div className="garden-tree-panel-bar">
              <div
                className="garden-tree-panel-fill"
                style={{ width: `${treeProgressPct(tree)}%` }}
              />
            </div>
            <span className="garden-tree-panel-bar-label">
              已开 {tree.count} 朵
              {tree.next
                ? ` · 再开 ${Math.max(0, tree.next.threshold - tree.count)} 朵，长成「${tree.next.title}」`
                : " · 已是最成熟的模样"}
            </span>
          </div>
        )}
        <div className="garden-tree-panel-water">
          {w?.watered ? (
            <span className="garden-tree-panel-water-ok">💧 今天浇过水了 · 已连续 {w.streak} 天</span>
          ) : (
            <span className="garden-tree-panel-water-todo">💧 今天还没浇水，回来陪陪它</span>
          )}
        </div>
        <div className="garden-tree-panel-flowers">这棵树上，开了 <b>{flowerCount}</b> 朵花</div>
      </div>
    </GardenPanel>
  );
}

/* ================= 花丛 · 花圃 ================= */

interface FlowersPanelProps {
  flowers: GardenFlower[];
  stats: GardenStats | null;
  p2?: GardenP2 | null;
  water?: { watered: boolean; streak: number; today: string } | null;
  litIds?: string[] | null;
  onLit: (flowerId: string) => void;
  flora?: GardenFloraItem[] | null;
  story?: StoryBook | null;
  onClose: () => void;
}

export function GardenFlowersPanel({ flowers, stats, p2, water, litIds, onLit, flora, story, onClose }: FlowersPanelProps) {
  return (
    <GardenPanel title="花丛" icon="🌷" sub={`${flowers.length} 朵花 · 情绪定色 · 可点亮`} onClose={onClose} width="xl">
      <GardenBed flowers={flowers} stats={stats} p2={p2} water={water} litIds={litIds} onLit={onLit} flora={flora} story={story} />
    </GardenPanel>
  );
}

/* ================= 它眼中的我（完整、可滚动、可展开） ================= */

interface PortraitArticle { title: string; content: string; evidence?: string; tags?: string[]; }
interface PortraitSection {
  id: string; title: string; icon: string;
  status: "ready" | "learning" | "empty"; headline: string; articles: PortraitArticle[];
}
interface Portrait {
  sections: PortraitSection[];
  summary: { ready_count: number; learning_count: number; empty_count: number; total_articles: number };
}

export function GardenPortraitPanel({ onClose }: { onClose: () => void }) {
  const [portrait, setPortrait] = useState<Portrait | null>(null);
  const [openSection, setOpenSection] = useState<PortraitSection | null>(null);
  const [enabled, setEnabled] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/alter-ego`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!alive) return;
        if (d && d.portrait) setPortrait(d.portrait);
        if (d && typeof d.enabled === "boolean") setEnabled(d.enabled);
      })
      .catch(() => { /* 静默降级 */ });
    return () => { alive = false; };
  }, []);

  if (!portrait && enabled === null) {
    return (
      <GardenPanel title="它眼中的我" icon="🌱" sub="小桔正在慢慢认识你" onClose={onClose} width="lg">
        <div className="garden-panel-loading">正在翻看小桔的记事本…</div>
      </GardenPanel>
    );
  }

  const sections = portrait?.sections ?? [];
  const summary = portrait?.summary;
  const ready = sections.filter((s) => s.status === "ready").length;

  return (
    <GardenPanel title="它眼中的我" icon="🌱" sub={ready > 0 ? `${ready} 个角落已就绪，其余还在学` : "小桔正在慢慢认识你"} onClose={onClose} width="lg">
      {summary && (
        <div className="garden-portrait-stats">
          <div className="garden-portrait-stat"><b>{summary.ready_count}</b><span>就绪</span></div>
          <div className="garden-portrait-stat"><b>{summary.learning_count}</b><span>学习中</span></div>
          <div className="garden-portrait-stat"><b>{summary.empty_count}</b><span>还没有</span></div>
          <div className="garden-portrait-stat"><b>{summary.total_articles}</b><span>篇成果</span></div>
        </div>
      )}

      {openSection ? (
        <div className="garden-portrait-detail">
          <button type="button" className="garden-portrait-back" onClick={() => setOpenSection(null)}>← 返回</button>
          <div className="garden-portrait-detail-head">
            <span className="garden-portrait-detail-icon">{openSection.icon}</span>
            <div>
              <div className="garden-portrait-detail-title">{openSection.title}</div>
              <div className="garden-portrait-detail-desc">{openSection.headline}</div>
            </div>
          </div>
          {openSection.articles.length === 0 ? (
            <div className="garden-portrait-empty">还在学习中，多和小桔聊聊，这里会慢慢长出来。</div>
          ) : (
            <div className="garden-portrait-articles">
              {openSection.articles.map((a, i) => (
                <div key={i} className="garden-portrait-article">
                  <div className="garden-portrait-article-title">{a.title}</div>
                  <div className="garden-portrait-article-content">{a.content}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="garden-portrait-grid">
          {sections.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`garden-portrait-card status-${s.status}`}
              onClick={() => s.status !== "empty" && setOpenSection(s)}
              disabled={s.status === "empty"}
            >
              <div className="garden-portrait-card-head">
                <span className="garden-portrait-card-icon">{s.icon}</span>
                <span className="garden-portrait-card-title">{s.title}</span>
                <span className="garden-portrait-card-status">
                  {s.status === "ready" ? "已就绪" : s.status === "learning" ? "学习中" : "还没有"}
                </span>
              </div>
              <div className="garden-portrait-card-line">{s.headline}</div>
              <div className="garden-portrait-card-meta">
                {s.articles.length > 0 ? `${s.articles.length} 篇` : "还在积累"}
                {s.status !== "empty" && " · 查看 →"}
              </div>
            </button>
          ))}
        </div>
      )}
    </GardenPanel>
  );
}

/* ================= 长期记忆 ================= */

interface LearnedItem { category?: string; fact: string; ts?: string; }

function fmtTime(ts?: string): string {
  if (!ts) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})T?(\d{2}:\d{2})?/.exec(ts);
  if (!m) return ts.slice(0, 10);
  const today = new Date();
  const sameDay = today.getFullYear() === Number(m[1]) && today.getMonth() + 1 === Number(m[2]) && today.getDate() === Number(m[3]);
  if (sameDay) return `今天 ${m[4] ?? ""}`.trim();
  return `${Number(m[2])}月${Number(m[3])}日`;
}

export function GardenMemoryPanel({ onClose }: { onClose: () => void }) {
  const [learned, setLearned] = useState<LearnedItem[] | null>(null);
  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/learned-history?limit=200`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d && Array.isArray(d.items)) setLearned(d.items); })
      .catch(() => { /* 静默降级 */ });
    return () => { alive = false; };
  }, []);

  const items = learned ? [...learned].reverse() : [];
  return (
    <GardenPanel title="长期记忆" icon="📝" sub="它记得的你，全在这" onClose={onClose} width="lg">
      {items.length > 0 ? (
        <div className="garden-memory-list">
          {items.map((it, i) => (
            <div key={i} className="garden-memory-item">
              <div className="garden-memory-head">
                <span className="garden-memory-tag">{it.category || "记忆"}</span>
                <span className="garden-memory-time">{fmtTime(it.ts)}</span>
              </div>
              <div className="garden-memory-fact">{it.fact}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="garden-panel-empty">还没有长期记忆。多聊几句，小桔就会慢慢记住你。</div>
      )}
    </GardenPanel>
  );
}

/* ================= 统计与默契 ================= */

interface GrowthSnapshot {
  level?: number; total_chats?: number; streak_days?: number;
  achievements?: Array<{ unlocked: boolean }>; bond?: { stage?: string; depth?: number };
}

export function GardenStatsPanel({ onClose }: { onClose: () => void }) {
  const [growth, setGrowth] = useState<GrowthSnapshot | null>(null);
  const [learnedCount, setLearnedCount] = useState<number | null>(null);
  const [diaryCount, setDiaryCount] = useState<number | null>(null);

  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/growth`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d && typeof d.level === "number") setGrowth(d); })
      .catch(() => { /* 静默降级 */ });
    authedFetch(`${httpBase()}/v1/deskmate/learned-history?limit=200`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d && Array.isArray(d.items)) setLearnedCount(d.items.length); })
      .catch(() => { /* 静默降级 */ });
    authedFetch(`${httpBase()}/v1/deskmate/diary`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d && Array.isArray(d.dates)) setDiaryCount(d.dates.length); })
      .catch(() => { /* 静默降级 */ });
    return () => { alive = false; };
  }, []);

  const badgeUnlocked = growth?.achievements?.filter((a) => a.unlocked).length ?? null;
  const bond = growth?.bond;

  return (
    <GardenPanel title="统计与默契" icon="📊" sub="这座院子为你长着的数字" onClose={onClose} width="lg">
      <div className="garden-stats-panel-grid">
        <div className="garden-stats-panel-card"><b>{learnedCount ?? "—"}</b><span>条长期记忆</span></div>
        <div className="garden-stats-panel-card"><b>{badgeUnlocked ?? "—"}</b><span>枚勋章</span></div>
        <div className="garden-stats-panel-card"><b>{diaryCount ?? "—"}</b><span>篇日记</span></div>
        <div className="garden-stats-panel-card"><b>{bond?.depth ? Math.round(bond.depth) : "—"}</b><span>默契深度</span></div>
      </div>
      <div className="garden-stats-panel-bond">
        <BondPanel growth={growth as never} />
      </div>
    </GardenPanel>
  );
}

/* ================= 勋章墙 ================= */

export function GardenBadgesPanel({ onClose, onBackHome }: { onClose: () => void; onBackHome: () => void }) {
  return (
    <GardenPanel title="勋章墙" icon="🏅" sub="攒下的勋章，都收在小桔的小窝里" onClose={onClose} width="lg">
      <BadgeWall onBack={onBackHome} />
    </GardenPanel>
  );
}
