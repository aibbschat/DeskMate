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
import { splitDiaryEntries } from "../../diaryEntries";

const USER_NAME = "萧盛";

/** 时间线每页条数（客户端分页；后端 cursor 仅在超过首屏时按需续拉更旧的） */
const PAGE_SIZE = 12;

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
  /** 小桔日记卡在当天文件里的顺序（1=最早），用于同分钟多则的稳定排序 */
  seq?: number;
  comments: MomentComment[];
  /** 发帖后小桔真回复（后台 LLM 异步落库）尚未轮询到时的占位态 */
  pendingReply?: boolean;
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

/** 用户日记正文：按行渲染，保留换行分出的段落，行内支持表情 token。
 *  空行只是段间距，做成矮间隔，不撑出一个带行高的空段落。 */
function renderUserContent(text: string): React.ReactNode {
  return text.split("\n").map((line, i) =>
    line.trim() === "" ? (
      <div key={i} style={{ height: 6 }} aria-hidden="true" />
    ) : (
      <p key={i} style={{ margin: "4px 0" }}>
        {renderEmoteInline(line, 20)}
      </p>
    ),
  );
}

/**
 * 把后端小桔日记 markdown 拆成「当日大标题 + 各篇正文」（一篇占一张卡）。
 * 每则标题行嵌了稳定 eid（<!--xj:...-->）——它就是这则的"文章 ID"，
 * 跟着这则走，不随位置变。留言/🩷 都按这个 eid 索引，删中间一篇后
 * 后面补位也不会把留言串到别人头上。
 *
 * 实现已提到 `src/diaryEntries.ts`（2026-10-05 手机端 M3 复用）——两端必须共用一份，
 * 否则同一条日记在桌面分成 3 篇、手机分成 2 篇，按 entry id 挂的留言/🩷 会串。
 */

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

/** 光轨上的日期点：月.日够定位，星期交给条目头那行 */
function dayTag(date: string): string {
  try {
    const d = new Date(`${date}T12:00:00`);
    return `${d.getMonth() + 1}.${String(d.getDate()).padStart(2, "0")}`;
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
  const [page, setPage] = useState(1);
  const [showArchived, setShowArchived] = useState(false);

  const flashNotice = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(""), 2400);
  };

  /** 拉取小桔真实日记（独立接口），用 commentsMap 挂留言、lovedSet 回显🩷 */
  const fetchXjMoments = useCallback(
    async (
      commentsMap: Record<string, MomentComment[]>,
      lovedSet: Set<string>,
    ): Promise<Moment[]> => {
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
                    // 优先用稳定 eid 当 id；老文件没 eid 时退回位置序号（兼容存量留言）
                    const id = entry.eid ? `xj-${entry.eid}` : `xj-${date}#${idx}`;
                    xj.push({
                      id,
                      author: "xiaoju",
                      date,
                      at: `${date}T${entry.time || "12:00"}:00`,
                      content: entry.body,
                      // 🩷 按 id 从后端回显（之前写死 false 导致刷新即丢）
                      loved: lovedSet.has(id),
                      archived: false,
                      seq: entry.seq,
                      comments: commentsMap[id] || commentsMap[`xj-${date}`] || [],
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
    let loved: string[] = [];
    let next: string | null = null;
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/moments?limit=50`);
      if (r.ok) {
        const d = await r.json();
        meItems = Array.isArray(d.items) ? d.items : [];
        comments = d.comments || {};
        loved = Array.isArray(d.loved) ? d.loved : [];
        next = d.next_cursor ?? null;
      }
    } catch {
      /* 静默 */
    }
    const lovedSet = new Set<string>(loved);
    const xj = await fetchXjMoments(comments, lovedSet);
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
    setPage(1);
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
    () => [...moments].sort((a, b) => {
      // 时间降序（新在上）；同分钟的多则靠 seq 稳定排序（同一篇文件里靠后=较新）
      if (a.at !== b.at) return a.at < b.at ? 1 : -1;
      return (b.seq ?? 0) - (a.seq ?? 0);
    }),
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

  /** 客户端分页：把已加载的时间线切成页（后端 cursor 仅在超过首屏时按需续拉更旧） */
  const totalPages = Math.max(1, Math.ceil(activeMoments.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageItems = activeMoments.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  /** 按天分组：光轨上的日期点跟着内容走，不靠绝对定位去对齐 */
  const dayGroups = useMemo(() => {
    const out: { date: string; items: Moment[] }[] = [];
    for (const m of pageItems) {
      const last = out[out.length - 1];
      if (last && last.date === m.date) last.items.push(m);
      else out.push({ date: m.date, items: [m] });
    }
    return out;
  }, [pageItems]);

  const remembered = useMemo(
    () => activeMoments.filter((m) => m.loved).length,
    [activeMoments],
  );

  /** 卡片属性：时间线和已归档区共用一份，免得两处漏传 */
  const cardProps = (m: Moment) => ({
    moment: m,
    openComments: !!openComments[m.id],
    commentDraft: commentDrafts[m.id] || "",
    onToggleComments: () => toggleComments(m.id),
    onCommentDraft: (v: string) => setCommentDrafts((p) => ({ ...p, [m.id]: v })),
    onAddComment: () => addComment(m.id),
    onLove: () => toggleLove(m.id, m.content, m.loved),
    onArchive: () => toggleArchive(m.id, !m.archived),
  });

  /** 发帖后轮询小桔真回复：后台 LLM 异步把回复作为 xiaoju 留言落库，轮询到即替换显示。
   *  绝不让兜底模板冒充小桔回复——pending 期间显示「正在想…」，真留言到达即替换。 */
  const pollXiaojuReply = useCallback((id: string) => {
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const r = await authedFetch(`${httpBase()}/v1/deskmate/moments/${id}`);
        if (r.ok) {
          const d = await r.json();
          const cs: any[] = Array.isArray(d.comments) ? d.comments : [];
          const hasXj = cs.some((c) => c.author === "xiaoju");
          if (hasXj) {
            const mapped: MomentComment[] = cs.map((c: any) => ({
              id: c.id,
              author: c.author === "xiaoju" ? "xiaoju" : "me",
              text: c.text,
              at: c.createdAt,
            }));
            setMoments((prev) =>
              prev.map((m) =>
                m.id === id ? { ...m, comments: mapped, pendingReply: false } : m,
              ),
            );
            return;
          }
        }
      } catch {
        /* 静默重试 */
      }
      if (tries < 12) {
        window.setTimeout(tick, 2500);
      } else {
        // 超时仍未拿到（极端 LLM 全挂）：解除 pending，下次刷新时间线会补上
        setMoments((prev) =>
          prev.map((m) => (m.id === id ? { ...m, pendingReply: false } : m)),
        );
      }
    };
    window.setTimeout(tick, 2000);
  }, []);

  /** 用户发日记；后端返回 post（pending_reply），小桔真回复后台异步落库 */
  const publish = async () => {
    const text = draft.trim();
    if (!text || publishing) return;
    setPublishing(true);
    try {
      const r = await authedFetch(
        `${httpBase()}/v1/deskmate/moments/post`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: text }),
        },
        // 后端现已即时返回；此处 30s 兜底仅防极端情况，避免『发送中』永久卡死
        { timeoutMs: 30000 },
      );
      if (r.ok) {
        const d = await r.json();
        const post = d.post;
        // 小桔真回复由后台 LLM 异步生成、作为 xiaoju 留言落库。发帖即时只返回
        // pending_reply，前端轮询真留言——绝不要把兜底模板当小桔回复显示。
        const me: Moment = {
          id: post.id,
          author: "me",
          date: (post.createdAt || "").slice(0, 10),
          at: post.createdAt,
          content: post.content,
          loved: !!post.loved,
          archived: false,
          comments: [],
          pendingReply: true,
        };
        setMoments((prev) => [me, ...prev]);
        setDraft("");
        setComposerOpen(false);
        setPage(1);
        // 自动展开这条的留言区，让「正在想…」→ 真回复可见
        setOpenComments((p) => ({ ...p, [post.id]: true }));
        flashNotice("小桔正在回你…");
        pollXiaojuReply(post.id);
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
      <div className="mom-main">
        <div className="mom-feed-header">
          <div className="mom-feed-title">
            <XiaojuAvatar size={22} />
            <span>我们的日记</span>
            <span className="mom-feed-sub">只有我们看得见</span>
          </div>
          {loaded && activeMoments.length > 0 && (
            <div className="mom-meter">
              <span className="mom-meter-lab">
                她记着 {remembered} / {activeMoments.length}
              </span>
              <span className="mom-meter-bar">
                <i
                  style={{
                    width: `${Math.round((remembered / activeMoments.length) * 100)}%`,
                  }}
                />
              </span>
            </div>
          )}
        </div>

        {notice && <div className="mom-notice">{notice}</div>}

        {!loaded ? (
          <p className="mom-loading">翻开我们的日记…</p>
        ) : sorted.length === 0 ? (
          <div className="mom-empty">
            <div className="mom-empty-ring" aria-hidden="true" />
            <p>这里还没有日记。</p>
            <p className="mom-empty-hint">写一条，或等小桔记下今天——回忆就从这里开始。</p>
          </div>
        ) : (
          <>
            <div className="mom-timeline">
              {dayGroups.map((g, gi) => (
                <div className={`mom-day${gi === 0 ? "" : " pale"}`} key={`${g.date}-${gi}`}>
                  <div className="mom-day-mark">
                    <i />
                    <span>{dayTag(g.date)}</span>
                  </div>
                  {g.items.map((m) => (
                    <MomentCard key={m.id} {...cardProps(m)} />
                  ))}
                </div>
              ))}
            </div>
          {activeMoments.length > 0 && (
              <div className="mom-pager">
                <button
                  type="button"
                  className="mom-pager-btn"
                  onClick={() => safePage > 1 && setPage(safePage - 1)}
                  disabled={safePage <= 1}
                >
                  ‹ 上一页
                </button>
                {totalPages > 1 &&
                  totalPages <= 7 &&
                  Array.from({ length: totalPages }, (_, i) => i + 1).map((n) => (
                    <button
                      key={n}
                      type="button"
                      className={`mom-pager-num${n === safePage ? " active" : ""}`}
                      onClick={() => setPage(n)}
                    >
                      {n}
                    </button>
                  ))}
                <span className="mom-pager-info">第 {safePage} / {totalPages} 页</span>
                <button
                  type="button"
                  className="mom-pager-btn"
                  onClick={() => {
                    if (safePage < totalPages) {
                      setPage(safePage + 1);
                    } else if (hasMore) {
                      loadMore();
                      setPage(Number.MAX_SAFE_INTEGER); // 续拉后由 safePage 收敛到新末页
                    }
                  }}
                  disabled={safePage >= totalPages && !hasMore}
                >
                  {safePage >= totalPages && hasMore
                    ? loadingMore
                      ? "加载中…"
                      : "下一页 ›"
                    : "下一页 ›"}
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
                  <div className="mom-timeline mom-timeline-flat">
                    {archivedMoments.map((m) => (
                      <MomentCard key={m.id} {...cardProps(m)} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <aside className={`lm-editor${composerOpen ? " writing" : ""}`}>
        <div className="ed-head">
          <span className="ed-dot" aria-hidden="true" />
          <button type="button" className="ed-lab" onClick={() => setComposerOpen((v) => !v)}>
            ✎ 我也写一条
          </button>
          <span className="ed-sp" />
          {composerOpen && draft.trim() !== "" && (
            <span className="ed-cnt">{draft.trim().length} 字</span>
          )}
        </div>

        {composerOpen ? (
          <>
            <div className="ed-body">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="写下今天发生的事，或想对小桔说的话…（可插表情）"
                rows={7}
              />
            </div>
            {emoteOpen && (
              <div className="mom-emote-picker">
                <EmotePicker onPick={insertEmote} />
              </div>
            )}
            <div className="ed-rule" />
            <div className="ed-foot">
              <button
                type="button"
                className="ed-emote"
                onClick={() => setEmoteOpen((v) => !v)}
                aria-label="表情"
              >
                <span style={{ fontSize: 15 }}>🙂</span>
              </button>
              <span className="ed-hint">写完她回你一条</span>
              <span className="ed-sp" />
              <button type="button" className="ed-cancel" onClick={() => setComposerOpen(false)}>
                取消
              </button>
              <button
                type="button"
                className="ed-go"
                onClick={publish}
                disabled={!draft.trim() || publishing}
              >
                {publishing ? "发布中…" : "发布"}
              </button>
            </div>
          </>
        ) : (
          <div className="ed-idle">写下今天发生的事，或想对小桔说的话…</div>
        )}
      </aside>
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
    <div className={`mom-card ${isXj ? "mom-xj" : "mom-me"}${moment.loved ? "" : " dim"}`}>
      <div className="mom-card-meta">
        <span className="mom-author">{isXj ? "小桔" : USER_NAME}</span>
        <span className="mom-date">
          {isXj ? `${dateLabel(moment.date)} ${xjTime}`.trim() : dateLabel(moment.date)}
        </span>
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
          {moment.pendingReply && (
            <div className="mom-comment-item xj">
              <div className="mom-comment-bubble">
                <span className="mom-comment-author">小桔</span>
                <span>正在想… 🤔</span>
              </div>
            </div>
          )}
          {moment.comments.map((c) => (
            <div key={c.id} className={`mom-comment-item ${c.author === "xiaoju" ? "xj" : "me"}`}>
              <div className="mom-comment-bubble">
                <span className="mom-comment-author">
                  {c.author === "xiaoju" ? "小桔" : USER_NAME}
                  {c.at ? ` · ${c.at.slice(11, 16)}` : ""}
                </span>
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
