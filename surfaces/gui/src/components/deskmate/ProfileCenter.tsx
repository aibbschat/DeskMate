/**
 * ProfileCenter — 小桔个人中心。
 *
 * 把原本散落在桌搭首页的私人化内容统一收纳进来（首页只留功能入口）。
 * 五 Tab：概览 / 长期记忆 / 小桔日记 / 勋章 / 设置。
 *
 * - 概览：hero（小桔形象）+ 统计卡 + 默契·深度（BondPanel）+ 最近记忆
 * - 长期记忆：learned-history 条目列表（搜索 + 分类筛选）+ 记忆同步状态
 * - 小桔日记：复用 DiaryPanel
 * - 勋章：复用 BadgeWall
 * - 设置：小桔是什么 / 记忆同步 / 个人分身 / 长期知识 / 它在学什么
 *
 * 视觉：以小桔的桔色为主色调（见 styles.css 的 .profile-*）。
 * 数据均为后端真实接口，失败静默降级，不编造。
 */

import { useEffect, useMemo, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon, type IconName } from "../Icon";
import XiaojuAvatar from "../XiaojuAvatar";
import { BondPanel } from "./BondPanel";
import { DiaryPanel } from "./DiaryPanel";
import { MomentFeed } from "./MomentFeed";
import { SyncCard } from "./SyncCard";
import { PositioningCard } from "./PositioningCard";
import { AlterEgoCard } from "./AlterEgoCard";
import { AlterEgoPage } from "./AlterEgoPage";
import { VaultPanel } from "./VaultPanel";
import { InsightPanel } from "./InsightPanel";
import { BadgeWall } from "./BadgeWall";
import type { GrowthSnapshot, LearnedHistory, LearnedItem } from "./StatusPanel";

type Tab = "overview" | "memory" | "diary" | "badges" | "settings";

const TABS: Array<{ id: Tab; label: string; icon: IconName }> = [
  { id: "overview", label: "概览", icon: "logo" },
  { id: "memory", label: "长期记忆", icon: "archive" },
  { id: "diary", label: "小桔日记", icon: "book" },
  { id: "badges", label: "勋章", icon: "medal" },
  { id: "settings", label: "设置", icon: "gear" },
];

/** 相对时间（中文）：今天 HH:MM / M月D日 / YYYY-MM-DD */
function fmtTime(ts: string): string {
  if (!ts) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})T?(\d{2}:\d{2})?/.exec(ts);
  if (!m) return ts.slice(0, 10);
  const today = new Date();
  const sameDay =
    today.getFullYear() === Number(m[1]) &&
    today.getMonth() + 1 === Number(m[2]) &&
    today.getDate() === Number(m[3]);
  if (sameDay) return `今天 ${m[4] ?? ""}`.trim();
  return `${Number(m[2])}月${Number(m[3])}日`;
}

export function ProfileCenter({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState<Tab>("overview");
  const [growth, setGrowth] = useState<GrowthSnapshot | null>(null);
  const [learned, setLearned] = useState<LearnedHistory | null>(null);
  const [diaryCount, setDiaryCount] = useState<number | null>(null);
  const [diaryView, setDiaryView] = useState<"timeline" | "archive">("timeline");
  const [alterEgoOpen, setAlterEgoOpen] = useState(false);

  // 记忆列表的搜索与分类筛选
  const [query, setQuery] = useState("");
  const [cat, setCat] = useState("all");

  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/growth`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && typeof d.level === "number") setGrowth(d);
      })
      .catch(() => {
        /* 静默降级 */
      });
    // 默认 30 条不够看：这是「长期记忆」的主视图，能翻到全部记录才有意义。
    // 后端 clamp 到 5000；当前真机 200 余条，全量读取毫无压力。
    authedFetch(`${httpBase()}/v1/deskmate/learned-history?limit=500`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.items) setLearned(d);
      })
      .catch(() => {
        /* 静默降级 */
      });
    authedFetch(`${httpBase()}/v1/deskmate/diary`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && Array.isArray(d.dates)) setDiaryCount(d.dates.length);
      })
      .catch(() => {
        /* 静默降级 */
      });
  }, []);

  const categories = useMemo(() => {
    const by = learned?.stats?.by_category ?? {};
    return Object.keys(by);
  }, [learned]);

  // 后端按时间**正序**返回（最老在前），个人中心一律按「最新在前」展示。
  // 漏掉这一步的表现很隐蔽：概览「最近记住的事」slice(0,4) 会拿到最老的 4 条，
  // 于是界面看起来"最近的记忆停在半个月前"，但文件里其实一直在写
  // （owner 反馈 2026-09-02）。老组件 UnderstandingCard 是各自 reverse 的，
  // 这里集中反转一次，下游（搜索/筛选/概览）全部复用。
  const memoryItems = useMemo(() => [...(learned?.items ?? [])].reverse(), [learned]);

  const filteredMemories = useMemo(() => {
    const items = memoryItems;
    const q = query.trim().toLowerCase();
    return items.filter(
      (it) =>
        (cat === "all" || it.category === cat) &&
        (q === "" || it.fact.toLowerCase().includes(q)),
    );
  }, [memoryItems, query, cat]);

  const badgeUnlocked =
    growth?.achievements?.filter((a) => a.unlocked).length ?? null;
  const bond = growth?.bond;
  const stage = bond?.stage ?? "";
  const depth = bond?.depth ?? 0;

  /* ------------------------- 概览 ------------------------- */
  const renderOverview = () => (
    <div className="profile-ov">
      <div className="profile-hero">
        <div className="profile-hero-txt">
          <div className="profile-hero-hi">欢迎回来</div>
          <div className="profile-hero-name">这是你的小桔园</div>
          <div className="profile-hero-desc">
            这里保存着小桔对你的了解——聊过的事、写下的日记、一路攒下的勋章，都安安静静地长在这里。
          </div>
          <div className="profile-hero-chips">
            <span className="profile-hero-chip">{stage ? `默契阶段 · ${stage}` : "默契阶段"}</span>
            {growth?.total_chats != null && (
              <span className="profile-hero-chip">已陪伴 {growth.total_chats} 次对话</span>
            )}
            {growth?.streak_days != null && growth.streak_days > 0 && (
              <span className="profile-hero-chip">连续 {growth.streak_days} 天</span>
            )}
          </div>
        </div>
        <div className="profile-hero-avatar">
          <div className="profile-hero-halo" />
          <XiaojuAvatar size={116} className="profile-hero-img" />
        </div>
      </div>

      <div className="profile-stats">
        <div className="profile-stat">
          <div className="profile-stat-ico t1">
            <Icon name="archive" size={22} />
          </div>
          <div>
            <div className="profile-stat-num">{learned?.stats?.total ?? "—"}</div>
            <div className="profile-stat-label">条长期记忆</div>
          </div>
        </div>
        <div className="profile-stat">
          <div className="profile-stat-ico t2">
            <Icon name="medal" size={22} />
          </div>
          <div>
            <div className="profile-stat-num">{badgeUnlocked ?? "—"}</div>
            <div className="profile-stat-label">枚勋章已解锁</div>
          </div>
        </div>
        <div className="profile-stat">
          <div className="profile-stat-ico t3">
            <Icon name="book" size={22} />
          </div>
          <div>
            <div className="profile-stat-num">{diaryCount ?? "—"}</div>
            <div className="profile-stat-label">篇小桔日记</div>
          </div>
        </div>
        <div className="profile-stat">
          <div className="profile-stat-ico t4">
            <Icon name="sparkle" size={22} />
          </div>
          <div>
            <div className="profile-stat-num">{depth > 0 ? Math.round(depth) : "—"}</div>
            <div className="profile-stat-label">默契 · 深度</div>
          </div>
        </div>
      </div>

      <div className="profile-bond">
        <BondPanel growth={growth} />
      </div>

      <div className="profile-panel">
        <div className="profile-panel-head">
          <span className="profile-panel-title">最近记住的事</span>
          <button type="button" className="profile-link" onClick={() => setTab("memory")}>
            查看全部
          </button>
        </div>
        {(memoryItems.length ?? 0) > 0 ? (
          <div className="profile-mem-list">
            {memoryItems.slice(0, 4).map((it, i) => (
              <MemoryRow key={i} item={it} />
            ))}
          </div>
        ) : (
          <div className="profile-empty">还没有长期记忆。多聊几句，小桔就会慢慢记住你。</div>
        )}
      </div>
    </div>
  );

  /* ------------------------- 长期记忆 ------------------------- */
  const renderMemory = () => (
    <div className="profile-page">
      <div className="profile-page-title">长期记忆</div>
      <div className="profile-page-sub">
        小桔把和你说过的话、你表达过的心意，认真收在这里。这是「小桔记得你」的全部证据。
      </div>

      <div className="profile-toolbar">
        <div className="profile-search">
          <Icon name="search" size={16} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索记忆，例如：咖啡、孩子、写作"
            aria-label="搜索记忆"
          />
        </div>
        <div className="profile-filters">
          <button
            type="button"
            className={"profile-chip" + (cat === "all" ? " on" : "")}
            onClick={() => setCat("all")}
          >
            全部
          </button>
          {categories.map((c) => (
            <button
              key={c}
              type="button"
              className={"profile-chip" + (cat === c ? " on" : "")}
              onClick={() => setCat(c)}
            >
              {c}
            </button>
          ))}
        </div>
        <span className="profile-sync-pill">
          <span className="profile-sync-dot" />
          记忆已同步
        </span>
      </div>

      {filteredMemories.length > 0 ? (
        <div className="profile-mem-list wide">
          {filteredMemories.map((it, i) => (
            <MemoryRow key={i} item={it} />
          ))}
        </div>
      ) : (
        <div className="profile-empty">没有找到符合条件的记忆，试试换个关键词或分类。</div>
      )}
    </div>
  );

  /* ------------------------- 设置 ------------------------- */
  const renderSettings = () => (
    <div className="profile-page">
      <div className="profile-page-title">设置</div>
      <div className="profile-page-sub">
        关于小桔的一切，都可以在这里打理。
      </div>
      <div className="profile-settings-grid">
        <div className="profile-settings-col">
          <PositioningCard />
          <SyncCard />
          <VaultPanel />
        </div>
        <div className="profile-settings-col">
          <AlterEgoCard onOpenPage={() => setAlterEgoOpen(true)} />
          <InsightPanel />
        </div>
      </div>
    </div>
  );

  return (
    <div className="profile-center">
      <div className="profile-head">
        <button className="experts-back" onClick={onBack} aria-label="返回" type="button">
          <Icon name="arrowLeft" size={16} />
        </button>
        <div className="experts-head-text">
          <h1 className="experts-title">个人中心</h1>
          <p className="experts-subtitle">小桔记得的你，从默契到日记，都在这一处。</p>
        </div>
        <div className="profile-head-avatar">
          <XiaojuAvatar size={40} />
        </div>
      </div>

      <div className="profile-tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={"profile-tab" + (tab === t.id ? " active" : "")}
            onClick={() => setTab(t.id)}
          >
            <Icon name={t.icon} size={16} />
            {t.label}
          </button>
        ))}
      </div>

      <div className="profile-body">
        {tab === "overview" && renderOverview()}
        {tab === "memory" && renderMemory()}
        {tab === "diary" && (
          <div className="profile-page">
            <div className="profile-page-title">小桔日记</div>
            <div className="profile-page-sub">我们俩的记录，同一条时间线——彼此看得见，也可以互相留言。</div>
            <div className="dm-diary-tabs" style={{ marginBottom: 12 }}>
              <button
                type="button"
                className={diaryView === "timeline" ? "active" : ""}
                onClick={() => setDiaryView("timeline")}
              >
                时间线
              </button>
              <button
                type="button"
                className={diaryView === "archive" ? "active" : ""}
                onClick={() => setDiaryView("archive")}
              >
                小桔的记录
              </button>
            </div>
            <div className="dm-diary-tabs-hint">
              时间线＝你和小桔共写、可🩷可留言；小桔的记录＝小桔自己写的每日随笔（只读查阅）。
            </div>
            {diaryView === "timeline" ? <MomentFeed /> : <DiaryPanel />}
          </div>
        )}
        {tab === "badges" && <BadgeWall onBack={() => setTab("overview")} />}
        {tab === "settings" && renderSettings()}
      </div>

      <AlterEgoPage open={alterEgoOpen} onClose={() => setAlterEgoOpen(false)} />
    </div>
  );
}

/** 单条记忆行：分类 tag + 内容 + 时间。 */
function MemoryRow({ item }: { item: LearnedItem }) {
  return (
    <div className="profile-mem-item">
      <div className="profile-mem-head">
        <span className="profile-mem-tag">{item.category || "记忆"}</span>
        <span className="profile-mem-time">{fmtTime(item.ts)}</span>
      </div>
      <div className="profile-mem-fact">{item.fact}</div>
    </div>
  );
}
