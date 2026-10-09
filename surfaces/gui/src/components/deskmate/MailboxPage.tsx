/**
 * MailboxPage — 信箱（送达层门面，2026-10-09）。
 *
 * 信箱 = proactive_ledger 的门面：只持信件元数据，所有对话走同一个聊天引擎、
 * 同一个会话消息库。两条路径：
 *   ① 轻回信（关怀类）→ 隐藏 mailbox 会话 + 复用 Session 类（同一 WS 引擎），
 *      以「书信体」渲染往来（萧哥拍板：绝不弹聊天气泡）。
 *   ② 进聊天（值得聊的）→ 把隐藏往来 promote 成普通会话，气泡样式续聊（App 侧处理）。
 *
 * 展开即已读 + 显式「标已读」；标已读在后端回流「用户看见了」（设计单 §7 要害），
 * 免得信箱成她的表白墙+坟场。已读超 7 天仍未处理 → 「已搁置」灰度归档，不删。
 */

import { useEffect, useMemo, useRef, useState } from "react";
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

// 来源/种类 → 信笺抬头旁的人话标签。落不到映射时**绝不**把 `cloud_audit` /
// `memory_write_error` 这类工程键名甩到界面上（一律回退「她的话」）。
const SOURCE_LABEL: Record<string, string> = {
  cloud_audit: "她留意到",
  mobile_voice: "手机那头",
  automation: "家里的事",
  care: "关怀",
  interview: "访谈",
  observation: "观察",
  emotional_care: "她想关心你",
  idea_capture: "灵感",
  writing_assistance: "写作上",
  information_support: "资料佐证",
  plot_contradiction: "书稿前后对不上",
  plot_repetition: "书稿重复",
  propose: "提议",
};
const KIND_FALLBACK = "她的话";

export function letterLabel(l: MailboxLetter): string {
  const s = SOURCE_LABEL[l.source] ?? "";
  const k = SOURCE_LABEL[l.kind] ?? "";
  if (s && k) return s === k ? s : `${s} · ${k}`;
  return s || k || KIND_FALLBACK;
}
export function shortHandle(id: string): string {
  return id.split(":").pop()?.slice(0, 6) ?? "";
}

interface ThreadMsg {
  role: "user" | "assistant";
  text: string;
  ts: number; // epoch **毫秒**（历史 ts 是秒，取回时统一乘 1000）
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
  const [thread, setThread] = useState<ThreadMsg[]>([]); // 当前信的往来
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [replyErr, setReplyErr] = useState<string | null>(null);
  const [showShelved, setShowShelved] = useState(false); // 「已搁置」默认收起
  const sessionRef = useRef<Session | null>(null);

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

  const items = view?.items ?? [];
  const live = useMemo(() => items.filter((l) => !l.stale), [items]);
  const shelved = useMemo(() => items.filter((l) => l.stale), [items]);

  const closeReply = () => {
    sessionRef.current?.close();
    sessionRef.current = null;
    setOpenId(null);
    setThread([]);
    setDraft("");
    setReplyErr(null);
    setSending(false);
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
    setOpenId(letter.id);
    setDraft("");
    setReplyErr(null);
    setThread([]);
    setSending(false);
    // 展开即已读（设计单 §3.1「标已读/已展开」同一时间戳语义）。
    if (!letter.read) {
      await doRead(letter);
    }

    const { ok, session_id } = await replyMailboxLetter(letter.id);
    if (!ok || !session_id) {
      setReplyErr(t("deskmate.mailbox.replyFailed", { defaultValue: "回信会话创建失败" }));
      return;
    }
    // 历史往来（同一消息库，书信体渲染）。
    try {
      const hist = (await getSessionMessages(session_id)) as ConversationMessage[];
      setThread(
        hist
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m) => ({
            role: m.role === "user" ? "user" : "assistant",
            text: typeof m.content === "string" ? m.content : JSON.stringify(m.content),
            ts: typeof m.ts === "number" ? m.ts * 1000 : Date.now(),
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
    setView((v) =>
      v
        ? {
            ...v,
            unread_count: Math.max(0, v.unread_count - (l.read ? 0 : 1)),
            items: v.items.map((it) =>
              it.id === l.id
                ? { ...it, read: true, read_at: it.read_at || new Date().toISOString() }
                : it
            ),
          }
        : v
    );
    announceMailboxChanged();
  };
  const doSnooze = async (l: MailboxLetter) => {
    await snoozeMailboxLetter(l.id);
    if (openId === l.id) closeReply();
    dropLetter(l, "snoozed");
  };
  const doDismiss = async (l: MailboxLetter) => {
    await dismissMailboxLetter(l.id);
    if (openId === l.id) closeReply();
    dropLetter(l, "dismissed");
  };
  // 本地即时收信（稍后/不用了都让它离开当前列表），再广播给侧栏刷未读数。
  const dropLetter = (l: MailboxLetter, _via: "snoozed" | "dismissed") => {
    setView((v) => (v ? { ...v, items: v.items.filter((it) => it.id !== l.id) } : v));
    announceMailboxChanged();
  };

  // 案 A（萧哥 10-09 拍板）：一摞信纸。她的信是一张纸（抬头 + 正文 + 署名落款），
  // 往来各占一张、微微右移一档表示「同一件事的后续」，回信是压在最下面的一张空白稿纸。
  // 处置那排小字放在纸**外面**——纸上不钉按钮，一钉就成工具卡了。
  const renderLetter = (l: MailboxLetter) => {
    const expanded = openId === l.id;
    return (
      <article
        key={l.id}
        className={
          "dm-letter" + (l.read ? " dm-letter-read" : "") + (l.stale ? " dm-letter-stale" : "")
        }
      >
        <div className="dm-sheet">
          <header className="dm-letter-head">
            <span className="dm-letter-sig">小桔：</span>
            <span className="dm-letter-tag">{letterLabel(l)}</span>
            <span className="dm-letter-id">#{shortHandle(l.id)}</span>
            {l.stale && (
              <span className="dm-letter-shelf-chip">
                {t("deskmate.mailbox.shelved", { defaultValue: "已搁置" })}
              </span>
            )}
          </header>

          <div className="dm-letter-body">
            {l.content}
            {l.understanding ? (
              <p className="dm-letter-understanding">她的理解：{l.understanding}</p>
            ) : null}
            {l.evidence ? <p className="dm-letter-evidence">依据：{l.evidence}</p> : null}
          </div>

          <div className="dm-letter-tail">
            <span className="dm-letter-by">—— 小桔</span>
            <time className="dm-letter-date">{fmtDate(l.created_at)}</time>
          </div>
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
          <button type="button" className="dm-letter-btn" onClick={() => openReply(l)}>
            {expanded
              ? t("deskmate.mailbox.closeReply", { defaultValue: "收起" })
              : t("deskmate.mailbox.reply", { defaultValue: "回信" })}
          </button>
          <button
            type="button"
            className="dm-letter-btn dm-letter-btn-primary"
            onClick={() => onOpenChat(l)}
          >
            {l.thread_promoted
              ? t("deskmate.mailbox.viewChat", { defaultValue: "看对话" })
              : t("deskmate.mailbox.openChat", { defaultValue: "进聊天" })}
          </button>
        </div>

        {expanded && (
          <div className="dm-letter-thread">
            {thread.length === 0 ? (
              <p className="dm-letter-thread-empty">
                {t("deskmate.mailbox.threadStart", {
                  defaultValue: "还没有往来——下面那张纸写几句寄给她。",
                })}
              </p>
            ) : (
              thread.map((m, i) => (
                <div key={i} className={"dm-sheet dm-letter-msg dm-letter-msg-" + m.role}>
                  <div className="dm-letter-msg-from">
                    {m.role === "assistant"
                      ? t("deskmate.mailbox.hiHer", { defaultValue: "小桔：" })
                      : t("deskmate.mailbox.hiMe", { defaultValue: "萧哥 提笔：" })}
                  </div>
                  <div className="dm-letter-msg-text">{m.text}</div>
                  <div className="dm-letter-tail">
                    <span className="dm-letter-by">
                      —— {m.role === "assistant" ? "小桔" : "萧盛"}
                    </span>
                    <time className="dm-letter-msg-date">{fmtDate(new Date(m.ts).toISOString())}</time>
                  </div>
                </div>
              ))
            )}
            {replyErr && <div className="dm-letter-reply-err">{replyErr}</div>}
          </div>
        )}

        {expanded && (
          <div className="dm-letter-compose">
            <div className="dm-compose-hi">
              {t("deskmate.mailbox.hiHer", { defaultValue: "小桔：" })}
            </div>
            <textarea
              className="dm-letter-textarea"
              value={draft}
              placeholder={t("deskmate.mailbox.replyPlaceholder", { defaultValue: "这里写回她的话…" })}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  sendReply();
                }
              }}
              rows={4}
            />
            <div className="dm-compose-bar">
              <span className="dm-compose-hint">
                {t("deskmate.mailbox.composeHint", { defaultValue: "写完直接寄，她这一轮就看见" })}
              </span>
              <div className="dm-stamp" aria-hidden>
                小桔
                <br />
                邮
              </div>
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
  };

  return (
    <div className="dm-mailbox">
      <div className="dm-mailbox-head">
        <button type="button" className="dm-mailbox-back" onClick={onBack} aria-label={t("common.back", { defaultValue: "返回" })}>
          ‹
        </button>
        <h1 className="dm-mailbox-title">{t("deskmate.mailbox.title", { defaultValue: "信箱" })}</h1>
        <span className="dm-mailbox-unread">
          {view
            ? t("deskmate.mailbox.unreadCount", {
                n: view.unread_count,
                defaultValue: `${view.unread_count} 封未读`,
              })
            : ""}
        </span>
      </div>

      {loading && !view ? (
        <div className="dm-mailbox-empty">{t("common.loading", { defaultValue: "加载中…" })}</div>
      ) : items.length === 0 ? (
        <div className="dm-mailbox-empty">
          {t("deskmate.mailbox.allClear", { defaultValue: "信箱是空的，小桔没啥要递给你的。" })}
        </div>
      ) : (
        <>
          <div className="dm-mailbox-list">{live.map(renderLetter)}</div>
          {shelved.length > 0 && (
            <div className="dm-mailbox-shelf">
              <button
                type="button"
                className="dm-mailbox-shelf-toggle"
                onClick={() => setShowShelved((v) => !v)}
                data-testid="toggle-shelved"
              >
                {t("deskmate.mailbox.shelvedGroup", {
                  n: shelved.length,
                  defaultValue: `已搁置 ${shelved.length} 封`,
                })}
                <span className="dm-mailbox-arrow" aria-hidden>
                  {showShelved ? "⌃" : "⌄"}
                </span>
              </button>
              {showShelved && <div className="dm-mailbox-list">{shelved.map(renderLetter)}</div>}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function fmtDate(s: string): string {
  if (!s) return "";
  // 后端 created_at/read_at 是不带时区的本地时间串（`%Y-%m-%dT%H:%M:%S`）——
  // 长度 ≤ 11 视为纯日期。补 Z 只是让它可解析，展示口径本就是服务端本地时间。
  const d = new Date(s.length <= 11 ? `${s}Z` : s);
  if (isNaN(d.getTime())) return s;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
