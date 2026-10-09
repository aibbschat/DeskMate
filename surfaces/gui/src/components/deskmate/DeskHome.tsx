/**
 * DeskHome — 桌搭模式空会话首页。
 *
 * 仅当 agent === "deskmate" 且会话为空（idle）时由 App.tsx 渲染。
 *
 * 首页只保留「小桔在忙」——展示小桔自主层正在做什么；主动产出的待办不再以
 * 一长串清单挂在首页（难看、不优雅），改为一个干净的「去信箱」指针 + 未读数
 * （信箱 = proactive_ledger 的门面，信件样式渲染，设计单 2026-10-09）。
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgencyPanel } from "./AgencyPanel";
import { getMailbox, MAILBOX_CHANGED } from "../../api";

export function DeskHome({
  onIntentExecuted,
  onOpenMailbox,
}: {
  onIntentExecuted?: (intentId: string) => void;
  onOpenMailbox?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const [unread, setUnread] = useState(0);

  const refreshUnread = () => {
    getMailbox()
      .then((m) => setUnread(m.ok ? m.unread_count : 0))
      .catch(() => {});
  };

  useEffect(() => {
    refreshUnread();
    const onChanged = () => refreshUnread();
    window.addEventListener(MAILBOX_CHANGED, onChanged);
    return () => window.removeEventListener(MAILBOX_CHANGED, onChanged);
  }, []);

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

      {/* 主动产出台账的「送达层」入口：不再把一长串待办挂在首页，改挂干净的
          信箱指针 + 未读数。信件往来（标已读/稍后/回信/进聊天）都在信箱页完成。 */}
      <button
        type="button"
        className="dm-mailbox-pointer"
        onClick={onOpenMailbox}
        data-testid="go-mailbox"
      >
        <span className="dm-mailbox-bell" aria-hidden>
          ✉
        </span>
        <span className="dm-mailbox-text">
          {unread > 0
            ? t("deskmate.mailbox.hasUnread", { n: unread, defaultValue: `你有 ${unread} 封信在信箱` })
            : t("deskmate.mailbox.empty", { defaultValue: "信箱 · 去看看" })}
        </span>
        {unread > 0 && <span className="dm-mailbox-count">{unread > 99 ? "99+" : unread}</span>}
        <span className="dm-mailbox-arrow" aria-hidden>
          ›
        </span>
      </button>

      {/* L2 自主层「小桔在忙」——常驻展示自主行为 + 高风险提议卡待裁决 */}
      <AgencyPanel onJumpToChat={onIntentExecuted} />
    </div>
  );
}
