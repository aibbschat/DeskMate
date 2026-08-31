/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 * 把原本藏在桌宠背后的能力（文稿编校 / 记忆 / 了解度）
 * 直接陈列成一排功能 chip，像 WorkBuddy 那样一眼可达；日后加新功能只要在
 * CHIPS 配置数组里 push 一项即可，不动布局代码。
 * （书稿库 / 风格库 已并入「文稿编校」面板；主动建议是功能开关，留在状态面板。）
 *
 * 桌宠不再占据中心，改为在底部沿输入框上方横向走动（由 useMascotWalk 的 dock 模式负责）。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon, type IconName } from "../Icon";
import { authedFetch, httpBase } from "../../api";
import { EXPERTS } from "./experts";
import { DeskDigest } from "./DeskDigest";
import { BondPanel } from "./BondPanel";
import { DiaryPanel } from "./DiaryPanel";
import { PositioningCard } from "./PositioningCard";
import { SyncCard } from "./SyncCard";
import { AlterEgoCard } from "./AlterEgoCard";
import { AlterEgoPage } from "./AlterEgoPage";
import { VaultPanel } from "./VaultPanel";
import { InsightPanel } from "./InsightPanel";
import type { GrowthSnapshot } from "./StatusPanel";

/** 当前可召唤的就绪专家（取第一个 ready 的，保证「召唤专家」chip 不写死具体 id）。 */
const READY_EXPERT_ID = EXPERTS.find((e) => e.status !== "soon")?.id ?? "editor";

interface ChipDef {
  key: string;
  icon: IconName;
  label: string;
  badge?: string;
  onClick: () => void;
}

function dispatchDeskmate(name: string, detail?: Record<string, unknown>) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

export function DeskHome() {
  const { t, i18n } = useTranslation();
  const [learnedTotal, setLearnedTotal] = useState<number | null>(null);
  // 关系阶段 + 默契细分（8.2 可视化）：从 growth snapshot 读取 bond / empathy_domains
  const [growth, setGrowth] = useState<GrowthSnapshot | null>(null);
  // P1-3：小桔日记面板内联展开
  const [showDiary, setShowDiary] = useState(false);
  // P2-8：合规定位卡（关于小桔）内联展开
  const [showPositioning, setShowPositioning] = useState(false);
  // P2-7：记忆同步卡内联展开
  const [showSync, setShowSync] = useState(false);
  // P2-6：个人 AI 分身卡内联展开
  const [showAlterEgo, setShowAlterEgo] = useState(false);
  const [showAlterEgoPage, setShowAlterEgoPage] = useState(false);
  const [showVault, setShowVault] = useState(false);
  // P1-7 补漏：行为规律（P2-3）+ 后台整理（P2-1）此前前端 0 消费，用户看不见
  const [showInsight, setShowInsight] = useState(false);

  // 每次回到首页（组件重新挂载）随机抽一条问候，制造新鲜感。
  const greeting = useMemo(() => {
    const list = t("deskmate.greetings", { returnObjects: true }) as unknown as string[];
    if (!Array.isArray(list) || list.length === 0) return "";
    return list[Math.floor(Math.random() * list.length)];
  }, [t, i18n.language]);

  // 拉取首页 chip 的状态徽标：了解度总数。
  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/learned-history`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d?.stats) setLearnedTotal(d.stats.total ?? 0); })
      .catch(() => {/* 静默降级，徽标不显示 */});
  }, []);

  // 拉取成长快照（bond 阶段 + 默契细分）。失败静默降级，不阻塞首页。
  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/growth`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d && typeof d.level === "number") setGrowth(d); })
      .catch(() => {/* 静默降级 */});
  }, []);

  const chips: ChipDef[] = [
    {
      key: "edit",
      icon: "pencil",
      label: t("deskmate.editMode"),
      onClick: () => dispatchDeskmate("deskmate:open-proofread", { focus: "edit" }),
    },
    {
      key: "memory",
      icon: "shield",
      label: t("deskmate.myMemory"),
      onClick: () => dispatchDeskmate("deskmate:open-memory"),
    },
    {
      key: "understanding",
      icon: "sparkle",
      label: t("deskmate.understanding"),
      badge:
        learnedTotal !== null && learnedTotal > 0 ? String(learnedTotal) : undefined,
      onClick: () => dispatchDeskmate("deskmate:open-understanding"),
    },
    {
      key: "summon-expert",
      icon: "sparkle",
      label: "召唤专家",
      onClick: () => dispatchDeskmate("deskmate:summon-expert", { expertId: READY_EXPERT_ID }),
    },
    {
      key: "diary",
      icon: "book",
      label: "小桔日记",
      onClick: () => setShowDiary((v) => !v),
    },
    {
      key: "positioning",
      icon: "shield",
      label: "小桔是什么",
      onClick: () => setShowPositioning((v) => !v),
    },
    {
      key: "sync",
      icon: "refresh",
      label: "记忆同步",
      onClick: () => setShowSync((v) => !v),
    },
    {
      key: "alter-ego",
      icon: "sparkle",
      label: "个人分身",
      onClick: () => setShowAlterEgo((v) => !v),
    },
    {
      key: "vault",
      icon: "book",
      label: "长期知识",
      onClick: () => setShowVault((v) => !v),
    },
    {
      key: "insight",
      icon: "sparkle",
      label: "它在学什么",
      onClick: () => setShowInsight((v) => !v),
    },
  ];

  return (
    <div className="hero">
      <h1 className="dm-home-greeting">
        <span className="mark">✦</span>
        {greeting}
      </h1>
      <div className="dm-chip-grid">
        {chips.map((c) => (
          <button key={c.key} className="dm-chip" onClick={c.onClick} type="button">
            <Icon name={c.icon} size={16} />
            <span>{c.label}</span>
            {c.badge && <span className="dm-chip-badge">{c.badge}</span>}
          </button>
        ))}
      </div>
      {/* 关系阶段 + 默契细分（8.2：让「越用越懂你」可感可看） */}
      <div className="dm-bond-slot">
        <BondPanel growth={growth} />
      </div>
      {/* P1-3：小桔日记（内联展开，chip 切换） */}
      {showDiary && <DiaryPanel />}
      {/* P2-8：合规定位卡（关于小桔，内联展开，chip 切换） */}
      {showPositioning && <PositioningCard />}
      {/* P2-7：记忆同步卡（内联展开，chip 切换） */}
      {showSync && <SyncCard />}
      {/* P2-6：个人 AI 分身卡（内联展开，chip 切换） */}
      {showAlterEgo && <AlterEgoCard onOpenPage={() => setShowAlterEgoPage(true)} />}
      {/* P2-6：「它眼中的我」全屏页 */}
      <AlterEgoPage open={showAlterEgoPage} onClose={() => setShowAlterEgoPage(false)} />
      {/* P1-7：长期知识库 */}
      {showVault && <VaultPanel />}
      {/* P1-7 补漏：行为规律（P2-3）+ 后台整理（P2-1）此前前端 0 消费 */}
      {showInsight && <InsightPanel />}
      <DeskDigest />
    </div>
  );
}
