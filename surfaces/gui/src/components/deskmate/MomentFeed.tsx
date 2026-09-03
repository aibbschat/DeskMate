/**
 * MomentFeed — 小桔日记的微博/朋友圈样式时间线（私密双人）。
 *
 * 数据闭环（后端做实版）：
 * - 小桔日记 → GET /v1/deskmate/diary（真实 DiaryWriter 生成）
 * - 用户日记 / 留言 / 🩷 沉淀 → /v1/deskmate/moments*（MomentsStore 持久化）
 * - 🩷『聊天主动回忆』开关 → POST /moments/{id}/love（body.loved 控制沉淀/静音；A1 默认全开，发日记即自动进回忆，点按=静音 toggle），后端落 SQLite（loved_entries）并导出 diary_memory.md 检索视图（懂我持久化，不再写 MEMORY.md）
 * - 小桔真回复 → POST /moments/post 由 LLM 生成，失败回落规则
 *
 * 展示细节：用户昵称「萧盛」+ 本地头像；发帖可选表情（复用聊天 EmotePicker）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import XiaojuAvatar from "../XiaojuAvatar";
import { EmotePicker } from "./emotes/EmotePicker";
import { emoteLabel } from "./emotes/emoteManifest";
import { getActivePack } from "./emotes/emotePack";
import { renderEmoteInline } from "./emotes/Emote";
import userAvatar from "../../assets/user-avatar.jpg";

const USER_NAME = "萧盛";

type Author = "xiaoju" | "me";

interface MomentComment {
  id: string;
  author: Author;
  text: string;
  at: string;
}

interface Moment {
  id: string;
  author: Author;
  date: string;
  at: string;
  content: string;
  loved: boolean;
  archived: boolean;
  comments: MomentComment[];
}

function uid(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 轻量 markdown 渲染（小桔日记用） */
function renderText(text: string): React.ReactNode {
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let listItems: string[] = [];
  const flushList = () => {
    if (listItems.length) {
      blocks.push(
        <ul key={`ul-${blocks.length}`}>
          {listItems.map((it, i) => (
            <li key={i}>{it}</li>
          ))}
        </ul>,
      );
      listItems = [];
    }
  };
  lines.forEach((line, i) => {
    const t = line.trim();
    if (t.startsWith("# ")) {
      flushList();
      blocks.push(<h3 key={`h-${i}`}>{t.slice(2)}</h3>);
    } else if (t.startsWith("- ")) {
      listItems.push(t.slice(2));
    } else if (t.startsWith("> ")) {
      flushList();
      blocks.push(<blockquote key={`q-${i}`}>{t.slice(2)}</blockquote>);
    } else if (t === "") {
      flushList();
    } else {
      flushList();
      blocks.push(<p key={`p-${i}`}>{t}</p>);
    }
  });
  flushList();
  return <>{blocks}</>;
}

/** 用户日记正文：按行渲染，行内支持表情 token */
function renderUserContent(text: string): React.ReactNode {
  return text.split("\n").map((line, i) => (
    <p key={i} style={{ margin: "4px 0" }}>
      {line ? renderEmoteInline(line, 20) : "\u00A0"}
    </p>
  ));
}

/** 把后端小桔日记 markdown 拆成「当日大标题 + 各篇正文」（一篇占一张卡）。 */
function splitDiaryEntries(text: string): { dayTitle: string; entries: { time: string; body: string }[] } {
  const lines = text.split("\n");
  let dayTitle = "";
  const entries: { time: string; body: string }[] = [];
  let curTime: string | null = null;
  let curLines: string[] = [];
  const push = () => {
    if (curTime !== null || curLines.length > 0) {
      entries.push({ time: curTime ?? "", body: curLines.join("\n").trim() });
    }
    curTime = null;
    curLines = [];
  };
  for (const line of lines) {
    if (line.startsWith("# ")) {
      dayTitle = line.slice(2).trim();
    } else if (line.startsWith("## ")) {
      push();
      curTime = line.slice(3).trim();
    } else {
      curLines.push(line);
    }
  }
  push();
  return { dayTitle, entries };
}

/** 中文日期展示 */
function dateLabel(date: string): string {
  try {
    const d = new Date(`${date}T12:00:00`);
    const week = "日一二三四五六"[d.getDay()];
    return `${d.getMonth() + 1}月${d.getDate()}日 周${week}`;
  } catch {
    return date;
  }
}

export function MomentFeed() {
  const [moments, setMoments] = useState<Moment[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [emoteOpen, setEmoteOpen] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [notice, setNotice] = useState("");
  const [commentDrafts, setCommentDrafts] = useState<Record<string, string>>({});
  const [openComments, setOpenComments] = useState<Record<string, boolean>>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const flashNotice = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 2400);
  };

  /** 拉取小桔真实日记（独立接口），用 commentsMap 给每条挂留言 */
  const fetchXjMoments = useCallback(
    async (commentsMap: Record<string, MomentComment[]>): Promise<Moment[]> => {
      const xj: Moment[] = [];
      try {
        const r = await authedFetch(`${httpBase()}/v1/deskmate/diary`);
        if (r.ok) {
          const d = await r.json();
          const dates: string[] = Array.isArray(d.dates) ? d.dates : [];
          for (const date of dates.slice(-14).reverse()) {
            try {
              const rr = await authedFetch(`${httpBase()}/v1/deskmate/diary/${date}`);
              if (rr.ok) {
                const dd = await rr.json();
                if (dd.content) {
                  // 一天连着记几篇 → 每篇独立成一张卡（微博式），各自可🩷/留言/归档
                  const { entries } = splitDiaryEntries(dd.content);
                  entries.forEach((entry, idx) => {
                    if (!entry.body.trim()) return;
                    xj.push({
                      id: `xj-${date}#${idx}`,
                      author: "xiaoju",
                      date,
                      at: `${date}T${entry.time || "12:00"}:00`,
                      content: entry.body,
                      loved: false,
                      archived: false,
                      comments: commentsMap[`xj-${date}#${idx}`] || commentsMap[`xj-${date}`] || [],
                    });
                  });
                }
              }
            } catch {
              /* 单篇失败跳过 */
            }
          }
        }
      } catch {
        /* 静默 */
      }
      return xj;
    },
    [],
  );

  /** 载入第一页 moments（后端 cursor 分页：/v1/deskmate/moments?limit=50） */
  const loadAll = useCallback(async () => {
    let meItems: {
      id: string;
      content: string;
      createdAt: string;
      loved: boolean;
      archived?: boolean;
      comments?: MomentComment[];
    }[] = [];
    let comments: Record<string, MomentComment[]> = {};
    let next: string | null = null;
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments?limit=50`);
      if (r.ok) {
        const d = await r.json();
        meItems = Array.isArray(d.items) ? d.items : [];
        comments = d.comments || {};
        next = d.next_cursor ?? null;
      }
    } catch {
      /* 静默 */
    }
    const xj = await fetchXjMoments(comments);
      const me: Moment[] = meItems.map((p) => ({
        id: p.id,
        author: "me",
        date: (p.createdAt || "").slice(0, 10),
        at: p.createdAt,
        content: p.content,
        loved: !!p.loved,
        archived: !!p.archived,
        comments: p.comments || comments[p.id] || [],
      }));
    setMoments([...xj, ...me]);
    setCursor(next);
    setHasMore(next !== null);
    setLoaded(true);
  }, [fetchXjMoments]);

  /** 加载下一页（cursor 翻页，追加更旧的日记） */
  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore || !cursor) return;
    setLoadingMore(true);
    try {
      const r = await authedFetch(
        `${httpBase()}/v1/deskmate/moments?limit=50&cursor=${encodeURIComponent(cursor)}`,
      );
      if (r.ok) {
        const d = await r.json();
        const items: {
          id: string;
          content: string;
          createdAt: string;
          loved: boolean;
          archived?: boolean;
          comments?: MomentComment[];
        }[] = Array.isArray(d.items) ? d.items : [];
        const more: Moment[] = items.map((p) => ({
          id: p.id,
          author: "me",
          date: (p.createdAt || "").slice(0, 10),
          at: p.createdAt,
          content: p.content,
          loved: !!p.loved,
          archived: !!p.archived,
          comments: p.comments || [],
        }));
        setMoments((prev) => [...prev, ...more]);
        setCursor(d.next_cursor ?? null);
        setHasMore(d.next_cursor != null);
      } else {
        setHasMore(false);
      }
    } catch {
      setHasMore(false);
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, hasMore, loadingMore]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const sorted = useMemo(
    () => [...moments].sort((a, b) => (a.at < b.at ? 1 : -1)),
    [moments],
  );

  /** 时间线只显示未归档；已归档的收进底部「已归档」区（可取消归档恢复）。 */
  const activeMoments = useMemo(
    () => sorted.filter((m) => !m.archived),
    [sorted],
  );
  const archivedMoments = useMemo(
    () => sorted.filter((m) => m.archived),
    [sorted],
  );

  /** 用户发日记；后端返回 post + 小桔真回复（LLM） */
  const publish = async () => {
    const text = draft.trim();
    if (!text || publishing) return;
    setPublishing(true);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments/post`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      });
      if (r.ok) {
        const d = await r.json();
        const post = d.post;
        // 信任后端落库的评论：小桔回复已作为 comments[0] 持久化（author=xiaoju），
        // 重载日记页从 DB 读回不丢。仅当后端未返回时本地兜底（防旧包/异常）。
        const persistedReply: MomentComment =
          post.comments && post.comments[0]
            ? {
                id: post.comments[0].id,
                author: post.comments[0].author,
                text: post.comments[0].text,
                at: post.comments[0].createdAt,
              }
            : {
                id: uid(),
                author: "xiaoju",
                text: d.reply || "我记下了。",
                at: new Date().toISOString(),
              };
        const me: Moment = {
          id: post.id,
          author: "me",
          date: (post.createdAt || "").slice(0, 10),
          at: post.createdAt,
          content: post.content,
          loved: !!post.loved,
          archived: false,
          comments: [persistedReply],
        };
        setMoments((prev) => [me, ...prev]);
        setDraft("");
        setComposerOpen(false);
        flashNotice("小桔回复了你");
      } else {
        flashNotice("发布失败，请稍后再试");
      }
    } catch {
      flashNotice("发布失败，请稍后再试");
    } finally {
      setPublishing(false);
    }
  };

  /** 留言（互动层） */
  const addComment = async (id: string) => {
    const text = (commentDrafts[id] || "").trim();
    if (!text) return;
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments/comment`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ moment_id: id, author: "me", text }),
      });
      if (r.ok) {
        const d = await r.json();
        const c: MomentComment = {
          id: d.comment.id,
          author: "me",
          text: d.comment.text,
          at: d.comment.createdAt,
        };
        // 双向：用户留言后，后端可能返回小桔的自动回复（author=xiaoju），一并追加
        const extra: MomentComment[] = [];
        if (d.reply) {
          extra.push({
            id: d.reply.id,
            author: d.reply.author,
            text: d.reply.text,
            at: d.reply.createdAt,
          });
        }
        setMoments((prev) =>
          prev.map((m) =>
            m.id === id ? { ...m, comments: [...m.comments, c, ...extra] } : m,
          ),
        );
        setCommentDrafts((p) => ({ ...p, [id]: "" }));
      }
    } catch {
      /* 静默 */
    }
  };

  /** 🩷『聊天主动回忆』开关（A1：默认全开，点按 = 静音/恢复 toggle） */
  const toggleLove = async (id: string, content: string, currentlyLoved: boolean) => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments/${id}/love`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content, loved: !currentlyLoved }),
      });
      if (r.ok) {
        const d = await r.json();
        setMoments((prev) => prev.map((m) => (m.id === id ? { ...m, loved: !!d.loved } : m)));
        flashNotice(d.message || (d.loved ? "已记进记忆" : "已取消主动回忆"));
      }
    } catch {
      flashNotice("操作失败，请稍后再试");
    }
  };

  /** 每条日记独立归档 / 取消归档（仅影响时间线展示，不影响已🩷记忆检索） */
  const toggleArchive = async (id: string, archived: boolean) => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments/${id}/archive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived }),
      });
      if (r.ok) {
        setMoments((prev) => prev.map((m) => (m.id === id ? { ...m, archived } : m)));
      } else {
        flashNotice("归档失败，请稍后再试");
      }
    } catch {
      flashNotice("归档失败，请稍后再试");
    }
  };

  const toggleComments = (id: string) =>
    setOpenComments((p) => ({ ...p, [id]: !p[id] }));

  /** 插入表情 token（与聊天 Composer 一致：emote:PACK:NAME，渲染冻结） */
  const insertEmote = (name: string) => {
    const label = emoteLabel(name);
    const token = `![${label}](emote:${getActivePack()}:${name})`;
    setDraft((d) => (d.trim() ? `${d.trimEnd()} ${token}` : token));
    setEmoteOpen(false);
  };

  return (
    <div className="mom-feed">
      <div className="mom-feed-header">
        <div className="mom-feed-title">
          <XiaojuAvatar size={22} />
          <span>我们的日记</span>
          <span className="mom-feed-sub">只有我们看得见</span>
        </div>
        <button
          type="button"
          className="mom-compose-btn"
          onClick={() => setComposerOpen((v) => !v)}
        >
          ✎ 我也写一条
        </button>
      </div>

      {notice && <div className="mom-notice">{notice}</div>}

      {composerOpen && (
        <div className="mom-composer">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="写下今天发生的事，或想对小桔说的话…（可插表情）"
            rows={3}
          />
          {emoteOpen && (
            <div className="mom-emote-picker">
              <EmotePicker onPick={insertEmote} />
            </div>
          )}
          <div className="mom-composer-actions">
            <button type="button" onClick={() => setEmoteOpen((v) => !v)} aria-label="表情">
              <span style={{ fontSize: 15 }}>🙂</span>
            </button>
            <span className="mom-composer-spacer" />
            <button type="button" onClick={() => setComposerOpen(false)}>
              取消
            </button>
            <button
              type="button"
              className="mom-primary"
              onClick={publish}
              disabled={!draft.trim() || publishing}
            >
              {publishing ? "发布中…" : "发布"}
            </button>
          </div>
        </div>
      )}

      {!loaded ? (
        <p className="mom-loading">翻开我们的日记…</p>
      ) : sorted.length === 0 ? (
        <div className="mom-empty">
          <p>这里还没有日记。</p>
          <p className="mom-empty-hint">写一条，或等小桔记下今天——回忆就从这里开始。</p>
        </div>
      ) : (
        <>
          <div className="mom-timeline">
            {activeMoments.map((m) => (
              <MomentCard
                key={m.id}
                moment={m}
                openComments={!!openComments[m.id]}
                commentDraft={commentDrafts[m.id] || ""}
                onToggleComments={() => toggleComments(m.id)}
                onCommentDraft={(v) => setCommentDrafts((p) => ({ ...p, [m.id]: v }))}
                onAddComment={() => addComment(m.id)}
                onLove={() => toggleLove(m.id, m.content, m.loved)}
                onArchive={() => toggleArchive(m.id, !m.archived)}
              />
            ))}
          </div>
          {hasMore && (
            <div className="mom-loadmore">
              <button type="button" onClick={loadMore} disabled={loadingMore}>
                {loadingMore ? "加载中…" : "加载更多日记"}
              </button>
            </div>
          )}
          {archivedMoments.length > 0 && (
            <div className="mom-archived">
              <button
                type="button"
                className="mom-archived-toggle"
                onClick={() => setShowArchived((v) => !v)}
              >
                📦 已归档（{archivedMoments.length}）{showArchived ? "▾" : "▸"}
              </button>
              {showArchived && (
                <div className="mom-timeline">
                  {archivedMoments.map((m) => (
                    <MomentCard
                      key={m.id}
                      moment={m}
                      openComments={!!openComments[m.id]}
                      commentDraft={commentDrafts[m.id] || ""}
                      onToggleComments={() => toggleComments(m.id)}
                      onCommentDraft={(v) => setCommentDrafts((p) => ({ ...p, [m.id]: v }))}
                      onAddComment={() => addComment(m.id)}
                      onLove={() => toggleLove(m.id, m.content, m.loved)}
                      onArchive={() => toggleArchive(m.id, !m.archived)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function MomentCard({
  moment,
  openComments,
  commentDraft,
  onToggleComments,
  onCommentDraft,
  onAddComment,
  onLove,
  onArchive,
}: {
  moment: Moment;
  openComments: boolean;
  commentDraft: string;
  onToggleComments: () => void;
  onCommentDraft: (v: string) => void;
  onAddComment: () => void;
  onLove: () => void;
  onArchive: () => void;
}) {
  const isXj = moment.author === "xiaoju";
  // 每篇独立成卡：时间取自该篇的 at（HH:MM），与当日日期拼成卡片头
  const xjTime = isXj ? moment.at.slice(11, 16) : "";

  return (
    <div className={`mom-card ${isXj ? "mom-xj" : "mom-me"}`}>
      <div className="mom-card-head">
        {isXj ? (
          <XiaojuAvatar size={36} />
        ) : (
          <img src={userAvatar} alt={USER_NAME} className="mom-me-avatar" />
        )}
        <div className="mom-card-meta">
          <span className="mom-author">{isXj ? "小桔" : USER_NAME}</span>
          <span className="mom-date">
            {isXj ? `${dateLabel(moment.date)} ${xjTime}`.trim() : dateLabel(moment.date)}
          </span>
        </div>
      </div>

      <div className="mom-card-body">
        {isXj ? renderText(moment.content) : renderUserContent(moment.content)}
      </div>

      <div className="mom-actions">
        <button
          type="button"
          className={`mom-love ${moment.loved ? "loved" : ""}`}
          onClick={onLove}
          title={moment.loved ? "点按：取消这条在聊天的主动回忆" : "点按：恢复这条在聊天的主动回忆"}
        >
          <span className="mom-love-heart">{moment.loved ? "🩷" : "🤍"}</span>
          <span>{moment.loved ? "可回忆" : "已静音"}</span>
        </button>
        <button type="button" className="mom-comment" onClick={onToggleComments}>
          <span>💬</span>
          <span>{moment.comments.length ? `${moment.comments.length} 条留言` : "留言"}</span>
        </button>
        {!isXj && (
          <button
            type="button"
            className={`mom-archive ${moment.archived ? "archived" : ""}`}
            onClick={onArchive}
            title={moment.archived ? "取消归档" : "归档这条"}
          >
            <span>{moment.archived ? "📂" : "📦"}</span>
            <span>{moment.archived ? "取消归档" : "归档"}</span>
          </button>
        )}
      </div>

      {openComments && (
        <div className="mom-comments">
          {moment.comments.map((c) => (
            <div key={c.id} className={`mom-comment-item ${c.author === "xiaoju" ? "xj" : "me"}`}>
              {c.author === "xiaoju" ? (
                <XiaojuAvatar size={20} />
              ) : (
                <img src={userAvatar} alt={USER_NAME} className="mom-me-avatar sm" />
              )}
              <div className="mom-comment-bubble">
                <span className="mom-comment-author">{c.author === "xiaoju" ? "小桔" : USER_NAME}</span>
                <span>{renderEmoteInline(c.text, 16)}</span>
              </div>
            </div>
          ))}
          <div className="mom-comment-input">
            <input
              value={commentDraft}
              onChange={(e) => onCommentDraft(e.target.value)}
              placeholder={isXj ? `给小桔留言…` : "回复…"}
              onKeyDown={(e) => {
                if (e.key === "Enter") onAddComment();
              }}
            />
            <button type="button" onClick={onAddComment} disabled={!commentDraft.trim()}>
              发送
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
