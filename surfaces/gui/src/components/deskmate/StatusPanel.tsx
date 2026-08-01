import { useState, useEffect } from "react";
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
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

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

  return (
    <div
      className="absolute bottom-full left-1/2 mb-3 z-30"
      style={{
        transform: "translateX(-50%)",
        width: 280,
        opacity: open ? 1 : 0,
        transition: "opacity 0.2s ease, transform 0.2s ease",
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
              DeskMate
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

        {/* Body */}
        <div className="px-3.5 py-2.5 flex flex-col gap-2.5">
          {/* Current activity */}
          <div>
            <div
              className="text-[10px] uppercase tracking-wider mb-1"
              style={{ color: "var(--faint)" }}
            >
              当前
            </div>
            <div className="text-[12.5px]" style={{ color: "var(--ink)" }}>
              {currentActivity || params.description}
            </div>
          </div>

          {/* Proactive mode toggle */}
          {onToggleProactive && (
            <div>
              <div
                className="text-[10px] uppercase tracking-wider mb-1"
                style={{ color: "var(--faint)" }}
              >
                主动模式
              </div>
              <button
                className="flex items-center gap-2 w-full text-left group"
                onClick={() => onToggleProactive(!proactiveEnabled)}
              >
                {/* Toggle switch */}
                <div
                  className="relative w-8 h-4.5 rounded-full transition-colors duration-200"
                  style={{
                    background: proactiveEnabled
                      ? params.color
                      : "var(--line)",
                  }}
                >
                  <div
                    className="absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform duration-200"
                    style={{
                      left: proactiveEnabled ? "calc(100% - 1rem)" : "0.125rem",
                    }}
                  />
                </div>
                <span
                  className="text-[11.5px]"
                  style={{
                    color: proactiveEnabled ? "var(--ink)" : "var(--faint)",
                  }}
                >
                  {proactiveEnabled ? "已开启" : "已关闭"}
                </span>
              </button>
              {!proactiveEnabled && (
                <div
                  className="text-[10.5px] mt-1"
                  style={{ color: "var(--faint)" }}
                >
                  关闭后不再主动弹出建议
                </div>
              )}
            </div>
          )}

          {/* Confidence */}
          {confidence !== undefined && (
            <div>
              <div
                className="text-[10px] uppercase tracking-wider mb-1"
                style={{ color: "var(--faint)" }}
              >
                置信度
              </div>
              <div className="flex items-center gap-2">
                <div
                  className="flex-1 h-1.5 rounded-full overflow-hidden"
                  style={{ background: "var(--line)" }}
                >
                  <div
                    className="h-full rounded-full transition-all duration-500"
                    style={{
                      width: `${Math.round(confidence * 100)}%`,
                      background: params.color,
                    }}
                  />
                </div>
                <span
                  className="text-[11px] tabular-nums"
                  style={{ color: "var(--muted)" }}
                >
                  {Math.round(confidence * 100)}%
                </span>
              </div>
            </div>
          )}

          {/* Recent memories */}
          {recentMemories.length > 0 && (
            <div>
              <div
                className="text-[10px] uppercase tracking-wider mb-1"
                style={{ color: "var(--faint)" }}
              >
                近期记忆
              </div>
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
              <div
                className="text-[10px] uppercase tracking-wider mb-1"
                style={{ color: "var(--faint)" }}
              >
                我对你的了解
              </div>
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
          {recentMemories.length === 0 && (
            <div
              className="text-[11.5px] italic"
              style={{ color: "var(--faint)" }}
            >
              暂无记忆
            </div>
          )}

          {/* Open memory viewer */}
          {onOpenMemory && (
            <button
              className="text-[11.5px] font-medium px-2 py-1.5 rounded text-left w-full"
              style={{ color: "var(--accent)", background: "var(--paper)" }}
              onClick={onOpenMemory}
            >
              📝 查看 / 编辑记忆
            </button>
          )}

          {/* Open writing proofread (润色台) */}
          {onOpenProofread && (
            <button
              className="inline-flex items-center gap-1.5 text-[11.5px] font-medium px-2 py-1.5 rounded text-left w-full"
              style={{ color: "var(--accent)", background: "var(--paper)" }}
              onClick={onOpenProofread}
            >
              <Icon name="pencil" size={13} />
              编辑模式
            </button>
          )}
        </div>
      </div>

      {/* Arrow pointing down */}
      <div
        className="absolute left-1/2 -translate-x-1/2 -bottom-1.5"
        style={{
          width: 0,
          height: 0,
          borderLeft: "6px solid transparent",
          borderRight: "6px solid transparent",
          borderTop: "6px solid var(--line)",
        }}
      />
    </div>
  );
}
