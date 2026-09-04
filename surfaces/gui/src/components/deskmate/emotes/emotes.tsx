/**
 * emotes.tsx — 小桔专属表情包渲染实现（多 Pack / 主题化）。
 *
 * 设计：语义（有哪些表情、各自含义/触发场景）在 emotes.manifest.json 单一真相源定义；
 * 本文件只承载「渲染实现」——每个表情 id 对应一个 SVG 组件。
 * 通过 EmoteTheme 调色板，同一套 id 可以有多种画风（Pack）：
 *   - moling-default：黄色圆脸 + 大五官（微信默认表情风，小尺寸也清晰）
 *   - moling-mono：白底线描单色风（另一种画风，验证「什么表情都能接入」的 Pack 机制）
 *
 * 切换 Pack 不改语义、不改代码，只换 manifest 里的 pack + 这里的 EmoteTheme。
 * 各端（前端 SVG、未来其它端）按 id 取自己的实现即可——这就是「自有接入标准」。
 */

import type { FC, ReactNode } from "react";
import { EMOTE_MANIFEST } from "./emoteManifest";

/** 表情调色板：一个 Pack 就是一套 EmoteTheme。 */
export interface EmoteTheme {
  skin: string;
  skinDark: string;
  stroke: string;
  eye: string;
  mouth: string;
  blush: string;
  white: string;
  hand: string;
  heart: string;
  flame: string;
  sweat: string;
  bulb: string;
  bulbLine: string;
  metal: string;
  dot1: string;
  dot2: string;
  dot3: string;
  envLine: string;
  envLine2: string;
  ringFill: string;
  ring: string;
  zzz: string;
  tea: string;
  mask: string;
}

/** 默认黄脸主题（还原此前黄脸圆脸观感）。 */
export const THEME_YELLOW: EmoteTheme = {
  skin: "#FFD54F",
  skinDark: "#FFB300",
  stroke: "#E69600",
  eye: "#2C2C2C",
  mouth: "#2C2C2C",
  blush: "#FF8A80",
  white: "#FFFFFF",
  hand: "#FFB300",
  heart: "#E91E63",
  flame: "#D84315",
  sweat: "#81D4FA",
  bulb: "#FFEB3B",
  bulbLine: "#F0A700",
  metal: "#BDBDBD",
  dot1: "#42A5F5",
  dot2: "#EC407A",
  dot3: "#66BB6A",
  envLine: "#BDBDBD",
  envLine2: "#9E9E9E",
  ringFill: "rgba(255,255,255,0.35)",
  ring: "#4FC3F7",
  zzz: "#90A4AE",
  tea: "#8D6E63",
  mask: "#5D4037",
};

/** 线描单色主题：白底 + 深灰描边 + 墨色五官/道具，与黄脸明显区分。 */
export const THEME_MONO: EmoteTheme = {
  skin: "#FFFFFF",
  skinDark: "#ECEFF1",
  stroke: "#2C2C2C",
  eye: "#2C2C2C",
  mouth: "#2C2C2C",
  blush: "#E0E0E0",
  white: "#FAFAFA",
  hand: "#ECEFF1",
  heart: "#2C2C2C",
  flame: "#2C2C2C",
  sweat: "#9E9E9E",
  bulb: "#2C2C2C",
  bulbLine: "#9E9E9E",
  metal: "#9E9E9E",
  dot1: "#2C2C2C",
  dot2: "#2C2C2C",
  dot3: "#2C2C2C",
  envLine: "#9E9E9E",
  envLine2: "#BDBDBD",
  ringFill: "rgba(0,0,0,0.06)",
  ring: "#2C2C2C",
  zzz: "#9E9E9E",
  tea: "#9E9E9E",
  mask: "rgba(0,0,0,0.18)",
};

/** 小桔桔子主题：以小桔吉祥物为原型——桔子帽（橙+绿叶）、棕发刘海、浅肤色圆脸。
 *  字段复用：hand→桔子帽橙, mask→头发棕, dot3→叶子绿, envLine2→叶子深绿。
 *  与黄圆脸/线描明显区分：脸底不是纯圆，而是「桔子帽+脸+刘海」的组合造型。 */
export const THEME_XIAOJU: EmoteTheme = {
  skin: "#FFE0C2",
  skinDark: "#FFCC99",
  stroke: "#6D4C41",
  eye: "#4E342E",
  mouth: "#4E342E",
  blush: "#FF9E80",
  white: "#FFFFFF",
  hand: "#FF9F43",
  heart: "#E91E63",
  flame: "#D84315",
  sweat: "#81D4FA",
  bulb: "#FFEB3B",
  bulbLine: "#F0A700",
  metal: "#BDBDBD",
  dot1: "#42A5F5",
  dot2: "#EC407A",
  dot3: "#66BB6A",
  envLine: "#BDBDBD",
  envLine2: "#2E7D32",
  ringFill: "rgba(255,255,255,0.35)",
  ring: "#4FC3F7",
  zzz: "#90A4AE",
  tea: "#8D6E63",
  mask: "#5D4037",
};

/** 圆脸底色 + 高光，表情在之上叠加（moling-default 黄脸包用）。 */
export function FaceBg({
  children,
  theme,
}: {
  children?: ReactNode;
  theme: EmoteTheme;
}) {
  return (
    <g>
      <circle cx={32} cy={32} r={29} fill={theme.skin} stroke={theme.stroke} strokeWidth={2} />
      <ellipse cx={23} cy={21} rx={7} ry={4.5} fill={theme.white} opacity={0.22} />
      {children}
    </g>
  );
}

/** 方贴纸脸底（moling-mono 线描包用）：白底圆角方卡 + 墨色描边，与黄圆脸明显区分。 */
export function FaceCard({
  children,
  theme,
}: {
  children?: ReactNode;
  theme: EmoteTheme;
}) {
  return (
    <g>
      <rect x={6} y={6} width={52} height={52} rx={15} fill={theme.skin} stroke={theme.stroke} strokeWidth={2} />
      {children}
    </g>
  );
}

/** 小桔脸底（xiaoju-orange 包用）：桔子帽（橙+绿叶绿梗）+ 浅肤色圆脸 + 棕色刘海 + 两侧头发。
 *  帽子覆盖到 y≈33，脸可见部分 y=33~62，五官集中在 y=42~56。
 *  字段映射：theme.hand=帽橙, theme.mask=发棕, theme.dot3=叶绿, theme.envLine2=叶深绿。 */
export function XiaojuFaceBg({
  children,
  theme,
}: {
  children?: ReactNode;
  theme: EmoteTheme;
}) {
  const hat = theme.hand;
  const hatDark = theme.skinDark; // 用肤色深做帽檐阴影
  const hair = theme.mask;
  const leaf = theme.dot3;
  const leafDark = theme.envLine2;
  return (
    <g>
      {/* 脸（浅肤色圆，略偏下，帽子遮住上半） */}
      <circle cx={32} cy={41} r={23} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.5} />
      {/* 两侧头发（棕色，从帽檐垂到脸颊旁） */}
      <path d="M9 38 Q6 50 12 58 L17 55 Q12 47 15 38 Z" fill={hair} />
      <path d="M55 38 Q58 50 52 58 L47 55 Q52 47 49 38 Z" fill={hair} />
      {/* 桔子帽主体（橙色，覆盖头顶到 y=34） */}
      <path
        d="M3 33 Q3 5 32 4 Q61 5 61 33 Q61 37 56 37 L8 37 Q3 37 3 33 Z"
        fill={hat}
        stroke={theme.stroke}
        strokeWidth={1.5}
      />
      {/* 帽子纹理（桔子瓣的浅弧线，增加辨识度） */}
      <path d="M14 14 Q20 22 16 32" stroke={hatDark} strokeWidth={1} fill="none" opacity={0.4} />
      <path d="M50 14 Q44 22 48 32" stroke={hatDark} strokeWidth={1} fill="none" opacity={0.4} />
      <path d="M32 6 Q30 18 32 33" stroke={hatDark} strokeWidth={1} fill="none" opacity={0.3} />
      {/* 帽子高光 */}
      <ellipse cx={19} cy={15} rx={6.5} ry={3.2} fill={theme.white} opacity={0.3} transform="rotate(-22 19 15)" />
      {/* 绿梗 */}
      <line x1={32} y1={5} x2={32} y2={0.5} stroke={leafDark} strokeWidth={2.5} strokeLinecap="round" />
      {/* 绿叶（两片，左右展开） */}
      <path d="M32 4 Q39 -1 45 3 Q41 9 32 7 Z" fill={leaf} stroke={leafDark} strokeWidth={0.8} />
      <path d="M32 4 Q25 -1 19 3 Q23 9 32 7 Z" fill={leaf} stroke={leafDark} strokeWidth={0.8} opacity={0.85} />
      {/* 棕色刘海（帽檐下方，波浪形三缕） */}
      <path
        d="M10 37 Q16 44 22 37 Q28 44 32 37 Q36 44 42 37 Q48 44 54 37 L54 34 L10 34 Z"
        fill={hair}
      />
      {/* 脸的腮红底晕（很淡，所有表情通用） */}
      <ellipse cx={17} cy={50} rx={4.5} ry={2.8} fill={theme.blush} opacity={0.25} />
      <ellipse cx={47} cy={50} rx={4.5} ry={2.8} fill={theme.blush} opacity={0.25} />
      {/* 脸侧高光 */}
      <ellipse cx={21} cy={47} rx={4} ry={2.5} fill={theme.white} opacity={0.12} />
      {children}
    </g>
  );
}

/* ----------------------------- 复用小零件（主题化） ----------------------------- */

function EyeSmile(cx: number, cy: number, theme: EmoteTheme) {
  return (
    <path
      d={`M${cx - 4} ${cy} q4 -4 8 0`}
      stroke={theme.eye}
      strokeWidth={3}
      fill="none"
      strokeLinecap="round"
    />
  );
}

function EyeOpen(cx: number, cy: number, lookX = 0, lookY = 0, theme?: EmoteTheme) {
  const eye = theme?.eye ?? "#2C2C2C";
  return (
    <g>
      <circle cx={cx} cy={cy} r={5} fill="#FFFFFF" stroke={eye} strokeWidth={1.5} />
      <circle cx={cx + lookX} cy={cy + lookY} r={2} fill={eye} />
    </g>
  );
}

function EyeFlat(cx: number, cy: number, theme: EmoteTheme) {
  return (
    <line
      x1={cx - 4}
      y1={cy}
      x2={cx + 4}
      y2={cy}
      stroke={theme.eye}
      strokeWidth={3}
      strokeLinecap="round"
    />
  );
}

function MouthSmile(open = false, theme?: EmoteTheme) {
  const mouth = theme?.mouth ?? "#2C2C2C";
  if (open) {
    return <path d="M24 44 q8 9 16 0 Z" fill={mouth} />;
  }
  return (
    <path
      d="M25 45 q7 5 14 0"
      stroke={mouth}
      strokeWidth={3}
      fill="none"
      strokeLinecap="round"
    />
  );
}

function Blush(theme: EmoteTheme) {
  return (
    <g fill={theme.blush} opacity={0.5}>
      <ellipse cx={17} cy={39} rx={4} ry={2.5} />
      <ellipse cx={47} cy={39} rx={4} ry={2.5} />
    </g>
  );
}

/* ------------------------------- 表情（主题化） ------------------------------- */

/** 招手问好 */
const WaveFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 33, theme)}
    {EyeSmile(39, 33, theme)}
    {MouthSmile(false, theme)}
    {Blush(theme)}
    <ellipse cx={50} cy={29} rx={6} ry={7.5} fill={theme.hand} stroke={theme.stroke} strokeWidth={1.5}>
      <animateTransform
        attributeName="transform"
        type="rotate"
        values="-12 50 42;12 50 42;-12 50 42"
        dur="1.1s"
        repeatCount="indefinite"
      />
    </ellipse>
    <g stroke={theme.eye} strokeWidth={2.2} strokeLinecap="round">
      <line x1={47} y1={24} x2={46} y2={18} />
      <line x1={50} y1={23} x2={50} y2={17} />
      <line x1={53} y1={24} x2={54} y2={18} />
    </g>
  </g>
);

/** 撒花庆祝 */
const CheerFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 34, theme)}
    {EyeSmile(39, 34, theme)}
    {MouthSmile(true, theme)}
    {Blush(theme)}
    <ellipse cx={13} cy={18} rx={5} ry={7} fill={theme.hand} transform="rotate(-25 13 25)" />
    <ellipse cx={51} cy={18} rx={5} ry={7} fill={theme.hand} transform="rotate(25 51 25)" />
    <circle cx={9} cy={32} r={2} fill={theme.dot1} />
    <circle cx={55} cy={34} r={2} fill={theme.dot2} />
    <circle cx={32} cy={8} r={2} fill={theme.dot3} />
  </g>
);

/** 灵感来了 */
const IdeaFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <circle cx={32} cy={9} r={5} fill={theme.bulb} stroke={theme.bulbLine} strokeWidth={1.5}>
      <animate attributeName="opacity" values="1;0.55;1" dur="1.6s" repeatCount="indefinite" />
    </circle>
    <rect x={29} y={13} width={6} height={3} rx={1.5} fill={theme.metal} />
    <g stroke={theme.bulbLine} strokeWidth={1.8} strokeLinecap="round">
      <line x1={32} y1={1} x2={32} y2={4} />
      <line x1={24} y1={4} x2={26} y2={7} />
      <line x1={40} y1={4} x2={38} y2={7} />
    </g>
    {EyeOpen(24, 33, 0, -1, theme)}
    {EyeOpen(40, 33, 0, -1, theme)}
    {MouthSmile(false, theme)}
  </g>
);

/** 害羞捂脸 */
const ShyFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M21 35 q3 3 6 0" stroke={theme.eye} strokeWidth={2.8} fill="none" strokeLinecap="round" />
    <path d="M37 35 q3 3 6 0" stroke={theme.eye} strokeWidth={2.8} fill="none" strokeLinecap="round" />
    {MouthSmile(false, theme)}
    {Blush(theme)}
    <ellipse cx={14} cy={39} rx={4} ry={5} fill={theme.hand} transform="rotate(20 14 39)" />
    <ellipse cx={50} cy={39} rx={4} ry={5} fill={theme.hand} transform="rotate(-20 50 39)" />
  </g>
);

/** 嘚瑟比耶 */
const VFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 33, theme)}
    {EyeSmile(39, 33, theme)}
    {MouthSmile(true, theme)}
    {Blush(theme)}
    <ellipse cx={50} cy={22} rx={5} ry={6} fill={theme.hand} stroke={theme.stroke} strokeWidth={1.2} />
    <g stroke={theme.eye} strokeWidth={2.5} strokeLinecap="round">
      <line x1={47} y1={18} x2={46} y2={10} />
      <line x1={53} y1={18} x2={54} y2={10} />
    </g>
  </g>
);

/** 困到睁不开 */
const SleepyFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeFlat(24, 34, theme)}
    {EyeFlat(40, 34, theme)}
    <ellipse cx={32} cy={47} rx={3} ry={4} fill={theme.mouth} />
    <g stroke={theme.zzz} strokeWidth={2.2} strokeLinecap="round">
      <line x1={44} y1={22} x2={46} y2={18} />
      <line x1={48} y1={15} x2={51} y2={10} />
      <line x1={52} y1={8} x2={56} y2={3} />
    </g>
  </g>
);

/** 摸鱼喝茶 */
const TeaFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 34, theme)}
    {EyeSmile(39, 34, theme)}
    {MouthSmile(false, theme)}
    {Blush(theme)}
    <path d="M42 49 h12 a6 6 0 0 1 -12 0 Z" fill={theme.white} stroke={theme.metal} strokeWidth={1.5} />
    <path d="M44 51 h8 a5 5 0 0 1 -10 0 Z" fill={theme.tea} />
    <g stroke={theme.metal} strokeWidth={1.5} strokeLinecap="round" fill="none">
      <path d="M46 48 q2 -5 0 -9" />
      <path d="M51 48 q2 -5 0 -9" />
    </g>
  </g>
);

/** 安慰抱抱 */
const HugFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 33, theme)}
    {EyeSmile(39, 33, theme)}
    {MouthSmile(true, theme)}
    {Blush(theme)}
    <path
      d="M32 42 c-3 -6 -11 -3 -11 3 c0 5 7 8 11 11 c4 -3 11 -6 11 -11 c0 -6 -8 -9 -11 -3 Z"
      fill={theme.heart}
    />
  </g>
);

/** 卡文抓狂 */
const StuckFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 30 L27 35 M27 30 L20 35" stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <path d="M37 30 L44 35 M44 30 L37 35" stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <path d="M24 48 q8 -5 16 0 Z" fill={theme.mouth} />
    <path d="M32 5 q5 6 2 11 q-6 -1 -3 -7 q-4 5 -1 9 q6 4 8 -2 q2 -7 -6 -11 Z" fill={theme.flame} />
    <path d="M50 29 q2 4 0 6 q-2 -2 0 -6 Z" fill={theme.sweat} />
  </g>
);

/** 扶额无语 */
const FacepalmFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeFlat(24, 34, theme)}
    {EyeFlat(40, 34, theme)}
    <line x1={28} y1={49} x2={36} y2={49} stroke={theme.mouth} strokeWidth={3} strokeLinecap="round" />
    <ellipse cx={22} cy={21} rx={7} ry={8} fill={theme.hand} stroke={theme.stroke} strokeWidth={1.2} transform="rotate(-15 22 21)" />
  </g>
);

/** 暗中观察 */
const PeekFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <rect x={5} y={5} width={24} height={54} rx={3} fill={theme.mask} opacity={0.2} />
    {EyeOpen(38, 33, 1, -1, theme)}
    <path d="M30 47 q6 4 12 0" stroke={theme.mouth} strokeWidth={3} fill="none" strokeLinecap="round" />
    <ellipse cx={14} cy={36} rx={4} ry={5} fill={theme.hand} transform="rotate(15 14 36)" />
  </g>
);

/** 鼓掌 */
const ClapFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeSmile(25, 33, theme)}
    {EyeSmile(39, 33, theme)}
    {MouthSmile(true, theme)}
    {Blush(theme)}
    <ellipse cx={24} cy={52} rx={5} ry={6} fill={theme.hand} stroke={theme.stroke} strokeWidth={1.2}>
      <animateTransform attributeName="transform" type="translate" values="2 0;-2 0;2 0" dur="0.5s" repeatCount="indefinite" />
    </ellipse>
    <ellipse cx={40} cy={52} rx={5} ry={6} fill={theme.hand} stroke={theme.stroke} strokeWidth={1.2}>
      <animateTransform attributeName="transform" type="translate" values="-2 0;2 0;-2 0" dur="0.5s" repeatCount="indefinite" />
    </ellipse>
  </g>
);

/** 查证无误 */
const CheckFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeOpen(24, 33, 0, 0, theme)}
    {EyeOpen(40, 33, 0, 0, theme)}
    <line x1={28} y1={49} x2={36} y2={49} stroke={theme.mouth} strokeWidth={3} strokeLinecap="round" />
    <circle cx={47} cy={42} r={7} fill={theme.ringFill} stroke={theme.eye} strokeWidth={2} />
    <circle cx={47} cy={42} r={7} fill="none" stroke={theme.ring} strokeWidth={1.5} opacity={0.7} />
    <line x1={52} y1={48} x2={58} y2={55} stroke={theme.eye} strokeWidth={3} strokeLinecap="round" />
  </g>
);

/** 震惊 */
const ShockFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeOpen(23, 32, 0, 0, theme)}
    {EyeOpen(41, 32, 0, 0, theme)}
    <ellipse cx={32} cy={48} rx={5} ry={6.5} fill={theme.mouth} />
    <path d="M17 23 q5 -4 10 0" stroke={theme.eye} strokeWidth={2} fill="none" strokeLinecap="round" />
    <path d="M37 23 q5 -4 10 0" stroke={theme.eye} strokeWidth={2} fill="none" strokeLinecap="round" />
  </g>
);

/** 比心 */
const HeartFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M22 35 c-3 -4 -9 -2 -9 2 c0 4 5 7 9 10 c4 -3 9 -6 9 -10 c0 -4 -6 -6 -9 -2 Z" fill={theme.heart} />
    <path d="M42 35 c-3 -4 -9 -2 -9 2 c0 4 5 7 9 10 c4 -3 9 -6 9 -10 c0 -4 -6 -6 -9 -2 Z" fill={theme.heart} />
    {MouthSmile(false, theme)}
    {Blush(theme)}
  </g>
);

/** 资料到账 */
const InboxFace: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {EyeOpen(24, 33, 0, 0, theme)}
    {EyeOpen(40, 33, 0, 0, theme)}
    {MouthSmile(true, theme)}
    {Blush(theme)}
    <rect x={22} y={8} width={20} height={13} rx={2} fill={theme.white} stroke={theme.envLine} strokeWidth={1.5} />
    <path d="M22 10 L32 17 L42 10" fill="none" stroke={theme.envLine2} strokeWidth={1.5} />
  </g>
);

/* =========================== 小桔表情（xiaoju-orange） ===========================
   五官位置：眼睛 y≈43（x=24/40），嘴 y≈52-55，脸底已含淡腮红。
   每个表情在 XiaojuFaceBg 之上叠加五官+道具，保留桔子帽+刘海的识别性。 */

/** 小桔·招手问好 */
const XiaojuWave: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M26 53 q6 5 12 0" stroke={theme.mouth} strokeWidth={2.8} fill="none" strokeLinecap="round" />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    {/* 挥动的小手（右侧） */}
    <ellipse cx={52} cy={38} rx={5.5} ry={7} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.3}>
      <animateTransform attributeName="transform" type="rotate" values="-10 52 48;10 52 48;-10 52 48" dur="1.1s" repeatCount="indefinite" />
    </ellipse>
    <g stroke={theme.eye} strokeWidth={2} strokeLinecap="round">
      <line x1={49} y1={33} x2={48} y2={27} />
      <line x1={52} y1={32} x2={52} y2={26} />
      <line x1={55} y1={33} x2={56} y2={27} />
    </g>
  </g>
);

/** 小桔·撒花庆祝 */
const XiaojuCheer: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M25 52 q7 8 14 0 Z" fill={theme.mouth} />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    {/* 举起的双手 */}
    <ellipse cx={12} cy={28} rx={4.5} ry={6.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2} transform="rotate(-25 12 34)" />
    <ellipse cx={52} cy={28} rx={4.5} ry={6.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2} transform="rotate(25 52 34)" />
    {/* 彩点（下移到帽子两侧，避免超出 viewBox） */}
    <circle cx={8} cy={40} r={2} fill={theme.dot1} />
    <circle cx={56} cy={42} r={2} fill={theme.dot2} />
    <circle cx={32} cy={10} r={2} fill={theme.dot3} />
    <circle cx={16} cy={18} r={1.5} fill={theme.dot2} />
    <circle cx={48} cy={16} r={1.5} fill={theme.dot1} />
  </g>
);

/** 小桔·灵感来了 */
const XiaojuIdea: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {/* 灯泡在帽子右上方（避开顶部叶子） */}
    <circle cx={50} cy={12} r={4.5} fill={theme.bulb} stroke={theme.bulbLine} strokeWidth={1.3}>
      <animate attributeName="opacity" values="1;0.5;1" dur="1.6s" repeatCount="indefinite" />
    </circle>
    <rect x={47.5} y={15.5} width={5} height={2.5} rx={1} fill={theme.metal} />
    <g stroke={theme.bulbLine} strokeWidth={1.5} strokeLinecap="round">
      <line x1={50} y1={5} x2={50} y2={8} />
      <line x1={43} y1={8} x2={45} y2={11} />
      <line x1={57} y1={8} x2={55} y2={11} />
    </g>
    {/* 眼睛向右上方看 */}
    <circle cx={24} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={25.5} cy={41.5} r={1.8} fill={theme.eye} />
    <circle cx={40} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={41.5} cy={41.5} r={1.8} fill={theme.eye} />
    <path d="M27 53 q5 4 10 0" stroke={theme.mouth} strokeWidth={2.5} fill="none" strokeLinecap="round" />
  </g>
);

/** 小桔·害羞捂脸 */
const XiaojuShy: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 44 q3 3 6 0" stroke={theme.eye} strokeWidth={2.5} fill="none" strokeLinecap="round" />
    <path d="M38 44 q3 3 6 0" stroke={theme.eye} strokeWidth={2.5} fill="none" strokeLinecap="round" />
    <path d="M28 54 q4 3 8 0" stroke={theme.mouth} strokeWidth={2.2} fill="none" strokeLinecap="round" />
    {/* 大腮红 */}
    <ellipse cx={16} cy={51} rx={5.5} ry={3.5} fill={theme.blush} opacity={0.7} />
    <ellipse cx={48} cy={51} rx={5.5} ry={3.5} fill={theme.blush} opacity={0.7} />
    {/* 双手捂脸（轻贴脸颊） */}
    <ellipse cx={13} cy={49} rx={4} ry={5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.1} transform="rotate(18 13 49)" />
    <ellipse cx={51} cy={49} rx={4} ry={5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.1} transform="rotate(-18 51 49)" />
  </g>
);

/** 小桔·嘚瑟比耶 */
const XiaojuV: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M25 52 q7 7 14 0 Z" fill={theme.mouth} />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    {/* 比耶手（右侧） */}
    <ellipse cx={51} cy={32} rx={4.5} ry={5.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2} />
    <g stroke={theme.eye} strokeWidth={2.3} strokeLinecap="round">
      <line x1={48} y1={28} x2={47} y2={20} />
      <line x1={54} y1={28} x2={55} y2={20} />
    </g>
  </g>
);

/** 小桔·困到睁不开 */
const XiaojuSleepy: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <line x1={20} y1={44} x2={28} y2={44} stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <line x1={36} y1={44} x2={44} y2={44} stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <ellipse cx={32} cy={55} rx={2.8} ry={3.5} fill={theme.mouth} />
    {/* zzz */}
    <g stroke={theme.zzz} strokeWidth={2} strokeLinecap="round" fill="none">
      <line x1={45} y1={30} x2={47} y2={26} />
      <line x1={49} y1={23} x2={52} y2={18} />
      <line x1={53} y1={16} x2={57} y2={11} />
    </g>
  </g>
);

/** 小桔·摸鱼喝茶 */
const XiaojuTea: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M27 53 q5 4 10 0" stroke={theme.mouth} strokeWidth={2.5} fill="none" strokeLinecap="round" />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.45} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.45} />
    {/* 茶杯（右下） */}
    <path d="M40 57 h13 a5.5 5.5 0 0 1 -13 0 Z" fill={theme.white} stroke={theme.metal} strokeWidth={1.3} />
    <path d="M42 59 h9 a4.5 4.5 0 0 1 -9 0 Z" fill={theme.tea} />
    <g stroke={theme.metal} strokeWidth={1.3} strokeLinecap="round" fill="none">
      <path d="M44 56 q2 -4 0 -8" />
      <path d="M49 56 q2 -4 0 -8" />
    </g>
  </g>
);

/** 小桔·安慰抱抱 */
const XiaojuHug: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M25 52 q7 7 14 0 Z" fill={theme.mouth} />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    {/* 大爱心（脸前） */}
    <path
      d="M32 50 c-3 -5 -10 -3 -10 2 c0 4 6 6 10 9 c4 -3 10 -5 10 -9 c0 -5 -7 -7 -10 -2 Z"
      fill={theme.heart}
    />
  </g>
);

/** 小桔·卡文抓狂 */
const XiaojuStuck: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {/* X眼 */}
    <path d="M19 40 L27 46 M27 40 L19 46" stroke={theme.eye} strokeWidth={2.5} strokeLinecap="round" />
    <path d="M37 40 L45 46 M45 40 L37 46" stroke={theme.eye} strokeWidth={2.5} strokeLinecap="round" />
    <path d="M25 56 q7 -5 14 0 Z" fill={theme.mouth} />
    {/* 头顶火焰（帽子上方偏右，缩小） */}
    <path d="M44 2 q4 4 1.5 8 q-5 -1 -2.5 -5 q-3 3 -0.5 6.5 q5 2.5 6.5 -2.5 q1.5 -5 -5 -7 Z" fill={theme.flame} />
    {/* 汗滴 */}
    <path d="M51 40 q2 4 0 6 q-2 -2 0 -6 Z" fill={theme.sweat} />
  </g>
);

/** 小桔·扶额无语 */
const XiaojuFacepalm: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <line x1={20} y1={44} x2={28} y2={44} stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <line x1={36} y1={44} x2={44} y2={44} stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
    <line x1={27} y1={55} x2={37} y2={55} stroke={theme.mouth} strokeWidth={2.8} strokeLinecap="round" />
    {/* 手扶额（左侧，搭在帽子+额头） */}
    <ellipse cx={21} cy={30} rx={6.5} ry={7.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2} transform="rotate(-12 21 30)" />
  </g>
);

/** 小桔·暗中观察 */
const XiaojuPeek: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {/* 左侧遮挡（阴影板） */}
    <rect x={2} y={2} width={22} height={60} rx={3} fill={theme.mask} opacity={0.18} />
    {/* 一只睁大的眼（右侧） */}
    <circle cx={40} cy={43} r={5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={41} cy={42} r={2.2} fill={theme.eye} />
    <circle cx={41.8} cy={41.2} r={0.7} fill="#FFFFFF" />
    {/* 小嘴（窃笑） */}
    <path d="M32 53 q5 3 9 0" stroke={theme.mouth} strokeWidth={2.3} fill="none" strokeLinecap="round" />
    {/* 小手扒着遮挡边 */}
    <ellipse cx={18} cy={46} rx={3.5} ry={4.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.1} transform="rotate(12 18 46)" />
  </g>
);

/** 小桔·鼓掌 */
const XiaojuClap: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <path d="M20 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M36 43 q4 -4 8 0" stroke={theme.eye} strokeWidth={3} fill="none" strokeLinecap="round" />
    <path d="M25 52 q7 7 14 0 Z" fill={theme.mouth} />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.55} />
    {/* 鼓掌双手（下方，相向运动） */}
    <ellipse cx={25} cy={61} rx={4.5} ry={5.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2}>
      <animateTransform attributeName="transform" type="translate" values="2 0;-2 0;2 0" dur="0.5s" repeatCount="indefinite" />
    </ellipse>
    <ellipse cx={39} cy={61} rx={4.5} ry={5.5} fill={theme.skin} stroke={theme.stroke} strokeWidth={1.2}>
      <animateTransform attributeName="transform" type="translate" values="-2 0;2 0;-2 0" dur="0.5s" repeatCount="indefinite" />
    </ellipse>
  </g>
);

/** 小桔·查证无误 */
const XiaojuCheck: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <circle cx={24} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={24} cy={43} r={2} fill={theme.eye} />
    <circle cx={40} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={40} cy={43} r={2} fill={theme.eye} />
    <line x1={27} y1={55} x2={37} y2={55} stroke={theme.mouth} strokeWidth={2.8} strokeLinecap="round" />
    {/* 放大镜（右侧） */}
    <circle cx={49} cy={48} r={6} fill={theme.ringFill} stroke={theme.eye} strokeWidth={1.8} />
    <circle cx={49} cy={48} r={6} fill="none" stroke={theme.ring} strokeWidth={1.3} opacity={0.7} />
    <line x1={53.5} y1={52.5} x2={59} y2={59} stroke={theme.eye} strokeWidth={2.8} strokeLinecap="round" />
  </g>
);

/** 小桔·震惊 */
const XiaojuShock: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {/* 挑眉 */}
    <path d="M17 37 q5 -4 10 0" stroke={theme.eye} strokeWidth={1.8} fill="none" strokeLinecap="round" />
    <path d="M37 37 q5 -4 10 0" stroke={theme.eye} strokeWidth={1.8} fill="none" strokeLinecap="round" />
    {/* 大眼 */}
    <circle cx={24} cy={44} r={5.2} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.4} />
    <circle cx={24} cy={44} r={2.5} fill={theme.eye} />
    <circle cx={24.8} cy={43.2} r={0.9} fill="#FFFFFF" />
    <circle cx={40} cy={44} r={5.2} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.4} />
    <circle cx={40} cy={44} r={2.5} fill={theme.eye} />
    <circle cx={40.8} cy={43.2} r={0.9} fill="#FFFFFF" />
    {/* 张嘴（O形） */}
    <ellipse cx={32} cy={55} rx={4} ry={5.2} fill={theme.mouth} />
  </g>
);

/** 小桔·比心 */
const XiaojuHeart: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    {/* 双眼变爱心 */}
    <path d="M24 40 c-2.5 -3.5 -7 -2 -7 1.5 c0 3 4 4.5 7 6.5 c3 -2 7 -3.5 7 -6.5 c0 -3.5 -4.5 -5 -7 -1.5 Z" fill={theme.heart} />
    <path d="M40 40 c-2.5 -3.5 -7 -2 -7 1.5 c0 3 4 4.5 7 6.5 c3 -2 7 -3.5 7 -6.5 c0 -3.5 -4.5 -5 -7 -1.5 Z" fill={theme.heart} />
    <path d="M27 53 q5 4 10 0" stroke={theme.mouth} strokeWidth={2.5} fill="none" strokeLinecap="round" />
    <ellipse cx={16} cy={51} rx={5} ry={3} fill={theme.blush} opacity={0.6} />
    <ellipse cx={48} cy={51} rx={5} ry={3} fill={theme.blush} opacity={0.6} />
  </g>
);

/** 小桔·资料到账 */
const XiaojuInbox: FC<{ theme: EmoteTheme }> = ({ theme }) => (
  <g>
    <circle cx={24} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={24} cy={43} r={2} fill={theme.eye} />
    <circle cx={40} cy={43} r={4.5} fill="#FFFFFF" stroke={theme.eye} strokeWidth={1.3} />
    <circle cx={40} cy={43} r={2} fill={theme.eye} />
    <path d="M25 52 q7 7 14 0 Z" fill={theme.mouth} />
    <ellipse cx={17} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.45} />
    <ellipse cx={47} cy={50} rx={4} ry={2.5} fill={theme.blush} opacity={0.45} />
    {/* 信封（帽子左上方，避开顶部叶子） */}
    <rect x={10} y={8} width={18} height={11} rx={2} fill={theme.white} stroke={theme.envLine} strokeWidth={1.3} />
    <path d="M10 10 L19 16 L28 10" fill="none" stroke={theme.envLine2} strokeWidth={1.3} />
  </g>
);

/* ------------------------------- 多 Pack 注册表 ------------------------------- */

type FaceFactory = FC;

function makeRegistry(theme: EmoteTheme): Record<string, FaceFactory> {
  return {
    wave: () => <WaveFace theme={theme} />,
    cheer: () => <CheerFace theme={theme} />,
    idea: () => <IdeaFace theme={theme} />,
    shy: () => <ShyFace theme={theme} />,
    v: () => <VFace theme={theme} />,
    sleepy: () => <SleepyFace theme={theme} />,
    tea: () => <TeaFace theme={theme} />,
    hug: () => <HugFace theme={theme} />,
    stuck: () => <StuckFace theme={theme} />,
    facepalm: () => <FacepalmFace theme={theme} />,
    peek: () => <PeekFace theme={theme} />,
    clap: () => <ClapFace theme={theme} />,
    check: () => <CheckFace theme={theme} />,
    shock: () => <ShockFace theme={theme} />,
    heart: () => <HeartFace theme={theme} />,
    inbox: () => <InboxFace theme={theme} />,
  };
}

/** 小桔包注册表：16 个表情 id → 小桔风格 SVG 组件。 */
function makeXiaojuRegistry(theme: EmoteTheme): Record<string, FaceFactory> {
  return {
    wave: () => <XiaojuWave theme={theme} />,
    cheer: () => <XiaojuCheer theme={theme} />,
    idea: () => <XiaojuIdea theme={theme} />,
    shy: () => <XiaojuShy theme={theme} />,
    v: () => <XiaojuV theme={theme} />,
    sleepy: () => <XiaojuSleepy theme={theme} />,
    tea: () => <XiaojuTea theme={theme} />,
    hug: () => <XiaojuHug theme={theme} />,
    stuck: () => <XiaojuStuck theme={theme} />,
    facepalm: () => <XiaojuFacepalm theme={theme} />,
    peek: () => <XiaojuPeek theme={theme} />,
    clap: () => <XiaojuClap theme={theme} />,
    check: () => <XiaojuCheck theme={theme} />,
    shock: () => <XiaojuShock theme={theme} />,
    heart: () => <XiaojuHeart theme={theme} />,
    inbox: () => <XiaojuInbox theme={theme} />,
  };
}

/** 每个 Pack 一套渲染实现（按 EmoteTheme 主题化）。 */
export const PACK_REGISTRIES: Record<string, Record<string, FaceFactory>> = {
  "moling-default": makeRegistry(THEME_YELLOW),
  "moling-mono": makeRegistry(THEME_MONO),
  "xiaoju-orange": makeXiaojuRegistry(THEME_XIAOJU),
};

/** 每个 Pack 对应的调色板（供 Emote 渲染脸底/五官时取用）。 */
export const PACK_THEMES: Record<string, EmoteTheme> = {
  "moling-default": THEME_YELLOW,
  "moling-mono": THEME_MONO,
  "xiaoju-orange": THEME_XIAOJU,
};

/** 每个 Pack 的脸底容器（圆脸 / 方贴纸 / 小桔桔子帽脸 …），Emote 按 pack 选取，使不同包观感真正不同。 */
export const PACK_BGS: Record<string, FC<{ children?: ReactNode; theme: EmoteTheme }>> = {
  "moling-default": FaceBg,
  "moling-mono": FaceCard,
  "xiaoju-orange": XiaojuFaceBg,
};

export const PACK_IDS: string[] = Object.keys(PACK_REGISTRIES);

const DEFAULT_PACK = EMOTE_MANIFEST.defaultPack || "moling-default";

/** 取某 Pack 的渲染注册表（找不到回落到默认包）。 */
export function getPackRegistry(packId: string): Record<string, FaceFactory> {
  return PACK_REGISTRIES[packId] ?? PACK_REGISTRIES[DEFAULT_PACK] ?? PACK_REGISTRIES["moling-default"];
}
