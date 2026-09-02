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

import { XiaojuMascot } from "../deskmate/XiaojuMascot";
import { DAYPARTS, SEASONS, type DaypartKey, type SeasonKey } from "./SeasonTheme";
import imgSpring from "../../assets/garden/garden-spring.jpg";
import imgSummer from "../../assets/garden/garden-summer.jpg";
import imgAutumn from "../../assets/garden/garden-autumn.jpg";
import imgWinter from "../../assets/garden/garden-winter.jpg";
import imgTreehouse from "../../assets/garden/garden-treehouse.jpg";
import imgHut from "../../assets/garden/garden-hut.jpg";
import imgToolroom from "../../assets/garden/garden-toolroom.jpg";

export type GardenSceneKey = "yard" | "treehouse" | "hut" | "toolroom";

interface Props {
  season: SeasonKey;
  daypart: DaypartKey;
  visitCount: number;
  treeStage?: { key: string; title: string } | null;
  scene?: GardenSceneKey;
}

/** 季节 → 院子底图（yard 用，随季节切换）。 */
const SEASON_BG: Record<string, string> = {
  spring: imgSpring,
  summer: imgSummer,
  autumn: imgAutumn,
  winter: imgWinter,
};

/** 房间 → 专属底图 + 标题 + 氛围副标题。 */
const ROOM_SCENES: Record<string, { bg: string; title: string; sub: string }> = {
  treehouse: { bg: imgTreehouse, title: "树屋书房", sub: "最私密的一间" },
  hut: { bg: imgHut, title: "常驻的朋友", sub: "小窝亮着灯等你" },
  toolroom: { bg: imgToolroom, title: "工具房", sub: "打理花园的日常" },
};

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
  const bg = room ? room.bg : (SEASON_BG[season] ?? imgAutumn);
  const particles = SEASON_PARTICLES[season] ?? SEASON_PARTICLES.spring;
  const indoor = scene === "treehouse" || scene === "toolroom";

  return (
    <div className={`garden-scene${room ? " is-room" : ""}${indoor ? " is-indoor" : ""}`} data-season={season} data-daypart={daypart} data-scene={scene}>
      {/* 场景底图（房间各有其景） */}
      <img className="garden-scene-bg" src={bg} alt="" draggable={false} />

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
