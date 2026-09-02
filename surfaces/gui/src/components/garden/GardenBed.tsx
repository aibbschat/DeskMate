/**
 * GardenBed — 花园（蓝图第二层 · 空间分区 · 花园·情绪花丛）。
 *
 * 花园的树（真 SVG 树 + 浇水/年花/年鉴）｜花圃（真实 SVG 花，分页+点亮）
 * ｜羁绊故事簿｜花园图鉴 —— 都长在这片花园里，不打断地铺陈。
 */

import { GardenFlora } from "./GardenFlora";
import { GardenFlowers, type GardenFlower, type GardenStats } from "./GardenFlowers";
import { GardenStoryBook, type StoryBook } from "./GardenStoryBook";
import { GardenTree } from "./GardenTree";
import type { GardenFloraItem } from "./GardenFlora";
import type { GardenP2 } from "./useGarden";

interface Props {
  flowers: GardenFlower[];
  stats: GardenStats | null;
  p2?: GardenP2 | null;
  water?: { watered: boolean; streak: number; today: string } | null;
  litIds?: string[] | null;
  onLit: (flowerId: string) => void;
  flora?: GardenFloraItem[] | null;
  story?: StoryBook | null;
}

export function GardenBed({ flowers, stats, p2, water, litIds, onLit, flora, story }: Props) {
  // 浇水实时值（visit 返回）优先于快照占位
  const p2merged = p2 ? { ...p2, water: water ?? p2.water } : p2;

  return (
    <div className="garden-space-garden">
      <GardenTree p2={p2merged} />
      <GardenFlowers flowers={flowers} stats={stats} litIds={litIds} onLit={onLit} />
      <GardenStoryBook story={story} />
      <GardenFlora flora={flora} />
    </div>
  );
}
