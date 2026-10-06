/**
 * 花园屏（个人中心）。
 *
 * 桌面版是 yard 全景（重 SVG + hover-only 花丛提示）。手机端**不搬全景**：390px 上把装饰性
 * 场景缩到能看是负优化（信息密度掉光还费渲染）。这里降级成「门楣带 + 门的清单」，第一眼给
 * 日记时间线——它本来就是时间线形态，也是手机上最值钱的屏。
 *
 * 写口只给三个：记一笔 / 留言 / 🩷。**不给删除**——GUI 侧本来就没有 wired 的删除端点，
 * 手机端不自造（架构稿红线：不许出现「看着能删其实删不掉」）。
 *
 * D3：长期记忆默认对可疑条目做展示层折叠（可点开）。这是展示层启发式，界面上明说不后端判定。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { authedFetch, httpBase } from "../api";
import { Markdown } from "../components/Markdown";
import { FOLD_LABEL, shouldFold } from "./sensitive";
import { useInstall } from "./useInstall";
import { INSTALL_STATE_LABEL, type InstallState } from "./install";
import {
  commentMoment,
  faultText,
  loadFacts,
  loadGrowth,
  loadPortrait,
  loadTimeline,
  loveMoment,
  markFailed,
  mergeRefresh,
  mergeCommentResult,
  mergePostResult,
  pendingMoment,
  postMoment,
  type Fact,
  type Growth,
  type Moment,
  type PortraitSection,
} from "./yard";

type Sub = null | "facts" | "portrait" | "diary" | "stats" | "install";

interface Props {
  /** 顶栏齿轮 → 让 App 打开连接自检抽屉 */
  onOpenDebug: () => void;
}

/** 读失败时的有界自动重试：1.5s 起指数退避，最多 6 次（约 95 秒），之后交给手动按钮。
 *  与 useChat 的保活重试同一套路数——手机上「过一会儿自己好了」比「一直红着要人刷」重要。 */
const AUTO_RETRY_MAX = 6;
const AUTO_RETRY_BASE_MS = 1500;

export function YardScreen({ onOpenDebug }: Props) {
  const [sub, setSub] = useState<Sub>(null);
  const [moments, setMoments] = useState<Moment[]>([]);
  const [loaded, setLoaded] = useState(false);
  /** 真故障（两路都没读到）才非空；文案按 站点口令 / 应用 token / 网络 分开，绝不混着说 */
  const [fault, setFault] = useState<{ title: string; hint: string } | null>(null);
  /** 部分成功（她的在、你的没读到）时的软提示 */
  const [degraded, setDegraded] = useState("");
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  const [openComments, setOpenComments] = useState<Record<string, boolean>>({});
  const [cDraft, setCDraft] = useState<Record<string, string>>({});
  const [facts, setFacts] = useState<Fact[] | null>(null);
  const [portrait, setPortrait] = useState<PortraitSection[] | null>(null);
  const [growth, setGrowth] = useState<Growth | null>(null);
  const [dates, setDates] = useState<string[]>([]);
  const [dayText, setDayText] = useState<Record<string, string>>({});
  const [unfolded, setUnfolded] = useState<Record<string, boolean>>({});
  const tries = useRef(0);
  /** 「日记 → 就在下面」滚的是花园自己的容器：两屏同时挂载，
   *  `document.querySelector(".m-scroll")` 会抓到对话屏那个（点一下没反应）。 */
  const feedRef = useRef<HTMLDivElement>(null);
  const install = useInstall();

  const flash = (m: string) => {
    setNotice(m);
    window.setTimeout(() => setNotice(""), 2400);
  };

  const refresh = useCallback(async () => {
    const r = await loadTimeline(30);
    if (r.ok) {
      // 不能整表覆盖：本地还在 pending/failed、或刚发出 15 秒内的条目要保住
      setMoments((prev) => mergeRefresh(r.data.moments, prev));
      setFault(null);
      setDegraded(r.data.degraded || "");
      tries.current = 0;
    } else {
      setFault(faultText(r));
      setDegraded("");
    }
    setLoaded(true);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 失败了先自己再试几次（首屏抢跑这种一次性抖动不该让人去手动刷新）
  useEffect(() => {
    if (!fault || tries.current >= AUTO_RETRY_MAX) return;
    const wait = AUTO_RETRY_BASE_MS * 2 ** tries.current;
    const t = window.setTimeout(() => {
      tries.current += 1;
      void refresh();
    }, wait);
    return () => window.clearTimeout(t);
  }, [fault, refresh]);

  // 切回前台立刻补一次：她这会儿多半已经多记了几条
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState !== "visible") return;
      tries.current = 0;
      void refresh();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [refresh]);

  // 子页按需加载（不一次把 676 条记忆拖进首屏）
  useEffect(() => {
    if (sub === "facts" && facts === null) void loadFacts(200).then((r) => r.ok && setFacts(r.data.items));
    if (sub === "portrait" && portrait === null)
      void loadPortrait().then((r) => r.ok && setPortrait(r.data.sections));
    if (sub === "stats" && growth === null) void loadGrowth().then((r) => r.ok && setGrowth(r.data));
    if (sub === "diary" && dates.length === 0)
      void authedFetch(`${httpBase()}/v1/deskmate/diary`)
        .then((r) => (r.ok ? r.json() : { dates: [] }))
        .then((d) => setDates(Array.isArray(d.dates) ? d.dates : []))
        .catch(() => setDates([]));
  }, [sub, facts, portrait, growth, dates.length]);

  /**
   * 记一笔：先乐观上屏（草稿立刻离开输入框、卡片显示「正在记」），后端回了我再换成真条目。
   *
   * 为什么必须这样：`POST /moments/post` 要现场让小桔生成一条回复，实测要好几秒；
   * 旧写法是"等返回才清空草稿 + 不刷新列表"，萧哥真机上看到的就是"点了没反应、字还在框里"，
   * 打完字回头才发现其实已经发上去了。发没发出去这件事，不该让人猜。
   */
  const publish = async (text?: string) => {
    const body = (text ?? draft).trim();
    if (!body) return;
    const pend = pendingMoment(body);
    if (text === undefined) setDraft("");
    setMoments((prev) => [pend, ...prev]);
    const r = await postMoment(body);
    if (r.ok) {
      setMoments((prev) => mergePostResult(prev, pend.id, r.data));
      flash(r.data?.comment ? "小桔回复了你" : "记下了");
      // 计数与她的新记录放后台补，不挡这次提交的反馈
      void refresh();
    } else {
      setMoments((prev) => markFailed(prev, pend.id));
      flash("没记上，点卡片上的重试");
    }
  };

  const addComment = async (id: string) => {
    const text = (cDraft[id] || "").trim();
    if (!text) return;
    // 留言同样先本地追加，不等小桔的自动回复生成完
    setMoments((prev) =>
      mergeCommentResult(prev, id, { comment: { id: `local-${Date.now()}`, author: "me", text, createdAt: new Date().toISOString() } }),
    );
    setCDraft((s) => ({ ...s, [id]: "" }));
    const r = await commentMoment(id, text);
    if (r.ok) {
      // 用后端返回的正式条目覆盖（含小桔的自动回复）；本地那条临时条目由 refresh 收敛
      setMoments((prev) => mergeCommentResult(prev, id, r.data));
      void refresh();
    } else flash("留言没发出去");
  };

  const toggleLove = async (m: Moment) => {
    const r = await loveMoment(m.id, m.content, !m.loved);
    if (r.ok) {
      setMoments((prev) => prev.map((x) => (x.id === m.id ? { ...x, loved: !!r.data?.loved } : x)));
      flash(r.data?.message || (r.data?.loved ? "已记进记忆" : "已取消主动回忆"));
    } else flash("操作失败");
  };

  const mine = moments.filter((m) => m.author === "me" && !m.archived).length;
  const hers = moments.filter((m) => m.author === "xiaoju").length;
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = moments.filter((m) => m.date === today).length;

  if (sub) {
    return (
      <>
        <div className="m-top">
          <button type="button" className="m-back" onClick={() => setSub(null)} aria-label="回花园">
            <svg width="17" height="17" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7">
              <path d="M11 3.5 5.5 9 11 14.5" />
            </svg>
          </button>
          <div className="m-hdr">
            <div className="m-hdr-t">{SUB_TITLE[sub]}</div>
            <div className="m-hdr-s">{SUB_SUB[sub]}</div>
          </div>
          <div className="m-spacer" />
        </div>
        <div className="m-scroll">
          {sub === "facts" && (
            <div className="m-card">
              {(facts || []).map((f, i) => {
                const key = `${f.ts}-${i}`;
                const fold = shouldFold(f.fact);
                const open = !!unfolded[key];
                return (
                  <div className="m-fact" key={key}>
                    <span className="m-fact-ts">{(f.ts || "").slice(0, 10)}</span>
                    <div className="m-fact-b">
                      <div className={"m-fact-t" + (fold.fold && !open ? " blur" : "")}>{f.fact}</div>
                      {!!f.category && <span className="m-cat">{f.category}</span>}
                      {fold.fold && !open && (
                        <button
                          type="button"
                          className="m-unfold"
                          onClick={() => setUnfolded((u) => ({ ...u, [key]: true }))}
                        >
                          已折叠（{FOLD_LABEL[fold.reason!]}）· 点开
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
              {facts === null && <div className="m-empty-in">在读她的记忆…</div>}
              {!!facts?.length && (
                <div className="m-note">
                  折叠是这台设备上的判断（词表启发式，可能误判），不是后端的分级；点开后本轮不再折
                </div>
              )}
            </div>
          )}

          {sub === "portrait" && (
            <div className="m-card">
              {(portrait || []).map((s) => (
                <div key={s.id} className="m-sec">
                  <div className="m-sec-h">
                    {s.icon && <span>{s.icon}</span>}
                    {s.title}
                    {s.status && <em className="m-sec-st">{s.status}</em>}
                  </div>
                  {s.headline && <div className="m-sec-hl">{s.headline}</div>}
                  {s.articles.map((a, i) => (
                    <div key={i} className="m-art">
                      <div className="m-art-t">{a.title}</div>
                      <Markdown text={a.content} />
                      {!!a.tags?.length && (
                        <div className="m-tags">{a.tags.map((t) => <span key={t} className="m-cat">{t}</span>)}</div>
                      )}
                    </div>
                  ))}
                </div>
              ))}
              {portrait === null && <div className="m-empty-in">在读她眼中的你…</div>}
              {!portrait?.length && <div className="m-empty-in">还没有画像，等你们多聊几轮</div>}
            </div>
          )}

          {sub === "diary" && (
            <div className="m-card">
              {dates
                .slice(-20)
                .reverse()
                .map((d) => (
                  <details key={d} className="m-day">
                    <summary>{d}</summary>
                    <DayBody date={d} cache={dayText} onLoad={setDayText} />
                  </details>
                ))}
              {dates.length === 0 && <div className="m-empty-in">还没有她的记录</div>}
            </div>
          )}

          {sub === "stats" && (
            <div className="m-card">
              <div className="m-stat">
                <span className="m-stat-k">等级</span>
                <span className="m-stat-v">{growth ? growth.level : "—"}</span>
              </div>
              <div className="m-stat">
                <span className="m-stat-k">默契</span>
                <span className="m-stat-v">{growth ? growth.bondStage || "—" : "—"}</span>
              </div>
              <div className="m-stat">
                <span className="m-stat-k">深度</span>
                <span className="m-stat-v">{growth ? growth.bondDepth : "—"}</span>
              </div>
              <div className="m-stat">
                <span className="m-stat-k">里程碑</span>
                <span className="m-stat-v">{growth ? growth.achievements.filter((a) => a.unlocked).length : "—"}</span>
              </div>
              <div className="m-stat">
                <span className="m-stat-k">她记下的事实</span>
                <span className="m-stat-v">{facts ? facts.length : "进「长期记忆」看"}</span>
              </div>
              <div className="m-note">只读。这些数在桌面端能点开看明细，手机上不重复一遍</div>
            </div>
          )}

          {sub === "install" && (
            <div className="m-card">
              <div className="m-inst-h">
                <div className="m-inst-t">{install.copy.title}</div>
                <div className="m-inst-p">{install.copy.hint}</div>
              </div>
              {install.copy.action === "prompt" && (
                <div className="m-inst-btn">
                  <button
                    type="button"
                    className="btn pri wide"
                    disabled={install.busy}
                    onClick={() =>
                      void install.install().then((r) =>
                        flash(
                          r === "accepted"
                            ? "在装了，回桌面看她"
                            : r === "dismissed"
                              ? "这次没装，随时可以再来"
                              : "这台设备的浏览器没给安装弹窗，按上面的手动路径来",
                        ),
                      )
                    }
                  >
                    {install.busy ? "在问浏览器…" : "装到主屏"}
                  </button>
                </div>
              )}
              <Line k="浏览器判定" v={INSTALL_STATE_LABEL[install.state]} />
              <Line k="manifest" v={install.probe ? `${install.probe.ok ? "✓" : "✗"} ${install.probe.note}` : "在读…"} />
              <div className="m-inst-btn">
                <button type="button" className="btn opt wide" onClick={install.recheck}>
                  重新查一次
                </button>
              </div>
              <div className="m-note">
                装一次就够：她跟着这个浏览器走，换浏览器要再装一次。装完她不会自己冒出来找你——第一批没做推送，切回前台时她会把落下的补给你
              </div>
            </div>
          )}
        </div>
        {notice && <div className="m-toast">{notice}</div>}
      </>
    );
  }

  return (
    <>
      <div className="m-top">
        <div className="m-hdr">
          <div className="m-hdr-t">花园</div>
          <div className="m-hdr-s">它眼中的你 · 你的记录 · 默契</div>
        </div>
        <div className="m-spacer" />
        <button type="button" className="m-flag" onClick={onOpenDebug} aria-label="连接与同步状态">
          <svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="9" cy="9" r="2.6" />
            <path d="M9 1.6v2M9 14.4v2M1.6 9h2M14.4 9h2M3.8 3.8l1.4 1.4M12.8 12.8l1.4 1.4M14.2 3.8l-1.4 1.4M5.2 12.8l-1.4 1.4" />
          </svg>
        </button>
      </div>

      <div className="m-scroll" ref={feedRef}>
        {/* 门楣带：桌面全景的等比压缩版，in-flow（不做负偏移，所以不会飘） */}
        <div className="m-lintel">
          <div className="m-lintel-in">
            <div className="m-lintel-t">今天她在这园子里</div>
            <div className="m-lintel-d">
              {todayCount > 0 ? `今天记下 ${todayCount} 条` : "今天还没记"} · 你写的 {mine} · 她写的 {hers}
            </div>
          </div>
        </div>

        <div className="m-card m-list">
          <Row icon="📖" name="日记" sub={`你写的 ${mine} · 她写的 ${hers}`} foot="就在下面" onClick={() => scrollFeed(feedRef.current)} />
          <Row icon="👁" name="它眼中的我" sub="她对你的画像" onClick={() => setSub("portrait")} />
          <Row icon="⌗" name="长期记忆" sub="只增不改的事实" onClick={() => setSub("facts")} />
          <Row icon="✎" name="她自己的记录" sub="她的日记原文，只读" onClick={() => setSub("diary")} />
          <Row icon="▦" name="统计与默契" sub="等级 / 默契 / 里程碑" onClick={() => setSub("stats")} />
          <Row icon="⌂" name="装到主屏" sub={installRowSub(install.state, install.probe?.ok)} onClick={() => setSub("install")} />
        </div>

        <div className="m-feed-h">
          <b>日记</b>
          <span>按天倒序</span>
        </div>

        {fault && (
          <div className="m-fault bad">
            <div className="m-fault-row">
              <div>
                <b>{fault.title}</b>
                <span>{fault.hint}</span>
              </div>
              <button
                type="button"
                className="m-retry"
                onClick={() => {
                  tries.current = 0;
                  void refresh();
                }}
              >
                重试
              </button>
            </div>
          </div>
        )}
        {!fault && degraded && (
          <div className="m-fault">
            <b>有一部分没读到</b>
            <span>{degraded}</span>
          </div>
        )}
        {loaded && !moments.length && !fault && (
          <div className="m-empty-in">还没有日记。下面记第一句，她会回你。</div>
        )}

        {moments
          .filter((m) => !m.archived)
          .map((m) => (
            <div className={"m-post" + (m.state ? " " + m.state : "")} key={m.id}>
              <div className="m-post-h">
                <span className="m-post-n">{m.author === "me" ? "萧盛" : "小桔"}</span>
                <span className="m-post-ts">{m.state === "pending" ? "正在记…" : m.state === "failed" ? "没记上" : fmt(m.at)}</span>
                {m.state === "failed" && (
                  <button type="button" className="m-retry" onClick={() => void publish(m.content)}>
                    重试
                  </button>
                )}
              </div>
              <div className="m-post-b">
                <Markdown text={m.content} />
              </div>
              {!m.state && (
                <div className="m-post-a">
                <button type="button" className={"m-act" + (m.loved ? " on" : "")} onClick={() => toggleLove(m)}>
                  ❤ {m.loved ? "在回忆里" : "记进回忆"}
                </button>
                <button
                  type="button"
                  className="m-act"
                  onClick={() => setOpenComments((s) => ({ ...s, [m.id]: !s[m.id] }))}
                >
                  留言 {m.comments?.length ? m.comments.length : ""}
                </button>
                </div>
              )}
              {openComments[m.id] && (
                <div className="m-cms">
                  {(m.comments || []).map((c) => (
                    <div key={c.id} className="m-cm">
                      <b>{c.author === "me" ? "萧盛" : "小桔"}</b> {c.text}
                    </div>
                  ))}
                  <div className="m-cm-row">
                    <input
                      className="m-cm-in"
                      value={cDraft[m.id] || ""}
                      placeholder="回一句…"
                      onChange={(e) => setCDraft((s) => ({ ...s, [m.id]: e.target.value }))}
                      onKeyDown={(e) => e.key === "Enter" && void addComment(m.id)}
                    />
                    <button type="button" className="m-cm-send" onClick={() => void addComment(m.id)}>
                      发送
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
      </div>

      <div className="m-dock">
        <div className="mc-row">
          <input
            className="m-dock-in"
            value={draft}
            placeholder="记下这一句…"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void publish()}
          />
          <button type="button" className={"mc-send" + (draft.trim() ? "" : " dis")} onClick={() => void publish()}>
            <svg width="17" height="17" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 2.4 13.6 8 8 13.6v-3.4H2.4V7.8H8z" />
            </svg>
          </button>
        </div>
      </div>
      {notice && <div className="m-toast">{notice}</div>}
    </>
  );
}

const SUB_TITLE: Record<Exclude<Sub, null>, string> = {
  facts: "长期记忆",
  portrait: "它眼中的我",
  diary: "她自己的记录",
  stats: "统计与默契",
  install: "装到主屏",
};
const SUB_SUB: Record<Exclude<Sub, null>, string> = {
  facts: "只增不改的事实",
  portrait: "她对你的画像",
  diary: "她的日记原文 · 只读",
  stats: "只读",
  install: "这台设备 · 与她的账号无关",
};

/** 门口那一行就要能看出问题，不用点进去（安卓装不上时这里直接见分晓）。 */
function installRowSub(state: InstallState, probeOk: boolean | undefined): string {
  if (state === "installed") return "已经在这台设备上";
  if (probeOk === false) return "装不上：manifest 被判无效";
  if (state === "prompt") return "可以装，点一下";
  return INSTALL_STATE_LABEL[state] || "看这里";
}

function Line({ k, v }: { k: string; v: string }) {
  return (
    <div className="m-kv">
      <span className="m-kv-k">{k}</span>
      <span className="m-kv-v">{v}</span>
    </div>
  );
}

function Row({
  icon,
  name,
  sub,
  foot,
  onClick,
}: {
  icon: string;
  name: string;
  sub: string;
  foot?: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className="m-row" onClick={onClick}>
      <span className="m-row-g">{icon}</span>
      <span className="m-row-b">
        <span className="m-row-n">{name}</span>
        <span className="m-row-s">{sub}</span>
      </span>
      {foot && <span className="m-row-f">{foot}</span>}
      <span className="m-row-c">›</span>
    </button>
  );
}

function DayBody({
  date,
  cache,
  onLoad,
}: {
  date: string;
  cache: Record<string, string>;
  onLoad: React.Dispatch<React.SetStateAction<Record<string, string>>>;
}) {
  const [err, setErr] = useState("");
  useEffect(() => {
    if (cache[date] !== undefined) return;
    let alive = true;
    authedFetch(`${httpBase()}/v1/deskmate/diary/${encodeURIComponent(date)}`)
      .then((r) => (r.ok ? r.json() : { content: "" }))
      .then((d) => alive && onLoad((c) => ({ ...c, [date]: String(d.content || "") })))
      .catch(() => alive && setErr("读不到这一天"));
    return () => {
      alive = false;
    };
  }, [date, cache, onLoad]);
  if (err) return <div className="m-empty-in">{err}</div>;
  if (cache[date] === undefined) return <div className="m-empty-in">在读…</div>;
  return <Markdown text={cache[date]} />;
}

function fmt(iso: string): string {
  const s = String(iso || "");
  const d = s.slice(5, 10).replace("-", "/");
  const t = s.slice(11, 16);
  return t ? `${d} ${t}` : d || s;
}

/** 「日记」那行不跳子页，就是滚到下面的时间线（滚自己那一屏，不抓对话屏的容器）。
 *  用 getBoundingClientRect 算距离：offsetTop 是相对 offsetParent 的，门楣带一进来就会偏。 */
function scrollFeed(pane: HTMLElement | null) {
  const target = pane?.querySelector(".m-feed-h") as HTMLElement | null;
  if (!pane || !target) return;
  const delta = target.getBoundingClientRect().top - pane.getBoundingClientRect().top;
  pane.scrollTo({ top: Math.max(0, pane.scrollTop + delta - 12), behavior: "smooth" });
}
