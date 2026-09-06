/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只保留「小桔在忙」——展示小桔自主层正在做什么。
 * 功能入口（文稿编校）已移入专家中心；个人中心入口已移至左侧栏；
 * 今日资讯已撤掉（小桔会自己找资讯）。
 */

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { AgencyPanel } from "./AgencyPanel";

export function DeskHome() {
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

      {/* L2 自主层「小桔在忙」——常驻展示自主行为 + 高风险提议卡待裁决 */}
      <AgencyPanel />
    </div>
  );
}
