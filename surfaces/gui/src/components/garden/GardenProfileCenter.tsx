/**
 * GardenProfileCenter — 私密花园 · 个人中心（沉浸式漫游重构）。
 *
 * 用户核心诉求（2026-09-02 确认）：
 *   1. 点「个人中心」直接就是一座花园（视觉冲击），不再有独立院门页；
 *   2. 花园本身就是界面：花、草、树、小桔、房屋都站在画面里，是园里的物件，不是列表；
 *   3. 「花园的门」是功能总入口——点它各项功能（它眼中的我/记忆/统计/勋章/设置）才显示、才可点；
 *   4. 点房屋进屋（树屋书房写日记）、点小窝去朋友、点工具棚去工具房，游戏式漫游。
 *
 * 结构：
 *   space = yard（花园全景，默认）| diary（树屋书房）| friends（小窝）| tools（工具棚）
 *   panel = gate（花园的门→功能菜单）| tree（树的详情）| flowers（花圃）| portrait | memory | stats | badges
 *
 * 数据全部来自后端真实接口，失败静默降级；零 LLM；官方小桔统一形象。
 */

import { useMemo, useState } from "react";
import { AlterEgoPage } from "../deskmate/AlterEgoPage";
import { GardenClockOverlay } from "./GardenClockOverlay";
import { GardenDiary } from "./GardenDiary";
import { GardenFriends } from "./GardenFriends";
import { GardenScene } from "./GardenScene";
import { GardenTools } from "./GardenTools";
import { GardenYard } from "./GardenYard";
import {
  GardenBadgesPanel,
  GardenFlowersPanel,
  GardenGateMenu,
  GardenMemoryPanel,
  GardenStatsPanel,
  GardenTreePanel,
} from "./GardenPanels";
import { daypartOf, gardenTheme, seasonOf, type DaypartKey, type SeasonKey } from "./SeasonTheme";
import { useGarden } from "./useGarden";

type Space = "yard" | "diary" | "friends" | "tools";
type Panel = "gate" | "tree" | "flowers" | "portrait" | "memory" | "stats" | "badges" | null;

export function GardenProfileCenter({ onBack }: { onBack: () => void }) {
  const { snapshot, story, toast, water, lit, dismiss } = useGarden();

  const [space, setSpace] = useState<Space>("yard");
  const [panel, setPanel] = useState<Panel>(null);
  const [mascotMsg, setMascotMsg] = useState<string | null>(null);
  const [alterEgoOpen, setAlterEgoOpen] = useState(false);
  const [guestMode, setGuestMode] = useState(() => {
    try { return localStorage.getItem("garden.guestMode") === "1"; } catch { return false; }
  });

  const seasonKey = (snapshot?.season?.season?.key as SeasonKey) ?? seasonOf();
  const daypartKey = (snapshot?.season?.daypart?.key as DaypartKey) ?? daypartOf();
  const themeVars = useMemo(() => gardenTheme(seasonKey, daypartKey), [seasonKey, daypartKey]);
  const flowerCount = snapshot?.stats?.total ?? snapshot?.flowers?.length ?? 0;

  const p2 = useMemo(() => {
    if (!snapshot?.p2) return snapshot?.p2 ?? null;
    return { ...snapshot.p2, water: water ?? snapshot.p2.water };
  }, [snapshot?.p2, water]);

  const toggleGuest = (on: boolean) => {
    setGuestMode(on);
    try { localStorage.setItem("garden.guestMode", on ? "1" : "0"); } catch { /* ignore */ }
  };

  const greet = () => {
    if (guestMode) {
      setMascotMsg("这里只为你留着门，长势不对外展示。");
      return;
    }
    const dp = daypartKey === "dusk" ? "黄昏" : daypartKey === "night" ? "深夜" : daypartKey === "morning" ? "清晨" : "午后";
    setMascotMsg(`第 ${snapshot?.visit_count ?? 0} 次回来陪你啦，${dp}的花园我一直在打理。`);
  };

  /* ---------- 花园全景（yard）：整座院子就是界面 ---------- */
  if (space === "yard") {
    return (
      <div className="garden-root garden-yard-mode" style={themeVars}>
        <GardenYard
          season={seasonKey}
          daypart={daypartKey}
          visitCount={snapshot?.visit_count ?? 0}
          flowerCount={guestMode ? 0 : flowerCount}
          treeStage={guestMode ? null : p2?.tree ? { key: p2.tree.key, title: p2.tree.title } : null}
          onOpenDiary={() => setSpace("diary")}
          onOpenFriends={() => setSpace("friends")}
          onOpenTools={() => setSpace("tools")}
          onOpenTree={() => setPanel("tree")}
          onOpenFlowers={() => setPanel("flowers")}
          onOpenGate={() => setPanel("gate")}
          onMascot={greet}
          onBack={onBack}
        />

        {/* 小桔的问候气泡 */}
        {mascotMsg && (
          <div className="garden-mascot-msg" onClick={() => setMascotMsg(null)}>
            <span className="garden-mascot-msg-text">{mascotMsg}</span>
            <button type="button" className="garden-mascot-msg-close" onClick={() => setMascotMsg(null)}>✕</button>
          </div>
        )}

        {/* 花园的门 → 功能菜单 */}
        {panel === "gate" && (
          <GardenGateMenu
            onOpen={(f) => {
              // 「它眼中的我」→ 直接打开全屏懂我中心（AlterEgoPage：像我度 + 画像栏目 + 待澄清）
              if (f === "portrait") {
                setPanel(null);
                setAlterEgoOpen(true);
              } else setPanel(f === "memory" ? "memory" : f === "stats" ? "stats" : "badges");
            }}
            onClose={() => setPanel(null)}
            onToggleGuest={toggleGuest}
            guestMode={guestMode}
          />
        )}

        {panel === "tree" && (
          <GardenTreePanel
            p2={guestMode ? null : p2}
            water={guestMode ? null : water}
            flowerCount={guestMode ? 0 : flowerCount}
            onClose={() => setPanel(null)}
          />
        )}
        {panel === "flowers" && (
          <GardenFlowersPanel
            flowers={guestMode ? [] : snapshot?.flowers ?? []}
            stats={guestMode ? null : snapshot?.stats ?? null}
            p2={guestMode ? null : p2}
            water={guestMode ? null : water}
            litIds={guestMode ? null : p2?.lit?.ids}
            onLit={lit}
            flora={guestMode ? null : snapshot?.flora}
            story={guestMode ? null : story}
            onClose={() => setPanel(null)}
          />
        )}
        {panel === "memory" && <GardenMemoryPanel onClose={() => setPanel(null)} />}
        {panel === "stats" && <GardenStatsPanel onClose={() => setPanel(null)} />}
        {panel === "badges" && <GardenBadgesPanel onClose={() => setPanel(null)} onBackHome={() => setSpace("friends")} />}

        {/* 花园时钟轻问候（进园确定触发，可关闭） */}
        {toast && <GardenClockOverlay event={toast} flowerCount={flowerCount} onClose={dismiss} />}

        {alterEgoOpen && <AlterEgoPage open onClose={() => setAlterEgoOpen(false)} />}
      </div>
    );
  }

  /* ---------- 房间（走进某间房） ---------- */
  const roomScene = space === "diary" ? "treehouse" : space === "friends" ? "hut" : "toolroom";

  return (
    <div className="garden-root" style={themeVars}>
      <GardenScene
        season={seasonKey}
        daypart={daypartKey}
        visitCount={snapshot?.visit_count ?? 0}
        treeStage={p2?.tree ? { key: p2.tree.key, title: p2.tree.title } : null}
        scene={roomScene}
      />

      <div className="garden-body">
        {space === "diary" && <GardenDiary />}
        {space === "friends" && <GardenFriends onBackHome={() => setSpace("yard")} />}
        {space === "tools" && <GardenTools onOpenAlterEgo={() => setAlterEgoOpen(true)} />}
      </div>

      {/* 回到花园 */}
      <button type="button" className="garden-back-to-yard" onClick={() => setSpace("yard")} title="回到花园">
        ← 回到花园
      </button>

      {toast && <GardenClockOverlay event={toast} flowerCount={flowerCount} onClose={dismiss} />}
      {alterEgoOpen && <AlterEgoPage open onClose={() => setAlterEgoOpen(false)} />}
    </div>
  );
}
