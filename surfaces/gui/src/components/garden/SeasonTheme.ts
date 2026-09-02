/**
 * SeasonTheme — 花园的四季 × 昼夜主题（前端确定性计算）。
 *
 * 与后端 deskmate/garden/season.py 同一套语义（本地时间派生，零依赖）：
 * - 季节：月份 → 春/夏/秋/冬
 * - 昼夜：小时 → 晨/昼/黄昏/深夜
 *
 * 返回 CSS 变量对象（内联 style 注入 garden-root），四季与昼夜各成一组，
 * 切换只换变量不换组件。颜色延续现有小桔暖橙体系（styles.css .profile-*）。
 */

export type SeasonKey = "spring" | "summer" | "autumn" | "winter";
export type DaypartKey = "morning" | "day" | "dusk" | "night";

export const SEASONS: Record<SeasonKey, { label: string; emoji: string }> = {
  spring: { label: "春", emoji: "🌸" },
  summer: { label: "夏", emoji: "🌿" },
  autumn: { label: "秋", emoji: "🍁" },
  winter: { label: "冬", emoji: "❄️" },
};

export const DAYPARTS: Record<DaypartKey, { label: string; emoji: string }> = {
  morning: { label: "晨", emoji: "🌅" },
  day: { label: "昼", emoji: "☀️" },
  dusk: { label: "黄昏", emoji: "🌇" },
  night: { label: "深夜", emoji: "🌙" },
};

export function seasonOf(d: Date = new Date()): SeasonKey {
  const m = d.getMonth() + 1;
  if (m >= 3 && m <= 5) return "spring";
  if (m >= 6 && m <= 8) return "summer";
  if (m >= 9 && m <= 11) return "autumn";
  return "winter";
}

export function daypartOf(d: Date = new Date()): DaypartKey {
  const h = d.getHours();
  if (h >= 5 && h < 11) return "morning";
  if (h >= 11 && h < 17) return "day";
  if (h >= 17 && h < 20) return "dusk";
  return "night";
}

/** 四季天空与叶色的基调（后面叠昼夜光照）。 */
const SEASON_BASE: Record<SeasonKey, { sky: [string, string]; leaf: string; soil: string }> = {
  spring: { sky: ["#FFE8F0", "#A8D8EA"], leaf: "#8FBF6A", soil: "#9C7A4F" },
  summer: { sky: ["#E8F7E9", "#9FD9B4"], leaf: "#4E9B5A", soil: "#8A6A44" },
  autumn: { sky: ["#FFE3C2", "#FFB77E"], leaf: "#D98A3D", soil: "#7A5230" },
  winter: { sky: ["#EAF0F8", "#C8DAEC"], leaf: "#5E7D8C", soil: "#8B97A5" },
};

/** 昼夜光照层：叠加在季节天空之上。 */
const DAYPART_TINT: Record<DaypartKey, { top: string; bottom: string; lamp: string; ink: string }> = {
  morning: { top: "rgba(255,214,150,0.55)", bottom: "rgba(255,170,90,0.30)", lamp: "#FFC24B", ink: "#5B4636" },
  day: { top: "rgba(255,255,255,0.0)", bottom: "rgba(255,255,255,0.0)", lamp: "#FFC24B", ink: "#3B2A22" },
  dusk: { top: "rgba(255,150,110,0.40)", bottom: "rgba(255,110,80,0.30)", lamp: "#FFB84D", ink: "#4A3428" },
  night: { top: "rgba(30,42,72,0.55)", bottom: "rgba(10,18,40,0.55)", lamp: "#FFE08A", ink: "#EAD9C8" },
};

/** 花园主题变量（注入 garden-root）。 */
export function gardenTheme(
  season: SeasonKey,
  daypart: DaypartKey,
): React.CSSProperties {
  const base = SEASON_BASE[season];
  const tint = DAYPART_TINT[daypart];
  return {
    "--garden-sky-1": base.sky[0],
    "--garden-sky-2": base.sky[1],
    "--garden-leaf": base.leaf,
    "--garden-soil": base.soil,
    "--garden-lamp": tint.lamp,
    "--garden-ink": tint.ink,
    "--garden-tint-top": tint.top,
    "--garden-tint-bottom": tint.bottom,
  } as React.CSSProperties;
}
