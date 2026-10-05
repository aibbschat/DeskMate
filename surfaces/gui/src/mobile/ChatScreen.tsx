/**
 * 对话屏（手机端第一批的主屏）。
 *
 * 正文直接复用桌面 `Transcript`（纯 props、无 Tauri/three/pdfjs，设计稿 §五判定为可原样
 * import），窄屏版式靠 mobile.css 覆写 `.transcript` 的 768px 上限与字号。
 *
 * 两处手机端专属：
 *  - 顶栏旗子＝待答数量（走 REST 收件箱，不必常驻 socket）；
 *  - 成果芯片：桌面点它会派发给 RightRail（pdfjs/xlsx 重模块），手机端**拦住这两个事件**，
 *    只提示「去桌面打开看」——不为了一个芯片把两个大库拖进窄屏首屏。
 */
import { useEffect, useRef, useState } from "react";
import { Transcript } from "../components/Transcript";
import { OPEN_ARTIFACT_EVENT, OPEN_BOARD_EVENT } from "../components/Markdown";
import { connFaultLabel, faultCopy } from "./session";
import { pendingInteractive } from "./reducer";
import { InteractiveDock } from "./InteractiveDock";
import { MobileComposer } from "./MobileComposer";
import type { useChat } from "./useChat";

export type Chat = ReturnType<typeof useChat>;

interface Props {
  chat: Chat;
  onOpenSheet: (s: "inbox" | "sessions") => void;
}

export function ChatScreen({ chat, onOpenSheet }: Props) {
  const { state, title, fault, booting, historyLoading, inbox } = chat;
  const scRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  const [toast, setToast] = useState("");

  // 跟着流走：只有本来就在底部时才自动滚（用户上滑读旧消息不该被拽回去）
  useEffect(() => {
    const el = scRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [state.items.length, state.streaming, state.running]);

  // 成果/看板芯片 → 手机端不做预览，给一句实话
  useEffect(() => {
    const flash = () => {
      setToast("这份成果去桌面打开看");
      window.setTimeout(() => setToast(""), 2600);
    };
    window.addEventListener(OPEN_ARTIFACT_EVENT, flash);
    window.addEventListener(OPEN_BOARD_EVENT, flash);
    return () => {
      window.removeEventListener(OPEN_ARTIFACT_EVENT, flash);
      window.removeEventListener(OPEN_BOARD_EVENT, flash);
    };
  }, []);

  const pending = pendingInteractive(state.items);

  return (
    <>
      <div className="m-top">
        <button type="button" className="m-pill" onClick={() => onOpenSheet("sessions")}>
          <span className="m-pill-t">{title || "新的对话"}</span>
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.6">
            <path d="M2 3.5 5 6.5 8 3.5" />
          </svg>
        </button>
        <div className="m-spacer" />
        <span className={"m-conn" + (state.connected ? "" : " off")}>
          <i className="m-dot" />
          {state.connected ? "云端 · 在线" : "连接中…"}
        </span>
        <button
          type="button"
          className={"m-flag" + (inbox.length ? " hot" : "")}
          onClick={() => onOpenSheet("inbox")}
          aria-label={"在等你点头 " + inbox.length + " 件"}
        >
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M3.5 2v12M3.5 2.6h9l-2 3.6 2 3.6h-9" />
          </svg>
          {!!inbox.length && <span className="m-badge">{inbox.length}</span>}
        </button>
      </div>

      {(() => {
        const copy = faultCopy(fault) || (!state.connected && !booting ? connFaultLabel() : null);
        if (!copy) return null;
        const hard = fault === "site" || fault === "token";
        return (
          <div className={"m-fault" + (hard ? " bad" : "")}>
            <div className="m-fault-row">
              <div>
                <b>{copy.title}</b>
                <span>{copy.hint}</span>
              </div>
              <button type="button" className="m-retry" onClick={chat.retryBoot}>
                重试
              </button>
            </div>
          </div>
        );
      })()}

      <div
        className="m-scroll"
        ref={scRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        {booting || historyLoading ? (
          <div className="m-empty">正在翻你们的旧话…</div>
        ) : state.items.length === 0 ? (
          <div className="m-empty">
            <img src="/m-icon-180.png" alt="" className="m-empty-av" />
            <p>她在。说点什么吧。</p>
            <span>手机上聊的，她会记住（记忆回灌你 Mac），但 Mac 的会话列表里不会长出这段记录</span>
          </div>
        ) : (
          <Transcript
            items={state.items}
            running={state.running}
            streamingText={state.streaming}
            onApprove={(d) => chat.approve(d as "once" | "deny" | "always_task" | "always_tool")}
            onRetry={chat.retry}
          />
        )}
        {state.running && !state.streaming && (
          <div className="m-thinking">
            <i className="m-dot" />
            {state.toolCount > 0 ? `她在用工具（${state.toolCount}）…` : "她在想…"}
          </div>
        )}
      </div>

      {!!toast && <div className="m-toast">{toast}</div>}

      {pending && (
        <InteractiveDock
          item={pending}
          fromTask={false}
          onApprove={chat.approve}
          onAnswer={chat.answerQuestion}
          onDirectory={chat.respondDir}
          onPlan={chat.respondPlan}
        />
      )}

      <MobileComposer
        live={state.connected}
        running={state.running}
        disabled={booting}
        onSend={chat.send}
        onInterrupt={chat.interrupt}
      />
    </>
  );
}
