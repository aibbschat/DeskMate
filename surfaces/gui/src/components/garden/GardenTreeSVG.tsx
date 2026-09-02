/**
 * GardenTreeSVG — 花园的树（真实 SVG 树，替换 P2 的进度条）。
 *
 * 随成长等级（treeStage.key）真实生长：
 *   seed 一粒种子 → sprout 一株新苗 → sapling 一棵小树 → bloom 一树繁花 → canopy 亭亭如盖
 * 树冠繁茂度、花朵数随等级递增；颜色随季节（春樱/夏绿/秋金/冬雪）切换。
 * 全部确定性：由 key + season 决定，零随机、零 LLM。
 */

import type { SeasonKey } from "./SeasonTheme";

interface Props {
  stage: string; // seed | sprout | sapling | bloom | canopy
  season: SeasonKey;
  size?: number;
}

/** 季节 → 树冠主色（叶/花）。 */
const SEASON_FOLIAGE: Record<SeasonKey, { leaf: string; leaf2: string; flower: string; trunk: string }> = {
  spring: { leaf: "#A8D87A", leaf2: "#8FC45A", flower: "#F7B7D3", trunk: "#A5744A" },
  summer: { leaf: "#5EA55E", leaf2: "#4E9B5A", flower: "#8FC45A", trunk: "#9A6B45" },
  autumn: { leaf: "#E8A23D", leaf2: "#D9822B", flower: "#F5B04B", trunk: "#8A5A30" },
  winter: { leaf: "#7E93A8", leaf2: "#6B8095", flower: "#E8F0F8", trunk: "#6E5540" },
};

const STAGE_RANK: Record<string, number> = {
  seed: 0,
  sprout: 1,
  sapling: 2,
  bloom: 3,
  canopy: 4,
};

export function GardenTreeSVG({ stage, season, size = 120 }: Props) {
  const f = SEASON_FOLIAGE[season] ?? SEASON_FOLIAGE.autumn;
  const rank = STAGE_RANK[stage] ?? 0;
  const view = 160;

  // 树冠三层（从小树到如盖逐层放大），冬削枝、秋金、春带花
  const canopyR = [26, 34, 42][Math.min(2, rank - 2)] ?? 0;
  const canopyY = [84, 78, 70][Math.min(2, rank - 2)] ?? 0;
  const flowerCount = rank === 3 ? 7 : rank === 4 ? 11 : 0;
  const winter = season === "winter";

  // 花朵位置（确定性，不随机）
  const FLOWER_SPOTS = [
    [80, 44], [104, 56], [56, 56], [92, 34], [66, 66], [112, 74], [48, 72],
    [80, 66], [120, 62], [40, 62], [98, 76],
  ];

  // 树干：seed/sprout 不画粗干
  const trunk = (() => {
    switch (stage) {
      case "seed":
        return (
          <g>
            <ellipse cx={80} cy={138} rx={30} ry={7} fill="#9C7A4F" opacity={0.55} />
            <ellipse cx={80} cy={136} rx={24} ry={5} fill="#8A6A44" opacity={0.8} />
            <circle cx={80} cy={124} r={7} fill="#C98D5A" stroke={f.trunk} strokeWidth={1.5} />
            <path d="M 78 120 q 0 -7 3 -9 q 3 2 1 8 z" fill={f.leaf} />
            <path d="M 82 121 q 1 -7 5 -8 q 3 3 0 8 z" fill={f.leaf2} />
          </g>
        );
      case "sprout":
        return (
          <g>
            <ellipse cx={80} cy={140} rx={22} ry={5} fill="#8A6A44" opacity={0.75} />
            <path d="M 80 138 C 80 120, 79 112, 80 102" stroke={f.trunk} strokeWidth={4} strokeLinecap="round" fill="none" />
            <path d="M 80 106 C 68 104, 62 98, 60 90 C 70 92, 78 96, 80 104 Z" fill={f.leaf} />
            <path d="M 80 108 C 92 106, 98 100, 100 92 C 90 94, 82 98, 80 106 Z" fill={f.leaf2} />
          </g>
        );
      default: {
        const trunkW = stage === "canopy" ? 11 : stage === "bloom" ? 9 : 7;
        return (
          <g>
            {/* 树干（有树冠才立得住） */}
            <path
              d={`M ${80 - trunkW / 2} 140 C ${80 - trunkW / 2 - 2} 118, ${80 + trunkW / 2 + 2} 112, ${80 + trunkW / 2} 96 L ${80 - trunkW / 2} 96 Z`}
              fill={f.trunk}
            />
            <path d={`M ${80 - trunkW / 2 - 2} 96 L ${80 - trunkW / 2} 86`} stroke={f.trunk} strokeWidth={3.5} strokeLinecap="round" />
            <path d={`M ${80 + trunkW / 2 + 2} 100 L ${80 + trunkW / 2 + 5} 90`} stroke={f.trunk} strokeWidth={3.5} strokeLinecap="round" />
          </g>
        );
      }
    }
  })();

  const canopyLayer = (r: number, y: number, i: number) => (
    <circle
      key={i}
      cx={80 + (i - 1) * 22 - 8}
      cy={y + (i === 1 ? 4 : 0)}
      r={r}
      fill={`url(#gt-${season}-${stage}-${i})`}
      stroke={f.leaf2}
      strokeOpacity={0.35}
    />
  );

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${view} ${view}`}
      className="garden-tree-svg"
      role="img"
      aria-label={`花园的树：${stage}`}
    >
      <defs>
        {[0, 1, 2].map((i) => (
          <radialGradient key={i} id={`gt-${season}-${stage}-${i}`} cx="38%" cy="32%" r="78%">
            <stop offset="0%" stopColor={winter ? "#EAF0F8" : f.leaf} />
            <stop offset="100%" stopColor={f.leaf2} />
          </radialGradient>
        ))}
      </defs>

      {/* 树冠层（sapling 及以上） */}
      {rank >= 2 &&
        [0, 1, 2].map((i) =>
          canopyLayer(canopyR + (i === 1 ? 4 : 0), canopyY - (i === 2 ? 6 : 0) + 6, i),
        )}

      {/* 花点（bloom / canopy） */}
      {flowerCount > 0 &&
        FLOWER_SPOTS.slice(0, flowerCount).map(([fx, fy], i) => (
          <g key={i}>
            <circle cx={fx} cy={fy} r={4} fill={f.flower} stroke="rgba(255,255,255,0.5)" strokeWidth={0.6} />
            <circle cx={fx} cy={fy} r={1.6} fill="#FFF3E0" opacity={0.9} />
          </g>
        ))}

      {trunk}
    </svg>
  );
}
