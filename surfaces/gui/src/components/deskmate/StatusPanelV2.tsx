/**
 * StatusPanelV2 — 墨灵可视化面板（精致重设计版）。
 *
 * 4 层信息架构：
 *   Layer 1: 核心身份（桌宠形象 + 名称 + 等级 + 情绪）
 *   Layer 2: 核心状态（心情 + 活力 + 关系指标 + 成长进度）
 *   Layer 3: 成就与个性（成就徽章 + 人格指纹）
 *   Layer 4: 详情与操作（技能 + 记忆 + 了解度 + 操作按钮）
 *
 * 设计原则：
 *   - 3 秒抓住重点
 *   - 渐进披露，次要信息折叠
 *   - 精致细腻的视觉质感
 *   - 温和鼓励式反馈
 */

import { useState, useEffect, useRef, type ReactNode } from "react";
import { type EmotionName, getEmotionParams } from "./EmotionState";
import { Icon } from "../Icon";
import type { GrowthSnapshot, LearnedHistory, Achievement, PersonalityProfile } from "./StatusPanel";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface StatusPanelV2Props {
  emotion: EmotionName;
  currentActivity?: string;
  recentMemories?: string[];
  confidence?: number;
  open: boolean;
  onClose: () => void;
  onOpenMemory?: () => void;
  onOpenProofread?: () => void;
  proactiveEnabled?: boolean;
  onToggleProactive?: (enabled: boolean) => void;
  learned?: LearnedHistory | null;
  growth?: GrowthSnapshot | null;
  achievements?: Achievement[];
  personality?: PersonalityProfile;
  streakDays?: number;
  totalChats?: number;
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/** 相对时间（中文） */
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

/** 心情 emoji */
function getMoodEmoji(mood: number): string {
  if (mood >= 80) return "😊";
  if (mood >= 60) return "🙂";
  if (mood >= 40) return "😐";
  if (mood >= 20) return "😔";
  return "😢";
}

/** 心情描述 */
function getMoodText(mood: number): string {
  if (mood >= 80) return "活力满满";
  if (mood >= 60) return "心情不错";
  if (mood >= 40) return "平平淡淡";
  if (mood >= 20) return "有点低落";
  return "需要关爱";
}

// ---------------------------------------------------------------------------
// 精致组件
// ---------------------------------------------------------------------------

/** 精致进度条 */
function RefinedProgress({
  value,
  max = 100,
  colorScheme = "default",
  size = "sm",
  showValue = false,
  label,
}: {
  value: number;
  max?: number;
  colorScheme?: "high" | "medium" | "low" | "relation" | "growth" | "default";
  size?: "xs" | "sm" | "md";
  showValue?: boolean;
  label?: string;
}) {
  const percent = Math.min(100, (value / max) * 100);

  const colorMap = {
    high: "from-emerald-500 to-emerald-400",
    medium: "from-amber-500 to-amber-400",
    low: "from-red-500 to-red-400",
    relation: "from-indigo-500 to-indigo-400",
    growth: "from-amber-500 to-yellow-300",
    default: "from-gray-500 to-gray-400",
  };

  const sizeMap = {
    xs: "h-1",
    sm: "h-1.5",
    md: "h-2",
  };

  return (
    <div className="flex flex-col gap-1">
      {label && (
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-gray-500 font-medium tracking-wide uppercase">
            {label}
          </span>
          {showValue && (
            <span className="text-[10px] text-gray-400 tabular-nums">
              {Math.round(percent)}%
            </span>
          )}
        </div>
      )}
      <div
        className={`${sizeMap[size]} rounded-full overflow-hidden`}
        style={{
          background: "rgba(0, 0, 0, 0.05)",
          boxShadow: "inset 0 1px 1px rgba(0,0,0,0.06)",
        }}
      >
        <div
          className={`h-full rounded-full bg-gradient-to-r ${colorMap[colorScheme]} transition-all duration-500 ease-out relative`}
          style={{
            width: `${percent}%`,
            boxShadow: percent > 0 ? "0 0 3px rgba(0,0,0,0.1)" : "none",
          }}
        >
          {/* 高光 */}
          <div
            className="absolute top-0 left-0 right-0 h-1/2 rounded-t-full"
            style={{
              background: "linear-gradient(to bottom, rgba(255,255,255,0.3), transparent)",
            }}
          />
        </div>
      </div>
    </div>
  );
}

/** 精致折叠面板 */
function CollapsibleSection({
  title,
  icon,
  badge,
  defaultOpen = false,
  children,
}: {
  title: string;
  icon?: ReactNode;
  badge?: string | number;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const contentRef = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | undefined>(defaultOpen ? undefined : 0);

  useEffect(() => {
    if (isOpen && contentRef.current) {
      setHeight(contentRef.current.scrollHeight);
      const timer = setTimeout(() => setHeight(undefined), 200);
      return () => clearTimeout(timer);
    } else {
      setHeight(0);
    }
  }, [isOpen]);

  return (
    <div
      className="rounded-lg overflow-hidden transition-shadow duration-200"
      style={{
        border: "0.5px solid rgba(0, 0, 0, 0.06)",
        boxShadow: isOpen
          ? "0 1px 3px rgba(0,0,0,0.06)"
          : "0 1px 2px rgba(0,0,0,0.04)",
      }}
    >
      <button
        className="w-full flex items-center justify-between px-3 py-2.5 text-left transition-colors duration-100 hover:bg-gray-50 active:bg-gray-100"
        onClick={() => setIsOpen(!isOpen)}
      >
        <div className="flex items-center gap-2">
          {icon && <span className="text-gray-400">{icon}</span>}
          <span className="text-[11px] font-semibold text-gray-900 tracking-wide">
            {title}
          </span>
          {badge !== undefined && (
            <span className="text-[9px] font-medium text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded-full">
              {badge}
            </span>
          )}
        </div>
        <div
          className="text-gray-400 transition-transform duration-200"
          style={{
            transform: isOpen ? "rotate(90deg)" : "rotate(0deg)",
          }}
        >
          <Icon name="chevronRight" size={12} />
        </div>
      </button>
      <div
        ref={contentRef}
        className="overflow-hidden transition-all duration-200 ease-in-out"
        style={{
          maxHeight: height !== undefined ? `${height}px` : undefined,
          opacity: isOpen ? 1 : 0,
        }}
      >
        <div className="px-3 pb-3 pt-1">{children}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layer 1: 核心身份卡片
// ---------------------------------------------------------------------------

function Layer1Identity({
  level,
  stageName,
  emotion,
  streakDays,
  totalChats,
}: {
  level: number;
  stageName: string;
  emotion: EmotionName;
  streakDays: number;
  totalChats: number;
}) {
  const params = getEmotionParams(emotion);

  return (
    <div
      className="flex items-start gap-3 p-3 rounded-xl"
      style={{
        background: "linear-gradient(135deg, rgba(99, 102, 241, 0.03), rgba(139, 92, 246, 0.02))",
        border: "0.5px solid rgba(99, 102, 241, 0.1)",
        boxShadow: "0 2px 8px rgba(99, 102, 241, 0.04)",
      }}
    >
      {/* 左侧：桌宠微缩形象（占位） */}
      <div
        className="w-20 h-20 rounded-xl flex items-center justify-center flex-shrink-0"
        style={{
          background: "rgba(0, 0, 0, 0.03)",
          border: "0.5px solid rgba(0, 0, 0, 0.06)",
        }}
      >
        <div className="text-4xl">🐱</div>
      </div>

      {/* 右侧：信息 */}
      <div className="flex-1 min-w-0">
        {/* 名称和等级 */}
        <div className="flex items-center gap-2 mb-1.5">
          <span className="text-base font-bold text-gray-900">墨灵</span>
          <span
            className="text-[10px] font-bold px-2 py-0.5 rounded-md"
            style={{
              color: "#92400e",
              background: "linear-gradient(90deg, #fef3c7, #fde68a)",
              border: "0.5px solid rgba(245, 158, 11, 0.2)",
            }}
          >
            Lv.{level} · {stageName}
          </span>
        </div>

        {/* 情绪状态 */}
        <div className="flex items-center gap-1.5 mb-2">
          <span className="text-sm">{getMoodEmoji(80)}</span>
          <span className="text-xs text-gray-600">{getMoodText(80)}</span>
          <span
            className="text-[9px] font-medium px-1.5 py-0.5 rounded"
            style={{
              color: params.color,
              background: params.colorSoft,
            }}
          >
            {params.label}
          </span>
        </div>

        {/* 统计徽章 */}
        <div className="flex items-center gap-2">
          {streakDays > 0 && (
            <div
              className="flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium"
              style={{
                color: streakDays >= 7 ? "#92400e" : "#4b5563",
                background: streakDays >= 7
                  ? "linear-gradient(90deg, #fef3c7, #fde68a)"
                  : "#f9fafb",
                border: streakDays >= 7 ? "0.5px solid rgba(245, 158, 11, 0.2)" : "0.5px solid rgba(0, 0, 0, 0.06)",
              }}
            >
              <span>{streakDays >= 7 ? "🔥" : "📅"}</span>
              <span>连续 {streakDays} 天</span>
            </div>
          )}
          {totalChats > 0 && (
            <div
              className="flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium text-gray-600"
              style={{
                background: "#f9fafb",
                border: "0.5px solid rgba(0, 0, 0, 0.06)",
              }}
            >
              <span>💬</span>
              <span>{totalChats} 次对话</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layer 2: 核心状态面板
// ---------------------------------------------------------------------------

function Layer2Stats({
  mood,
  vitality,
  intimacy,
  trust,
  empathy,
  level,
  growthProgress,
}: {
  mood: number;
  vitality: number;
  intimacy: number;
  trust: number;
  empathy: number;
  level: number;
  growthProgress: number;
}) {
  return (
    <div className="flex flex-col gap-3">
      {/* 心情和活力 */}
      <div
        className="grid grid-cols-2 gap-2 p-2.5 rounded-lg"
        style={{
          background: "#fafafa",
          border: "0.5px solid rgba(0, 0, 0, 0.05)",
        }}
      >
        <div className="flex items-center gap-2">
          <span className="text-lg">{getMoodEmoji(mood)}</span>
          <div className="flex-1">
            <div className="text-[10px] text-gray-500 font-medium mb-1">心情</div>
            <div className="text-xl font-bold text-gray-900 tabular-nums">{Math.round(mood)}</div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-lg">⚡</span>
          <div className="flex-1">
            <div className="text-[10px] text-gray-500 font-medium mb-1">活力</div>
            <div className="text-xl font-bold text-gray-900 tabular-nums">{Math.round(vitality)}</div>
          </div>
        </div>
      </div>

      {/* 关系指标 */}
      <div
        className="grid grid-cols-3 gap-1.5 p-2 rounded-lg"
        style={{
          background: "#fafafa",
          border: "0.5px solid rgba(0, 0, 0, 0.05)",
        }}
      >
        <div className="text-center">
          <div className="text-[9px] text-gray-500 font-medium mb-1">💗 亲密度</div>
          <div className="text-sm font-bold text-indigo-600 tabular-nums">{Math.round(intimacy)}</div>
        </div>
        <div className="text-center">
          <div className="text-[9px] text-gray-500 font-medium mb-1">🤝 信任度</div>
          <div className="text-sm font-bold text-indigo-600 tabular-nums">{Math.round(trust * 100)}</div>
        </div>
        <div className="text-center">
          <div className="text-[9px] text-gray-500 font-medium mb-1">✨ 默契度</div>
          <div className="text-sm font-bold text-indigo-600 tabular-nums">{Math.round(empathy)}</div>
        </div>
      </div>

      {/* 成长进度 */}
      <RefinedProgress
        value={growthProgress * 100}
        colorScheme="growth"
        size="md"
        showValue
        label={`✨ 成长 Lv.${level} → Lv.${level + 1}`}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layer 3: 成就与个性
// ---------------------------------------------------------------------------

function Layer3Achievements({
  achievements,
  personality,
}: {
  achievements: Achievement[];
  personality?: PersonalityProfile;
}) {
  return (
    <div className="flex flex-col gap-2">
      {/* 成就徽章 */}
      {achievements.length > 0 && (
        <div>
          <div className="text-[10px] text-gray-500 font-medium mb-1.5 px-1">最近成就</div>
          <div className="flex flex-wrap gap-1.5">
            {achievements.filter(a => a.unlocked).slice(0, 4).map((ach) => (
              <div
                key={ach.id}
                className="flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium"
                style={{
                  background: ach.unlocked ? "#f9fafb" : "transparent",
                  border: "0.5px solid rgba(0, 0, 0, 0.06)",
                  color: "#374151",
                }}
              >
                <span>{ach.icon}</span>
                <span>{ach.name}</span>
              </div>
            ))}
            {achievements.filter(a => a.unlocked).length > 4 && (
              <div className="flex items-center px-2 py-1 rounded-md text-[10px] text-gray-400">
                +{achievements.filter(a => a.unlocked).length - 4} 更多
              </div>
            )}
          </div>
        </div>
      )}

      {/* 人格指纹 */}
      {personality && (
        <div className="flex items-center gap-3 p-2 rounded-lg" style={{ background: "#fafafa" }}>
          <div className="text-[10px] text-gray-500 font-medium">🎭 人格</div>
          <div className="flex-1 grid grid-cols-4 gap-1">
            <div className="text-center">
              <div className="text-[8px] text-gray-400">活力</div>
              <div className="text-xs font-bold text-amber-600">{personality.energy}</div>
            </div>
            <div className="text-center">
              <div className="text-[8px] text-gray-400">创意</div>
              <div className="text-xs font-bold text-purple-600">{personality.creativity}</div>
            </div>
            <div className="text-center">
              <div className="text-[8px] text-gray-400">逻辑</div>
              <div className="text-xs font-bold text-blue-600">{personality.logic}</div>
            </div>
            <div className="text-center">
              <div className="text-[8px] text-gray-400">共情</div>
              <div className="text-xs font-bold text-pink-600">{personality.empathy}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layer 4: 详情与操作
// ---------------------------------------------------------------------------

function Layer4Details({
  growth,
  learned,
  proactiveEnabled,
  onToggleProactive,
  onOpenMemory,
  onOpenProofread,
}: {
  growth?: GrowthSnapshot | null;
  learned?: LearnedHistory | null;
  proactiveEnabled: boolean;
  onToggleProactive?: (enabled: boolean) => void;
  onOpenMemory?: () => void;
  onOpenProofread?: () => void;
}) {
  const skillCount = growth ? Object.keys(growth.skill_proficiency).length : 0;
  const memoryCount = learned?.stats.total ?? 0;

  return (
    <div className="flex flex-col gap-2">
      {/* 技能熟练度 */}
      {skillCount > 0 && (
        <CollapsibleSection title="技能熟练度" icon="⚒️" badge={skillCount}>
          <div className="flex flex-col gap-1.5">
            {Object.entries(growth!.skill_proficiency)
              .sort((a, b) => b[1].proficiency - a[1].proficiency)
              .slice(0, 3)
              .map(([name, p]) => (
                <div key={name} className="flex items-center gap-2">
                  <span className="text-[11px] text-gray-700 flex-1 truncate">{name}</span>
                  <span className="text-[10px] text-gray-400 tabular-nums">
                    {Math.round(p.proficiency * 100)}%
                  </span>
                </div>
              ))}
          </div>
        </CollapsibleSection>
      )}

      {/* 我们的记忆 */}
      {memoryCount > 0 && (
        <CollapsibleSection title="我们的记忆" icon="📚" badge={memoryCount}>
          <div className="flex flex-col gap-1">
            {learned?.items.slice(-3).reverse().map((it, i) => (
              <div key={`${it.ts}-${i}`} className="text-[10px] text-gray-600 leading-relaxed">
                <span className="text-gray-400">{relTime(it.ts)} </span>
                {it.fact.length > 40 ? it.fact.slice(0, 40) + "…" : it.fact}
              </div>
            ))}
          </div>
        </CollapsibleSection>
      )}

      {/* 主动模式 */}
      <div
        className="flex items-center justify-between p-2.5 rounded-lg"
        style={{
          background: "#fafafa",
          border: "0.5px solid rgba(0, 0, 0, 0.05)",
        }}
      >
        <div>
          <div className="text-[11px] font-medium text-gray-900">主动模式</div>
          <div className="text-[10px] text-gray-500 mt-0.5">
            {proactiveEnabled ? "桌宠会主动提供建议" : "桌宠等待你的指令"}
          </div>
        </div>
        <button
          className="relative w-10 h-6 rounded-full transition-colors duration-200"
          style={{
            background: proactiveEnabled ? "#6366f1" : "#e5e7eb",
          }}
          onClick={() => onToggleProactive?.(!proactiveEnabled)}
        >
          <div
            className="absolute top-1 w-4 h-4 rounded-full bg-white shadow-sm transition-transform duration-200"
            style={{
              left: proactiveEnabled ? "calc(100% - 1.25rem)" : "0.25rem",
            }}
          />
        </button>
      </div>

      {/* 操作按钮 */}
      <div className="flex gap-2 mt-1">
        {onOpenMemory && (
          <button
            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[11px] font-medium text-gray-700 transition-all duration-100 hover:bg-gray-100 active:scale-[0.98]"
            style={{
              background: "#f9fafb",
              border: "0.5px solid rgba(0, 0, 0, 0.06)",
            }}
            onClick={onOpenMemory}
          >
            <Icon name="folder" size={13} />
            查看记忆
          </button>
        )}
        {onOpenProofread && (
          <button
            className="flex-1 flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-[11px] font-medium text-gray-700 transition-all duration-100 hover:bg-gray-100 active:scale-[0.98]"
            style={{
              background: "#f9fafb",
              border: "0.5px solid rgba(0, 0, 0, 0.06)",
            }}
            onClick={onOpenProofread}
          >
            <Icon name="pencil" size={13} />
            文稿编校
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function StatusPanelV2({
  emotion,
  open,
  onClose,
  onOpenMemory,
  onOpenProofread,
  proactiveEnabled = true,
  onToggleProactive,
  learned = null,
  growth = null,
  achievements = [],
  personality,
  streakDays = 0,
  totalChats = 0,
}: StatusPanelV2Props) {
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

  // 计算数值
  const level = growth?.level ?? 1;
  const stageName = level >= 10 ? "墨仙" : level >= 7 ? "墨客" : level >= 4 ? "墨生" : "墨童";
  const mood = growth?.mood ?? 80;
  const vitality = growth?.vitality ?? 100;
  const intimacy = growth?.intimacy ?? 0;
  const trust = growth?.trust ?? 0;
  const empathy = growth?.empathy ?? 0;
  const growthProgress = growth?.level_progress?.progress ?? 0;

  return (
    <div
      className="fixed z-50 animate-in fade-in slide-in-from-bottom-2 duration-200"
      style={{
        right: 12,
        bottom: 12,
        width: 340,
        opacity: open ? 1 : 0,
        transition: "opacity 200ms ease-out",
      }}
    >
      <div
        className="rounded-2xl overflow-hidden"
        style={{
          background: "white",
          border: "0.5px solid rgba(0, 0, 0, 0.08)",
          boxShadow: `
            0 1px 2px rgba(0, 0, 0, 0.04),
            0 4px 12px rgba(0, 0, 0, 0.06),
            0 12px 36px rgba(0, 0, 0, 0.08)
          `,
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-4 py-3"
          style={{
            borderBottom: "0.5px solid rgba(0, 0, 0, 0.06)",
            background: "linear-gradient(to bottom, rgba(255,255,255,0.8), rgba(255,255,255,0.6))",
          }}
        >
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-gray-500 font-medium">状态面板</span>
          </div>
          <button
            className="w-6 h-6 rounded-lg flex items-center justify-center text-gray-400 transition-colors duration-100 hover:text-gray-600 hover:bg-gray-100"
            onClick={onClose}
            aria-label="Close"
          >
            <Icon name="x" size={14} />
          </button>
        </div>

        {/* Body */}
        <div
          className="p-3 flex flex-col gap-3 overflow-y-auto"
          style={{ maxHeight: "min(70vh, 480px)" }}
        >
          {/* Layer 1: 核心身份 */}
          <Layer1Identity
            level={level}
            stageName={stageName}
            emotion={emotion}
            streakDays={streakDays}
            totalChats={totalChats}
          />

          {/* Layer 2: 核心状态 */}
          <Layer2Stats
            mood={mood}
            vitality={vitality}
            intimacy={intimacy}
            trust={trust}
            empathy={empathy}
            level={level}
            growthProgress={growthProgress}
          />

          {/* Layer 3: 成就与个性 */}
          {(achievements.length > 0 || personality) && (
            <Layer3Achievements achievements={achievements} personality={personality} />
          )}

          {/* Layer 4: 详情与操作 */}
          <Layer4Details
            growth={growth}
            learned={learned}
            proactiveEnabled={proactiveEnabled}
            onToggleProactive={onToggleProactive}
            onOpenMemory={onOpenMemory}
            onOpenProofread={onOpenProofread}
          />
        </div>
      </div>
    </div>
  );
}
