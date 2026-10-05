/**
 * 手机端外壳：两屏（对话 / 花园）+ 顶部抽屉 + 底部 tab。
 *
 * 两屏**同时挂载**、用 class 隐藏，不是切屏就卸载——因为她可能正在跑一轮长任务，
 * 切去看个日记不该把 socket 拆掉（`Session.close()` 会停保活、取消重连）。
 * 待答走 REST，所以即使 socket 断了也不影响她在云端继续跑。
 */
import { useState } from "react";
import { ChatScreen } from "./ChatScreen";
import { DebugPanel, InboxList, Sheet, SessionsList, type SheetKind } from "./Sheet";
import { YardScreen } from "./YardScreen";
import { useChat } from "./useChat";

const SHEET_META: Record<Exclude<SheetKind, "none">, { title: string; sub: string }> = {
  inbox: { title: "在等你点头", sub: "答完她就继续跑" },
  sessions: { title: "换个会话续聊", sub: "手机上也认得你们聊到哪了" },
  debug: { title: "连接与同步", sub: "出问题时看这里" },
};

/** 深链初始屏：`/mobile.html#yard` 直接开花园（PWA 快捷方式与真机验证都用它）。 */
const initialTab = (): "chat" | "yard" =>
  (typeof location !== "undefined" && location.hash.replace("#", "") === "yard" ? "yard" : "chat");

export function MobileApp() {
  const chat = useChat();
  const [tab, setTab] = useState<"chat" | "yard">(initialTab);
  const [sheet, setSheet] = useState<SheetKind>("none");
  const meta = sheet === "none" ? null : SHEET_META[sheet];

  return (
    <div className="m-app">
      <div className={"m-pane" + (tab === "chat" ? "" : " hide")}>
        <ChatScreen chat={chat} onOpenSheet={(k) => setSheet(k)} />
      </div>
      <div className={"m-pane" + (tab === "yard" ? "" : " hide")}>
        <YardScreen onOpenDebug={() => setSheet("debug")} />
      </div>

      <Sheet
        kind={sheet}
        title={meta?.title || ""}
        sub={meta?.sub}
        onClose={() => setSheet("none")}
      >
        {sheet === "inbox" && (
          <InboxList
            items={chat.inbox}
            onResolve={(id, resolution) => {
              void chat.resolveAsk(id, resolution);
            }}
          />
        )}
        {sheet === "sessions" && (
          <SessionsList
            current={chat.sessionId}
            onPick={(id, title) => {
              chat.openSession(id, title);
              setSheet("none");
              setTab("chat");
            }}
          />
        )}
        {sheet === "debug" && (
          <DebugPanel
            sessionId={chat.sessionId}
            connected={chat.state.connected}
            running={chat.state.running}
            fault={chat.fault}
            items={chat.state.items.length}
            inbox={chat.inbox.length}
          />
        )}
      </Sheet>

      <nav className="m-tabs">
        <button type="button" className={"m-tab" + (tab === "yard" ? " on" : "")} onClick={() => setTab("yard")}>
          <svg width="19" height="19" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M10 2.6c2.2 0 3.8 1.6 3.8 3.6 0 1.2-.6 2.2-1.4 2.9 1.9.7 3.2 2.4 3.2 4.4v.9H4.4v-.9c0-2 1.3-3.7 3.2-4.4-.8-.7-1.4-1.7-1.4-2.9 0-2 1.6-3.6 3.8-3.6z" />
            <path d="M7 6.2h6" />
          </svg>
          <span>花园</span>
        </button>
        <button type="button" className={"m-tab" + (tab === "chat" ? " on" : "")} onClick={() => setTab("chat")}>
          <svg width="19" height="19" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M3.4 5.4h13.2v8.2H9.8L6.4 16.6V13.6H3.4z" />
          </svg>
          <span>对话</span>
          {!!chat.inbox.length && <i className="m-tab-dot" />}
        </button>
      </nav>
      <div className="m-sab" />
    </div>
  );
}
