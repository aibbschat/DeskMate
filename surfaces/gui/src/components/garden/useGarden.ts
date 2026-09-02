/**
 * useGarden — 花园数据与事件上报 hook。
 *
 * 进入个人中心（花园）时：
 * 1. POST /v1/deskmate/garden/visit —— 上报一次进园，后端判定是否触发花园时钟事件
 *    （晨间/黄昏/深夜/归来/节日），返回 triggered + 最新季节/昼夜。
 * 2. GET  /v1/deskmate/garden —— 拉取花园快照（四季/昼夜/花/统计/痕迹）。
 *
 * 全部失败静默降级（前端回退到本地时间计算季节/昼夜），不阻塞页面。
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import type { GardenEvent } from "./GardenClockOverlay";
import type { GardenFloraItem } from "./GardenFlora";
import type { GardenFlower, GardenStats } from "./GardenFlowers";
import type { StoryBook } from "./GardenStoryBook";

export interface GardenSeason {
  season: { key: string; label: string };
  daypart: { key: string; label: string };
}

/** P2 · 沉淀与打磨：成长树 / 浇水 / 年花 / 点亮 / 年鉴摘要。 */
export interface GardenP2 {
  tree?: {
    key: string;
    title: string;
    desc: string;
    count: number;
    threshold: number;
    next?: { title: string; threshold: number } | null;
  } | null;
  water?: { watered: boolean; streak: number; today: string };
  year_flowers?: Array<{ year: string; first_visit_date: string }>;
  years?: Record<string, number>;
  lit?: { count: number; ids: string[] };
  archive?: {
    count: number;
    years?: Array<{ year: string; count: number; sample_title?: string }>;
  };
}

export interface GardenSnapshot {
  season: GardenSeason;
  flowers: GardenFlower[];
  stats: GardenStats | null;
  flora?: GardenFloraItem[] | null;
  log: Array<Record<string, unknown>>;
  visit_count: number;
  last_visit_date: string | null;
  p2?: GardenP2 | null;
}

interface VisitResult {
  triggered: GardenEvent | null;
  season: GardenSeason;
  visit_count: number;
  water?: { watered: boolean; streak: number; today: string };
  year_flower?: { year: string; first_visit_date: string };
}

export function useGarden() {
  const [snapshot, setSnapshot] = useState<GardenSnapshot | null>(null);
  const [story, setStory] = useState<StoryBook | null>(null);
  const [toast, setToast] = useState<GardenEvent | null>(null);
  const [water, setWater] = useState<{ watered: boolean; streak: number; today: string } | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/garden/visit`, { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: VisitResult | null) => {
        if (alive && d) {
          if (d.triggered) setToast(d.triggered);
          if (d.water) setWater(d.water);
        }
      })
      .catch(() => {
        /* 静默降级 */
      });
    authedFetch(`${httpBase()}/v1/deskmate/garden`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: GardenSnapshot | null) => {
        if (alive && d && d.season) setSnapshot(d);
      })
      .catch(() => {
        /* 静默降级 */
      });
    // 羁绊故事簿（P1）：独立拉取，失败静默，不影响花园主流程。
    authedFetch(`${httpBase()}/v1/deskmate/garden/storybook`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d: StoryBook | null) => {
        if (alive && d && d.bond) setStory(d);
      })
      .catch(() => {
        /* 静默降级 */
      })
      .finally(() => {
        if (alive) setLoaded(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  /** P2 · 点亮一朵花：POST 幂等，成功后本地更新 lit 集合。 */
  const lit = async (flowerId: string) => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/garden/lit`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flower_id: flowerId }),
      });
      if (!r.ok) return;
      const d = (await r.json()) as { lit?: boolean; total?: number };
      setSnapshot((prev) => {
        if (!prev) return prev;
        const ids = new Set(prev.p2?.lit?.ids ?? []);
        if (d.lit) ids.add(flowerId);
        return {
          ...prev,
          p2: {
            ...(prev.p2 ?? {}),
            lit: { count: d.total ?? ids.size, ids: [...ids] },
          },
        };
      });
    } catch {
      /* 静默降级 */
    }
  };

  return { snapshot, story, toast, water, loaded, lit, dismiss: () => setToast(null) };
}
