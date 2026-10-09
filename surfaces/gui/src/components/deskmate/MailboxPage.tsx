/**
 * MailboxPage — 信箱（送达层门面，2026-10-09 第二轮：三栏收件箱）。
 *
 * 萧哥 10-09 真机判词：上一版把所有信一次摊开，「像一篇篇日记，不像信箱」，
 * 而且 68 封拉不动。这版按邮箱的礼仪重排：
 *
 *   左目录（未读 / 已读 / 归档，带计数，**默认哪栏都不展开**）
 *   → 中标题列（信没有主题行，标题取**正文首句截断**，不另造字段）
 *   → 右信纸（案 A 的一摞纸：抬头 + 正文 + 署名落款；往来各一张；回信是一张空白稿纸）
 *
 * 信箱只持信件元数据，所有对话走同一个聊天引擎、同一个会话消息库（设计单 §5）。
 * 处置过的信不再蒸发：稍后/不用了之后它**移到归档**，界面上看得见去了哪。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { renderEmoteInline } from "./emotes/Emote";
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
} from "../../api";

export type MailboxCat = "unread" | "read" | "archived";

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
  writing_task_assistance: "写作上",
  information_support: "资料佐证",
  valuable_information_provision: "资料佐证",
  plot_contradiction: "书稿前后对不上",
  plot_repetition: "书稿重复",
  propose: "提议",
};
const KIND_FALLBACK = "她的话";
// 归档只有一栏（萧哥拍板），但行上要标出它为什么在这儿。
const SHELF_LABEL: Record<string, string> = {
  snoozed: "稍后 · 到期自己回来",
  dismissed: "不用了",
  handled: "已办",
};

export function letterLabel(l: MailboxLetter): string {
  const s = SOURCE_LABEL[l.source] ?? "";
  const k = SOURCE_LABEL[l.kind] ?? "";
  if (s && k) return s === k ? s : `${s} · ${k}`;
  return s || k || KIND_FALLBACK;
}
export function shortHandle(id: string): string {
  return id.split(":").pop()?.slice(0, 6) ?? "";
}

/** 标题 = 正文首句截断。信本来没有主题行，不为此加字段、也不让她现编。
 *  换行也算句末（她写长信时第一段就是标题该停的地方）。 */
export function letterTitle(l: MailboxLetter): string {
  const text = (l.content || "").trim();
  if (!text) return "（这封没写字）";
  const first = text.split(/[\n。！？；!?;]/)[0].trim() || text.replace(/\s+/g, " ");
  return first.length > 42 ? `${first.slice(0, 42)}…` : first;
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
  const [cat, setCat] = useState<MailboxCat | null>(null); // 默认全折叠
  const [inbox, setInbox] = useState<MailboxLetter[]>([]); // 未读 + 已读（一次拉全）
  const [archived, setArchived] = useState<MailboxLetter[]>([]);
  const [counts, setCounts] = useState({ unread: 0, read: 0, archived: 0 });
  const [openId, setOpenId] = useState<string | null>(null);
  const [thread, setThread] = useState<ThreadMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [replyErr, setReplyErr] = useState<string | null>(null);
  const [replying, setReplying] = useState(false); // 是否已摊开稿纸
  const sessionRef = useRef<Session | null>(null);

  const load = useCallback(() => {
    // 两趟并排：收件箱（含已读）+ 归档。counts 用收件箱那趟返的（同一次查询口径）。
    Promise.all([getMailbox("all"), getMailbox("archived")])
      .then(([a, b]) => {
        setInbox(a.ok ? a.items : []);
        setArchived(b.ok ? b.items : []);
        setCounts(a.ok && a.counts ? a.counts : { unread: a.unread_count, read: 0, archived: b.items.length });
      })
      .catch(() => {
        setInbox([]);
        setArchived([]);
      });
  }, []);

  useEffect(() => {
    load();
    return () => {
      // 卸载时关掉可能存在的 mailbox WS，避免幽灵 socket（2026-10-05 教训）。
      sessionRef.current?.close();
      sessionRef.current = null;
    };
  }, [load]);

  const rows = useMemo<MailboxLetter[]>(() => {
    if (cat === "archived") return archived;
    if (cat === "unread") return inbox.filter((l) => !l.read);
    if (cat === "read") return inbox.filter((l) => l.read);
    return [];
  }, [cat, inbox, archived]);

  const letter = useMemo(
    () => [...inbox, ...archived].find((l) => l.id === openId) ?? null,
    [inbox, archived, openId],
  );

  const CATS: { key: MailboxCat; label: string; n: number }[] = [
    { key: "unread", label: t("deskmate.mailbox.catUnread", { defaultValue: "未读" }), n: counts.unread },
    { key: "read", label: t("deskmate.mailbox.catRead", { defaultValue: "已读" }), n: counts.read },
    { key: "archived", label: t("deskmate.mailbox.catArchived", { defaultValue: "归档" }), n: counts.archived },
  ];

  const closeReply = () => {
    sessionRef.current?.close();
    sessionRef.current = null;
    setReplying(false);
    setThread([]);
    setDraft("");
    setReplyErr(null);
    setSending(false);
  };

  // 拉这封信已有的往来（同一会话消息库）。历史 ts 是**秒**，统一乘 1000 成毫秒。
  const loadThread = async (sessionId: string) => {
    try {
      const hist = (await getSessionMessages(sessionId)) as ConversationMessage[];
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
      /* 历史拉取失败不阻断看信 */
    }
  };

  // 点标题 = 摊开这封。从「未读」里点开即视为已读（设计单 §3.1「标已读/已展开」同一语义）。
  const openLetter = async (l: MailboxLetter) => {
    if (openId === l.id) return;
    closeReply();
    setOpenId(l.id);
    // 已经有往来的信，**打开就把往来一起摊开**，不必非要先点「回信」。
    // （萧哥 10-09 实测：回过信的信落进「已读」后再点开，回复内容不见了。）
    if (l.mailbox_session_id) void loadThread(l.mailbox_session_id);
    if (!l.read) {
      await readMailboxLetter(l.id);
      setInbox((xs) => xs.map((x) => (x.id === l.id ? { ...x, read: true, read_at: x.read_at || now() } : x)));
      setCounts((c) => ({ ...c, unread: Math.max(0, c.unread - 1), read: c.read + 1 }));
      announceMailboxChanged();
    }
  };

  // 回信：确保隐藏往来会话存在（同一引擎），拉历史，连 WS。
  const startReply = async (l: MailboxLetter) => {
    if (replying && openId === l.id) {
      closeReply();
      return;
    }
    sessionRef.current?.close();
    sessionRef.current = null;
    setReplying(true);
    setDraft("");
    setReplyErr(null);
    setSending(false);
    const { ok, session_id } = await replyMailboxLetter(l.id);
    if (!ok || !session_id) {
      setReplyErr(t("deskmate.mailbox.replyFailed", { defaultValue: "回信会话创建失败" }));
      return;
    }
    // 打开信时若已拉过同一会话的往来，就不重复拉（免得已显示的纸闪一下）。
    if (thread.length === 0) await loadThread(session_id);
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
    setInbox((xs) => xs.map((x) => (x.id === l.id ? { ...x, read: true, read_at: x.read_at || now() } : x)));
    setCounts((c) => ({ ...c, unread: Math.max(0, c.unread - (l.read ? 0 : 1)), read: c.read + (l.read ? 0 : 1) }));
    announceMailboxChanged();
  };
  // 稍后 / 不用了：重拉两栏，让它**当着面从收件箱移到归档**（不再一按就蒸发）。
  const dispose = async (
    l: MailboxLetter,
    fn: (id: string) => Promise<boolean>,
  ) => {
    closeReply();
    setOpenId(null);
    await fn(l.id);
    load();
    announceMailboxChanged();
  };

  return (
    <div className={"dm-mailbox" + (letter ? " mb-open" : "")}>
      <nav className="dm-mb-rail">
        <button type="button" className="dm-mb-backtop" onClick={onBack} aria-label={t("common.back", { defaultValue: "返回" })}>
          ‹ 返回
        </button>
        <div className="dm-mb-rail-title">
          {t("deskmate.mailbox.title", { defaultValue: "信箱" })}
          <span>{counts.unread + counts.read + counts.archived} 封</span>
        </div>
        {CATS.map((c) => (
          <button
            key={c.key}
            type="button"
            data-testid={`mb-cat-${c.key}`}
            className={"dm-mb-cat" + (cat === c.key ? " on" : "")}
            onClick={() => setCat(cat === c.key ? null : c.key)}
          >
            <span className="dm-mb-cat-caret" aria-hidden>
              {cat === c.key ? "▾" : "▸"}
            </span>
            <span className="dm-mb-cat-t">{c.label}</span>
            <span className="dm-mb-cat-n">{c.n}</span>
          </button>
        ))}
      </nav>

      <section className="dm-mb-list">
        {!cat ? (
          <p className="dm-mb-blank">
            {t("deskmate.mailbox.pickCat", { defaultValue: "左边挑一栏。" })}
            <span>{t("deskmate.mailbox.pickCatSub", { defaultValue: "默认什么都不展开——打开信箱不再是一桌子纸。" })}</span>
          </p>
        ) : rows.length === 0 ? (
          <p className="dm-mb-blank">{t("deskmate.mailbox.emptyCat", { defaultValue: "这一栏是空的。" })}</p>
        ) : (
          <>
            <div className="dm-mb-list-hd">
              {CATS.find((c) => c.key === cat)?.label} · {rows.length}
            </div>
            {rows.map((l) => (
              <button
                key={l.id}
                type="button"
                data-testid="mb-row"
                className={"dm-mb-row" + (l.id === openId ? " sel" : "")}
                onClick={() => openLetter(l)}
              >
                <span className={"dm-mb-dot" + (l.read ? " read" : "")} aria-hidden />
                <span className="dm-mb-row-bd">
                  <span className="dm-mb-row-from">
                    小桔 · {letterLabel(l)}
                    <time>{fmtShort(l.created_at)}</time>
                  </span>
                  <span className="dm-mb-row-t serif">{letterTitle(l)}</span>
                  {l.shelf_reason ? (
                    <span className="dm-mb-row-chip">{SHELF_LABEL[l.shelf_reason] ?? l.shelf_reason}</span>
                  ) : null}
                </span>
              </button>
            ))}
          </>
        )}
      </section>

      <section className="dm-mb-read">
        {!letter ? (
          <p className="dm-mb-blank tall">
            {t("deskmate.mailbox.pickOne", { defaultValue: "挑一封，信在这里摊开。" })}
          </p>
        ) : (
          <>
            <button type="button" className="dm-mb-back" onClick={() => { closeReply(); setOpenId(null); }}>
              ‹ {t("deskmate.mailbox.backToList", { defaultValue: "回列表" })}
            </button>
            <article className={"dm-sheet" + (letter.read ? " dim" : "")}>
              <header className="dm-letter-head">
                <span className="dm-letter-sig">小桔：</span>
                <span className="dm-letter-tag">{letterLabel(letter)}</span>
                <span className="dm-letter-id">#{shortHandle(letter.id)}</span>
              </header>
              <div className="dm-letter-body">{renderEmoteInline(letter.content)}</div>
              {letter.understanding ? (
                <p className="dm-letter-understanding">她的理解：{renderEmoteInline(letter.understanding)}</p>
              ) : null}
              {letter.evidence ? (
                <p className="dm-letter-evidence">依据：{letter.evidence}</p>
              ) : null}
              <div className="dm-letter-tail">
                <span className="dm-letter-by">—— 小桔</span>
                <time className="dm-letter-date">{fmtDate(letter.created_at)}</time>
              </div>
            </article>

            <div className="dm-letter-actions">
              {!letter.read && (
                <button type="button" className="dm-letter-btn" onClick={() => doRead(letter)}>
                  {t("deskmate.mailbox.markRead", { defaultValue: "标已读" })}
                </button>
              )}
              <button type="button" className="dm-letter-btn" onClick={() => dispose(letter, snoozeMailboxLetter)}>
                {t("deskmate.mailbox.later", { defaultValue: "稍后" })}
              </button>
              <button type="button" className="dm-letter-btn" onClick={() => dispose(letter, dismissMailboxLetter)}>
                {t("deskmate.mailbox.dismiss", { defaultValue: "不用了" })}
              </button>
              <button type="button" className="dm-letter-btn" onClick={() => startReply(letter)}>
                {replying
                  ? t("deskmate.mailbox.closeReply", { defaultValue: "收起" })
                  : t("deskmate.mailbox.reply", { defaultValue: "回信" })}
              </button>
              {/* 值得聊的才「进聊天」：把这封的隐藏往来提升成普通会话，气泡样式续。 */}
              <button
                type="button"
                className="dm-letter-btn dm-letter-btn-primary"
                onClick={() => onOpenChat(letter)}
              >
                {letter.thread_promoted
                  ? t("deskmate.mailbox.viewChat", { defaultValue: "看对话" })
                  : t("deskmate.mailbox.openChat", { defaultValue: "进聊天" })}
              </button>
            </div>

            {thread.map((m, i) => (
              <div key={i} className={"dm-sheet dm-letter-msg dm-letter-msg-" + m.role}>
                <div className="dm-letter-msg-from">
                  {m.role === "assistant"
                    ? t("deskmate.mailbox.hiHer", { defaultValue: "小桔：" })
                    : t("deskmate.mailbox.hiMe", { defaultValue: "萧哥 提笔：" })}
                </div>
                <div className="dm-letter-msg-text">{renderEmoteInline(m.text)}</div>
                <div className="dm-letter-tail">
                  <span className="dm-letter-by">—— {m.role === "assistant" ? "小桔" : "萧盛"}</span>
                  <time className="dm-letter-msg-date">{fmtDate(new Date(m.ts).toISOString())}</time>
                </div>
              </div>
            ))}
            {replyErr && <div className="dm-letter-reply-err">{replyErr}</div>}

            {replying && (
              <div className="dm-letter-compose">
                <div className="dm-compose-hi">{t("deskmate.mailbox.hiHer", { defaultValue: "小桔：" })}</div>
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
          </>
        )}
      </section>
    </div>
  );
}

function now(): string {
  return new Date().toISOString();
}

function fmtDate(s: string): string {
  if (!s) return "";
  // 后端 created_at/read_at 是不带时区的本地时间串（`%Y-%m-%dT%H:%M:%S`）。
  const d = new Date(s.length <= 11 ? `${s}Z` : s);
  if (isNaN(d.getTime())) return s;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 列表行的短时标：今天只显示时分，昨天标「昨天」，更早回退到日期。 */
function fmtShort(s: string): string {
  if (!s) return "";
  const d = new Date(s.length <= 11 ? `${s}Z` : s);
  if (isNaN(d.getTime())) return s;
  const p = (n: number) => String(n).padStart(2, "0");
  const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  if (sameDay(d, new Date())) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  const yest = new Date(Date.now() - 86400_000);
  if (sameDay(d, yest)) return "昨天";
  return `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
