/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只保留真正的功能入口（文稿编校 / 召唤专家）+ 今日资讯；
 * 所有私人化内容——我的记忆 / 了解度 / 小桔日记 / 小桔是什么 / 记忆同步 /
 * 个人分身 / 长期知识 / 它在学什么 / 勋章 / 默契·深度——统一收纳进
 * 「个人中心」（ProfileCenter），入口即下方横幅，还首页一个干净的空间。
 */

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Icon } from "../Icon";
import XiaojuAvatar from "../XiaojuAvatar";
import { EXPERTS } from "./experts";
import { DeskDigest } from "./DeskDigest";
import { AgencyPanel } from "./AgencyPanel";

/** 当前可召唤的就绪专家（取第一个 ready 的，保证「召唤专家」chip 不写死具体 id）。 */
const READY_EXPERT_ID = EXPERTS.find((e) => e.status !== "soon")?.id ?? "editor";

function dispatchDeskmate(name: string, detail?: Record<string, unknown>) {
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

interface DeskHomeProps {
  /** 打开个人中心（由 App.tsx 切到 profile surface） */
  onOpenProfile?: () => void;
}

export function DeskHome({ onOpenProfile }: DeskHomeProps) {
  const { t, i18n } = useTranslation();

  // 每次回到首页（组件重新挂载）随机抽一条问候，制造新鲜感。
  const greeting = useMemo(() => {
    const list = t("deskmate.greetings", { returnObjects: true }) as unknown as string[];
    if (!Array.isArray(list) || list.length === 0) return "";
    return list[Math.floor(Math.random() * list.length)];
  }, [t, i18n.language]);

  return (
    <div className="hero">
      <h1 className="dm-home-greeting">
        <span className="mark">✦</span>
        {greeting}
      </h1>

      {/* 个人中心入口 —— 私人化内容统一收纳处 */}
      <button className="dm-profile-entry" onClick={onOpenProfile} type="button">
        <XiaojuAvatar size={46} className="dm-profile-entry-avatar" />
        <span className="dm-profile-entry-txt">
          <span className="dm-profile-entry-title">个人中心</span>
          <span className="dm-profile-entry-sub">默契 · 记忆 · 日记 · 勋章，都在这一处</span>
        </span>
        <Icon name="chevronRight" size={18} className="dm-profile-entry-arrow" />
      </button>

      {/* 真正的功能入口 */}
      <div className="dm-chip-grid">
        <button
          className="dm-chip"
          onClick={() => dispatchDeskmate("deskmate:open-proofread", { focus: "edit" })}
          type="button"
        >
          <Icon name="pencil" size={16} />
          <span>{t("deskmate.editMode")}</span>
        </button>
        <button
          className="dm-chip"
          onClick={() => dispatchDeskmate("deskmate:summon-expert", { expertId: READY_EXPERT_ID })}
          type="button"
        >
          <Icon name="sparkle" size={16} />
          <span>召唤专家</span>
        </button>
      </div>

      {/* L2 自主层「小桔在忙」——常驻展示自主行为 + 高风险提议卡待裁决 */}
      <AgencyPanel />

      <DeskDigest />
    </div>
  );
}
