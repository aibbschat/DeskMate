// 创作流水线视图（线路 B 原生 teams 执行 + 线路 A 圆桌终审）。
// 透明化改造：创建页流程总览 + 结构化制作要求（风格/节奏/集数/产出范围）；
// 进行中页六步步骤条 + 分集卡片（每集独立操作） + 产出预览（草稿内容可见）。
// 全程驱动后端 deskmate.creation.service：创建 → 写/审大纲 → 主编拍板 →
// 写/审剧本（分集逐集） → 主编拍板/圆桌终审 → docx 落地。
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "./Icon";
import { ProposalCard, type FinalProposal } from "./ProposalCard";
import {
  createPipeline,
  getPipeline,
  getNovelIndex,
  pickNovelChapters,
  setRelatedChapters,
  getItemContent,
  extendEpisodes,
  draftOutline,
  reviewOutline,
  leadOutline,
  draftScreen,
  reviewScreen,
  leadScreen,
  finalReviewProposal,
  streamFinalReview,
  finalReviewApply,
  type CreationSnapshot,
  type CreationItem,
  type FinalReviewEvent,
} from "../api";

// ---- 流程总览（透明化：把整个流程摆出来）----
const FLOW_STEPS = [
  { label: "写大纲", who: "编剧" },
  { label: "审大纲", who: "审核专家" },
  { label: "拍板", who: "主编" },
  { label: "写剧本", who: "编剧" },
  { label: "审剧本", who: "审核专家" },
  { label: "拍板 / 圆桌终审", who: "主编 + 7专家" },
];
const PHASE_IDX: Record<string, number> = {
  draft_outline: 0,
  review_outline: 1,
  lead_outline: 2,
  draft_screen: 3,
  review_screen: 4,
  lead_screen: 5,
  done: 6,
};
const PHASE_LABEL: Record<string, string> = {
  draft_outline: "① 撰写大纲",
  review_outline: "② 审核大纲",
  lead_outline: "③ 主编拍板（大纲）",
  draft_screen: "④ 撰写剧本",
  review_screen: "⑤ 审核剧本",
  lead_screen: "⑥ 主编拍板（剧本）",
  done: "✅ 已定稿",
};

const STATE_COLOR: Record<string, string> = {
  open: "text-muted",
  in_progress: "text-accent",
  review: "text-amber-500",
  done: "text-emerald-600",
  blocked: "text-rose-500",
};
const STATE_LABEL: Record<string, string> = {
  open: "待办",
  in_progress: "撰写中",
  review: "待审",
  done: "已定稿",
  blocked: "阻塞",
};

// 风格预设 chips（可选多个）与节奏 chips（单选）
const STYLE_PRESETS = ["权谋正剧", "古装悬疑", "轻喜剧", "甜宠", "职场", "奇幻传奇"];
const PACING_PRESETS = ["紧凑快节奏", "张弛有度", "舒缓慢节奏"];

export function CreationPipelineView({
  onOpenTeamChat,
  onBack,
}: {
  onOpenTeamChat: (teamId: string) => void;
  onBack: () => void;
}) {
  const [pid, setPid] = useState<string | null>(null);
  const [snap, setSnap] = useState<CreationSnapshot | null>(null);
  const [concept, setConcept] = useState("");
  const [kind, setKind] = useState<"outline" | "screenplay">("screenplay");
  const [executor, setExecutor] = useState<"stub" | "llm">("stub");
  const [novelRef, setNovelRef] = useState("");
  // 结构化制作要求（可提要求）
  const [styles, setStyles] = useState<string[]>([]);
  const [styleInput, setStyleInput] = useState("");
  const [pacing, setPacing] = useState("");
  const [episodes, setEpisodes] = useState(1);
  const [episodeRange, setEpisodeRange] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [proposal, setProposal] = useState<FinalProposal | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewing, setReviewing] = useState(false);
  const [events, setEvents] = useState<any[]>([]);
  const [synthesis, setSynthesis] = useState("");
  const [finalizing, setFinalizing] = useState(false);
  const [docxPath, setDocxPath] = useState("");

  // 原著索引（阶段 5）：章节目录缓存 + 主题匹配候选 + 已选参考章节
  const [toc, setToc] = useState<Map<number, string>>(new Map());
  const [pickTopic, setPickTopic] = useState("");
  const [candidates, setCandidates] = useState<{ idx: number; title: string; chars: number; head: string }[]>([]);
  const [picking, setPicking] = useState(false);
  const [savingRelated, setSavingRelated] = useState(false);
  const [selectedRelated, setSelectedRelated] = useState<number[]>([]);

  // 产出预览（透明化：点开看草稿文本）
  const [preview, setPreview] = useState<{ itemId: number; content: string } | null>(null);
  const [loadingPreview, setLoadingPreview] = useState<number | null>(null);

  // 加集续写面板（同一本书做完前 N 集后接着做）
  const [extendOpen, setExtendOpen] = useState(false);
  const [extFrom, setExtFrom] = useState(1);
  const [extTo, setExtTo] = useState(2);
  const [extPrior, setExtPrior] = useState<"summary" | "summary_recent">("summary_recent");
  const [extNotes, setExtNotes] = useState(true);
  const [extBusy, setExtBusy] = useState(false);

  const refresh = async (id?: string) => {
    const use = id ?? pid;
    if (!use) return;
    try {
      const s = await getPipeline(use);
      setSnap(s);
      // 索引就绪时补拉章节目录（标题映射，仅首次）
      if (s.novel_index?.status === "ready" && toc.size === 0) {
        const idx = await getNovelIndex(use);
        if (idx.chapters) setToc(new Map(idx.chapters.map((c) => [c.idx, c.title])));
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  const toggleStyle = (s: string) =>
    setStyles((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));

  const parseRange = (): number[] | undefined => {
    const raw = episodeRange.trim();
    if (!raw) return undefined;
    const nums = raw
      .split(/[,，\s]+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => !isNaN(n) && n >= 1 && n <= Math.max(1, episodes));
    return nums.length ? nums : undefined;
  };

  const start = async () => {
    if (!concept.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await createPipeline(kind, concept, executor, novelRef, {
        style: styles.length ? styles : undefined,
        pacing: pacing || undefined,
        episodes,
        episodeRange: parseRange(),
      });
      setPid(r.pid);
      await refresh(r.pid);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const run = async (fn: () => Promise<CreationSnapshot>) => {
    if (!pid || busy) return;
    setBusy(true);
    setError("");
    try {
      setSnap(await fn());
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const toggleRelated = (i: number) =>
    setSelectedRelated((prev) => (prev.includes(i) ? prev.filter((x) => x !== i) : [...prev, i]));

  const matchChapters = async () => {
    if (!pid) return;
    setPicking(true);
    setError("");
    try {
      const r = await pickNovelChapters(pid, pickTopic.trim() || snap?.concept || "");
      setCandidates(r.chapters);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setPicking(false);
    }
  };

  const saveRelated = async (indices: number[]) => {
    if (!pid) return;
    setSavingRelated(true);
    setError("");
    try {
      await setRelatedChapters(pid, indices);
      setCandidates([]);
      setSelectedRelated([]);
      await refresh();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setSavingRelated(false);
    }
  };

  const showPreview = async (itemId: number) => {
    if (!pid) return;
    if (preview?.itemId === itemId) {
      setPreview(null);
      return;
    }
    setLoadingPreview(itemId);
    setError("");
    try {
      const r = await getItemContent(pid, itemId);
      setPreview({ itemId, content: r.content || "（暂无草稿内容）" });
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setLoadingPreview(null);
    }
  };

  const doExtend = async () => {
    if (!pid || extBusy) return;
    const from = Math.max(1, extFrom);
    const to = Math.max(from, extTo);
    const range: number[] = [];
    for (let i = from; i <= to; i++) range.push(i);
    if (!range.length) return;
    setExtBusy(true);
    setError("");
    try {
      await extendEpisodes(pid, range, {
        priorContext: extPrior,
        includeReviewNotes: extNotes,
      });
      setExtendOpen(false);
      await refresh();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setExtBusy(false);
    }
  };

  const openProposal = async (itemKind: "outline" | "screenplay", episode?: number) => {
    if (!pid) return;
    setError("");
    try {
      const p = await finalReviewProposal(pid, itemKind, undefined, episode);
      setProposal(p);
      setSelected(p.recommended);
      setSynthesis("");
      setEvents([]);
      setDocxPath("");
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  const runReview = async () => {
    if (!pid || !proposal) return;
    setReviewing(true);
    setError("");
    setEvents([]);
    setSynthesis("");
    try {
      for await (const ev of streamFinalReview(
        pid,
        proposal.item_kind as any,
        selected,
        undefined,
        proposal.episode ?? undefined,
      )) {
        setEvents((prev) => [...prev, ev as FinalReviewEvent]);
        if (ev.type === "final_review_ready") setSynthesis(ev.synthesis as string);
      }
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setReviewing(false);
    }
  };

  const apply = async (verdict: boolean) => {
    if (!pid || !proposal) return;
    setFinalizing(true);
    setError("");
    try {
      const r = await finalReviewApply(pid, proposal.item_kind as any, verdict, proposal.episode ?? undefined);
      setProposal(null);
      setSynthesis("");
      setEvents([]);
      // 分集定稿后 docx 键为「第X集」，单集为 item_kind
      const key = proposal.episode ? `第${proposal.episode}集` : proposal.item_kind;
      const p = r.docx?.[key] ?? r.docx?.[proposal.item_kind];
      if (p) setDocxPath(p);
      await refresh();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setFinalizing(false);
    }
  };

  const toggleExpert = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const phase = snap?.phase ?? "";
  const isScreenplay = snap?.kind === "screenplay";
  const isEpisodic = (snap?.episodes ?? 1) > 1;

  /** 单集操作推导：根据该集产出/审核项状态给出可用按钮。 */
  const epActions = (ep: number, prod?: CreationItem, rev?: CreationItem) => {
    const btns: { label: string; primary?: boolean; disabled?: boolean; hint?: string; onClick: () => void }[] = [];
    if (!prod || !rev) return btns;
    // 透明化门禁：剧本依赖大纲，大纲未定稿前不允许开写（避免后端门禁 400 打断流程）
    if (!outlineDone) {
      btns.push({ label: "大纲未就绪", disabled: true, hint: "需先完成大纲定稿（点上方步骤条「写大纲」）", onClick: () => {} });
      return btns;
    }
    if (prod.state === "open" || prod.state === "in_progress") {
      btns.push({ label: `写第${ep}集`, primary: true, onClick: () => run(() => draftScreen(pid!, ep)) });
    }
    if (prod.state === "review" && (rev.state === "open" || rev.state === "in_progress")) {
      btns.push({ label: `审第${ep}集`, onClick: () => run(() => reviewScreen(pid!, ep)) });
    }
    // 拍板只在「产出已待审 + 审核已出意见」时可用（打回重写中不显示，避免非法定稿）
    if (prod.state === "review" && rev.state === "review") {
      btns.push({
        label: "通过定稿",
        primary: true,
        onClick: () => run(() => leadScreen(pid!, true, ep)),
      });
      btns.push({ label: "打回", onClick: () => run(() => leadScreen(pid!, false, ep)) });
      btns.push({ label: "送圆桌终审", onClick: () => openProposal("screenplay", ep) });
    }
    if (prod.state === "done") btns.push({ label: "已定稿", disabled: true, onClick: () => {} });
    return btns;
  };

  // 分集卡片数据：episode -> {prod, rev}
  const epGroups = (() => {
    if (!snap || !isEpisodic) return [];
    const map = new Map<number, { prod?: CreationItem; rev?: CreationItem }>();
    for (const it of snap.items) {
      if (it.episode == null) continue;
      const g = map.get(it.episode) ?? {};
      if (it.title.endsWith("审核")) g.rev = it;
      else g.prod = it;
      map.set(it.episode, g);
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0]);
  })();

  const maxEp = epGroups.length ? epGroups[epGroups.length - 1][0] : 0;
  const outlineItem = snap?.items.find((it) => it.title.startsWith("大纲") && !it.title.includes("审核"));
  const outlineDone = outlineItem?.state === "done";

  const stepIdx = PHASE_IDX[phase] ?? -1;

  return (
    <div className="creation-view">
      <div className="creation-head">
        <button className="artifact-icon-btn" onClick={onBack} aria-label="返回" title="返回">
          <Icon name="arrowLeft" size={16} />
        </button>
        <div className="creation-title">
          <span className="font-semibold text-ink">创作流水线</span>
          {snap && (
            <span className="text-[12px] text-muted">
              · {PHASE_LABEL[phase] ?? phase}
              {snap.team_id ? ` · team ${snap.team_id.slice(0, 8)}` : ""}
            </span>
          )}
        </div>
        {snap?.team_id && (
          <button className="btn" onClick={() => onOpenTeamChat(snap.team_id)}>
            <Icon name="users" size={14} /> 团队聊天
          </button>
        )}
      </div>

      <div className="creation-body">
        {error && (
          <div className="rounded-lg bg-rose-500/10 text-rose-600 text-[13px] px-3 py-2 mb-3">{error}</div>
        )}

        {/* 未开始：创建表单（流程总览 + 结构化制作要求，宽屏两栏降密度） */}
        {!pid && (
          <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-5 space-y-5 max-w-4xl">
            {/* 流程总览：把整个流程摆出来（横贯全宽） */}
            <div>
              <div className="text-[13px] font-semibold text-ink mb-2.5">创作流水线全貌</div>
              <div className="flex items-center gap-1.5 overflow-x-auto pb-1">
                {FLOW_STEPS.map((s, i) => (
                  <div key={s.label} className="flex items-center gap-1.5 shrink-0">
                    <div className="flex flex-col items-center">
                      <div className="w-7 h-7 rounded-full bg-accent/15 text-accent flex items-center justify-center text-[12px] font-semibold">
                        {i + 1}
                      </div>
                      <span className="text-[11px] text-ink mt-1 whitespace-nowrap">{s.label}</span>
                      <span className="text-[10px] text-muted whitespace-nowrap">{s.who}</span>
                    </div>
                    {i < FLOW_STEPS.length - 1 && <span className="text-muted text-[12px] mb-5">→</span>}
                  </div>
                ))}
                <div className="flex flex-col items-center shrink-0 ml-1">
                  <div className="w-7 h-7 rounded-full bg-emerald-500/15 text-emerald-600 flex items-center justify-center text-[12px] font-semibold">
                    ✓
                  </div>
                  <span className="text-[11px] text-emerald-600 mt-1 whitespace-nowrap">定稿 docx</span>
                </div>
              </div>
              <div className="text-[11px] text-muted mt-1.5">
                每位成员都是独立 Agent：编剧写稿、审核专家把关、主编拍板；剧本可在「拍板」处送圆桌 7 位专家终审。
              </div>
            </div>

            <div className="border-t border-black/5 dark:border-white/10 pt-4">
              <div className="text-[13px] font-semibold text-ink">新建创作项目</div>
              <div className="text-[11px] text-muted mt-0.5">
                左侧填基本盘，右侧提制作要求 —— 要求会成为强约束，写稿时严格执行。
              </div>
            </div>

            {/* 两栏：左=基本盘，右=制作要求 */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-4">
                <div>
                  <div className="text-[12px] text-muted mb-1">概念 / 剧情点子</div>
                  <textarea
                    className="w-full rounded-lg border border-black/10 dark:border-white/15 bg-transparent p-2.5 text-[13px] text-ink"
                    rows={3}
                    placeholder="例如：一部明代背景的历史剧，主角以锦衣卫身份周旋于东厂与文官之间"
                    value={concept}
                    onChange={(e) => setConcept(e.target.value)}
                  />
                </div>
                <div>
                  <div className="text-[12px] text-muted mb-1">原著文件路径（可选）</div>
                  <input
                    type="text"
                    className="w-full rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2.5 py-2 text-[13px] text-ink"
                    placeholder="如 /Users/.../原著.docx 或 .txt；留空则仅凭概念创作"
                    value={novelRef}
                    onChange={(e) => setNovelRef(e.target.value)}
                  />
                </div>
                <div className="flex gap-4">
                  <label className="flex items-center gap-1.5 text-[13px] text-ink">
                    <input type="radio" checked={kind === "screenplay"} onChange={() => setKind("screenplay")} /> 剧本（含大纲）
                  </label>
                  <label className="flex items-center gap-1.5 text-[13px] text-ink">
                    <input type="radio" checked={kind === "outline"} onChange={() => setKind("outline")} /> 仅大纲
                  </label>
                </div>
                <div className="flex items-center gap-2 text-[12px] text-muted">
                  执行器：
                  <select
                    className="rounded border border-black/10 dark:border-white/15 bg-transparent px-2 py-1 text-[12px] text-ink"
                    value={executor}
                    onChange={(e) => setExecutor(e.target.value as any)}
                  >
                    <option value="stub">stub（演示·不烧 token）</option>
                    <option value="llm">llm（真实创作）</option>
                  </select>
                </div>
                <button className="btn primary" disabled={busy || !concept.trim()} onClick={start}>
                  {busy ? (novelRef.trim() ? "正在建立原著索引…" : "创建中…") : "开始创作"}
                </button>
                {busy && novelRef.trim() && (
                  <div className="text-[12px] text-muted">
                    首次读取原著需建索引（读全文 → 切章 → 生成改编圣经）；stub 模式为降级索引、不烧 token，请稍候…
                  </div>
                )}
              </div>

              {/* 制作要求（右侧栏，整体一个视觉分组） */}
              <div
                className={
                  "space-y-4 rounded-xl bg-black/[0.02] dark:bg-white/[0.03] p-3.5 " +
                  (kind === "screenplay" ? "" : "opacity-60")
                }
              >
                <div className="text-[12px] font-semibold text-ink">
                  制作要求<span className="text-[10px] font-normal text-muted"> · 可提要求，强约束</span>
                </div>

                {/* 风格 chips（多选） */}
                <div>
                  <div className="text-[12px] text-muted mb-1.5">
                    风格（可多选）<span className="text-[10px]">· 将成为剧本的强制要求</span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {STYLE_PRESETS.map((s) => (
                      <button
                        key={s}
                        className={
                          "px-2.5 py-1 rounded-full text-[12px] border " +
                          (styles.includes(s)
                            ? "bg-accent/15 border-accent/40 text-accent font-medium"
                            : "border-black/10 dark:border-white/15 text-ink hover:border-accent/40")
                        }
                        onClick={() => toggleStyle(s)}
                      >
                        {s}
                      </button>
                    ))}
                    {styleInput.trim() && (
                      <button
                        className={
                          "px-2.5 py-1 rounded-full text-[12px] border " +
                          (styles.includes(styleInput.trim())
                            ? "bg-accent/15 border-accent/40 text-accent font-medium"
                            : "border-black/10 dark:border-white/15 text-ink")
                        }
                        onClick={() => toggleStyle(styleInput.trim())}
                      >
                        {styleInput.trim()}
                      </button>
                    )}
                    <input
                      className="w-32 rounded-full border border-black/10 dark:border-white/15 bg-transparent px-2.5 py-1 text-[12px] text-ink"
                      placeholder="自定义风格…"
                      value={styleInput}
                      onChange={(e) => setStyleInput(e.target.value)}
                    />
                  </div>
                </div>

                {/* 节奏 chips（单选） */}
                <div>
                  <div className="text-[12px] text-muted mb-1.5">叙事节奏</div>
                  <div className="flex flex-wrap gap-1.5">
                    {PACING_PRESETS.map((p) => (
                      <button
                        key={p}
                        className={
                          "px-2.5 py-1 rounded-full text-[12px] border " +
                          (pacing === p
                            ? "bg-accent/15 border-accent/40 text-accent font-medium"
                            : "border-black/10 dark:border-white/15 text-ink hover:border-accent/40")
                        }
                        onClick={() => setPacing(pacing === p ? "" : p)}
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                </div>

                {/* 集数 + 产出范围（仅剧本） */}
                {kind === "screenplay" && (
                  <div className="space-y-2">
                    <div className="flex items-center gap-4">
                      <div>
                        <div className="text-[12px] text-muted mb-1">剧本总集数</div>
                        <div className="flex items-center gap-1">
                          <input
                            type="number"
                            min={1}
                            max={24}
                            className="w-16 rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2 py-1 text-[13px] text-ink text-center"
                            value={episodes}
                            onChange={(e) => setEpisodes(Math.max(1, Math.min(24, parseInt(e.target.value || "1", 10) || 1)))}
                          />
                          <span className="text-[12px] text-muted">集</span>
                        </div>
                      </div>
                      <div className="text-[11px] text-muted flex-1">
                        {episodes > 1
                          ? "分集模式：每集独立撰写、审核、拍板；做完后可随时「续写加集」接着做"
                          : "全本模式：整本剧本一个文件"}
                      </div>
                    </div>
                    {episodes > 1 && (
                      <div>
                        <div className="text-[12px] text-muted mb-1">
                          本次只产出哪些集（可选，如 <span className="font-mono">1,3,5</span>；空=全部）
                        </div>
                        <input
                          type="text"
                          className="w-full rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2.5 py-1 text-[12px] text-ink"
                          placeholder={`1-${episodes} 中的集号，逗号分隔`}
                          value={episodeRange}
                          onChange={(e) => setEpisodeRange(e.target.value)}
                        />
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {/* 进行中：步骤条 + 制作要求 + board/分集卡片 + 产出预览 + 终审（加宽分层） */}
        {pid && snap && (
          <div className="space-y-3 max-w-4xl">
            {/* 流水线步骤条（六步状态可视化） */}
            <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-3">
              <div className="flex items-center gap-1 overflow-x-auto">
                {FLOW_STEPS.map((s, i) => {
                  const isDone = stepIdx > i || phase === "done";
                  const isActive = stepIdx === i && phase !== "done";
                  return (
                    <div key={s.label} className="flex items-center gap-1 shrink-0">
                      <div className="flex flex-col items-center">
                        <div
                          className={
                            "w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-semibold " +
                            (isDone
                              ? "bg-emerald-500/15 text-emerald-600"
                              : isActive
                                ? "bg-accent/20 text-accent ring-1 ring-accent/50"
                                : "bg-black/[0.04] dark:bg-white/[0.06] text-muted")
                          }
                        >
                          {isDone ? "✓" : i + 1}
                        </div>
                        <span className={"text-[10px] mt-0.5 whitespace-nowrap " + (isActive ? "text-accent font-medium" : "text-muted")}>
                          {s.label}
                        </span>
                        <span className="text-[9px] text-muted whitespace-nowrap">{s.who}</span>
                      </div>
                      {i < FLOW_STEPS.length - 1 && <span className="text-muted text-[11px] mb-4">→</span>}
                    </div>
                  );
                })}
              </div>
              {/* 制作要求 badge */}
              <div className="flex flex-wrap items-center gap-1.5 mt-2 pt-2 border-t border-black/5 dark:border-white/10">
                <span className="text-[11px] text-muted">制作要求：</span>
                {!snap.style?.length && !snap.pacing && (snap.episodes ?? 1) <= 1 && (
                  <span className="text-[11px] text-muted">未指定（默认创作）</span>
                )}
                {(snap.style ?? []).map((s) => (
                  <span key={s} className="px-2 py-0.5 rounded-full bg-accent/10 text-accent text-[11px]">
                    {s}
                  </span>
                ))}
                {snap.pacing && (
                  <span className="px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-700 text-[11px]">
                    节奏：{snap.pacing}
                  </span>
                )}
                {(snap.episodes ?? 1) > 1 && (
                  <span className="px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-700 text-[11px]">
                    {snap.episodes} 集
                    {snap.episode_range?.length && snap.episode_range.length < (snap.episodes ?? 1)
                      ? `（本次产出 ${snap.episode_range.join(",")}）`
                      : ""}
                  </span>
                )}
              </div>
            </div>

            <div className="text-[13px] text-ink">
              <span className="text-muted">概念：</span>
              {snap.concept}
            </div>
            {snap.novel_ref && (
              <div className="text-[13px] text-ink">
                <span className="text-muted">原著：</span>
                <span className="font-mono text-[12px]">{snap.novel_ref}</span>
              </div>
            )}

            {/* 原著索引失败提示（不阻断，按无原著继续） */}
            {snap.novel_index?.status === "failed" && (
              <div className="rounded-lg bg-amber-500/10 text-amber-700 text-[12px] px-3 py-2">
                原著索引失败：{snap.novel_index.error ?? "未知原因"}（已按无原著继续，修正路径后需重建流水线）
              </div>
            )}

            {/* 原著参考章节（阶段 5：创建时预建索引，进行中可指定创作参考章节） */}
            {snap.novel_index?.status === "ready" && (
              <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-[13px] font-semibold text-ink">原著参考章节</span>
                  <span className="text-[12px] text-muted">
                    {snap.novel_index.total_chars?.toLocaleString()} 字 · {snap.novel_index.n} 章
                    {snap.novel_index.degraded ? " · 降级索引" : " · LLM 精炼圣经"}
                  </span>
                </div>
                <div className="text-[12px] text-muted">
                  {snap.related_chapters?.length
                    ? `当前引用：${snap.related_chapters
                        .map((i) => `第${i}章${toc.get(i) ? `《${toc.get(i)}》` : ""}`)
                        .join("、")}`
                    : "当前引用：自动匹配（写剧本时按大纲主题挑 3 章）"}
                </div>
                <div className="flex items-center gap-2">
                  <input
                    className="flex-1 rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2.5 py-1.5 text-[12px] text-ink"
                    placeholder={`匹配主题（默认：${(snap.concept || "").slice(0, 12)}…）`}
                    value={pickTopic}
                    onChange={(e) => setPickTopic(e.target.value)}
                  />
                  <button className="btn" disabled={picking} onClick={matchChapters}>
                    {picking ? "匹配中…" : "重新匹配"}
                  </button>
                </div>
                {candidates.length > 0 && (
                  <div className="space-y-1.5 border-t border-black/5 dark:border-white/10 pt-2">
                    {candidates.map((c) => (
                      <label key={c.idx} className="flex items-start gap-2 text-[12px] text-ink cursor-pointer">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={selectedRelated.includes(c.idx)}
                          onChange={() => toggleRelated(c.idx)}
                        />
                        <span className="flex-1">
                          <span className="font-medium">
                            第{c.idx}章《{c.title}》
                          </span>
                          <span className="text-muted">（{c.chars.toLocaleString()} 字）</span>
                          <div className="text-muted text-[11px] truncate">{c.head}</div>
                        </span>
                      </label>
                    ))}
                    <div className="flex gap-2 justify-end pt-1">
                      <button className="btn" disabled={savingRelated} onClick={() => saveRelated([])}>
                        恢复自动
                      </button>
                      <button
                        className="btn primary"
                        disabled={savingRelated || selectedRelated.length === 0}
                        onClick={() => saveRelated(selectedRelated)}
                      >
                        设为创作参考
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 分集卡片（每集独立状态 + 操作 + 预览；宽屏两列降密度） */}
            {isEpisodic && epGroups.length > 0 && (
              <div className="space-y-2">
                {!outlineDone && (
                  <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-700 text-[12px] px-3 py-2">
                    <span className="font-medium">当前需先完成大纲：</span>请点击上方步骤条的「写大纲」，待大纲定稿后，「写第 N 集」按钮会自动亮起。
                  </div>
                )}
                <div className="flex items-center justify-between">
                  <div className="text-[13px] font-semibold text-ink">分集进度</div>
                  <div className="text-[11px] text-muted">
                    {epGroups.filter(([, g]) => g.prod?.state === "done").length}/{epGroups.length} 集已定稿
                  </div>
                </div>
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-2 items-start">
                  {epGroups.map(([ep, g]) => {
                    const btns = epActions(ep, g.prod, g.rev);
                    const prodState = g.prod?.state ?? "open";
                    return (
                      <div
                        key={ep}
                        className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-3 space-y-2"
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <span
                              className={
                                "text-[13px] font-semibold " +
                                (prodState === "done" ? "text-emerald-600" : "text-ink")
                              }
                            >
                              第{ep}集
                            </span>
                            <span className={"text-[12px] " + (STATE_COLOR[prodState] ?? "text-muted")}>
                              {STATE_LABEL[prodState] ?? prodState}
                            </span>
                            {g.rev && (
                              <span className={"text-[12px] " + (STATE_COLOR[g.rev.state] ?? "text-muted")}>
                                · 审核{STATE_LABEL[g.rev.state] ?? g.rev.state}
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-2">
                            <button
                              className="text-[12px] text-muted hover:text-accent"
                              onClick={() => g.prod && showPreview(g.prod.id)}
                            >
                              {loadingPreview === g.prod?.id ? "加载中…" : preview?.itemId === g.prod?.id ? "收起预览" : "预览"}
                            </button>
                          </div>
                        </div>
                        {btns.length > 0 && (
                          <div className="flex flex-wrap gap-2">
                            {btns.map((b) => (
                              <button
                                key={b.label}
                                className={"btn " + (b.primary ? "primary" : "")}
                                disabled={busy || b.disabled}
                                title={b.hint}
                                onClick={b.onClick}
                              >
                                {b.label}
                              </button>
                            ))}
                          </div>
                        )}
                        {preview && preview.itemId === g.prod?.id && (
                          <div className="rounded-lg bg-black/[0.02] dark:bg-white/[0.03] p-2.5 max-h-64 overflow-auto">
                            <ReactMarkdown>{preview.content}</ReactMarkdown>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* 加集续写入口（做完前 N 集后接着做，风格/节奏/前情自动继承） */}
                {outlineDone && (
                  <div>
                    {!extendOpen ? (
                      <button
                        className="w-full rounded-xl border border-dashed border-black/15 dark:border-white/20 py-2.5 text-[13px] text-muted hover:text-accent hover:border-accent/40"
                        onClick={() => {
                          setExtFrom(maxEp + 1);
                          setExtTo(maxEp + 3);
                          setExtPrior((snap?.prior_context as any) || "summary_recent");
                          setExtNotes(snap?.include_review_notes ?? true);
                          setExtendOpen(true);
                        }}
                      >
                        ＋ 续写加集（接着已有剧集继续做）
                      </button>
                    ) : (
                      <div className="rounded-xl border border-accent/30 bg-paper p-3.5 space-y-3">
                        <div className="flex items-center justify-between">
                          <span className="text-[13px] font-semibold text-ink">续写加集</span>
                          <button className="text-[12px] text-muted hover:text-ink" onClick={() => setExtendOpen(false)}>
                            收起
                          </button>
                        </div>
                        {/* 继承制作要求（只读） */}
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span className="text-[11px] text-muted">继承制作要求：</span>
                          {(snap?.style ?? []).map((s) => (
                            <span key={s} className="px-2 py-0.5 rounded-full bg-accent/10 text-accent text-[11px]">
                              {s}
                            </span>
                          ))}
                          {snap?.pacing && (
                            <span className="px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-700 text-[11px]">
                              节奏：{snap.pacing}
                            </span>
                          )}
                          <span className="text-[11px] text-muted">（自动延续，无需重选）</span>
                        </div>
                        {/* 续写范围 */}
                        <div className="flex items-center gap-3">
                          <span className="text-[12px] text-muted">续写范围</span>
                          <div className="flex items-center gap-1">
                            <span className="text-[12px] text-muted">第</span>
                            <input
                              type="number"
                              min={maxEp + 1}
                              max={99}
                              className="w-16 rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2 py-1 text-[13px] text-ink text-center"
                              value={extFrom}
                              onChange={(e) => setExtFrom(Math.max(1, parseInt(e.target.value || "1", 10) || 1))}
                            />
                            <span className="text-[12px] text-muted">至</span>
                            <input
                              type="number"
                              min={extFrom}
                              max={99}
                              className="w-16 rounded-lg border border-black/10 dark:border-white/15 bg-transparent px-2 py-1 text-[13px] text-ink text-center"
                              value={extTo}
                              onChange={(e) => setExtTo(Math.max(extFrom, parseInt(e.target.value || "1", 10) || 1))}
                            />
                            <span className="text-[12px] text-muted">集</span>
                          </div>
                        </div>
                        {/* 前情上下文档位 */}
                        <div className="space-y-1.5">
                          <div className="text-[12px] text-muted">前情上下文（新集动笔时注入，续写衔接）</div>
                          <label className="flex items-center gap-2 text-[12px] text-ink cursor-pointer">
                            <input
                              type="radio"
                              checked={extPrior === "summary_recent"}
                              onChange={() => setExtPrior("summary_recent")}
                            />
                            前集摘要 + 紧邻上集定稿全文
                            <span className="text-[11px] text-muted">（推荐，衔接最准）</span>
                          </label>
                          <label className="flex items-center gap-2 text-[12px] text-ink cursor-pointer">
                            <input
                              type="radio"
                              checked={extPrior === "summary"}
                              onChange={() => setExtPrior("summary")}
                            />
                            仅前集摘要
                            <span className="text-[11px] text-muted">（每集约150字，省 token）</span>
                          </label>
                          <label className="flex items-center gap-2 text-[12px] text-ink cursor-pointer">
                            <input type="checkbox" checked={extNotes} onChange={(e) => setExtNotes(e.target.checked)} />
                            附带历史审改意见
                            <span className="text-[11px] text-muted">（审核评语/打回理由，续写不重蹈覆辙）</span>
                          </label>
                        </div>
                        <div className="flex gap-2 justify-end">
                          <button className="btn" onClick={() => setExtendOpen(false)}>
                            取消
                          </button>
                          <button className="btn primary" disabled={extBusy} onClick={doExtend}>
                            {extBusy ? "加集中…" : `确认加集（第${extFrom}-${extTo}集）`}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* 非分集：board 项状态 + 全局阶段按钮 */}
            {!isEpisodic && (
              <>
                <div className="space-y-1.5">
                  {snap.items.map((it) => (
                    <div
                      key={it.id}
                      className="flex items-center gap-2 text-[13px] rounded-lg bg-black/[0.02] dark:bg-white/[0.03] px-3 py-2"
                    >
                      <span className="text-ink flex-1">{it.title}</span>
                      <span className={"text-[12px] font-medium " + (STATE_COLOR[it.state] ?? "text-muted")}>
                        {STATE_LABEL[it.state] ?? it.state}
                      </span>
                      <button
                        className="text-[12px] text-muted hover:text-accent"
                        onClick={() => showPreview(it.id)}
                      >
                        {loadingPreview === it.id ? "加载中…" : preview?.itemId === it.id ? "收起预览" : "预览"}
                      </button>
                    </div>
                  ))}
                  {preview && (
                    <div className="rounded-lg bg-black/[0.02] dark:bg-white/[0.03] p-2.5 max-h-64 overflow-auto">
                      <ReactMarkdown>{preview.content}</ReactMarkdown>
                    </div>
                  )}
                </div>

                {/* 阶段推进按钮（快速路径） */}
                <div className="flex flex-wrap gap-2">
                  {phase === "draft_outline" && (
                    <button className="btn primary" disabled={busy} onClick={() => run(() => draftOutline(pid))}>
                      写大纲
                    </button>
                  )}
                  {phase === "review_outline" && (
                    <button className="btn" disabled={busy} onClick={() => run(() => reviewOutline(pid))}>
                      审大纲
                    </button>
                  )}
                  {phase === "lead_outline" && (
                    <>
                      <button className="btn" disabled={busy} onClick={() => run(() => leadOutline(pid, true))}>
                        单点审核通过定稿
                      </button>
                      <button className="btn primary" disabled={busy} onClick={() => openProposal("outline")}>
                        送圆桌终审
                      </button>
                    </>
                  )}
                  {phase === "draft_screen" && isScreenplay && (
                    <button
                      className="btn primary"
                      disabled={busy || !outlineDone}
                      title={!outlineDone ? "需先完成大纲定稿（点上方「写大纲」）" : undefined}
                      onClick={() => run(() => draftScreen(pid))}
                    >
                      写剧本
                    </button>
                  )}
                  {phase === "review_screen" && (
                    <button className="btn" disabled={busy} onClick={() => run(() => reviewScreen(pid))}>
                      审剧本
                    </button>
                  )}
                  {phase === "lead_screen" && (
                    <>
                      <button className="btn" disabled={busy} onClick={() => run(() => leadScreen(pid, true))}>
                        单点审核通过定稿
                      </button>
                      <button className="btn primary" disabled={busy} onClick={() => openProposal("screenplay")}>
                        送圆桌终审
                      </button>
                    </>
                  )}
                  {phase === "done" && <div className="text-[13px] text-emerald-600 font-medium">✅ 已定稿</div>}
                </div>
              </>
            )}

            {/* 终审提案卡 */}
            {proposal && !synthesis && (
              <ProposalCard
                proposal={proposal}
                selected={selected}
                onToggle={toggleExpert}
                onApprove={() => runReview()}
                onCancel={() => {
                  setProposal(null);
                  setSelected([]);
                }}
              />
            )}
            {proposal && reviewing && (
              <div className="text-[12px] text-muted">圆桌研讨中…（{events.length} 事件）</div>
            )}

            {/* 终审 SSE 事件流（圆桌群聊） */}
            {events.length > 0 && (
              <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-3 space-y-2 max-h-80 overflow-auto">
                {events.map((ev, i) => {
                  if (ev.type === "speaking")
                    return (
                      <div key={i} className="text-[12px] text-muted">
                        {ev.speaker} 思考中…
                      </div>
                    );
                  if (ev.type === "turn_end" || ev.type === "turn")
                    return (
                      <div key={i} className="text-[13px]">
                        <span className="font-medium text-accent">{ev.speaker}：</span>
                        <span className="text-ink whitespace-pre-wrap">{ev.text}</span>
                      </div>
                    );
                  if (ev.type === "info")
                    return (
                      <div key={i} className="text-[12px] text-muted">
                        {ev.text}
                      </div>
                    );
                  return null;
                })}
              </div>
            )}

            {/* 终审结论 + 确认 */}
            {synthesis && (
              <div className="rounded-xl border border-accent/30 bg-accent-soft/40 p-3 space-y-2">
                <div className="text-[13px] font-semibold text-ink">圆桌《综合研判报告》</div>
                <div className="text-[13px] text-ink max-h-72 overflow-auto prose prose-sm">
                  <ReactMarkdown>{synthesis}</ReactMarkdown>
                </div>
                <div className="flex gap-2 justify-end">
                  <button className="btn" disabled={finalizing} onClick={() => apply(false)}>
                    打回修改
                  </button>
                  <button className="btn primary" disabled={finalizing} onClick={() => apply(true)}>
                    确认定稿
                  </button>
                </div>
              </div>
            )}

            {/* 定稿 docx 路径 */}
            {docxPath && (
              <div className="rounded-lg bg-emerald-500/10 text-emerald-700 text-[13px] px-3 py-2">
                定稿已落地：{docxPath}
              </div>
            )}
            {snap.docx && Object.keys(snap.docx).length > 0 && !docxPath && (
              <div className="text-[12px] text-muted">
                已导出：{Object.entries(snap.docx).map(([k, v]) => `${k} → ${v}`).join("；")}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
