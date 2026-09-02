/**
 * GardenYard — 花园全景（蓝图第二层 · 空间分区 · 整个个人中心 = 一座院子）。
 *
 * 用户核心诉求：点「个人中心」直接就是一座花园（视觉冲击），花园本身就是界面——
 * 花、草、树、小桔、房屋都"站在"这座花园的画面里，是可以点的物件，不是列表。
 * 每个物件都是园里的一个入口：
 *   🏡 房屋      → 树屋书房（写日记）
 *   🌳 花园的树  → 树的详情（长势/浇水/年花）
 *   🌷 花丛      → 花圃（真花/点亮/回响）
 *   🚪 花园的门  → 功能菜单（它眼中的我/记忆/统计/勋章/设置）——各项功能收敛在门后
 *   🛖 小窝      → 常驻的朋友（小桔的小窝 + 勋章墙）
 *   🔧 工具棚    → 工具房（同步/知识库/设置）
 *   小桔         → 园中忙碌，点它轻问候
 *
 * 全屏四季底图随本地季节/昼夜真实流转，季节粒子 + 昼夜光照；
 * 物件默认半透明常驻（园里的东西），hover 浮出标签 + 轻放大，点击进入。
 * 全部确定性、零 LLM、官方小桔（杜绝割裂）。
 */

import { GardenTreeSVG } from "./GardenTreeSVG";
import { XiaojuMascot } from "../deskmate/XiaojuMascot";
import { DAYPARTS, SEASONS, type DaypartKey, type SeasonKey } from "./SeasonTheme";
import imgSpring from "../../assets/garden/garden-spring.jpg";
import imgSummer from "../../assets/garden/garden-summer.jpg";
import imgAutumn from "../../assets/garden/garden-autumn.jpg";
import imgWinter from "../../assets/garden/garden-winter.jpg";

const SEASON_BG: Record<string, string> = {
  spring: imgSpring,
  summer: imgSummer,
  autumn: imgAutumn,
  winter: imgWinter,
};

const SEASON_PARTICLES: Record<string, { chars: string[]; cls: string }> = {
  spring: { chars: ["🌸", "🌷", "💮", "🦋", "🌸"], cls: "garden-season-petal" },
  summer: { chars: ["✨", "☀️", "💫", "🦋", "✨"], cls: "garden-season-light" },
  autumn: { chars: ["🍂", "🍁", "🍂", "🍁", "🍂"], cls: "garden-season-leaf" },
  winter: { chars: ["❄️", "❅", "❄️", "❆", "❄️"], cls: "garden-season-snow" },
};

interface YardProps {
  season: SeasonKey;
  daypart: DaypartKey;
  visitCount: number;
  flowerCount: number;
  treeStage?: { key: string; title: string } | null;
  onOpenDiary: () => void;
  onOpenFriends: () => void;
  onOpenTools: () => void;
  onOpenTree: () => void;
  onOpenFlowers: () => void;
  onOpenGate: () => void;
  onMascot: () => void;
  onBack: () => void;
}

export function GardenYard({
  season,
  daypart,
  visitCount,
  flowerCount,
  treeStage,
  onOpenDiary,
  onOpenFriends,
  onOpenTools,
  onOpenTree,
  onOpenFlowers,
  onOpenGate,
  onMascot,
  onBack,
}: YardProps) {
  const s = SEASONS[season];
  const dp = DAYPARTS[daypart];
  const bg = SEASON_BG[season] ?? imgAutumn;
  const particles = SEASON_PARTICLES[season] ?? SEASON_PARTICLES.spring;

  return (
    <div
      className="garden-yard"
      data-season={season}
      data-daypart={daypart}
      style={{ ["--garden-yard-glow" as string]: "rgba(255, 176, 92, 0.5)" }}
    >
      {/* 全屏四季底图 */}
      <img className="garden-yard-bg" src={bg} alt="" draggable={false} />

      {/* 昼夜光照 + 底部融入 */}
      <div className="garden-yard-light" />
      <div className="garden-yard-fade" />

      {/* 季节粒子 */}
      <div className="garden-yard-particles" aria-hidden>
        {particles.chars.map((c, i) => (
          <span
            key={i}
            className={`garden-yard-particle ${particles.cls}`}
            style={{
              left: `${10 + i * 18}%`,
              top: `${12 + (i % 3) * 20}%`,
              animationDelay: `${(i * 1.1) % 4}s`,
            }}
          >
            {c}
          </span>
        ))}
      </div>

      {/* 顶部信息：季节 · 回园 · 树等级 */}
      <div className="garden-yard-info">
        <div className="garden-yard-season">
          <span className="garden-yard-season-emoji">{s.emoji}</span>
          <span className="garden-yard-season-label">{s.label}日花园</span>
          <span className="garden-yard-daypart">
            {dp.emoji} {dp.label}
          </span>
        </div>
        {visitCount > 0 && <div className="garden-yard-return">第 {visitCount} 次回到花园</div>}
        <div className="garden-yard-hint">点一点园里的东西，它们都有话要说</div>
      </div>

      {/* 花园的树（左上方，画面里真长着一棵树） */}
      <button type="button" className="garden-yard-item yard-tree" onClick={onOpenTree} title="花园的树">
        <div className="garden-yard-tree-svg">
          <GardenTreeSVG stage={treeStage?.key ?? "seed"} season={season} size={120} />
        </div>
        <span className="garden-yard-tag">
          <b>🌳 {treeStage?.title ?? "一粒种子"}</b>
          <i>长势 · 浇水 · 年花</i>
        </span>
      </button>

      {/* 花园的门（左下方：功能总入口，点它各项功能才显示） */}
      <button type="button" className="garden-yard-item yard-gate" onClick={onOpenGate} title="花园的门">
        <span className="garden-yard-gate-icon">🚪</span>
        <span className="garden-yard-tag">
          <b>花园的门</b>
          <i>它眼中的我 · 记忆 · 统计</i>
        </span>
      </button>

      {/* 花丛（前景，园里的花都在这） */}
      <button type="button" className="garden-yard-item yard-flowers" onClick={onOpenFlowers} title="花丛">
        <span className="garden-yard-flowers-icon">🌷🌼🌻</span>
        <span className="garden-yard-tag">
          <b>花丛</b>
          <i>{flowerCount} 朵 · 点亮 · 回响</i>
        </span>
      </button>

      {/* 房屋（右上：树屋书房） */}
      <button type="button" className="garden-yard-item yard-house" onClick={onOpenDiary} title="树屋书房">
        <span className="garden-yard-house-icon">🏡</span>
        <span className="garden-yard-tag">
          <b>树屋书房</b>
          <i>写日记 · 最私密</i>
        </span>
      </button>

      {/* 朋友的小窝（屋旁） */}
      <button type="button" className="garden-yard-item yard-hut" onClick={onOpenFriends} title="朋友的小窝">
        <span className="garden-yard-hut-icon">🛖</span>
        <span className="garden-yard-tag">
          <b>朋友的小窝</b>
          <i>小桔住这里 · 勋章</i>
        </span>
      </button>

      {/* 工具棚（右下角） */}
      <button type="button" className="garden-yard-item yard-tools" onClick={onOpenTools} title="工具棚">
        <span className="garden-yard-tools-icon">🔧</span>
        <span className="garden-yard-tag">
          <b>工具棚</b>
          <i>打理花园的日常</i>
        </span>
      </button>

      {/* 小桔在园中忙碌 */}
      <button type="button" className="garden-yard-mascot" onClick={onMascot} title="小桔">
        <div className="garden-yard-mascot-halo" />
        <XiaojuMascot scale={0.34} emotion="idle" />
        <span className="garden-yard-mascot-bubble">我在这里🌱</span>
      </button>

      {/* 右下角：返回个人中心 */}
      <button type="button" className="garden-yard-back" onClick={onBack} title="退出花园">
        ← 退出花园
      </button>
    </div>
  );
}
