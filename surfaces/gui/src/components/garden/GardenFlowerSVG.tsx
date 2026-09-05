/**
 * GardenFlowerSVG — 花园里的真实 SVG 花朵（替换 emoji，对应设计图的真实花丛）。
 *
 * 9 种花种各有一种造型与配色（对齐后端 FLOWER_INFO）：
 *   starflower 星芒草 / sunflower 向日葵 / badge_flower 勋章花 / dandelion 蒲公英
 *   bellflower 风铃草 / hyacinth 风信子 / sprout 新芽 / iris 鸢尾 / daisy 雏菊
 * 情绪定色：mood（顺利/成长/平静…）微调花瓣主色，让「花随心情变色」落地可见。
 * 点亮：lit=true 时花冠外绕一圈小星光 + 光晕。
 *
 * 全部确定性：形状/颜色由 kind/mood 决定，零随机、零 LLM。
 */

interface Props {
  kind: string;
  mood?: string;
  size?: number;
  lit?: boolean;
}

/** 花种 → 造型参数。 */
interface FlowerSpec {
  petals: number;        // 花瓣数
  petalLen: number;      // 花瓣长度（相对花心半径的倍数）
  petalWid: number;      // 花瓣宽度
  petalColor: [string, string]; // 花瓣渐变（外→内）
  centerColor: string;   // 花心颜色
  centerSize: number;    // 花心半径
  shape: "round" | "star" | "cluster" | "bell" | "dandelion" | "sprout" | "iris";
  stem: boolean;         // 是否画茎叶
}

/** 情绪词 → 主色偏移（花随心情变色）。
 *
 * 词表要覆盖后端 flowers.py 能写进 mood_tint 的全部取值：
 * - 花种注册表默认（顺利/成长/平静/温暖）
 * - 情感时间线经 _TIMELINE_MOOD 映射来的（压力/低落/忙碌/顺利/平静）
 * 缺一个键前端就静默回落花种本色 —— 即「花随心情变色」对那天失效。
 * 低饱和处理：暖色基座上，压力/低落走冷调，一眼能和「顺利」分开。 */
const MOOD_TINT: Record<string, [string, string]> = {
  顺利: ["#FFB84D", "#FF8A3D"],
  成长: ["#F08A5D", "#E05D3A"],
  平静: ["#B8C8E8", "#8FA8D8"],
  温暖: ["#F5A97F", "#E88A5A"],
  压力: ["#9FB3C8", "#6B85A3"],
  低落: ["#B5A8C4", "#8878A0"],
  忙碌: ["#E9A87C", "#D4834F"],
};

const SPECS: Record<string, FlowerSpec> = {
  sunflower: { petals: 13, petalLen: 1.55, petalWid: 0.42, petalColor: ["#FFD23F", "#FF9F1C"], centerColor: "#7A4A21", centerSize: 0.42, shape: "round", stem: true },
  daisy: { petals: 10, petalLen: 1.35, petalWid: 0.4, petalColor: ["#FFFFFF", "#F3E2CE"], centerColor: "#FFC24B", centerSize: 0.34, shape: "round", stem: true },
  starflower: { petals: 5, petalLen: 1.7, petalWid: 0.34, petalColor: ["#8FB8F0", "#6A9BE0"], centerColor: "#FFE08A", centerSize: 0.24, shape: "star", stem: true },
  bellflower: { petals: 5, petalLen: 1.2, petalWid: 0.5, petalColor: ["#B79BF0", "#9674E0"], centerColor: "#F0E6FF", centerSize: 0.22, shape: "bell", stem: true },
  hyacinth: { petals: 6, petalLen: 0.95, petalWid: 0.6, petalColor: ["#C9A6F5", "#9D6FE0"], centerColor: "#E8D8FF", centerSize: 0.2, shape: "cluster", stem: true },
  iris: { petals: 3, petalLen: 1.7, petalWid: 0.55, petalColor: ["#7FA8F0", "#4E7FE0"], centerColor: "#FFE08A", centerSize: 0.22, shape: "iris", stem: true },
  dandelion: { petals: 0, petalLen: 0, petalWid: 0, petalColor: ["#FFFFFF", "#F0F0F0"], centerColor: "#E8E0D0", centerSize: 0.5, shape: "dandelion", stem: true },
  sprout: { petals: 0, petalLen: 0, petalWid: 0, petalColor: ["#A8D87A", "#7FB54A"], centerColor: "#8FC45A", centerSize: 0.3, shape: "sprout", stem: true },
  badge_flower: { petals: 12, petalLen: 1.05, petalWid: 0.5, petalColor: ["#FFD08A", "#FF9F1C"], centerColor: "#C96F2A", centerSize: 0.4, shape: "round", stem: true },
};

const MOOD_TEXT: Record<string, string> = {
  顺利: "#8A5A2B",
  成长: "#B05E2C",
  平静: "#5B6C8C",
  温暖: "#A0552F",
  压力: "#5A6B80",
  低落: "#6B5C80",
  忙碌: "#8A4A28",
};

function tintSpec(kind: string, mood?: string): FlowerSpec {
  const base = SPECS[kind] ?? SPECS.daisy;
  if (!mood || !MOOD_TINT[mood]) return base;
  const [a, b] = MOOD_TINT[mood];
  return { ...base, petalColor: [a, b] };
}

/** 画一瓣花瓣：从花心向外，椭圆旋转铺开。 */
function Petal({ cx, cy, len, wid, colorTo, angle, id }: { cx: number; cy: number; len: number; wid: number; colorTo: string; angle: number; id: string }) {
  return (
    <g transform={`rotate(${angle} ${cx} ${cy})`}>
      <ellipse
        cx={cx}
        cy={cy - len * 8}
        rx={wid * 8}
        ry={len * 8}
        fill={`url(#${id})`}
        stroke={colorTo}
        strokeOpacity={0.35}
        strokeWidth={0.7}
      />
    </g>
  );
}

/** 花心。 */
function Center({ cx, cy, r, color }: { cx: number; cy: number; r: number; color: string }) {
  return (
    <circle cx={cx} cy={cy} r={r} fill={color} stroke="rgba(0,0,0,0.12)" strokeWidth={0.6} />
  );
}

/** 茎 + 两片叶。 */
function Stem({ cx, topY, bottomY, color }: { cx: number; topY: number; bottomY: number; color: string }) {
  const midY = (topY + bottomY) / 2;
  return (
    <g>
      <path
        d={`M ${cx} ${topY} C ${cx + 1.5} ${midY - 6}, ${cx - 1.5} ${midY + 4}, ${cx} ${bottomY}`}
        fill="none"
        stroke={color}
        strokeWidth={2}
        strokeLinecap="round"
      />
      <path
        d={`M ${cx} ${midY} C ${cx + 4} ${midY - 2}, ${cx + 9} ${midY - 4}, ${cx + 12} ${midY + 2} C ${cx + 9} ${midY + 6}, ${cx + 4} ${midY + 5}, ${cx} ${midY + 4}`}
        fill={color}
        opacity={0.85}
      />
      <path
        d={`M ${cx} ${midY + 5} C ${cx - 4} ${midY + 3}, ${cx - 9} ${midY + 1}, ${cx - 12} ${midY + 7} C ${cx - 9} ${midY + 11}, ${cx - 4} ${midY + 10}, ${cx} ${midY + 9}`}
        fill={color}
        opacity={0.7}
      />
    </g>
  );
}

/** 蒲公英：白色绒球（细线 + 端点），微风吹散感。 */
function Dandelion({ cx, cy, r, color }: { cx: number; cy: number; r: number; color: string }) {
  const rays = 16;
  const seeds = Array.from({ length: rays }, (_, i) => {
    const a = (i / rays) * Math.PI * 2 + 0.3;
    const inner = r * 0.35;
    const outer = r;
    return { x1: cx + Math.cos(a) * inner, y1: cy + Math.sin(a) * inner, x2: cx + Math.cos(a) * outer, y2: cy + Math.sin(a) * outer };
  });
  return (
    <g>
      {seeds.map((s, i) => (
        <g key={i}>
          <line x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2} stroke="rgba(255,255,255,0.9)" strokeWidth={0.6} />
          <circle cx={s.x2} cy={s.y2} r={1.1} fill="#FFF" />
        </g>
      ))}
      <circle cx={cx} cy={cy} r={r * 0.3} fill={color} opacity={0.8} />
    </g>
  );
}

/** 新芽：两片小叶破土。 */
function Sprout({ cx, topY, color, uid }: { cx: number; topY: number; color: string; uid: string }) {
  return (
    <g>
      <path d={`M ${cx} ${topY + 4} C ${cx - 4} ${topY - 2}, ${cx - 8} ${topY - 3}, ${cx - 10} ${topY - 7} C ${cx - 7} ${topY - 9}, ${cx - 3} ${topY - 8}, ${cx} ${topY - 5}`} fill={`url(#${uid}-leaf1)`} />
      <path d={`M ${cx} ${topY + 6} C ${cx + 4} ${topY + 1}, ${cx + 8} ${topY}, ${cx + 10} ${topY - 5} C ${cx + 7} ${topY - 7}, ${cx + 3} ${topY - 6}, ${cx} ${topY - 3}`} fill={`url(#${uid}-leaf2)`} />
      <line x1={cx} y1={topY + 6} x2={cx} y2={topY - 4} stroke={color} strokeWidth={1.6} strokeLinecap="round" />
    </g>
  );
}

export function GardenFlowerSVG({ kind, mood, size = 56, lit = false }: Props) {
  const spec = tintSpec(kind, mood);
  const viewH = 64;
  const viewW = 48;
  const cx = viewW / 2;
  const flowerTop = 14;       // 花头中心 Y
  const stemBottom = 58;
  const uid = `gf-${kind}-${mood ?? "n"}`;
  const moodText = mood ? MOOD_TEXT[mood] : "#7A5230";

  const renderHead = () => {
    switch (spec.shape) {
      case "dandelion":
        return <Dandelion cx={cx} cy={flowerTop} r={spec.centerSize * 26} color={spec.centerColor} />;
      case "sprout":
        return <Sprout cx={cx} topY={flowerTop + 6} color={spec.petalColor[0]} uid={uid} />;
      default: {
        const petals = Array.from({ length: spec.petals }, (_, i) => (i * 360) / spec.petals);
        return (
          <g>
            {spec.shape === "iris" && (
              <>
                {/* 鸢尾：三片大瓣 + 垂唇瓣 */}
                <path d={`M ${cx} ${flowerTop - 16} Q ${cx + 12} ${flowerTop - 6}, ${cx + 9} ${flowerTop + 8} Q ${cx + 4} ${flowerTop + 10}, ${cx} ${flowerTop + 4} Z`} fill={`url(#${uid})`} stroke={spec.petalColor[1]} strokeOpacity={0.4} />
                <path d={`M ${cx} ${flowerTop - 16} Q ${cx - 12} ${flowerTop - 6}, ${cx - 9} ${flowerTop + 8} Q ${cx - 4} ${flowerTop + 10}, ${cx} ${flowerTop + 4} Z`} fill={`url(#${uid})`} stroke={spec.petalColor[1]} strokeOpacity={0.4} />
                <path d={`M ${cx} ${flowerTop + 14} Q ${cx + 6} ${flowerTop + 22}, ${cx} ${flowerTop + 26} Q ${cx - 6} ${flowerTop + 22}, ${cx} ${flowerTop + 14} Z`} fill={spec.petalColor[1]} opacity={0.85} />
                <Center cx={cx} cy={flowerTop + 4} r={spec.centerSize * 14} color={spec.centerColor} />
              </>
            )}
            {spec.shape === "bell" && (
              <>
                {/* 风铃草：倒挂的小铃铛 */}
                <path d={`M ${cx} ${flowerTop - 10} C ${cx + 9} ${flowerTop - 4}, ${cx + 8} ${flowerTop + 8}, ${cx} ${flowerTop + 12} C ${cx - 8} ${flowerTop + 8}, ${cx - 9} ${flowerTop - 4}, ${cx} ${flowerTop - 10} Z`} fill={`url(#${uid})`} />
                <path d={`M ${cx - 4} ${flowerTop + 10} q 2 5 4 1`} fill="none" stroke={spec.petalColor[1]} strokeWidth={1} />
                <circle cx={cx} cy={flowerTop - 10} r={1.6} fill={spec.centerColor} />
              </>
            )}
            {spec.shape === "cluster" && (
              <>
                {/* 风信子：沿茎簇生的小花 */}
                {[0, 1, 2, 3, 4].map((i) => {
                  const yy = flowerTop - 2 - i * 5.5;
                  const xx = cx + (i % 2 === 0 ? -5 : 5) + (i === 4 ? 0 : 0);
                  return (
                    <circle key={i} cx={xx} cy={yy} r={3.4} fill={`url(#${uid})`} stroke={spec.petalColor[1]} strokeOpacity={0.4} />
                  );
                })}
                <circle cx={cx + 2} cy={flowerTop + 3} r={3} fill={`url(#${uid})`} stroke={spec.petalColor[1]} strokeOpacity={0.4} />
                <Center cx={cx} cy={flowerTop - 2} r={spec.centerSize * 14} color={spec.centerColor} />
              </>
            )}
            {spec.shape === "round" || spec.shape === "star" ? (
              <>
                {petals.map((a) => (
                <Petal key={a} cx={cx} cy={flowerTop} len={spec.petalLen} wid={spec.petalWid} colorTo={spec.petalColor[1]} angle={a} id={uid} />
                ))}
                <Center cx={cx} cy={flowerTop} r={spec.centerSize * 16} color={spec.centerColor} />
              </>
            ) : null}
          </g>
        );
      }
    }
  };

  return (
    <svg
      width={size}
      height={size * (viewH / viewW)}
      viewBox={`0 0 ${viewW} ${viewH}`}
      className={`garden-flower-svg${lit ? " is-lit" : ""}`}
      aria-hidden
    >
      <defs>
        <radialGradient id={uid} cx="35%" cy="30%" r="80%">
          <stop offset="0%" stopColor={spec.petalColor[0]} />
          <stop offset="100%" stopColor={spec.petalColor[1]} />
        </radialGradient>
        <linearGradient id={`${uid}-leaf1`} x1="0" y1="1" x2="1" y2="0">
          <stop offset="0%" stopColor={spec.petalColor[1]} />
          <stop offset="100%" stopColor={spec.petalColor[0]} />
        </linearGradient>
        <linearGradient id={`${uid}-leaf2`} x1="1" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor={spec.petalColor[1]} />
          <stop offset="100%" stopColor={spec.petalColor[0]} />
        </linearGradient>
      </defs>

      {/* 茎叶 */}
      {spec.stem && spec.shape !== "sprout" && (
        <Stem cx={cx} topY={flowerTop + 6} bottomY={stemBottom} color={moodText} />
      )}

      {/* 点亮：花冠外绕一圈星光 */}
      {lit && (
        <g className="garden-flower-lit-ring">
          {[0, 60, 120, 180, 240, 300].map((a) => (
            <path
              key={a}
              d={`M ${cx} ${flowerTop - 20} l 1.4 4.6 4.6 1.4 -4.6 1.4 -1.4 4.6 -1.4 -4.6 -4.6 -1.4 4.6 -1.4 z`}
              transform={`rotate(${a} ${cx} ${flowerTop}) translate(0 ${-8})`}
              fill="#FFC24B"
              opacity={0.95}
            />
          ))}
          <circle cx={cx} cy={flowerTop} r={24} fill="none" stroke="rgba(255,194,75,0.55)" strokeWidth={1} strokeDasharray="2 3" />
        </g>
      )}

      {/* 花头 */}
      {renderHead()}
    </svg>
  );
}
