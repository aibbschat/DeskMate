import { useState, useEffect, type ReactNode } from "react";
import { type EmotionName, getEmotionParams } from "./EmotionState";
import { Icon } from "../Icon";

// ---------------------------------------------------------------------------
// Types & helpers
// ---------------------------------------------------------------------------

export interface LearnedItem {
  ts: string;
  fact: string;
  category: string;
}

export interface LearnedHistory {
  items: LearnedItem[];
  stats: { total: number; by_category: Record<string, number> };
}

/** 相对时间（中文）：刚刚 / X 分钟前 / X 小时前 / 今天 HH:MM / M月D日 */
function relTime(ts: string): string {
  const then = new Date(ts).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const d = new Date(then);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return `今天 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

// ---------------------------------------------------------------------------
// 成长 / 亲密度（QQ 宠物式升级玩法）
// ---------------------------------------------------------------------------

/** 每一级所需的「累计成长值」阈值（cumulative）。Indexed by level-1。 */
const LEVEL_THRESHOLDS = [0, 5, 15, 30, 50, 80, 120, 170, 230, 300, 400, 520];

interface Growth {
  level: number;
  cur: number; // 当前级已积累
  span: number; // 当前级所需
  progress: number; // 0-1
  toNext: number; // 距下一阶还差几条
  maxed: boolean;
}

/** 由「成长值」推导等级与升级进度。 */
function computeGrowth(total: number): Growth {
  let level = 1;
  for (let i = 1; i < LEVEL_THRESHOLDS.length; i++) {
    if (total >= LEVEL_THRESHOLDS[i]) level = i + 1;
    else break;
  }
  const curBase = LEVEL_THRESHOLDS[level - 1];
  const nextBase = LEVEL_THRESHOLDS[level] ?? curBase + 120;
  const span = Math.max(1, nextBase - curBase);
  const cur = total - curBase;
  const progress = Math.min(1, cur / span);
  const toNext = Math.max(0, nextBase - total);
  return { level, cur, span, progress, toNext, maxed: level >= LEVEL_THRESHOLDS.length };
}

/** 成长系统快照（来自后端 /v1/deskmate/growth）。 */
export interface GrowthSnapshot {
  level: number;
  growth_value: number;
  intimacy: number;
  trust: number;
  empathy: number;
  mood: number;
  vitality: number;
  trust_controls_proactive: boolean;
  level_progress?: {
    level: number;
    cur: number;
    span: number;
    progress: number;
    toNext: number;
    maxed: boolean;
  };
  skill_proficiency: Record<string, { uses: number; success_rate: number; last_used: string; proficiency: number }>;
  recent_events: Array<{ ts: string; kind: string; [k: string]: unknown }>;
}

const PROFICIENCY_LABEL: Record<string, string> = {
  skill_created: "掌握了新技能",
  skill_used: "使用了技能",
  skill_evolved: "技能进化",
  suggestion_accepted: "采纳了我的建议",
  suggestion_ignored: "忽略了我的建议",
  memory_learned: "记住了新信息",
};

function proficiencyText(p: number): string {
  if (p >= 0.7) return "熟练";
  if (p >= 0.35) return "入门";
  return "生疏";
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface StatusPanelProps {
  /** Current emotion driving the panel content. */
  emotion: EmotionName;
  /** What the agent is currently doing (free text). */
  currentActivity?: string;
  /** Recent memory snippets (most recent first). */
  recentMemories?: string[];
  /** Confidence score (0 - 1). */
  confidence?: number;
  /** Open / closed state. */
  open: boolean;
  /** Close handler. */
  onClose: () => void;
  /** Open the memory viewer. */
  onOpenMemory?: () => void;
  /** 打开写作批改台。 */
  onOpenProofread?: () => void;
  /** Whether proactive mode is enabled (background suggestions). */
  proactiveEnabled?: boolean;
  /** Toggle proactive mode. */
  onToggleProactive?: (enabled: boolean) => void;
  /** 学习历史（越用越懂你的时间线 + 了解度统计）。 */
  learned?: LearnedHistory | null;
  /** 成长系统快照（等级/亲密度/信任感/默契度/技能熟练度）。 */
  growth?: GrowthSnapshot | null;
}

// ---------------------------------------------------------------------------
// Small presentational helpers
// ---------------------------------------------------------------------------

function StatBar({
  label,
  value,
  percent,
  color,
  inline = false,
}: {
  label: string;
  value: string;
  percent: number;
  color: string;
  inline?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center justify-between gap-1">
        <span className="text-[10.5px] truncate" style={{ color: "var(--muted)" }}>
          {label}
        </span>
        <span className="text-[10px] tabular-nums shrink-0" style={{ color: "var(--faint)" }}>
          {value}
        </span>
      </div>
      <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--line)" }}>
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{
            width: `${Math.min(100, Math.max(0, percent))}%`,
            background: `linear-gradient(90deg,${color},${color})`,
          }}
        />
      </div>
      {inline && <div />}
    </div>
  );
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <div
      className="text-[10px] uppercase tracking-wider mb-1.5"
      style={{ color: "var(--faint)" }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

const EDGE = 8;

export function StatusPanel({
  emotion,
  currentActivity,
  recentMemories = [],
  confidence,
  open,
  onClose,
  onOpenMemory,
  onOpenProofread,
  proactiveEnabled = true,
  onToggleProactive,
  learned = null,
  growth = null,
}: StatusPanelProps) {
  const params = getEmotionParams(emotion);

  // Animate out before unmounting
  const [visible, setVisible] = useState(open);
  useEffect(() => {
    if (open) {
      setVisible(true);
    } else {
      const t = setTimeout(() => setVisible(false), 200);
      return () => clearTimeout(t);
    }
  }, [open]);

  if (!visible) return null;

  // ----- 成长 / 亲密度 -----
  const growthData = growth?.level_progress ?? null;
  const cur = growthData?.cur ?? 0;
  const span = growthData?.span ?? 1;
  const progress = growthData?.progress ?? 0;
  const maxed = growthData?.maxed ?? false;
  // 回退：无成长快照时用了解度推导（向后兼容）
  const total = learned?.stats.total ?? 0;
  const fallback = computeGrowth(total);
  const level = growthData?.level ?? fallback.level;
  const toNext = growthData?.toNext ?? fallback.toNext;
  // 阶段名称：墨童(1-3) / 墨生(4-6) / 墨客(7-9) / 墨仙(10-12)
  const stageName =
    level >= 10 ? "墨仙" : level >= 7 ? "墨客" : level >= 4 ? "墨生" : "墨童";
  const intimacy = growth?.intimacy ?? Math.min(100, Math.round(total * 2.5));
  const trust = growth?.trust ?? 0;
  const empathy = growth?.empathy ?? 0;
  const mood = growth?.mood ?? 0;
  const vitality = growth?.vitality ?? 0;

  return (
    <div
      className="fixed z-50"
      style={{
        right: EDGE,
        bottom: EDGE,
        width: 372,
        opacity: open ? 1 : 0,
        transition: "opacity 0.2s ease",
      }}
    >
      <div
        className="rounded-xl border shadow-lg overflow-hidden"
        style={{
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 12px 36px rgba(0,0,0,0.14)",
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-3.5 py-2.5"
          style={{ borderBottom: "1px solid var(--line)" }}
        >
          <div className="flex items-center gap-2">
            <span
              className="w-2 h-2 rounded-full"
              style={{ background: params.color }}
            />
            <span
              className="text-[12.5px] font-semibold"
              style={{ color: "var(--ink)" }}
            >
              墨灵
            </span>
            <span
              className="text-[10.5px] font-bold px-1.5 py-0.5 rounded"
              style={{
                color: "#b8791f",
                background: "linear-gradient(90deg,#fde9c4,#f8d89a)",
              }}
            >
              Lv.{level} · {stageName}
            </span>
            <span
              className="text-[11px] px-1.5 py-0.5 rounded"
              style={{
                color: params.color,
                background: params.colorSoft,
              }}
            >
              {params.label}
            </span>
          </div>
          <button
            className="w-6 h-6 rounded flex items-center justify-center"
            style={{ color: "var(--faint)" }}
            onClick={onClose}
            aria-label="Close"
          >
            <Icon name="x" size={14} />
          </button>
        </div>

        {/* Body（超高时滚动，避免被边框/视口裁切） */}
        <div
          className="px-3.5 py-3 flex flex-col gap-3"
          style={{ maxHeight: "min(70vh, 520px)", overflowY: "auto" }}
        >
          {/* 当前 */}
          <div>
            <SectionTitle>当前</SectionTitle>
            <div className="text-[12.5px]" style={{ color: "var(--ink)" }}>
              {currentActivity || params.description}
            </div>
          </div>

          {/* 成长 / 等级（QQ 宠物式升级进度） */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <span
                className="text-[10px] uppercase tracking-wider"
                style={{ color: "var(--faint)" }}
              >
                ✨ 成长
              </span>
              <span
                className="text-[10.5px] tabular-nums"
                style={{ color: "var(--muted)" }}
              >
                {maxed ? "已满级 ✦" : `经验 ${cur}/${span}`}
              </span>
            </div>
            <div
              className="h-2 rounded-full overflow-hidden"
              style={{ background: "var(--line)" }}
            >
              <div
                className="h-full rounded-full transition-all duration-500"
                style={{
                  width: `${Math.round(progress * 100)}%`,
                  background: "linear-gradient(90deg,#f6c453,#f0a93b)",
                }}
              />
            </div>
            {!maxed && (
              <div className="text-[10.5px] mt-1" style={{ color: "var(--faint)" }}>
                再积累 {toNext} 点成长值，墨灵就能升到 Lv.{level + 1} 啦 ✨
              </div>
            )}
          </div>

          {/* 关系轨 + 状态轨（两栏并排，平铺不再堆叠） */}
          <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
            <div>
              <SectionTitle>关系轨</SectionTitle>
              <div className="flex flex-col gap-2">
                <StatBar
                  label="💗 亲密度"
                  value={`${Math.round(intimacy)}%`}
                  percent={intimacy}
                  color="#f78fb3"
                />
                <StatBar
                  label="🤝 信任感"
                  value={`${Math.round(trust * 100)}%`}
                  percent={trust * 100}
                  color="#6fb1f7"
                />
                <StatBar
                  label="✨ 默契度"
                  value={`${Math.round(empathy)}%`}
                  percent={empathy}
                  color="#5fcb86"
                />
              </div>
            </div>
            <div>
              <SectionTitle>状态轨</SectionTitle>
              <div className="flex flex-col gap-2">
                <StatBar
                  label="☀️ 心情"
                  value={`${Math.round(mood)}%`}
                  percent={mood}
                  color="#f8c34f"
                />
                <StatBar
                  label="⚡ 活力"
                  value={`${Math.round(vitality)}%`}
                  percent={vitality}
                  color="#8a5ff0"
                />
                {confidence !== undefined && (
                  <StatBar
                    label="🤝 置信度"
                    value={`${Math.round(confidence * 100)}%`}
                    percent={confidence * 100}
                    color={params.color}
                  />
                )}
              </div>
            </div>
          </div>

          {/* 技能熟练度（反哺匹配排序的真实能力） */}
          {growth && Object.keys(growth.skill_proficiency).length > 0 && (
            <div>
              <SectionTitle>技能熟练度</SectionTitle>
              <div className="flex flex-col gap-1.5 max-h-32 overflow-y-auto">
                {Object.entries(growth.skill_proficiency)
                  .sort((a, b) => b[1].proficiency - a[1].proficiency)
                  .map(([name, p]) => (
                    <div key={name} className="flex items-center gap-2">
                      <span
                        className="text-[11px] flex-1 truncate"
                        style={{ color: "var(--muted)" }}
                      >
                        {name}
                      </span>
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          color: "var(--ink)",
                          background: "var(--paper)",
                        }}
                      >
                        {proficiencyText(p.proficiency)} · {p.uses}次
                      </span>
                      <div
                        className="w-12 h-1 rounded-full overflow-hidden shrink-0"
                        style={{ background: "var(--line)" }}
                      >
                        <div
                          className="h-full rounded-full"
                          style={{
                            width: `${Math.round(p.proficiency * 100)}%`,
                            background: "linear-gradient(90deg,#f6c453,#f0a93b)",
                          }}
                        />
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          )}

          {/* 成长时间线 */}
          {growth && growth.recent_events.length > 0 && (
            <div>
              <SectionTitle>成长记录</SectionTitle>
              <div className="flex flex-col gap-1 max-h-28 overflow-y-auto">
                {[...growth.recent_events].reverse().slice(-5).map((ev, i) => (
                  <div
                    key={`${ev.ts}-${i}`}
                    className="text-[11px] px-2 py-1 rounded"
                    style={{
                      color: "var(--muted)",
                      background: "var(--paper)",
                      lineHeight: 1.4,
                    }}
                  >
                    <span style={{ color: "var(--faint)" }}>{relTime(String(ev.ts))} </span>
                    {PROFICIENCY_LABEL[String(ev.kind)] ?? String(ev.kind)}
                    {ev.skill_name ? ` · ${String(ev.skill_name)}` : ""}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 主动模式开关 */}
          {onToggleProactive && (
            <div>
              <div className="flex items-center justify-between">
                <span
                  className="text-[10px] uppercase tracking-wider"
                  style={{ color: "var(--faint)" }}
                >
                  主动模式
                </span>
                <button
                  className="flex items-center gap-2 group"
                  onClick={() => onToggleProactive(!proactiveEnabled)}
                >
                  <span
                    className="text-[11.5px]"
                    style={{
                      color: proactiveEnabled ? "var(--ink)" : "var(--faint)",
                    }}
                  >
                    {proactiveEnabled ? "已开启" : "已关闭"}
                  </span>
                  <div
                    className="relative w-8 h-4.5 rounded-full transition-colors duration-200"
                    style={{
                      background: proactiveEnabled ? params.color : "var(--line)",
                    }}
                  >
                    <div
                      className="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform duration-200"
                      style={{
                        left: proactiveEnabled ? "calc(100% - 1rem)" : "0.125rem",
                      }}
                    />
                  </div>
                </button>
              </div>
              {!proactiveEnabled && (
                <div className="text-[10.5px] mt-1" style={{ color: "var(--faint)" }}>
                  关闭后不再主动弹出建议
                </div>
              )}
            </div>
          )}

          {/* 近期记忆 */}
          {recentMemories.length > 0 && (
            <div>
              <SectionTitle>近期记忆</SectionTitle>
              <div className="flex flex-col gap-1">
                {recentMemories.slice(0, 4).map((mem, i) => (
                  <div
                    key={i}
                    className="text-[11.5px] px-2 py-1 rounded"
                    style={{
                      color: "var(--muted)",
                      background: "var(--paper)",
                      opacity: 1 - i * 0.12,
                      lineHeight: 1.4,
                    }}
                  >
                    {mem.length > 80 ? mem.slice(0, 80) + "..." : mem}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* 我对你的了解（越用越懂你的时间线 + 了解度） */}
          {learned && (
            <div>
              <SectionTitle>我对你的了解</SectionTitle>
              <div className="flex items-center gap-2 mb-2">
                <span
                  className="text-[12.5px] font-semibold"
                  style={{ color: "var(--ink)" }}
                >
                  了解度 {learned.stats.total}
                </span>
                <span className="text-[11px]" style={{ color: "var(--muted)" }}>
                  偏好 {learned.stats.by_category.preference || 0} · 习惯{" "}
                  {learned.stats.by_category.habit || 0} · 工具{" "}
                  {learned.stats.by_category.tool || 0}
                </span>
              </div>
              <div className="flex flex-col gap-1 max-h-40 overflow-y-auto">
                {[...learned.items].reverse().map((it, i) => (
                  <div
                    key={`${it.ts}-${i}`}
                    className="text-[11.5px] px-2 py-1 rounded"
                    style={{
                      color: "var(--muted)",
                      background: "var(--paper)",
                      lineHeight: 1.4,
                    }}
                  >
                    <span style={{ color: "var(--faint)" }}>{relTime(it.ts)} </span>
                    {it.fact.length > 64 ? it.fact.slice(0, 64) + "…" : it.fact}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Empty state */}
          {recentMemories.length === 0 && !learned && (
            <div
              className="text-[11.5px] italic"
              style={{ color: "var(--faint)" }}
            >
              暂无记忆
            </div>
          )}

          {/* Actions */}
          <div className="flex gap-2">
            {onOpenMemory && (
              <button
                className="flex-1 text-[11.5px] font-medium px-2 py-1.5 rounded text-left"
                style={{ color: "var(--accent)", background: "var(--paper)" }}
                onClick={onOpenMemory}
              >
                📝 查看 / 编辑记忆
              </button>
            )}
            {onOpenProofread && (
              <button
                className="flex-1 inline-flex items-center justify-center gap-1.5 text-[11.5px] font-medium px-2 py-1.5 rounded"
                style={{ color: "var(--accent)", background: "var(--paper)" }}
                onClick={onOpenProofread}
              >
                <Icon name="pencil" size={13} />
                文稿编校
              </button>
            )}
          </div>
        </div>
      </div>

      {/* 右下角锚定，无指向箭头 */}
    </div>
  );
}
