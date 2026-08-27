// 创作流水线视图（线路 B 原生 teams 执行 + 线路 A 圆桌终审）。
// 全程驱动后端 deskmate.creation.service：创建 → 写/审大纲 → 主编拍板 →
// 写/审剧本 → 主编拍板 → 终审定稿（知情批准 · SSE 流式圆桌研讨 → docx 落地）。
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { Icon } from "./Icon";
import { ProposalCard, type FinalProposal } from "./ProposalCard";
import {
  createPipeline,
  getPipeline,
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
  type FinalReviewEvent,
} from "../api";

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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [proposal, setProposal] = useState<FinalProposal | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewing, setReviewing] = useState(false);
  const [events, setEvents] = useState<any[]>([]);
  const [synthesis, setSynthesis] = useState("");
  const [finalizing, setFinalizing] = useState(false);
  const [docxPath, setDocxPath] = useState("");

  const refresh = (id?: string) => {
    const use = id ?? pid;
    if (!use) return;
    getPipeline(use)
      .then(setSnap)
      .catch((e) => setError(String(e?.message ?? e)));
  };

  const start = async () => {
    if (!concept.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const r = await createPipeline(kind, concept, executor, novelRef);
      setPid(r.pid);
      await refresh(r.pid);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const step = async (fn: (p: string) => Promise<CreationSnapshot>) => {
    if (!pid || busy) return;
    setBusy(true);
    setError("");
    try {
      const s = await fn(pid);
      setSnap(s);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const openProposal = async (itemKind: "outline" | "screenplay") => {
    if (!pid) return;
    setError("");
    try {
      const p = await finalReviewProposal(pid, itemKind);
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
      for await (const ev of streamFinalReview(pid, proposal.item_kind as any, selected)) {
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
      const r = await finalReviewApply(pid, proposal.item_kind as any, verdict);
      setProposal(null);
      setSynthesis("");
      setEvents([]);
      const p = r.docx?.[proposal.item_kind];
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

        {/* 未开始：创建表单 */}
        {!pid && (
          <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-4 space-y-3 max-w-xl">
            <div className="text-[13px] font-semibold text-ink">新建创作项目</div>
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
              开始创作
            </button>
          </div>
        )}

        {/* 进行中：board 状态 + 阶段按钮 */}
        {pid && snap && (
          <div className="space-y-3">
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

            {/* board 项状态 */}
            <div className="space-y-1.5">
              {snap.items.map((it) => (
                <div
                  key={it.id}
                  className="flex items-center gap-2 text-[13px] rounded-lg bg-black/[0.02] dark:bg-white/[0.03] px-3 py-2"
                >
                  <span className="text-ink flex-1">{it.title}</span>
                  <span className={"text-[12px] font-medium " + (STATE_COLOR[it.state] ?? "text-muted")}>
                    {it.state}
                  </span>
                </div>
              ))}
            </div>

            {/* 阶段推进按钮 */}
            <div className="flex flex-wrap gap-2">
              {phase === "draft_outline" && (
                <button className="btn primary" disabled={busy} onClick={() => step(draftOutline)}>
                  写大纲
                </button>
              )}
              {phase === "review_outline" && (
                <button className="btn" disabled={busy} onClick={() => step(reviewOutline)}>
                  审大纲
                </button>
              )}
              {phase === "lead_outline" && (
                <>
                  <button className="btn" disabled={busy} onClick={() => step((p) => leadOutline(p, true))}>
                    单点审核通过定稿
                  </button>
                  <button className="btn primary" disabled={busy} onClick={() => openProposal("outline")}>
                    送圆桌终审
                  </button>
                </>
              )}
              {phase === "draft_screen" && isScreenplay && (
                <button className="btn primary" disabled={busy} onClick={() => step(draftScreen)}>
                  写剧本
                </button>
              )}
              {phase === "review_screen" && (
                <button className="btn" disabled={busy} onClick={() => step(reviewScreen)}>
                  审剧本
                </button>
              )}
              {phase === "lead_screen" && (
                <>
                  <button className="btn" disabled={busy} onClick={() => step((p) => leadScreen(p, true))}>
                    单点审核通过定稿
                  </button>
                  <button className="btn primary" disabled={busy} onClick={() => openProposal("screenplay")}>
                    送圆桌终审
                  </button>
                </>
              )}
              {phase === "done" && <div className="text-[13px] text-emerald-600 font-medium">✅ 已定稿</div>}
            </div>

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
