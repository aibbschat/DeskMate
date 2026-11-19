/**
 * GardenScene — 花园各空间的场景头部（真实底图 + 官方小桔 + 季节粒子）。
 *
 * 按 scene 切换底图与氛围，让每个房间各有其景：
 *   yard       院子/花园 —— 四季真实花园底图（春樱/夏绿/秋金/冬雪，随本地季节切换）
 *   treehouse  树屋书房 —— 树屋室内底图（圆窗黄昏、书桌台灯、书架，暖橘治愈）
 *   hut        常驻的朋友 —— 小木屋小窝底图（暖黄窗灯、星星串灯、茶桌，傍晚陪伴感）
 *   toolroom   工具房 —— 原木工具房底图（工作台、煤油灯、花盆工具，克制温暖）
 *
 * 与 4 张「小桔版」设计图同款画风：3D 卡通治愈、暖橘黄昏主调、童话质感、无人物——
 * 小桔始终用官方形象单独叠加，杜绝"个人中心一个、花园一个"的割裂。
 * 全部确定性：底图/标题由 scene + 本地季节决定，零 LLM。
 */

import { useEffect, useState } from "react";
import { XiaojuMascot } from "../deskmate/XiaojuMascot";
import { DAYPARTS, SEASONS, type DaypartKey, type SeasonKey } from "./SeasonTheme";

/**
 * 2026-11-19 PR15（#31）：7 张 0.7–1.0MB 的底图原来静态 import，进一次花园
 * 会把 7 张全拉下来（~7MB）。改成按 scene/season 动态 import：首屏只加载当前
 * 那一张，其它等真正切过去再取；<img> 同时加 loading="lazy"。
 */
const SEASON_LOADERS: Record<string, () => Promise<{ default: string }>> = {
  spring: () => import("../../assets/garden/garden-spring.jpg"),
  summer: () => import("../../assets/garden/garden-summer.jpg"),
  autumn: () => import("../../assets/garden/garden-autumn.jpg"),
  winter: () => import("../../assets/garden/garden-winter.jpg"),
};
const ROOM_LOADERS: Record<string, () => Promise<{ default: string }>> = {
  treehouse: () => import("../../assets/garden/garden-treehouse.jpg"),
  hut: () => import("../../assets/garden/garden-hut.jpg"),
  toolroom: () => import("../../assets/garden/garden-toolroom.jpg"),
};

export type GardenSceneKey = "yard" | "treehouse" | "hut" | "toolroom";

interface Props {
  season: SeasonKey;
  daypart: DaypartKey;
  visitCount: number;
  treeStage?: { key: string; title: string } | null;
  scene?: GardenSceneKey;
}

/** 房间 → 标题 + 氛围副标题。 */
const ROOM_SCENES: Record<string, { title: string; sub: string }> = {
  treehouse: { title: "树屋书房", sub: "最私密的一间" },
  hut: { title: "常驻的朋友", sub: "小窝亮着灯等你" },
  toolroom: { title: "工具房", sub: "打理花园的日常" },
};

/** 当前场景/季节对应的底图 URL（按需加载，切走时丢弃上一张的在途结果）。 */
function useSceneBackground(scene: GardenSceneKey, season: SeasonKey): string {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let cancelled = false;
    setUrl("");
    const loader =
      scene !== "yard"
        ? ROOM_LOADERS[scene]
        : (SEASON_LOADERS[season] ?? SEASON_LOADERS.autumn);
    if (!loader) return;
    loader()
      .then((m) => {
        if (!cancelled) setUrl(m.default);
      })
      .catch(() => {
        /* 图片缺失不该让花园白屏：保持占位，等下一次切换 */
      });
    return () => {
      cancelled = true;
    };
  }, [scene, season]);
  return url;
}

const SEASON_PARTICLES: Record<string, { chars: string[]; cls: string }> = {
  spring: { chars: ["🌸", "🌷", "💮", "🌸", "🦋"], cls: "garden-season-petal" },
  summer: { chars: ["✨", "☀️", "💫", "✨", "🦋"], cls: "garden-season-light" },
  autumn: { chars: ["🍂", "🍁", "🍂", "🍁", "🍂"], cls: "garden-season-leaf" },
  winter: { chars: ["❄️", "❄️", "❅", "❄️", "❆"], cls: "garden-season-snow" },
};

export function GardenScene({ season, daypart, visitCount, treeStage, scene = "yard" }: Props) {
  const s = SEASONS[season];
  const dp = DAYPARTS[daypart];
  const room = scene !== "yard" ? ROOM_SCENES[scene] : null;
  const bgUrl = useSceneBackground(scene, season);
  const particles = SEASON_PARTICLES[season] ?? SEASON_PARTICLES.spring;
  const indoor = scene === "treehouse" || scene === "toolroom";

  return (
    <div className={`garden-scene${room ? " is-room" : ""}${indoor ? " is-indoor" : ""}`} data-season={season} data-daypart={daypart} data-scene={scene}>
      {/* 场景底图（房间各有其景） */}
      {bgUrl ? (
        <img
          className="garden-scene-bg"
          src={bgUrl}
          alt=""
          draggable={false}
          loading="lazy"
          decoding="async"
        />
      ) : (
        <div className="garden-scene-bg garden-scene-bg--loading" />
      )}

      {/* 光照层：院子随昼夜；室内是暖橘灯色 */}
      <div className="garden-scene-light" />

      {/* 底部渐变：融入主体区 */}
      <div className="garden-scene-fade" />

      {/* 左上：场景标题（对应设计图顶部标签） */}
      <div className="garden-scene-info">
        <div className="garden-season">
          {room ? (
            <>
              <span className="garden-season-emoji">{scene === "treehouse" ? "📖" : scene === "hut" ? "🏡" : "🔧"}</span>
              <span className="garden-season-label">{room.title}</span>
              <span className="garden-daypart">{room.sub}</span>
            </>
          ) : (
            <>
              <span className="garden-season-emoji">{s.emoji}</span>
              <span className="garden-season-label">{s.label}日花园</span>
              <span className="garden-daypart">
                {dp.emoji} {dp.label}
              </span>
            </>
          )}
        </div>
        {!room && visitCount > 0 && (
          <div className="garden-return">第 {visitCount} 次回到花园</div>
        )}
        {!room && treeStage?.title && (
          <div className="garden-scene-tree" title={treeStage.title}>
            🌳 {treeStage.title}
          </div>
        )}
      </div>

      {/* 右侧：官方小桔站园中/坐窗边 + 光晕 + 季节粒子 */}
      <div className="garden-scene-mascot">
        <div className="garden-halo" />
        <div className="garden-season-particles" aria-hidden>
          {particles.chars.map((c, i) => (
            <span
              key={i}
              className={`garden-season-particle ${particles.cls}`}
              style={{
                left: `${14 + i * 19}%`,
                top: `${18 + (i % 2) * 26}%`,
                animationDelay: `${(i * 0.9) % 4}s`,
              }}
            >
              {c}
            </span>
          ))}
        </div>
        <XiaojuMascot scale={0.5} emotion="idle" />
      </div>
    </div>
  );
}
