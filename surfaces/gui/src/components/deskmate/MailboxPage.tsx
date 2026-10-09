/**
 * MailboxPage — 信箱（送达层门面，2026-10-09）。
 *
 * 信箱 = proactive_ledger 的门面：只持信件元数据，所有对话走同一个聊天引擎、
 * 同一个会话消息库。两条路径：
 *   ① 轻回信（关怀类）→ 隐藏 mailbox 会话 + 复用 Session 类（同一 WS 引擎），
 *      以「信件样式」渲染往来（萧哥拍板：绝不弹聊天气泡）。
 *   ② 进聊天（值得聊的）→ 起一个普通 deskmate 会话，气泡样式续聊（App 侧处理）。
 *
 * 已读≠拒绝：标已读回流情感层「用户看见了」（后端 /read 落 mailbox_seen.jsonl）。
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  announceMailboxChanged,
  dismissMailboxLetter,
  getMailbox,
  getSessionMessages,
  readMailboxLetter,
  replyMailboxLetter,
  snoozeMailboxLetter,
  Session,
  type ConversationMessage,
  type MailboxLetter,
  type MailboxView,
} from "../../api";

// 来源/种类 → 信笺抬头旁的人话标签（无映射则回退 source:kind）。
const SOURCE_LABEL: Record<string, string> = {
  mobile_voice: "手机灵感",
  cloud_audit: "云端观察",
  care: "关怀",
  interview: "访谈",
  observation: "观察",
  idea_capture: "灵感",
  propose: "提议",
};
function sourceLabel(l: MailboxLetter): string {
  const s = SOURCE_LABEL[l.source] || l.source;
  const k = SOURCE_LABEL[l.kind] || l.kind;
  return s === k ? s : `${s} · ${k}`;
}
function shortHandle(id: string): string {
  return id.split(":").pop()?.slice(0, 6) ?? "";
}

interface ThreadMsg {
  role: "user" | "assistant";
  text: string;
  ts: number;
}

export function MailboxPage({
  onBack,
  onOpenChat,
}: {
  onBack: () => void;
  onOpenChat: (letter: MailboxLetter) => void;
}) {
  const { t } = useTranslation();
  const [view, setView] = useState<MailboxView | null>(null);
  const [loading, setLoading] = useState(true);
  const [openId, setOpenId] = useState<string | null>(null); // 当前展开的信
  const [thread, setThread] = useState<ThreadMsg[]>([]); // 当前信的轻回信往来
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [replyErr, setReplyErr] = useState<string | null>(null);
  const sessionRef = useRef<Session | null>(null);
  const activeLetterRef = useRef<string | null>(null);

  const load = () => {
    setLoading(true);
    getMailbox()
      .then((m) => setView(m))
      .catch(() => setView({ ok: false, unread_count: 0, items: [] }))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    return () => {
      // 卸载时关掉可能存在的 mailbox WS，避免幽灵 socket（2026-10-05 教训）。
      sessionRef.current?.close();
      sessionRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const closeReply = () => {
    sessionRef.current?.close();
    sessionRef.current = null;
    activeLetterRef.current = null;
    setOpenId(null);
    setThread([]);
    setDraft("");
    setReplyErr(null);
  };

  // 展开某封信 → 轻回信：确保隐藏 mailbox 会话存在，拉历史往来，连 WS 复用引擎。
  const openReply = async (letter: MailboxLetter) => {
    if (openId === letter.id) {
      closeReply();
      return;
    }
    // 切换信件前先关旧 WS。
    sessionRef.current?.close();
    sessionRef.current = null;
    activeLetterRef.current = letter.id;
    setOpenId(letter.id);
    setDraft("");
    setReplyErr(null);
    setThread([]);
    setSending(false);

    const { ok, session_id } = await replyMailboxLetter(letter.id);
    if (!ok || !session_id) {
      setReplyErr(t("deskmate.mailbox.replyFailed", { defaultValue: "回信会话创建失败" }));
      return;
    }
    // 历史往来（同一消息库，信件样式渲染）。
    try {
      const hist = (await getSessionMessages(session_id)) as ConversationMessage[];
      setThread(
        hist
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({
            role: m.role === "user" ? "user" : "assistant",
            text: typeof m.content === "string" ? m.content : JSON.stringify(m.content),
            ts: typeof m.ts === "number" ? m.ts : Date.now(),
          })),
      );
    } catch {
      /* 历史拉取失败不阻断回信 */
    }

    // 复用同一聊天引擎（Session 类含保活/重连/outbox）。
    sessionRef.current = new Session(session_id, "", "deskmate", {
      onEvent: (ev) => {
        if (ev.type === "assistant_message") {
          const text = (ev.data && (ev.data.text as string)) || "";
          if (text) setThread((p) => [...p, { role: "assistant", text, ts: Date.now() }]);
        } else if (ev.type === "turn_start") {
          setSending(true);
        } else if (ev.type === "turn_done" || ev.type === "turn_end") {
          setSending(false);
        } else if (ev.type === "error") {
          setReplyErr((ev.data && (ev.data.error as string)) || "出错了");
          setSending(false);
        }
      },
      onClose: () => setSending(false),
    });
  };

  const sendReply = () => {
    const text = draft.trim();
    if (!text || sending || !sessionRef.current) return;
    setThread((p) => [...p, { role: "user", text, ts: Date.now() }]);
    setDraft("");
    setSending(true);
    setReplyErr(null);
    sessionRef.current.userMessage(text);
  };

  const doRead = async (l: MailboxLetter) => {
    await readMailboxLetter(l.id);
    setView((v) => (v ? { ...v, items: v.items.map((it) => (it.id === l.id ? { ...it, read: true, read_at: it.read_at || new Date().toISOString() } : it)) } : v));
    announceMailboxChanged();
  };
  const doSnooze = async (l: MailboxLetter) => {
    await snoozeMailboxLetter(l.id);
    setView((v) => (v ? { ...v, items: v.items.filter((it) => it.id !== l.id) } : v));
    announceMailboxChanged();
  };
  const doDismiss = async (l: MailboxLetter) => {
    await dismissMailboxLetter(l.id);
    if (openId === l.id) closeReply();
    setView((v) => (v ? { ...v, items: v.items.filter((it) => it.id !== l.id) } : v));
    announceMailboxChanged();
  };

  const items = view?.items ?? [];

  return (
    <div className="dm-mailbox">
      <div className="dm-mailbox-head">
        <button type="button" className="dm-mailbox-back" onClick={onBack} aria-label={t("common.back")}>
          ‹
        </button>
        <h1 className="dm-mailbox-title">{t("deskmate.mailbox.title", { defaultValue: "信箱" })}</h1>
        <span className="dm-mailbox-unread">
          {view ? t("deskmate.mailbox.unreadCount", { count: view.unread_count, defaultValue: `${view.unread_count} 封未读` }) : ""}
        </span>
      </div>

      {loading && !view ? (
        <div className="dm-mailbox-empty">{t("common.loading", { defaultValue: "加载中…" })}</div>
      ) : items.length === 0 ? (
        <div className="dm-mailbox-empty">{t("deskmate.mailbox.allClear", { defaultValue: "信箱是空的，小桔没啥要递给你的。" })}</div>
      ) : (
        <div className="dm-mailbox-list">
          {items.map((l) => {
            const expanded = openId === l.id;
            return (
              <article
                key={l.id}
                className={"dm-letter" + (l.read ? " dm-letter-read" : "") + (expanded ? " dm-letter-open" : "")}
              >
                <header className="dm-letter-head">
                  <div className="dm-letter-from">
                    <span className="dm-letter-sig">小桔</span>
                    <span className="dm-letter-tag">{sourceLabel(l)}</span>
                    <span className="dm-letter-id">#{shortHandle(l.id)}</span>
                  </div>
                  <time className="dm-letter-date">{fmtDate(l.created_at)}</time>
                </header>

                <div className="dm-letter-body">
                  {l.content}
                  {l.understanding ? (
                    <p className="dm-letter-understanding">她的理解：{l.understanding}</p>
                  ) : null}
                  {l.evidence ? <p className="dm-letter-evidence">依据：{l.evidence}</p> : null}
                </div>

                <div className="dm-letter-actions">
                  {!l.read && (
                    <button type="button" className="dm-letter-btn" onClick={() => doRead(l)}>
                      {t("deskmate.mailbox.markRead", { defaultValue: "标已读" })}
                    </button>
                  )}
                  <button type="button" className="dm-letter-btn" onClick={() => doSnooze(l)}>
                    {t("deskmate.mailbox.later", { defaultValue: "稍后" })}
                  </button>
                  <button type="button" className="dm-letter-btn" onClick={() => doDismiss(l)}>
                    {t("deskmate.mailbox.dismiss", { defaultValue: "不用了" })}
                  </button>
                  <button type="button" className="dm-letter-btn dm-letter-btn-primary" onClick={() => onOpenChat(l)}>
                    {t("deskmate.mailbox.openChat", { defaultValue: "进聊天" })}
                  </button>
                  <button type="button" className="dm-letter-btn" onClick={() => openReply(l)}>
                    {expanded
                      ? t("deskmate.mailbox.closeReply", { defaultValue: "收起" })
                      : t("deskmate.mailbox.reply", { defaultValue: "回信" })}
                  </button>
                </div>

                {expanded && (
                  <div className="dm-letter-thread">
                    {thread.length === 0 ? (
                      <p className="dm-letter-thread-empty">
                        {t("deskmate.mailbox.threadStart", { defaultValue: "写封回信给她吧——走的是同一个聊天引擎，信箱里以信件样式呈现。" })}
                      </p>
                    ) : (
                      thread.map((m, i) => (
                        <div key={i} className={"dm-letter-msg dm-letter-msg-" + m.role}>
                          <div className="dm-letter-msg-from">{m.role === "assistant" ? "小桔" : "萧哥"}</div>
                          <div className="dm-letter-msg-text">{m.text}</div>
                          <time className="dm-letter-msg-date">{fmtDate(new Date(m.ts * 1000).toISOString())}</time>
                        </div>
                      ))
                    )}
                    {replyErr && <div className="dm-letter-reply-err">{replyErr}</div>}
                    <div className="dm-letter-compose">
                      <textarea
                        className="dm-letter-textarea"
                        value={draft}
                        placeholder={t("deskmate.mailbox.replyPlaceholder", { defaultValue: "给小桔写封回信…" })}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                            e.preventDefault();
                            sendReply();
                          }
                        }}
                        rows={3}
                      />
                      <button
                        type="button"
                        className="dm-letter-send"
                        disabled={!draft.trim() || sending}
                        onClick={sendReply}
                      >
                        {sending
                          ? t("deskmate.mailbox.sending", { defaultValue: "寄出中…" })
                          : t("deskmate.mailbox.send", { defaultValue: "寄出" })}
                      </button>
                    </div>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function fmtDate(s: string): string {
  if (!s) return "";
  const d = new Date(s.length <= 11 ? `${s}Z` : s);
  if (isNaN(d.getTime())) return s;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
