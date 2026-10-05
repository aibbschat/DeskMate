/**
 * 手机端花园（个人中心）数据层。
 *
 * 全部走**现网已验证**的读口，第一批不加任何后端端点（设计稿 §六.7）。
 * 每个函数都 fail-open：单条读失败返回 `{ok:false}` + 空数据，UI 显示诚实的空态，
 * 绝不因为一个 500 就把整屏崩掉（架构稿 §十「零数据态必测」的同族教训）。
 *
 * 与桌面的差异只为省移动端流量：小桔日记桌面回看 14 天、手机端只回看 5 天
 * （日记是「一天一个文件」，逐日要一次请求）。
 */
import { authedFetch, httpBase } from "../api";
import { splitDiaryEntries, diaryEntryId } from "../diaryEntries";

export type Author = "xiaoju" | "me";

export interface MomentComment {
  id: string;
  author: Author;
  text: string;
  at: string;
}

export interface Moment {
  id: string;
  author: Author;
  date: string;
  at: string;
  content: string;
  loved: boolean;
  archived: boolean;
  seq?: number;
  comments: MomentComment[];
  /** 本地乐观上屏的状态：pending=正在记（后端要给小桔生成回复，慢），failed=没记上可重试。
   *  服务端返回后由 mergePostResult 换成真实条目，这个字段不会留在最终数据里。 */
  state?: "pending" | "failed";
}

export interface Res<T> {
  ok: boolean;
  data: T;
  /** 失败原因（网络/非 200），只用于自检页，不进正文文案 */
  error?: string;
}

const J = (r: Response) => r.json().catch(() => ({}));

/**
 * 时间线排序必须按 epoch，不能按字符串。
 *
 * 坑在这：她的日记条目是**本地墙钟**（后端写的是 `2026-10-05T21:03:00`，没有时区后缀），
 * 而我本地乐观上屏用的是 **UTC ISO**（`...T13:39:59.886Z`）。按字符串比，"21:03" > "13:39Z"，
 * 结果「我刚记的那条」会沉到她傍晚的条目下面 —— 正好和该显示的顺序相反。
 * Date.parse 对无时区后缀的串按本地时区解释，两种格式就落到同一个可比标度上了。
 */
export function atMs(m: { at?: string; date?: string }): number {
  const raw = String(m.at || m.date || "");
  if (!raw) return 0;
  const t = Date.parse(raw); // 无时区后缀 → 按本地时区解释，与 UTC 串落到同一标度
  return Number.isFinite(t) ? t : 0;
}

export const byNewest = (a: Moment, b: Moment): number => atMs(b) - atMs(a);

async function get(path: string): Promise<Res<any>> {
  try {
    const r = await authedFetch(`${httpBase()}/v1/deskmate${path}`);
    if (!r.ok) return { ok: false, data: null, error: `HTTP ${r.status}` };
    return { ok: true, data: await J(r) };
  } catch (e: any) {
    return { ok: false, data: null, error: String(e?.message || e) };
  }
}

async function post(path: string, body: unknown): Promise<Res<any>> {
  try {
    const r = await authedFetch(`${httpBase()}/v1/deskmate${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) return { ok: false, data: null, error: `HTTP ${r.status}` };
    return { ok: true, data: await J(r) };
  } catch (e: any) {
    return { ok: false, data: null, error: String(e?.message || e) };
  }
}

/** 手机端回看天数（桌面 14，这里 5：每多一天多一次请求） */
const DIARY_LOOKBACK = 5;

/**
 * 时间线 = 我写的日记（user_posts，后端已内联留言）+ 她写的日记（文件）。
 * 两端合并后按时间倒序；同一天她可能连记几篇 → 每篇独立成卡（共用 splitDiaryEntries）。
 */
export async function loadTimeline(limit = 30): Promise<Res<{ moments: Moment[]; next: string | null }>> {
  const mine = await get(`/moments?limit=${limit}`);
  if (!mine.ok) return { ok: false, data: { moments: [], next: null }, error: mine.error };
  const d = mine.data || {};
  const commentsMap: Record<string, MomentComment[]> = d.comments || {};
  const lovedSet = new Set<string>(Array.isArray(d.loved) ? d.loved : []);

  const out: Moment[] = (Array.isArray(d.items) ? d.items : []).map((p: any) => ({
    id: String(p.id),
    author: "me" as Author,
    date: String(p.createdAt || p.created_at || "").slice(0, 10),
    at: String(p.createdAt || p.created_at || ""),
    content: String(p.content || ""),
    loved: !!p.loved,
    archived: !!p.archived,
    comments: Array.isArray(p.comments) ? p.comments : commentsMap[String(p.id)] || [],
  }));

  // 她的日记：先拿有记录的日期，再逐日取正文
  const dates = await get(`/diary`);
  if (dates.ok) {
    const list: string[] = Array.isArray(dates.data?.dates) ? dates.data.dates : [];
    for (const date of list.slice(-DIARY_LOOKBACK).reverse()) {
      const one = await get(`/diary/${encodeURIComponent(date)}`);
      if (!one.ok || !one.data?.content) continue;
      const { entries } = splitDiaryEntries(String(one.data.content));
      entries.forEach((entry, idx) => {
        if (!entry.body.trim()) return;
        const id = diaryEntryId(date, entry, idx);
        out.push({
          id,
          author: "xiaoju",
          date,
          at: `${date}T${entry.time || "12:00"}:00`,
          content: entry.body,
          loved: lovedSet.has(id),
          archived: false,
          seq: entry.seq,
          comments: commentsMap[id] || commentsMap[`xj-${date}`] || [],
        });
      });
    }
  }

  out.sort(byNewest);
  return { ok: true, data: { moments: out, next: d.next_cursor ?? null } };
}

/** 我记一笔；后端会落 user_posts 并让小桔回一条留言（她的回复已在 comments[0] 持久化） */
export const postMoment = (content: string) => post(`/moments/post`, { content });
export const commentMoment = (moment_id: string, text: string) =>
  post(`/moments/comment`, { moment_id, author: "me", text });
/** 🩷 = 「聊天主动回忆」开关（默认全开，点按是静音 toggle），不是收藏 */
export const loveMoment = (id: string, content: string, loved: boolean) =>
  post(`/moments/${encodeURIComponent(id)}/love`, { content, loved });

/** 本地乐观上屏用的临时条目（pending 态） */
let pendingSeq = 0;
export function pendingMoment(content: string): Moment {
  const at = new Date().toISOString();
  return {
    // 必须带序号：只用 Date.now() 时，同一毫秒内连点两次会撞 id，
    // mergePostResult 就会把两条不同的草稿当成同一条（真机双击/手快会碰到）
    id: `pending-${Date.now()}-${++pendingSeq}`,
    author: "me",
    date: at.slice(0, 10),
    at,
    content,
    loved: false,
    archived: false,
    comments: [],
    state: "pending",
  };
}

/**
 * 把后端返回的 {post, comment} 合并回时间线，替换掉那条 pending。
 *
 * 单独成函数是为了能测：这条链路的失败模式很难看（草稿留在框里、看起来没发出去，
 * 其实几秒后已经发上了——2026-10-05 萧哥真机就是这样误判的），所以逻辑要钉死。
 * 后端形状对齐桌面 MomentFeed.tsx:345-360（`post` + 小桔回复在 `comment`）。
 */
export function mergePostResult(list: Moment[], pendingId: string, data: any): Moment[] {
  const p = data?.post;
  const reply = data?.comment;
  if (!p) return list.map((m) => (m.id === pendingId ? { ...m, state: "failed" as const } : m));
  const real: Moment = {
    id: String(p.id ?? pendingId),
    author: "me",
    date: String(p.createdAt || "").slice(0, 10),
    at: String(p.createdAt || ""),
    content: String(p.content || ""),
    loved: !!p.loved,
    archived: false,
    comments: reply
      ? [{ id: String(reply.id), author: (reply.author || "xiaoju") as Author, text: String(reply.text || ""), at: String(reply.createdAt || "") }]
      : [],
  };
  const had = list.some((m) => m.id === pendingId);
  const next = list.map((m) => (m.id === pendingId ? real : m));
  return had ? next : [real, ...next];
}

/** 留言返回：把我那条 + 小桔可能的自动回复追加到目标条目上 */
export function mergeCommentResult(list: Moment[], momentId: string, data: any): Moment[] {
  const mine = data?.comment;
  const extra = data?.reply;
  const add: MomentComment[] = [];
  if (mine) add.push({ id: String(mine.id), author: (mine.author || "me") as Author, text: String(mine.text || ""), at: String(mine.createdAt || "") });
  if (extra) add.push({ id: String(extra.id), author: (extra.author || "xiaoju") as Author, text: String(extra.text || ""), at: String(extra.createdAt || "") });
  if (!add.length) return list;
  return list.map((m) => (m.id === momentId ? { ...m, comments: [...(m.comments || []), ...add] } : m));
}

/** 发失败了把 pending 标成可重试，而不是悄悄吞掉 */
export function markFailed(list: Moment[], pendingId: string): Moment[] {
  return list.map((m) => (m.id === pendingId ? { ...m, state: "failed" as const } : m));
}

/**
 * 后台刷新时合并本地状态，防止"刚发出去的那条被刷新冲没"。
 *
 * 场景：POST 成功 → 乐观条目换成服务端真 id → 紧接着的后台 refresh 读到的列表
 * 万一还没这条（读后写延迟、或将来两端各有一份），卡片就会凭空消失，
 * 用户看到的还是"我记的东西没了"。所以：
 *   ① 本地 pending / failed 的条目一定保留（那是还没落地的操作）；
 *   ② 本地 15 秒内发出去、服务端列表里还没有的条目保留（读后写宽限窗）。
 */
const READ_YOUR_WRITE_MS = 15_000;

export function mergeRefresh(serverList: Moment[], localList: Moment[], now: number = Date.now()): Moment[] {
  const seen = new Set(serverList.map((m) => m.id));
  const keep = localList.filter((m) => {
    if (seen.has(m.id)) return false;
    if (m.state === "pending" || m.state === "failed") return true;
    if (m.author !== "me") return false;
    const t = Date.parse(m.at || "");
    return Number.isFinite(t) && now - t < READ_YOUR_WRITE_MS;
  });
  return [...keep, ...serverList].sort(byNewest);
}

export interface Fact {
  ts: string;
  fact: string;
  category: string;
}

/** 长期记忆（learned_history，只增不改） */
export async function loadFacts(limit = 200): Promise<Res<{ items: Fact[]; total: number }>> {
  const r = await get(`/learned-history?limit=${limit}`);
  if (!r.ok) return { ok: false, data: { items: [], total: 0 }, error: r.error };
  const items: Fact[] = (Array.isArray(r.data?.items) ? r.data.items : []).map((x: any) => ({
    ts: String(x.ts ?? ""),
    fact: String(x.fact ?? ""),
    category: String(x.category ?? ""),
  }));
  return { ok: true, data: { items, total: Number(r.data?.stats?.total ?? items.length) || items.length } };
}

export interface PortraitSection {
  id: string;
  title: string;
  icon?: string;
  status?: string;
  headline?: string;
  articles: { title: string; content: string; evidence?: string[]; tags?: string[] }[];
}

/** 它眼中的我（alter-ego 画像） */
export async function loadPortrait(): Promise<Res<{ enabled: boolean; summary: string; sections: PortraitSection[] }>> {
  const r = await get(`/alter-ego`);
  if (!r.ok) return { ok: false, data: { enabled: false, summary: "", sections: [] }, error: r.error };
  const p = r.data?.portrait || {};
  return {
    ok: true,
    data: {
      enabled: !!r.data?.enabled,
      summary: String(p.summary || ""),
      sections: (Array.isArray(p.sections) ? p.sections : []).map((s: any) => ({
        id: String(s.id ?? s.title ?? ""),
        title: String(s.title ?? ""),
        icon: s.icon,
        status: s.status,
        headline: s.headline,
        articles: (Array.isArray(s.articles) ? s.articles : []).map((a: any) => ({
          title: String(a.title ?? ""),
          content: String(a.content ?? ""),
          evidence: Array.isArray(a.evidence) ? a.evidence.map(String) : undefined,
          tags: Array.isArray(a.tags) ? a.tags.map(String) : undefined,
        })),
      })),
    },
  };
}

export interface Growth {
  level: number;
  bondStage: string;
  bondDepth: number;
  achievements: { name?: string; unlocked?: boolean }[];
}

/** 统计与默契（growth 快照） */
export async function loadGrowth(): Promise<Res<Growth>> {
  const r = await get(`/growth`);
  if (!r.ok) return { ok: false, data: { level: 0, bondStage: "", bondDepth: 0, achievements: [] }, error: r.error };
  const g = r.data || {};
  return {
    ok: true,
    data: {
      level: Number(g.level ?? 0) || 0,
      bondStage: String(g.bond?.stage ?? ""),
      bondDepth: Number(g.bond?.depth ?? 0) || 0,
      achievements: Array.isArray(g.achievements) ? g.achievements : [],
    },
  };
}
