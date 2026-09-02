/**
 * GardenProfileCenter — 私密花园 · 个人中心（P0 + P1 + P2）。
 *
 * 在现有个人中心（ProfileCenter 五 Tab）之上加一层"花园"包裹：
 * - 顶部：花园场景条（天空 × 四季 × 昼夜，官方小桔 + 季节粒子站于其中）
 * - 中部：花开横幅（把值得记的事开成的花，P2 分页 + 点亮）
 * - P2 · 花园的树：成长线 + 每日浇水 + 年花 + 花园年鉴
 * - 主体：现有 ProfileCenter 完整保留（记忆/日记/勋章/设置）
 * - 浮层：花园时钟轻问候（进入花园时确定触发，可关闭）
 *
 * 设计原则（用户核心诉求）：
 * - 确定性 > 检测：互动挂在"进入花园 + 本地时间 + 归来间隔 + 节日表"上，绝不做
 *   无法验证的情境猜测，避免"说得好听却遇不到"。
 * - 形象统一：花园只用官方小桔（XiaojuMascot），与桌宠/个人中心同一形象，杜绝割裂。
 * - 沉浸感来自细节：四季昼夜真实随本地时间流转，花瓣平铺 + 原文摘录，小桔随季节换姿态。
 * - 克制：问候一条、可关闭；不打扰主流程。
 *
 * 数据均来自后端真实接口，失败静默降级（季节/昼夜回退到本地计算）。
 */

import { useMemo } from "react";
import { ProfileCenter } from "../deskmate/ProfileCenter";
import { GardenClockOverlay } from "./GardenClockOverlay";
import { GardenFlora } from "./GardenFlora";
import { GardenFlowers } from "./GardenFlowers";
import { GardenScene } from "./GardenScene";
import { GardenStoryBook } from "./GardenStoryBook";
import { GardenTree } from "./GardenTree";
import { daypartOf, gardenTheme, seasonOf, type DaypartKey, type SeasonKey } from "./SeasonTheme";
import { useGarden } from "./useGarden";

export function GardenProfileCenter({ onBack }: { onBack: () => void }) {
  const { snapshot, story, toast, water, lit, dismiss } = useGarden();

  // 优先用后端返回的实时季节/昼夜；后端不可用时回退到本地时间确定性计算。
  const seasonKey = (snapshot?.season?.season?.key as SeasonKey) ?? seasonOf();
  const daypartKey = (snapshot?.season?.daypart?.key as DaypartKey) ?? daypartOf();

  const themeVars = useMemo(() => gardenTheme(seasonKey, daypartKey), [seasonKey, daypartKey]);
  const flowerCount = snapshot?.stats?.total ?? snapshot?.flowers?.length ?? 0;

  // P2 · 浇水状态以 visit 实时结算为准；GET 快照的 water 为占位（避免 GET 副作用）。
  const p2 = useMemo(() => {
    if (!snapshot?.p2) return snapshot?.p2 ?? null;
    return { ...snapshot.p2, water: water ?? snapshot.p2.water };
  }, [snapshot?.p2, water]);

  return (
    <div className="garden-root" style={themeVars}>
      <GardenScene
        season={seasonKey}
        daypart={daypartKey}
        visitCount={snapshot?.visit_count ?? 0}
        treeStage={
          p2?.tree ? { key: p2.tree.key, title: p2.tree.title } : null
        }
      />
      <GardenTree p2={p2} />
      <GardenFlowers
        flowers={snapshot?.flowers ?? []}
        stats={snapshot?.stats ?? null}
        litIds={p2?.lit?.ids}
        onLit={lit}
      />
      <GardenFlora flora={snapshot?.flora} />

      <div className="garden-body">
        <GardenStoryBook story={story} />
        <ProfileCenter onBack={onBack} />
      </div>

      {toast && (
        <GardenClockOverlay event={toast} flowerCount={flowerCount} onClose={dismiss} />
      )}
    </div>
  );
}
