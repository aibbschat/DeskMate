/**
 * GardenScene — 花园顶部场景条（天空 + 地平线 + 小桔 + 季节/昼夜信息）。
 *
 * 纯 CSS/SVG 渲染，无外部图片资源：天空渐变色、远近两叠山丘/树影由 CSS 变量
 * （gardenTheme 注入 garden-root）驱动，随季节 × 昼夜切换。官方小桔（XiaojuMascot，
 * 与桌宠同一形象、复用情绪粒子机制）站在场景中作为花园的园丁。
 *
 * P2 · 季节粒子：春飘花瓣 / 夏浮光点 / 秋落红叶 / 冬飘细雪 —— 复用桌宠粒子
 * （xj-particle）的表现语言，给小桔一个随季节变化的专属姿态，不新增形象。
 *
 * 信息层级：
 * - 左侧：季节 + 昼夜标签 + 回园次数 + 花园的树等级
 * - 右侧：官方小桔 + 季节粒子
 */

import { XiaojuMascot } from "../deskmate/XiaojuMascot";
import { DAYPARTS, SEASONS, type DaypartKey, type SeasonKey } from "./SeasonTheme";

interface Props {
  season: SeasonKey;
  daypart: DaypartKey;
  visitCount: number;
  treeStage?: { key: string; title: string } | null;
}

/** 季节粒子：每季一组 emoji + 动画类（确定性，随季节固定，零检测）。 */
const SEASON_PARTICLES: Record<string, { chars: string[]; cls: string }> = {
  spring: { chars: ["🌸", "🌷", "💮", "🌸"], cls: "garden-season-petal" },
  summer: { chars: ["✨", "☀️", "💫", "✨"], cls: "garden-season-light" },
  autumn: { chars: ["🍂", "🍁", "🍂", "🍁"], cls: "garden-season-leaf" },
  winter: { chars: ["❄️", "❄️", "❅", "❄️"], cls: "garden-season-snow" },
};

export function GardenScene({ season, daypart, visitCount, treeStage }: Props) {
  const s = SEASONS[season];
  const dp = DAYPARTS[daypart];
  const particles = SEASON_PARTICLES[season] ?? SEASON_PARTICLES.spring;
  return (
    <div className="garden-scene">
      {/* 天空（季节 × 昼夜，由 garden-root 上的 CSS 变量决定） */}
      <div className="garden-sky" />
      <div className="garden-tint" />
      {/* 远景山丘 / 近景花丛（随季节变量变色） */}
      <svg className="garden-hills" viewBox="0 0 1440 120" preserveAspectRatio="none" aria-hidden>
        <path d="M0,90 Q240,40 520,80 T1080,70 T1440,88 L1440,120 L0,120 Z" fill="var(--garden-leaf)" opacity="0.30" />
        <path d="M0,105 Q320,70 720,96 T1440,102 L1440,120 L0,120 Z" fill="var(--garden-leaf)" opacity="0.20" />
      </svg>
      <div className="garden-scene-info">
        <div className="garden-season">
          <span className="garden-season-emoji">{s.emoji}</span>
          <span className="garden-season-label">{s.label}日花园</span>
          <span className="garden-daypart">
            {dp.emoji} {dp.label}
          </span>
        </div>
        {visitCount > 0 && (
          <div className="garden-return">第 {visitCount} 次回到花园</div>
        )}
        {treeStage?.title && (
          <div className="garden-scene-tree" title={treeStage.title}>
            🌳 {treeStage.title}
          </div>
        )}
      </div>
      <div className="garden-scene-mascot">
        <div className="garden-halo" />
        {/* P2 · 季节粒子层（复用桌宠粒子表现语言） */}
        <div className="garden-season-particles" aria-hidden>
          {particles.chars.map((c, i) => (
            <span
              key={i}
              className={`garden-season-particle ${particles.cls}`}
              style={{
                left: `${16 + i * 22}%`,
                animationDelay: `${(i * 0.9) % 4}s`,
              }}
            >
              {c}
            </span>
          ))}
        </div>
        <XiaojuMascot scale={0.36} emotion="idle" />
      </div>
    </div>
  );
}
