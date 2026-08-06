/**
 * BadgeMedal — 一枚真实矢量奖章，作为所有勋章的共用底图。
 *
 * 设计：金属外圈 + 一圈珠链 + 珐琅彩面 + 顶部缎带。
 * 复用规则（后端 BADGES 只持有 glyph/category/rarity，换色换字在前端做）：
 * - 底层颜色按稀有度变（凡品钢灰 / 珍品蓝 / 秘宝紫 / 传世金）
 * - 中央字体按门类变（相伴楷 / 笔耕宋 / 修行黑 / 博闻仿宋 / 奇遇行楷）
 * - 隐藏勋章未解锁时整体灰化、中央显示「?」
 *
 * 纯 SVG（非位图），任意尺寸清晰；可整体导出为 PNG/SVG 资源。
 */

import { useId } from "react";
import type { BadgeRarity } from "./BadgeWall";

interface MedalPalette {
  bg: string;
  border: string;
  text: string;
  rim: [string, string, string];
  rimStroke: string;
  ribbon: string;
}

const PALETTE: Record<BadgeRarity, MedalPalette> = {
  common: { bg: "#f1efe8", border: "#b4b2a9", text: "#5f5e5a",
    rim: ["#e9ebef", "#aeb4bd", "#f2f4f6"], rimStroke: "#8b929c", ribbon: "#9aa0a8" },
  rare: { bg: "#e6f1fb", border: "#378add", text: "#185fa5",
    rim: ["#cfe4fb", "#4f8fd6", "#e3f0ff"], rimStroke: "#2f6fb0", ribbon: "#378add" },
  epic: { bg: "#eeedfe", border: "#7f77dd", text: "#534ab7",
    rim: ["#ddd9fb", "#7a6fd6", "#efeefe"], rimStroke: "#5a4fbf", ribbon: "#7f77dd" },
  legendary: { bg: "#faeeda", border: "#ef9f27", text: "#854f0b",
    rim: ["#f7e6a6", "#c9962f", "#fbf0c4"], rimStroke: "#9c6f1e", ribbon: "#ef9f27" },
};

// 门类 -> 中央字体（字体变化）
const FONT: Record<string, string> = {
  companionship: "'Kaiti SC','KaiTi','STKaiti',serif",
  writing: "'Songti SC','STSong','SimSun',serif",
  cultivation: "'PingFang SC','Hiragino Sans GB',sans-serif",
  knowledge: "'FangSong','STFangsong',serif",
  serendipity: "'Xingkai SC','STXingkai','KaiTi',serif",
};

interface Props {
  rarity: BadgeRarity;
  /** 徽记汉字（单字） */
  glyph: string;
  /** 门类 id，决定中央字体 */
  category: string;
  /** 隐藏且未解锁 -> 灰化 + 显示「?」 */
  masked?: boolean;
  /** 像素尺寸（正方形） */
  size?: number;
}

export function BadgeMedal({ rarity, glyph, category, masked = false, size = 64 }: Props) {
  const uid = useId().replace(/:/g, "");
  const p: MedalPalette = masked
    ? { bg: "#e7e7e7", border: "#bcbcbc", text: "#9a9a9a",
        rim: ["#e9ebef", "#b9bcc2", "#f2f4f6"], rimStroke: "#9aa0a8", ribbon: "#bcbcbc" }
    : PALETTE[rarity];
  const font = FONT[category] ?? "sans-serif";
  const rimId = `rim-${uid}`;
  const fieldId = `field-${uid}`;

  // 珠链：绕外圈 40 颗小点
  const beads = Array.from({ length: 40 }, (_, i) => {
    const a = (Math.PI * 2 * i) / 40;
    return { x: 60 + 31 * Math.cos(a), y: 66 + 31 * Math.sin(a) };
  });

  return (
    <svg
      className="badge-medal"
      viewBox="0 0 120 120"
      width={size}
      height={size}
      style={masked ? { filter: "grayscale(1)", opacity: 0.55 } : undefined}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={rimId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={p.rim[0]} />
          <stop offset="0.5" stopColor={p.rim[1]} />
          <stop offset="1" stopColor={p.rim[2]} />
        </linearGradient>
        <radialGradient id={fieldId} cx="0.5" cy="0.42" r="0.7">
          <stop offset="0" stopColor="#ffffff" stopOpacity={0.55} />
          <stop offset="0.55" stopColor={p.bg} stopOpacity={1} />
          <stop offset="1" stopColor={p.bg} stopOpacity={1} />
        </radialGradient>
      </defs>

      {/* 缎带（在奖章之后） */}
      <polygon points="44,4 57,4 54,42 50,49 46,42" fill={p.ribbon} />
      <polygon points="63,4 76,4 74,42 70,49 66,42" fill={p.ribbon} />
      <polygon points="52,5 56,5 55,40 53.5,44 52,40" fill="#ffffff" opacity={0.35} />
      <polygon points="64,5 68,5 68,40 66.5,44 65,40" fill="#ffffff" opacity={0.35} />

      {/* 金属外圈 */}
      <circle cx="60" cy="66" r="38" fill={`url(#${rimId})`} stroke={p.rimStroke} strokeWidth={1.5} />
      {beads.map((b, i) => (
        <circle key={i} cx={b.x} cy={b.y} r={1.5} fill={p.rim[2]} />
      ))}

      {/* 珐琅彩面 */}
      <circle cx="60" cy="66" r="27" fill={`url(#${fieldId})`} stroke={p.border} strokeWidth={2.5} />

      {/* 中央字（字体按门类） */}
      <text
        x="60"
        y="66"
        fontFamily={font}
        fontSize={30}
        fontWeight={800}
        fill={p.text}
        textAnchor="middle"
        dominantBaseline="central"
      >
        {masked ? "?" : glyph}
      </text>
    </svg>
  );
}
