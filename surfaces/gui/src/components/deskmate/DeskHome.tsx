/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只留「小桔在忙」。主动产出的待办不在这里挂清单，也不挂「去信箱」条幅
 * （萧哥 10-09：那条幅没必要）——信箱的入口就是侧栏那一行 ✉ 带未读数。
 */

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { AgencyPanel } from "./AgencyPanel";

export function DeskHome({
  onIntentExecuted,
}: {
  onIntentExecuted?: (intentId: string) => void;
}) {
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
      <AgencyPanel onJumpToChat={onIntentExecuted} />
    </div>
  );
}
