// The Team View — ported from upstream's TeamView.tsx, trimmed to what this fork's
// data model can answer honestly.
//
// What it is: a workbench you stay on. Three tabs over ONE read model (the team
// summary): `work` groups tasks by what they need FROM THE HUMAN (等你 / 待审 /
// 进行中 / 排队 / 完成), `workers` lists who is running what right now, `stats`
// shows where the tokens and the time went.
//
// Deliberate divergences from upstream:
//  - No embedded worker conversation pane. This fork's Composer/Transcript carry
//    different approval plumbing (inbox cards, not `onApprove`), so re-wiring them
//    inside a second pane would fork the transcript twice. Clicking a worker opens
//    their real session instead — one click, zero duplicated rendering.
//  - Task detail reuses BoardPanel's ItemDetail (exported for this) so the rail and
//    the team view can never drift into two verdict action sets.
//  - Falls back to a plain grouped list when the summary is unavailable (same
//    degrade path upstream has), so a stale/older backend still renders.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  boardComment,
  boardTransition,
  fetchBoardAttachment,
  getBoardItem,
  getTeamSummary,
  type BoardItemDetail,
} from "../api";
import {
  GROUPS,
  tokenTotal,
  type TeamSummary,
  type TeamTask,
  type TeamWorker,
} from "../teamView";
import { formatTokens } from "../usage";
import { ItemDetail } from "./BoardPanel";

const POLL_MS = 3000;

export function TeamView({
  sessionId,
  teamId,
  onOpenSession,
  onClose,
}: {
  sessionId: string;
  teamId: string;
  onOpenSession: (sessionId: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [summary, setSummary] = useState<TeamSummary | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<"work" | "workers" | "stats">("work");
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<BoardItemDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [showDone, setShowDone] = useState(false);

  // One read model, polled while open. The endpoint is side-effect free by contract
  // (no wake, no cursor advance), so a 3s poll is safe even with a team mid-run.
  useEffect(() => {
    if (!teamId) return;
    let alive = true;
    const load = async () => {
      try {
        const next = await getTeamSummary(teamId);
        if (!alive) return;
        if ("error" in next) {
          setError(next.error);
          setSummary(null);
        } else {
          setSummary(next);
          setError("");
        }
      } catch {
        if (alive) setError(t("teamview.summary_unavailable"));
      }
    };
    void load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [teamId, t]);

  // Task detail — same loader the board rail uses (session-scoped, acts as the user).
  useEffect(() => {
    if (openId == null) {
      setDetail(null);
      setDetailError("");
      return;
    }
    let alive = true;
    setDetail(null);
    setDetailError("");
    void (async () => {
      const next = await getBoardItem(sessionId, openId);
      if (!alive) return;
      if ("error" in next) setDetailError(next.error);
      else setDetail(next);
    })();
    return () => {
      alive = false;
    };
  }, [openId, sessionId]);

  const refreshDetail = useCallback(async () => {
    if (openId == null) return;
    const next = await getBoardItem(sessionId, openId);
    if ("error" in next) setDetailError(next.error);
    else setDetail(next);
  }, [openId, sessionId]);

  const move = useCallback(
    async (item: number, to: string, comment?: string) => {
      const next = await boardTransition(sessionId, item, to, comment || "");
      if ("error" in next) setDetailError(next.error);
      else await refreshDetail();
    },
    [refreshDetail, sessionId],
  );

  const note = useCallback(
    async (item: number, body: string) => {
      const next = await boardComment(sessionId, item, body);
      if (next.error) setDetailError(next.error);
      else await refreshDetail();
    },
    [refreshDetail, sessionId],
  );

  const loadAttachment = useCallback(
    (stored: string) => fetchBoardAttachment(sessionId, stored),
    [sessionId],
  );

  const items = summary?.items ?? [];
  const byGroup = useMemo(() => {
    const map: Record<string, TeamTask[]> = {};
    for (const item of items) (map[item.group] ||= []).push(item);
    return map;
  }, [items]);

  if (!teamId) {
    return (
      <section className="flex-1 min-h-0 flex flex-col" data-testid="team-view">
        <TeamHeader tab={tab} onTab={setTab} onClose={onClose} waiting={0} hasSummary={false} />
        <div className="flex-1 grid place-items-center p-10">
          <div className="max-w-md text-center">
            <p className="text-ui font-medium mb-2">{t("teamview.no_team_title")}</p>
            <p className="text-meta text-[var(--muted)] leading-relaxed">
              {t("teamview.no_team_body")}
            </p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="flex-1 min-h-0 flex flex-col" data-testid="team-view">
      <TeamHeader
        tab={tab}
        onTab={setTab}
        onClose={onClose}
        waiting={summary?.counts?.waiting || 0}
        hasSummary={!!summary}
      />
      <div className="flex-1 min-h-0 overflow-auto px-5 py-4">
        {openId != null ? (
          <div>
            <button
              className="text-meta text-[var(--muted)] hover:text-[var(--ink)] mb-3"
              onClick={() => setOpenId(null)}
            >
              ← {t("teamview.title")}
            </button>
            {detailError && (
              <p className="text-meta text-[var(--danger)] mb-2" role="alert">
                {detailError}
              </p>
            )}
            {detail ? (
              <ItemDetail
                detail={detail}
                onTransition={move}
                onAddNote={note}
                loadAttachment={loadAttachment}
              />
            ) : (
              !detailError && <p className="text-meta text-[var(--muted)]">{t("teamview.loading")}</p>
            )}
          </div>
        ) : !summary ? (
          <p className="text-meta text-[var(--muted)]" role="status">
            {error || t("teamview.loading")}
          </p>
        ) : tab === "work" ? (
          <>
            <h2 className="text-title font-semibold tracking-tight mb-1">
              <Headline count={summary.counts.waiting} asks={summary.totals.asks_waiting} />
            </h2>
            <p className="text-meta text-[var(--muted)] mb-4">
              <Totals summary={summary} />
            </p>
            {!!summary.pending_requests?.length && (
              <PendingRequests
                summary={summary}
                onOpenSession={onOpenSession}
                onOpenTask={(id) => setOpenId(id)}
              />
            )}
            {!summary.totals.total && (
              <p className="text-meta text-[var(--muted)]">{t("teamview.no_tasks")}</p>
            )}
            {GROUPS.map((group) => {
              const rows = byGroup[group] || [];
              if (!rows.length) return null;
              if (group === "done") {
                return (
                  <div key={group} className="mt-4">
                    <button
                      className="text-ui text-[var(--muted)] hover:text-[var(--ink)]"
                      onClick={() => setShowDone((v) => !v)}
                    >
                      {showDone ? "▾" : "▸"} {t("teamview.state_done")}
                      <span className="text-label ml-1 text-[var(--faint)]">{rows.length}</span>
                    </button>
                    {showDone && (
                      <div className="mt-1">
                        {rows.map((item) => (
                          <TaskRow key={item.id} item={item} onOpen={setOpenId} />
                        ))}
                      </div>
                    )}
                  </div>
                );
              }
              return (
                <div key={group} className="mt-4">
                  <div className="board-group flex items-center gap-2">
                    {t("teamview.state_" + group)}
                    <span className="text-label text-[var(--faint)]">{rows.length}</span>
                  </div>
                  {rows.map((item) => (
                    <TaskRow key={item.id} item={item} onOpen={setOpenId} />
                  ))}
                </div>
              );
            })}
          </>
        ) : tab === "workers" ? (
          <>
            <h2 className="text-title font-semibold tracking-tight mb-1">
              {t("teamview.workers_count", { count: summary.workers.length })}
            </h2>
            <p className="text-meta text-[var(--muted)] mb-4">
              <Totals summary={summary} />
            </p>
            <div className="flex flex-col">
              {[summary.lead, ...summary.workers].map((worker) => (
                <WorkerRow
                  key={worker.session_id}
                  worker={worker}
                  items={items.filter((i) => worker.items.includes(i.id))}
                  lead={worker.session_id === summary.lead.session_id}
                  onOpenSession={onOpenSession}
                  onOpenTask={setOpenId}
                />
              ))}
            </div>
          </>
        ) : (
          <Stats summary={summary} />
        )}
      </div>
    </section>
  );
}

function TeamHeader({
  tab,
  onTab,
  onClose,
  waiting,
  hasSummary,
}: {
  tab: "work" | "workers" | "stats";
  onTab: (tab: "work" | "workers" | "stats") => void;
  onClose: () => void;
  waiting: number;
  hasSummary: boolean;
}) {
  const { t } = useTranslation();
  const tabs = (["work", "workers", "stats"] as const).filter(
    (name) => hasSummary || name === "work",
  );
  return (
    <header className="flex items-center gap-2 border-b border-[var(--line)] px-4 py-2 min-h-[48px]">
      <div className="flex items-center gap-1" role="tablist">
        {tabs.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            onClick={() => onTab(name)}
            className={
              "text-ui rounded-md px-2.5 py-1.5 " +
              (tab === name
                ? "bg-[var(--accent-soft)] text-[var(--ink)] font-medium"
                : "text-[var(--muted)] hover:text-[var(--ink)]")
            }
          >
            {t("teamview." + name)}
            {name === "work" && waiting > 0 && (
              <span className="ml-1.5 rounded-full bg-[var(--warn-soft)] px-1.5 text-label text-[var(--warn-ink)]">
                {waiting}
              </span>
            )}
          </button>
        ))}
      </div>
      <button
        className="ml-auto text-ui text-[var(--muted)] hover:text-[var(--ink)] px-2 py-1"
        onClick={onClose}
        aria-label={t("teamview.close")}
      >
        ×
      </button>
    </header>
  );
}

function Headline({ count, asks = 0 }: { count: number; asks?: number }) {
  const { t } = useTranslation();
  if (count) return <>{t("teamview.waiting_count", { count })}</>;
  if (asks) return <>{t("teamview.answer_needed")}</>;
  return <>{t("teamview.nothing_waiting")}</>;
}

function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s >= 3600) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m ? `${h}h ${m}m` : `${h}h`;
  }
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${s}s`;
}

function Totals({ summary }: { summary: TeamSummary }) {
  const { t } = useTranslation();
  const totals = summary.totals;
  return (
    <>
      {t("teamview.done_count", { done: totals.done, total: totals.total })} ·{" "}
      {duration(totals.elapsed_s)} ·{" "}
      {t("teamview.tokens_count", { value: formatTokens(totals.tokens) })}
      {totals.asks_answered + totals.asks_waiting > 0 && (
        <>
          {" · "}
          {t("teamview.asks_count", {
            count: totals.asks_answered + totals.asks_waiting,
          })}
          {totals.asks_waiting > 0 && " " + t("teamview.asks_waiting", { count: totals.asks_waiting })}
        </>
      )}
    </>
  );
}

function dot(state: string, waiting: boolean): string {
  return (
    "board-dot " +
    (waiting || state === "blocked"
      ? "blocked"
      : state === "in_progress"
        ? "work"
        : state === "review"
          ? "review"
          : state === "done"
            ? "done"
            : "idle")
  );
}

function TaskRow({
  item,
  onOpen,
  now,
}: {
  item: TeamTask;
  onOpen: (id: number) => void;
  now?: number;
}) {
  const { t } = useTranslation();
  // What the row says when the title no longer tells the whole story: the pending ask
  // wins (it is the human's turn), then a blocker, then the worker's own status line.
  const line =
    item.waiting?.preview ||
    (item.group === "waiting" ? item.blocker : "") ||
    item.status ||
    t("teamview.state_" + item.group);
  const clock = now ?? Date.now() / 1000;
  return (
    <button
      type="button"
      className="board-row w-full"
      onClick={() => onOpen(item.id)}
      data-testid={`team-task-${item.id}`}
    >
      <span className={dot(item.state, item.group === "waiting")} />
      <span className="board-row-main">
        <span className="board-row-title">{item.title}</span>
        <span
          className={
            "text-label truncate " +
            (item.group === "waiting" ? "text-[var(--warn-ink)]" : "text-[var(--muted)]")
          }
        >
          {line}
        </span>
      </span>
      {item.assignee && <span className="board-row-who">{item.assignee}</span>}
      {item.step && (
        <span className="text-label text-[var(--muted)]">
          {Math.min(item.step.done + 1, item.step.total)}/{item.step.total}
        </span>
      )}
      {!!item.updated && (
        <time
          className="text-label text-[var(--faint)] whitespace-nowrap"
          title={new Date(item.updated * 1000).toLocaleString()}
        >
          {duration(Math.max(0, clock - item.updated))}
        </time>
      )}
    </button>
  );
}

function PendingRequests({
  summary,
  onOpenSession,
  onOpenTask,
}: {
  summary: TeamSummary;
  onOpenSession: (sessionId: string) => void;
  onOpenTask: (id: number) => void;
}) {
  const { t } = useTranslation();
  const rows = summary.pending_requests || [];
  return (
    <div className="mb-4 rounded-lg border border-[var(--warn-soft)] bg-[var(--panel)] px-3 py-2">
      <div className="text-ui font-medium text-[var(--warn-ink)] mb-1">
        {t("teamview.pending_requests_other", { count: rows.length })}
      </div>
      {rows.map((row) => {
        const task = summary.items.find((i) => i.assignee === row.worker);
        return (
          <button
            key={row.id}
            className="flex w-full items-center gap-2 py-1 text-left text-meta"
            onClick={() =>
              task ? onOpenTask(task.id) : onOpenSession(row.session_id)
            }
          >
            <span className="text-[var(--warn-ink)]">{row.worker}</span>
            <span className="truncate text-[var(--ink)]">{row.title}</span>
            <span className="ml-auto text-label text-[var(--faint)]">
              {task ? t("teamview.task_details") : t("teamview.full_session")}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function WorkerRow({
  worker,
  items,
  lead,
  onOpenSession,
  onOpenTask,
}: {
  worker: TeamWorker;
  items: TeamTask[];
  lead: boolean;
  onOpenSession: (sessionId: string) => void;
  onOpenTask: (id: number) => void;
}) {
  const { t } = useTranslation();
  const active = items.filter((i) => i.group !== "done" && i.group !== "canceled");
  const current = active[0];
  return (
    <div className="border-b border-[var(--line)] py-2">
      <button
        className="board-row w-full"
        onClick={() => onOpenSession(worker.session_id)}
        title={t("teamview.full_session")}
      >
        <span className={dot(current?.state || "open", worker.running && !current)} />
        <span className="board-row-main">
          <span className="board-row-title">
            {worker.actor} <span className="text-label text-[var(--faint)]">· {worker.role}</span>
          </span>
          <span className="text-label truncate text-[var(--muted)]">
            {current
              ? [current.title, current.status].filter(Boolean).join(" · ")
              : t("teamview.idle")}
          </span>
        </span>
        {worker.running && (
          <span className="text-label text-[var(--accent)]">{t("teamview.state_working")}</span>
        )}
        <span className="text-label text-[var(--faint)]">
          {formatTokens(tokenTotal(worker.tokens))}
        </span>
        {lead && <span className="text-label text-[var(--faint)]">{t("teamview.lead")}</span>}
      </button>
      {active.length > 1 && (
        <div className="pl-6 flex flex-wrap gap-1 pt-1">
          {active.slice(1).map((item) => (
            <button
              key={item.id}
              className="text-label rounded-full border border-[var(--line)] px-2 py-0.5 text-[var(--muted)] hover:text-[var(--ink)]"
              onClick={() => onOpenTask(item.id)}
            >
              #{item.id} {item.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Stats({ summary }: { summary: TeamSummary }) {
  const { t } = useTranslation();
  const rows = [...summary.workers, summary.lead]
    .map((w) => ({ label: w.actor, value: tokenTotal(w.tokens) }))
    .sort((a, b) => b.value - a.value);
  const max = Math.max(1, ...rows.map((r) => r.value));
  const timing = summary.timing;
  const totalMs = timing
    ? timing.model_ms + timing.tool_ms + timing.waited_ms + timing.queued_ms
    : 0;
  const share = (ms: number) => (totalMs ? Math.round((ms / totalMs) * 100) : 0);
  return (
    <>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 mb-5">
        <Stat label={t("teamview.tokens")} value={formatTokens(summary.totals.tokens)} />
        <Stat
          label={t("teamview.elapsed")}
          value={duration(summary.totals.elapsed_s)}
        />
        <Stat
          label={t("teamview.needed_you")}
          value={String(summary.totals.asks_answered + summary.totals.asks_waiting)}
        />
        <Stat
          label={t("teamview.tasks")}
          value={`${summary.totals.done} / ${summary.totals.total}`}
        />
      </div>

      <section className="mb-6">
        <h3 className="text-ui font-medium mb-2">{t("teamview.where_tokens")}</h3>
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-2 py-1 text-meta">
            <span className="w-24 truncate text-[var(--ink)]">{row.label}</span>
            <span className="flex-1 h-1.5 rounded-full bg-[var(--panel)] overflow-hidden">
              <i
                className="block h-full bg-[var(--accent)]"
                style={{ width: `${(row.value / max) * 100}%` }}
              />
            </span>
            <span className="w-14 text-right text-[var(--muted)]">
              {formatTokens(row.value)}
            </span>
          </div>
        ))}
      </section>

      {timing && totalMs > 0 && (
        <section>
          <h3 className="text-ui font-medium mb-2">{t("teamview.where_time")}</h3>
          <div className="flex h-2 w-full overflow-hidden rounded-full">
            <span className="bg-[var(--accent)]" style={{ width: `${share(timing.model_ms)}%` }} />
            <span className="bg-[var(--teal-soft)]" style={{ width: `${share(timing.tool_ms)}%` }} />
            <span className="bg-[var(--warn-soft)]" style={{ width: `${share(timing.waited_ms)}%` }} />
            <span className="bg-[var(--line-strong)]" style={{ width: `${share(timing.queued_ms)}%` }} />
          </div>
          <p className="mt-2 text-label text-[var(--muted)]">
            {t("teamview.model_ms")} {share(timing.model_ms)}% · {t("teamview.tool_ms")}{" "}
            {share(timing.tool_ms)}% · {t("teamview.waited_ms")} {share(timing.waited_ms)}% ·{" "}
            {t("teamview.queued_ms")} {share(timing.queued_ms)}%
          </p>
          {summary.median_answer_s ? (
            <p className="mt-1 text-label text-[var(--muted)]">
              {t("teamview.median_answer", { value: duration(summary.median_answer_s) })}
            </p>
          ) : null}
        </section>
      )}
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-[var(--line)] px-3 py-2">
      <div className="text-label text-[var(--muted)]">{label}</div>
      <div className="text-heading font-semibold">{value}</div>
    </div>
  );
}
