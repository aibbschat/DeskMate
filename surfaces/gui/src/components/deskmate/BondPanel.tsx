/**
 * BondPanel — 关系阶段 + 默契细分（8.2「越用越懂你」的前端可视化）。
 *
 * 消费后端 growth snapshot 的 bond / empathy_domains 字段：
 * - bond.depth  → 关系深度（封顶不浪费：数值满格后溢出转入的长期轨道）
 * - bond.stage  → 关系阶段（初识 → 熟络 → 默契 → 知己 → 灵魂伴侣）
 * - empathy_domains.domains → 默契细分四子域（创作/记忆/主动/陪伴）
 *
 * 设计原则（呼应 8.0 红线「懂你 = 可感可看」）：
 * 让「越用越懂你」不只是后端数值，而是用户能一眼看到的、有温度的成长。
 * 无数据时静默返回 null（向后兼容：后端旧版本没有这些字段时界面不受影响）。
 */

import type { GrowthSnapshot } from "./StatusPanel";

const DOMAIN_LABELS: Record<string, string> = {
  creation: "创作",
  memory: "记忆",
  proactive: "主动",
  companion: "陪伴",
};

const STAGE_EMOJI: Record<string, string> = {
  初识: "🌱",
  熟络: "🌿",
  默契: "💞",
  知己: "🫂",
  灵魂伴侣: "💫",
};

function DomainBar({ label, value, top }: { label: string; value: number; top: boolean }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] text-gray-500 w-6 shrink-0">{label}</span>
      <div className="flex-1 h-1 rounded-full overflow-hidden" style={{ background: "#f0f0f0" }}>
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${pct}%`,
            background: top ? "#6366f1" : "#d4d4d8",
          }}
        />
      </div>
      <span
        className="text-[10px] tabular-nums shrink-0"
        style={{ color: top ? "#4f46e5" : "#9ca3af", fontWeight: top ? 600 : 400 }}
      >
        {Math.round(value)}
      </span>
    </div>
  );
}

export function BondPanel({ growth }: { growth?: GrowthSnapshot | null }) {
  const bond = growth?.bond;
  const domains = growth?.empathy_domains;
  if (!bond && !domains) return null;

  const stage = bond?.stage ?? "";
  const depth = bond?.depth ?? 0;
  const progress = bond?.progress ?? 0;
  const nextTh = bond?.next_threshold ?? null;
  const maxed = bond?.maxed ?? false;
  const emoji = STAGE_EMOJI[stage] ?? "💞";

  const domainEntries = Object.entries(domains?.domains ?? {}).filter(([, v]) => v > 0);
  const topDomain = domains?.top ?? "";

  return (
    <div
      className="rounded-xl p-3 flex flex-col gap-2"
      style={{
        background: "linear-gradient(to bottom, rgba(99,102,241,0.06), rgba(255,255,255,0))",
        border: "0.5px solid rgba(99,102,241,0.12)",
      }}
    >
      {/* 关系阶段 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-[12px]">{emoji}</span>
          <span className="text-[11px] font-semibold text-gray-900">
            {stage || "关系"}
          </span>
          <span className="text-[10px] text-gray-400">· 深度 {Math.round(depth)}</span>
        </div>
        {!maxed && nextTh != null && (
          <span className="text-[10px] text-gray-400 tabular-nums">
            到「{nextThName(nextTh)}」还需 {Math.max(0, Math.round((nextTh - depth) * 10) / 10)}
          </span>
        )}
      </div>
      {!maxed && (
        <div className="h-1 rounded-full overflow-hidden" style={{ background: "#eef0ff" }}>
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{ width: `${Math.round(progress * 100)}%`, background: "#6366f1" }}
          />
        </div>
      )}

      {/* 默契细分 */}
      {domainEntries.length > 0 && (
        <div className="flex flex-col gap-1 mt-0.5">
          {domainEntries
            .sort((a, b) => b[1] - a[1])
            .map(([key, value]) => (
              <DomainBar
                key={key}
                label={DOMAIN_LABELS[key] ?? key}
                value={value}
                top={key === topDomain}
              />
            ))}
        </div>
      )}
    </div>
  );
}

/** 下一阶段名（由阈值反查；后端只给阈值数字，这里用常量表保持一致）。 */
function nextThName(th: number): string {
  const table: Array<[number, string]> = [
    [30, "熟络"],
    [90, "默契"],
    [200, "知己"],
    [400, "灵魂伴侣"],
  ];
  for (const [t, name] of table) {
    if (th <= t) return name;
  }
  return "更高阶段";
}
