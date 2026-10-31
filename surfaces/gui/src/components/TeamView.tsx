// The Team View — ported from upstream's TeamView.tsx.
//
// What it is: a workbench you stay on. Three tabs over ONE read model (the team
// summary): `work` groups tasks by what they need FROM THE HUMAN (等你 / 待审 /
// 进行中 / 排队 / 完成), `workers` is a searchable roster, `stats` shows where the
// tokens and the time went (per worker, per role, over time).
//
// 2026-10-31: the first port was trimmed to ~47% of upstream AND none of upstream's
// `.team-*` design system came along, so the panel rendered as bare text lines with
// no cards, no status pills and no hierarchy. This is the full thing — upstream's
// task rows, metric bars, stat cards, roster (search + paging), pending requests and
// task detail pane — with upstream's stylesheet (see `styles.css`, "team view").
//
// Deliberate divergences from upstream (kept on purpose):
//  - No embedded worker conversation pane. This fork's Composer/Transcript carry
//    different approval plumbing (inbox cards, not `onApprove`), so re-wiring them
//    inside a second pane would fork the transcript twice. Clicking a worker opens
//    their real session instead — one click, zero duplicated rendering.
//  - Task detail reuses BoardPanel's ItemDetail (exported for this) so the rail and
//    the team view can never drift into two verdict action sets.
//  - Falls back to a plain grouped list when the summary is unavailable (same
//    degrade path upstream has), so a stale/older backend still renders.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  boardComment,
  boardTransition,
  fetchBoardAttachment,
  getBoardItem,
  getInbox,
  getTeamSummary,
  resolveInboxItem,
  type BoardItemDetail,
  type InboxItem,
} from "../api";
import {
  GROUPS,
  tokenTotal,
  type TeamSummary,
  type TeamTask,
  type TeamWorker,
  type Timing,
  type TokenKinds,
} from "../teamView";
import { formatTokens } from "../usage";
import { ItemDetail } from "./BoardPanel";
import { Icon } from "./Icon";
import { InboxItemCard } from "./InboxItemCard";
import { taskDot } from "./TaskChip";
import { WorkerSearch } from "./TeamRail";
import {
  teamRoster,
  filterWorkers,
  type WorkerFilter,
} from "../teamRoster";

const POLL_MS = 3000;
const ROSTER_PAGE = 8;

export const minutes = (seconds: number) =>
  Math.max(0, Math.round(seconds / 60));

function Duration({ seconds }: { seconds: number }) {
  const { t } = useTranslation();
  if (seconds >= 3600)
    return (
      <>
        {t("teamview.hours_minutes", {
          hours: Math.floor(seconds / 3600),
          minutes: Math.floor((seconds % 3600) / 60),
        })}
      </>
    );
  return (
    <>
      {seconds < 60
        ? t("teamview.seconds", { count: Math.round(seconds) })
        : t("teamview.minutes", { count: minutes(seconds) })}
    </>
  );
}

function TeamTotals({ summary: s }: { summary: TeamSummary }) {
  const { t } = useTranslation();
  return (
    <p className="team-totals">
      {t("teamview.done_count", { done: s.totals.done, total: s.totals.total })}{" "}
      · <Duration seconds={s.totals.elapsed_s} /> ·{" "}
      {t("teamview.tokens_count", {
        count: s.totals.tokens,
        value: formatTokens(s.totals.tokens),
      })}{" "}
      ·{" "}
      {t("teamview.asks_count", {
        count: s.totals.asks_answered + s.totals.asks_waiting,
      })}
      {s.totals.asks_waiting > 0 &&
        " " + t("teamview.asks_waiting", { count: s.totals.asks_waiting })}
    </p>
  );
}

function Headline({ count, asks = 0 }: { count: number; asks?: number }) {
  const { t } = useTranslation();
  return (
    <>
      {count
        ? t("teamview.waiting_count", { count })
        : asks
          ? t("teamview.answer_needed")
          : t("teamview.nothing_waiting")}
    </>
  );
}

export function TaskRow({
  item,
  onOpen,
  machine,
  now = Date.now() / 1000,
}: {
  item: TeamTask;
  onOpen: (id: number) => void;
  machine?: string;
  now?: number;
}) {
  const { t } = useTranslation();
  const line =
    item.waiting?.preview ||
    (item.group === "waiting" ? item.blocker : "") ||
    item.status ||
    t("teamview.state_" + item.group);
  return (
    <button
      type="button"
      className="team-task-row"
      onClick={() => onOpen(item.id)}
      data-testid={`team-task-${item.id}`}
    >
      <span className={taskDot(item.state, item.group === "waiting")} />
      <span className="team-task-main">
        <span className="team-task-title">{item.title}</span>
        <span
          className={
            "team-task-status" +
            (item.group === "waiting" ? " text-warnInk" : "")
          }
        >
          {line}
        </span>
      </span>
      {item.assignee && (
        <span className="team-worker-chip">
          {item.assignee}
          <small>{machine || t("teamview.this_machine")}</small>
        </span>
      )}
      {item.step && (
        <span className="team-pill">
          {Math.min(item.step.done + 1, item.step.total)}/{item.step.total}
        </span>
      )}
      {item.pr && <span className="team-pill">PR</span>}
      <time
        title={
          item.updated
            ? new Date(item.updated * 1000).toLocaleString()
            : undefined
        }
      >
        {item.updated ? (
          <Duration seconds={Math.max(0, now - item.updated)} />
        ) : (
          "—"
        )}
      </time>
    </button>
  );
}

const TIME_KEYS: (keyof Timing)[] = [
  "model_ms",
  "tool_ms",
  "waited_ms",
  "queued_ms",
];
const TOKEN_KEYS: (keyof TokenKinds)[] = [
  "input",
  "cache_read",
  "output",
  "cache_write",
];

function MetricBar({
  values,
  kind,
  partial,
}: {
  values?: Timing | TokenKinds | null;
  kind: "time" | "tokens";
  partial?: boolean;
}) {
  const { t } = useTranslation();
  if (!values)
    return <p className="team-totals">{t("teamview.timing_unavailable")}</p>;
  const keys = kind === "time" ? TIME_KEYS : TOKEN_KEYS;
  const rows = keys.map((k) => ({
    key: k,
    value: Math.max(
      0,
      Number((values as unknown as Record<string, number>)[k]) || 0,
    ),
  }));
  const total = rows.reduce((n, r) => n + r.value, 0);
  return (
    <div className={"team-metric " + kind}>
      <div className="team-metric-bar" aria-hidden="true">
        {rows.map((r, i) => (
          <span
            key={r.key}
            className={`segment-${i}`}
            style={{ width: `${total ? (r.value / total) * 100 : 0}%` }}
          />
        ))}
      </div>
      <div className="team-legend">
        {rows.map((r, i) => (
          <span key={r.key}>
            <i className={`segment-${i}`} />
            {t("teamview." + r.key)}{" "}
            {kind === "tokens" ? (
              formatTokens(r.value)
            ) : (
              <Duration seconds={r.value / 1000} />
            )}
          </span>
        ))}
      </div>
      {partial && (
        <p className="team-totals">
          {t(
            kind === "time"
              ? "teamview.timing_partial"
              : "teamview.usage_partial",
          )}
        </p>
      )}
    </div>
  );
}

// This fork has no session list to hand the roster, so it passes `[]` and lets
// teamRoster fall back to the workers the summary already describes (name, role,
// running, tokens) — the roster is honest about what it knows.
function WorkerRows({
  summary,
  onWorker,
  machine,
  filter,
  onFilter,
  page,
  onPage,
}: {
  summary: TeamSummary;
  onWorker: (worker: TeamWorker) => void;
  machine?: string;
  filter: WorkerFilter;
  onFilter: (filter: WorkerFilter) => void;
  page: number;
  onPage: (page: number) => void;
}) {
  const { t } = useTranslation();
  const entries = teamRoster(
    [],
    summary,
    machine || t("teamview.this_machine"),
  );
  const matches = filterWorkers(entries, filter);
  const current = Math.min(
    page,
    Math.max(0, Math.ceil(matches.length / ROSTER_PAGE) - 1),
  );
  return (
    <>
      <WorkerSearch
        value={filter.query || ""}
        onChange={(query) => {
          onFilter({ ...filter, query });
          onPage(0);
        }}
      />
      {(filter.role || filter.attention) && (
        <div className="team-roster-filter">
          <span>{filter.role || t("teamview.needs_attention")}</span>
          <button
            onClick={() => {
              onFilter({ query: filter.query });
              onPage(0);
            }}
          >
            {t("teamview.clear_filter")}
          </button>
        </div>
      )}
      <p className="team-roster-caption" role="status">
        {t("teamview.worker_matches", { count: matches.length })}
      </p>
      {matches
        .slice(current * ROSTER_PAGE, current * ROSTER_PAGE + ROSTER_PAGE)
        .map((w) => (
          <button
            className="team-task-row"
            key={w.id}
            data-testid={`team-worker-${w.name}`}
            onClick={() => w.worker && onWorker(w.worker)}
          >
            <span
              className={taskDot(
                w.attention ? "blocked" : w.working ? "in_progress" : "open",
              )}
            />
            <span className="team-task-main">
              <span className="team-task-title">
                {w.name} <small>· {w.role}</small>
              </span>
              <span className="team-task-status">
                {w.task || t("teamview.idle")}
              </span>
            </span>
            <span className="team-worker-chip">{w.machine}</span>
            <span>{formatTokens(tokenTotal(w.worker?.tokens ?? { input: 0, output: 0, cache_read: 0, cache_write: 0 }))}</span>
          </button>
        ))}
      {matches.length > ROSTER_PAGE && (
        <div className="team-roster-pagination">
          <button
            className="rail-mini-btn"
            disabled={current === 0}
            aria-label={t("teamview.previous_workers")}
            onClick={() => onPage(current - 1)}
          >
            <Icon
              name="chevronRight"
              size={14}
              className="team-roster-previous"
            />
          </button>
          <span>
            {t("teamview.worker_range", {
              start: current * ROSTER_PAGE + 1,
              end: Math.min(current * ROSTER_PAGE + ROSTER_PAGE, matches.length),
              count: matches.length,
            })}
          </span>
          <button
            className="rail-mini-btn"
            disabled={(current + 1) * ROSTER_PAGE >= matches.length}
            aria-label={t("teamview.next_workers")}
            onClick={() => onPage(current + 1)}
          >
            <Icon name="chevronRight" size={14} />
          </button>
        </div>
      )}
    </>
  );
}

function Stats({ summary: s }: { summary: TeamSummary }) {
  const { t } = useTranslation();
  const [by, setBy] = useState<"worker" | "role">("worker"),
    [chart, setChart] = useState(false);
  const [expandedRole, setExpandedRole] = useState<string | null>(null);
  const roles = [...new Set(s.workers.map((w) => w.role))];
  const rows =
    by === "role"
      ? roles.map((role) => ({
          label: role,
          value: s.workers
            .filter((w) => w.role === role)
            .reduce((n, w) => n + tokenTotal(w.tokens), 0),
        }))
      : s.workers.map((w) => ({ label: w.actor, value: tokenTotal(w.tokens) }));
  rows.sort((a, b) => b.value - a.value);
  const shown = by === "worker" && rows.length > 30 ? rows.slice(0, 5) : rows;
  const max = Math.max(1, ...rows.map((r) => r.value));
  const workTime = s.timing
    ? s.timing.model_ms + s.timing.tool_ms + s.timing.waited_ms
    : 0;
  const timelineRoles = [...new Set(s.usage_points.map((p) => p.role))];
  const start = s.usage_points[0]?.ts || 0,
    end = s.usage_points[s.usage_points.length - 1]?.ts || start + 1;
  const samples = Array.from(
    { length: 41 },
    (_, i) => start + ((end - start) * i) / 40,
  );
  let baseline = samples.map(() => 0);
  const paths = timelineRoles.map((role, ri) => {
    const top = samples.map(
      (ts, i) =>
        baseline[i] +
        s.usage_points
          .filter((p) => p.role === role && p.ts <= ts)
          .reduce((n, p) => n + p.tokens, 0),
    );
    const point = (v: number, i: number) =>
      `${(i / 40) * 500},${120 - (v / Math.max(1, s.totals.tokens)) * 120}`;
    const path =
      "M" +
      top.map(point).join(" L") +
      " L" +
      baseline.map(point).reverse().join(" L") +
      " Z";
    baseline = top;
    return <path key={role} d={path} className={`role-color-${ri % 5}`} />;
  });
  return (
    <>
      <div className="team-stat-cards">
        <Stat
          label={t("teamview.tokens")}
          value={formatTokens(s.totals.tokens)}
        />
        <Stat
          label={t("teamview.elapsed")}
          value={<Duration seconds={s.totals.elapsed_s} />}
        />
        <Stat
          label={t("teamview.needed_you")}
          value={s.totals.asks_answered + s.totals.asks_waiting}
        />
        <Stat
          label={t("teamview.tasks")}
          value={`${s.totals.done} / ${s.totals.total}`}
        />
      </div>
      <section className="team-stats-section">
        <h3>{t("teamview.tokens")}</h3>
        <div className="team-metric-bar">
          <span
            className="lead-segment"
            style={{
              width: `${(tokenTotal(s.lead.tokens) / Math.max(1, s.totals.tokens)) * 100}%`,
            }}
          />
          <span className="worker-segment" style={{ flex: 1 }} />
        </div>
        <p className="team-legend">
          {t("teamview.lead")} {formatTokens(tokenTotal(s.lead.tokens))} ·{" "}
          {t("teamview.workers")}{" "}
          {formatTokens(s.totals.tokens - tokenTotal(s.lead.tokens))}
        </p>
        <MetricBar
          kind="tokens"
          values={s.totals.tokens_by_kind}
          partial={s.usage_partial}
        />
      </section>
      <section className="team-stats-section">
        <div className="team-section-title">
          <h3>{t("teamview.where_tokens")}</h3>
          <div className="team-tabs">
            <button
              aria-pressed={by === "worker"}
              onClick={() => setBy("worker")}
            >
              {t("teamview.by_worker")}
            </button>
            <button aria-pressed={by === "role"} onClick={() => setBy("role")}>
              {t("teamview.by_role")}
            </button>
          </div>
        </div>
        {shown.length < rows.length && (
          <p className="team-totals">
            {t("teamview.top_five", { count: rows.length })}
          </p>
        )}
        {shown.map((r) => (
          <div key={r.label}>
            <button
              className="team-usage-row"
              disabled={by !== "role"}
              onClick={() =>
                setExpandedRole(expandedRole === r.label ? null : r.label)
              }
            >
              <span>{r.label}</span>
              <span className="team-usage-track">
                <i style={{ width: `${(r.value / max) * 100}%` }} />
              </span>
              <span>{formatTokens(r.value)}</span>
            </button>
            {by === "role" &&
              expandedRole === r.label &&
              s.workers
                .filter((w) => w.role === r.label)
                .map((w) => (
                  <p className="team-totals" key={w.actor}>
                    {w.actor} · {formatTokens(tokenTotal(w.tokens))}
                  </p>
                ))}
          </div>
        ))}
        <button
          className="team-open-link"
          onClick={() => setChart(!chart)}
          aria-expanded={chart}
        >
          {t(chart ? "teamview.hide_chart" : "teamview.show_chart")}
        </button>
        {chart &&
          (s.usage_points.length ? (
            <>
              <svg
                viewBox="0 0 500 130"
                role="img"
                aria-label={t("teamview.tokens_over_time")}
              >
                {paths}
              </svg>
              <div className="team-legend">
                {timelineRoles.map((role, i) => (
                  <span key={role}>
                    <i className={`role-color-${i % 5}`} />
                    {role === "lead" ? t("teamview.lead") : role}
                  </span>
                ))}
              </div>
              <p className="team-totals">
                {new Date(start * 1000).toLocaleTimeString()} —{" "}
                {new Date(end * 1000).toLocaleTimeString()}
              </p>
            </>
          ) : (
            <p>{t("teamview.no_usage")}</p>
          ))}
      </section>
      <section className="team-stats-section">
        <h3>{t("teamview.needed_you")}</h3>
        <table className="team-asks">
          <thead>
            <tr>
              <th></th>
              <th>{t("teamview.answered")}</th>
              <th>{t("teamview.waiting")}</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(s.asks).map(([kind, a]) => (
              <tr key={kind}>
                <th>{t("teamview.ask_" + kind)}</th>
                <td>{a.answered}</td>
                <td>{a.waiting}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {s.median_answer_s != null && (
          <p className="team-totals">
            {t("teamview.median_answer")}{" "}
            <Duration seconds={s.median_answer_s} />
          </p>
        )}
        {s.totals.done > 0 && (
          <p className="team-totals">
            {t("teamview.asks_per_task", {
              value: (
                (s.totals.asks_answered + s.totals.asks_waiting) /
                s.totals.done
              ).toFixed(1),
            })}
          </p>
        )}
      </section>
      <section className="team-stats-section">
        <h3>{t("teamview.where_time")}</h3>
        <MetricBar kind="time" values={s.timing} partial={s.timing_partial} />
        {s.timing && (
          <p className="team-totals">
            <Duration seconds={s.totals.elapsed_s} /> {t("teamview.elapsed")} ·{" "}
            <Duration seconds={workTime / 1000} /> {t("teamview.worker_time")} ·{" "}
            {s.totals.elapsed_s > 0
              ? (workTime / 1000 / s.totals.elapsed_s).toFixed(1)
              : "0"}
            ×
          </p>
        )}
      </section>
    </>
  );
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <small>{label}</small>
      <strong>{value}</strong>
    </div>
  );
}

function PendingTeamRequests({
  requests,
  onRefresh,
}: {
  requests: NonNullable<TeamSummary["pending_requests"]>;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const [opened, setOpened] = useState<InboxItem | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (opened && !requests.some((r) => r.id === opened.id)) setOpened(null);
  }, [requests, opened]);
  return (
    <section data-testid="team-pending-requests">
      <h3 className="team-group-heading">
        {t("teamview.pending_requests", { count: requests.length })}
      </h3>
      {requests.map((request) => (
        <button
          key={request.id}
          className="team-task-row"
          onClick={async () => {
            setError("");
            try {
              const items = await getInbox(request.session_id, "pending");
              setOpened(items.find((item) => item.id === request.id) || null);
              onRefresh();
            } catch {
              setError(t("teamview.load_error"));
            }
          }}
        >
          <span className="board-dot blocked" aria-hidden="true" />
          <span className="team-task-main">
            {request.worker} · {request.title}
          </span>
          <Icon name="chevronRight" size={14} />
        </button>
      ))}
      {error && (
        <p role="alert" className="team-danger">
          {error}
        </p>
      )}
      {opened && (
        <InboxItemCard
          item={opened}
          onResolve={async (id, answer) => {
            try {
              await resolveInboxItem(id, answer);
              setOpened(null);
              onRefresh();
            } catch {
              setError(t("teamview.action_error"));
            }
          }}
        />
      )}
    </section>
  );
}

function TaskDetailPane({
  task,
  leadSession,
  workers,
  onWorker,
  onRefresh,
  onOpenSession,
}: {
  task?: TeamTask;
  leadSession: string;
  workers: TeamWorker[];
  onWorker: (worker: TeamWorker) => void;
  onRefresh: () => void;
  onOpenSession?: (sessionId: string) => void;
}) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<BoardItemDetail | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let canceled = false;
    if (!task) return;
    getBoardItem(leadSession, task.id)
      .then((d) => {
        if (canceled) return;
        if ("error" in d) setError(t("teamview.load_error"));
        else {
          setDetail(d);
          setError("");
        }
      })
      .catch(() => {
        if (!canceled) setError(t("teamview.load_error"));
      });
    return () => {
      canceled = true;
    };
  }, [leadSession, task?.id, task?.updated, t]);
  const reloadDetail = async () => {
    if (!task) return;
    const d = await getBoardItem(leadSession, task.id);
    if ("error" in d) throw new Error(d.error);
    setDetail(d);
    setError("");
    onRefresh();
  };
  const contributors = new Set([
    detail?.assignee,
    ...(detail?.timeline || []).flatMap((event) => [
      event.actor,
      event.assignee,
    ]),
  ]);
  const relatedWorkers = workers.filter((w) => contributors.has(w.actor));

  return (
    <div
      className="team-pane-body team-task-detail"
      data-testid="team-task-detail"
    >
      <h2>{task?.title || t("teamview.task_unavailable")}</h2>
      {task && task.elapsed_s > 0 && (
        <>
          <MetricBar
            kind="time"
            values={task.timing}
            partial={task.timing_partial}
          />
          <MetricBar
            kind="tokens"
            values={task.tokens}
            partial={task.usage_partial}
          />
        </>
      )}
      {detail && (
        <ItemDetail
          hideTitle
          detail={detail}
          onOpenWorker={
            workers.some((w) => w.actor === detail.assignee)
              ? (actor) => {
                  const w = workers.find((x) => x.actor === actor);
                  if (w) onWorker(w);
                }
              : undefined
          }
          onTransition={(id, to, comment) => {
            void boardTransition(leadSession, id, to, comment)
              .then((result) => {
                if ("error" in result) throw new Error(result.error);
                return reloadDetail();
              })
              .catch(() => setError(t("teamview.action_error")));
          }}
          onAddNote={async (id, body) => {
            try {
              const result = await boardComment(leadSession, id, body);
              if (result.error) throw new Error(result.error);
              await reloadDetail();
            } catch (e) {
              setError(t("teamview.action_error"));
              throw e;
            }
          }}
          loadAttachment={(stored) => fetchBoardAttachment(leadSession, stored)}
        />
      )}
      {relatedWorkers.length > 0 && (
        <section className="team-related-workers">
          <h3>{t("teamview.related_conversations")}</h3>
          {relatedWorkers.map((w) => (
            <button
              key={w.session_id}
              className="task-chip"
              onClick={() => onWorker(w)}
            >
              {w.actor} ↗
            </button>
          ))}
        </section>
      )}
      {error && (
        <p role="alert" className="team-danger">
          {error}
        </p>
      )}
      {onOpenSession && task?.worker_session && (
        <button
          className="team-open-link"
          onClick={() => onOpenSession(task.worker_session!)}
        >
          {t("teamview.full_session")} →
        </button>
      )}
    </div>
  );
}

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
  const [selected, setSelected] = useState<number | null>(null);
  const [workerFilter, setWorkerFilter] = useState<WorkerFilter>({});
  const [workerPage, setWorkerPage] = useState(0);

  // One read model, polled while open. The endpoint is side-effect free by contract
  // (no wake, no cursor advance), so a 3s poll is safe even with a team mid-run.
  const load = useCallback(async () => {
    if (!teamId) return;
    try {
      const next = await getTeamSummary(teamId);
      if ("error" in next) {
        setError(next.error);
        setSummary(null);
      } else {
        setSummary(next);
        setError("");
      }
    } catch {
      setError(t("teamview.summary_unavailable"));
    }
  }, [teamId, t]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const items = summary?.items ?? [];
  const byGroup = useMemo(() => {
    const map: Record<string, TeamTask[]> = {};
    for (const item of items) (map[item.group] ||= []).push(item);
    return map;
  }, [items]);
  const task = items.find((i) => i.id === selected);
  const workers = summary?.workers ?? [];
  // Divergence: no embedded transcript — a worker row opens their real session.
  const openWorker = (w: TeamWorker) => onOpenSession(w.session_id);
  const openTask = (id: number) => setSelected(id);
  const goBack = () => setSelected(null);

  if (!teamId) {
    return (
      <section className="team-view" data-testid="team-view">
        <header className="team-pane-header">
          <button className="team-close" onClick={onClose} aria-label={t("teamview.close")}>
            ×
          </button>
        </header>
        <div className="team-pane-body">
          <p className="team-totals">{t("teamview.no_team_title")}</p>
          <p>{t("teamview.no_team_body")}</p>
        </div>
      </section>
    );
  }

  return (
    <section
      className="team-view"
      aria-label={t("teamview.title")}
      data-testid="team-view"
      onKeyDown={(e) => {
        if (
          e.key === "Escape" &&
          !(e.target instanceof HTMLTextAreaElement) &&
          !(e.target instanceof HTMLInputElement)
        ) {
          e.stopPropagation();
          onClose();
        }
      }}
    >
      <header className="team-pane-header">
        {selected != null ? (
          <button onClick={goBack}>← {t("teamview.title")}</button>
        ) : (
          <div className="team-tabs" role="tablist">
            {(["work", "workers", "stats"] as const)
              .filter((name) => !!summary || name === "work")
              .map((name) => (
                <button
                  role="tab"
                  key={name}
                  aria-selected={tab === name}
                  onClick={() => setTab(name)}
                >
                  {t("teamview." + name)}
                  {name === "work" && !!summary?.counts.waiting && (
                    <span className="team-attention-count">
                      {summary.counts.waiting}
                    </span>
                  )}
                </button>
              ))}
          </div>
        )}
        <button
          className="team-close"
          onClick={onClose}
          aria-label={t("teamview.close")}
        >
          ×
        </button>
      </header>
      {selected != null ? (
        <TaskDetailPane
          key={`${sessionId}:task:${selected}`}
          task={task}
          leadSession={sessionId}
          workers={workers}
          onWorker={openWorker}
          onRefresh={() => void load()}
          onOpenSession={onOpenSession}
        />
      ) : (
        <div className="team-pane-body">
          {!summary ? (
            <>
              <p role="status">{error || t("teamview.loading")}</p>
              {GROUPS.map((group) => {
                const rows = byGroup[group] || [];
                return (
                  !!rows.length && (
                    <section key={group}>
                      <h3 className="team-group-heading">
                        {t("teamview.state_" + group)}
                      </h3>
                      {rows.map((item) => (
                        <TaskRow key={item.id} item={item} onOpen={openTask} />
                      ))}
                    </section>
                  )
                );
              })}
            </>
          ) : tab === "work" ? (
            <>
              <h2>
                <Headline
                  count={
                    summary.pending_requests?.some((r) => !r.represented_by_task)
                      ? 0
                      : summary.counts.waiting
                  }
                  asks={summary.totals.asks_waiting}
                />
              </h2>
              <TeamTotals summary={summary} />
              {!!summary.pending_requests?.length && (
                <PendingTeamRequests
                  requests={summary.pending_requests}
                  onRefresh={() => void load()}
                />
              )}
              {!summary.totals.total && <p>{t("teamview.no_tasks")}</p>}
              {GROUPS.map((group) => {
                const rows = byGroup[group] || [];
                if (!rows.length) return null;
                const rendered = rows.map((item) => (
                  <TaskRow
                    key={item.id}
                    item={item}
                    onOpen={openTask}
                    now={summary.generated_at}
                  />
                ));
                return group === "done" ? (
                  <details className="team-done" key={group}>
                    <summary className="team-group-heading">
                      {t("teamview.state_done")} <span>{rows.length}</span>
                    </summary>
                    {rendered}
                  </details>
                ) : (
                  <section key={group}>
                    <h3 className="team-group-heading">
                      {t("teamview.state_" + group)} <span>{rows.length}</span>
                    </h3>
                    {rendered}
                  </section>
                );
              })}
            </>
          ) : tab === "workers" ? (
            <>
              <h2>
                {t("teamview.workers_count", { count: summary.workers.length })}
              </h2>
              <TeamTotals summary={summary} />
              <WorkerRows
                summary={summary}
                filter={workerFilter}
                onFilter={setWorkerFilter}
                page={workerPage}
                onPage={setWorkerPage}
                onWorker={openWorker}
              />
            </>
          ) : (
            <Stats summary={summary} />
          )}
        </div>
      )}
    </section>
  );
}
