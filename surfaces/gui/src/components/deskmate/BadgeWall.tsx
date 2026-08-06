/**
 * BadgeWall — 独立「勋章墙」页面（侧边栏「勋章」导航入口）。
 *
 * 数据源为后端 GET /v1/deskmate/badges，返回全量勋章（五门四阶）、
 * 每枚的获得状态/进度/解锁时间，以及统计摘要。前端只负责展示与筛选，
 * 不做任何判定——判定与解锁时间的持久化都在后端 GrowthEngine 里完成，
 * 保证「什么时候得的」这件事跨设备、跨重启都算数。
 *
 * 三类展示态：
 * - 已获得：徽记着色 + 稀有度描边 + 解锁日期（时间不可考的显示「早期补录」）
 * - 未获得：徽记灰化虚线 + 进度条
 * - 隐藏勋章未获得：名称与条件均被后端遮蔽，只显示一个问号
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";
import { BadgeMedal } from "./BadgeMedal";

// ---------------------------------------------------------------------------
// 类型（与后端 deskmate/growth/badges.py serialize() 一一对应）
// ---------------------------------------------------------------------------

export type BadgeRarity = "common" | "rare" | "epic" | "legendary";

export interface Badge {
  id: string;
  name: string;
  glyph: string;
  icon: string;
  description: string;
  category: string;
  categoryLabel: string;
  rarity: BadgeRarity;
  rarityLabel: string;
  hidden: boolean;
  masked: boolean;
  unlocked: boolean;
  progress: number;
  maxProgress: number;
  unlockedAt: string | null;
  backfilled: boolean;
}

interface GroupStat {
  id: string;
  label: string;
  hint?: string;
  total: number;
  unlocked: number;
}

interface BadgeSummary {
  total: number;
  unlocked: number;
  categories: GroupStat[];
  rarities: GroupStat[];
}

interface BadgePayload {
  badges: Badge[];
  summary: BadgeSummary;
  companionDays: number;
  streakDays: number;
}

interface Props {
  /** 返回（回到会话视图） */
  onBack: () => void;
}

// 稀有度配色：与 StatusPanel 的 RARITY_COLORS 同源，额外给出徽记描边色。
// 用固定色而非主题变量，是因为稀有度本身就是一套独立的语义色阶。
const RARITY_STYLE: Record<BadgeRarity, { bg: string; border: string; text: string }> = {
  common: { bg: "#f1efe8", border: "#b4b2a9", text: "#5f5e5a" },
  rare: { bg: "#e6f1fb", border: "#378add", text: "#185fa5" },
  epic: { bg: "#eeedfe", border: "#7f77dd", text: "#534ab7" },
  legendary: { bg: "#faeeda", border: "#ef9f27", text: "#854f0b" },
};

const ALL = "__all__";

/** 解锁时间 → 本地日期串；解析不了就原样返回，绝不显示 Invalid Date。 */
function formatUnlockDate(iso: string | null): string {
  if (!iso) return "";
  // 后端写的是带时区的 ISO（UTC），Safari/WebKit 对空格分隔的时间戳会返回 NaN，
  // 这里的值本身是标准 ISO，直接 parse 即可，但仍做兜底。
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso.slice(0, 10);
  const d = new Date(ts);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

export function BadgeWall({ onBack }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<BadgePayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState<string>(ALL);
  const [onlyUnlocked, setOnlyUnlocked] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await authedFetch(`${httpBase()}/v1/deskmate/badges`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as BadgePayload;
      // 后端异常时返回 {error}，这里要挡住，避免 .badges 为 undefined 崩掉整页
      if (!json || !Array.isArray(json.badges)) throw new Error("bad payload");
      setData(json);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const visible = useMemo(() => {
    if (!data) return [];
    return data.badges.filter(
      (b) =>
        (category === ALL || b.category === category) &&
        (!onlyUnlocked || b.unlocked),
    );
  }, [data, category, onlyUnlocked]);

  const summary = data?.summary;
  const pct = summary && summary.total > 0
    ? Math.round((summary.unlocked / summary.total) * 100)
    : 0;

  return (
    <div className="badge-wall">
      <div className="badge-wall-head">
        <button
          className="experts-back"
          onClick={onBack}
          aria-label={t("badges.back")}
          type="button"
        >
          <Icon name="arrowLeft" size={16} />
        </button>
        <div className="experts-head-text">
          <h1 className="experts-title">{t("badges.title")}</h1>
          <p className="experts-subtitle">
            {data
              ? t("badges.subtitle", {
                  days: data.companionDays,
                  streak: data.streakDays,
                })
              : t("badges.subtitleLoading")}
          </p>
        </div>
        {summary && (
          <div className="badge-wall-score">
            <div className="badge-wall-score-num">
              {summary.unlocked}
              <span className="badge-wall-score-total">/{summary.total}</span>
            </div>
            <div className="badge-wall-score-label">
              {t("badges.collected", { pct })}
            </div>
          </div>
        )}
      </div>

      {summary && (
        <div className="badge-wall-toolbar">
          <div className="badge-filters">
            <button
              type="button"
              className={"badge-chip" + (category === ALL ? " is-on" : "")}
              onClick={() => setCategory(ALL)}
            >
              {t("badges.all")}
              <span className="badge-chip-count">
                {summary.unlocked}/{summary.total}
              </span>
            </button>
            {summary.categories.map((c) => (
              <button
                key={c.id}
                type="button"
                className={"badge-chip" + (category === c.id ? " is-on" : "")}
                onClick={() => setCategory(c.id)}
                title={c.hint}
              >
                {c.label}
                <span className="badge-chip-count">
                  {c.unlocked}/{c.total}
                </span>
              </button>
            ))}
          </div>
          <label className="badge-toggle">
            <input
              type="checkbox"
              checked={onlyUnlocked}
              onChange={(e) => setOnlyUnlocked(e.target.checked)}
            />
            {t("badges.onlyUnlocked")}
          </label>
        </div>
      )}

      {loading && <div className="badge-wall-msg">{t("badges.loading")}</div>}
      {error && !loading && (
        <div className="badge-wall-msg">
          {t("badges.error")}
          <button type="button" className="badge-retry" onClick={() => void load()}>
            {t("badges.retry")}
          </button>
        </div>
      )}

      {!loading && !error && (
        <div className="badge-grid">
          {visible.map((b) => (
            <BadgeCard key={b.id} badge={b} t={t} />
          ))}
          {visible.length === 0 && (
            <div className="badge-wall-msg">{t("badges.empty")}</div>
          )}
        </div>
      )}

      {summary && !loading && !error && (
        <div className="badge-legend">
          {summary.rarities.map((r) => {
            const st = RARITY_STYLE[r.id as BadgeRarity] ?? RARITY_STYLE.common;
            return (
              <span key={r.id} className="badge-legend-item">
                <i className="badge-legend-dot" style={{ background: st.border }} />
                {r.label}
                <em className="badge-legend-count">
                  {r.unlocked}/{r.total}
                </em>
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

function BadgeCard({ badge, t }: { badge: Badge; t: (k: string, o?: any) => string }) {
  const st = RARITY_STYLE[badge.rarity] ?? RARITY_STYLE.common;
  const ratio = badge.maxProgress > 0
    ? Math.min(1, badge.progress / badge.maxProgress)
    : 0;

  return (
    <div
      className={
        "badge-card" +
        (badge.unlocked ? " is-unlocked" : " is-locked") +
        (badge.masked ? " is-masked" : "")
      }
      data-testid={`badge-${badge.id}`}
    >
      <BadgeMedal
        rarity={badge.rarity}
        glyph={badge.glyph}
        category={badge.category}
        masked={badge.masked}
        size={64}
      />

      <div className="badge-name">{badge.name}</div>

      {badge.unlocked ? (
        <>
          <div className="badge-rarity" style={{ color: st.text }}>
            {badge.rarityLabel}
          </div>
          <div className="badge-meta">
            {badge.backfilled
              ? t("badges.backfilled")
              : t("badges.gotAt", { date: formatUnlockDate(badge.unlockedAt) })}
          </div>
        </>
      ) : (
        <>
          <div className="badge-desc">{badge.description}</div>
          {!badge.masked && (
            <>
              <div className="badge-bar">
                <div
                  className="badge-bar-fill"
                  style={{ width: `${ratio * 100}%`, background: st.border }}
                />
              </div>
              <div className="badge-meta">
                {badge.progress.toLocaleString()} / {badge.maxProgress.toLocaleString()}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
