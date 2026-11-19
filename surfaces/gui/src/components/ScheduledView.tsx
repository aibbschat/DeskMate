import { useEffect, useState } from "react";
import {
  createAutomation,
  cancelAutomationRun,
  deleteAutomation,
  getAutomation,
  getAutomations,
  markAutomationSeen,
  announceAutomationsChanged,
  restartAutomationGoal,
  updateAutomation,
  type Automation,
  type AutomationRun,
} from "../api";
import { Icon } from "./Icon";
import { PanelHead } from "./IntegrationsView";
import { AutomationQuickstart } from "./AutomationQuickstart";
import { usePolling } from "../hooks/usePolling";

// Shared utility strings (the §28 page shell — mirrors IntegrationsView's constants).
const CARD = "rounded-xl2 border border-line bg-panel";

// Parse a simple "min hour * * dow" cron back into the time + frequency the editor uses.
// Falls back to 09:00 / daily for anything it doesn't recognize (e.g. agent-written crons).
function fromCron(cron?: string | null): { time: string; freq: string } {
  const parts = (cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return { time: "09:00", freq: "daily" };
  const [m, h, , , dow] = parts;
  const hh = String(Math.min(23, Math.max(0, parseInt(h, 10) || 9))).padStart(2, "0");
  const mm = String(Math.min(59, Math.max(0, parseInt(m, 10) || 0))).padStart(2, "0");
  const freq = dow === "1-5" ? "weekdays" : dow === "0,6" || dow === "6,0" ? "weekends" : "daily";
  return { time: `${hh}:${mm}`, freq };
}

const fmt = (t: number | null) =>
  t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

// Map a simple time-of-day + frequency selection to a 5-field cron string.
function toCron(time: string, freq: string): string {
  const [h, m] = (time || "09:00").split(":").map((x) => parseInt(x, 10) || 0);
  const dow = freq === "weekdays" ? "1-5" : freq === "weekends" ? "0,6" : "*";
  return `${m} ${h} * * ${dow}`;
}

// The §28 page shell: full-bleed main, centered ≤4xl column — same as Connectors/Activity/Inbox.
function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-4xl mx-auto px-7 py-6">{children}</div>
      </div>
    </main>
  );
}

interface Props {
  // `task` gives the opened run session its context (banner + "Back to runs"; owner ask 2026-07-04).
  onOpenRun: (
    sessionId: string,
    workspace: string,
    agent: string,
    task?: { id: string; title: string },
  ) => void;
  onRunNow: (taskId: string, title?: string) => void;
  // Open directly on a task's detail (set by the run banner's "Back to runs").
  initialOpenId?: string | null;
}

export function ScheduledView({ onOpenRun, onRunNow, initialOpenId }: Props) {
  const [tasks, setTasks] = useState<Automation[]>([]);
  const [openId, setOpenId] = useState<string | null>(initialOpenId ?? null);
  const [showForm, setShowForm] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  // The sidebar's Scheduled band can retarget an ALREADY-open Automations surface —
  // initial state alone would ignore the change (UX-023).
  useEffect(() => {
    if (initialOpenId) setOpenId(initialOpenId);
  }, [initialOpenId]);

  const refresh = () => getAutomations().then(setTasks).catch(() => setTasks([]));
  // PR20(#51)：列表轮询迁 usePolling（隐藏页暂停 / 失败退避 / 不并发）。
  usePolling(refresh, 5000, { immediate: true });

  // Create from a payload, refresh the list, and open the new task's detail. `permissions`
  // rides through for quickstart recipes (§25 write grants).
  const create = async (payload: {
    title: string;
    instructions: string;
    cron?: string;
    // Autonomy §C1: declared by the user in the form, not inferred from a category.
    goal_mode?: string;
    permissions?: { tool: string; target: string; access: "read" | "write" }[];
  }) => {
    setBusy(payload.title);
    try {
      const res = await createAutomation(payload);
      announceAutomationsChanged(); // new entry shows in the sidebar band right away
      await refresh();
      if (res.ok && res.task) {
        setShowForm(false);
        setOpenId(res.task.id);
      } else if (res.error) {
        alert(res.error);
      }
    } finally {
      setBusy(null);
    }
  };

  if (openId) {
    return (
      <TaskDetail
        id={openId}
        onBack={() => { setOpenId(null); refresh(); }}
        onOpenRun={onOpenRun}
        onRunNow={onRunNow}
      />
    );
  }

  const empty = tasks.length === 0;

  return (
    <Shell>
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <PanelHead title="Automations" sub="Recurring tasks OpenWorker runs on a schedule." />
        </div>
        <button
          className="text-ui px-3 py-1.5 rounded-lg border border-lineStrong bg-panel hover:border-accent hover:text-accent shrink-0"
          onClick={() => setShowForm((v) => !v)}
        >
          + New automation
        </button>
      </div>

      <div className="text-meta text-faint flex gap-1.5 mb-4">
        <span aria-hidden>ⓘ</span>
        <span>
          Runs only while openworker-server is up — a missed schedule catches up once when it next
          starts.
        </span>
      </div>

      {showForm && (
        <NewAutomationForm
          busy={busy !== null}
          onCancel={() => setShowForm(false)}
          onCreate={create}
        />
      )}

      {/* The quickstart (§29): ONE template system — role recipes + generic templates, each
          card with §27 connector dots; picking one expands the configure card. */}
      {(empty || showForm) && <AutomationQuickstart busy={busy !== null} onCreate={create} />}

      {empty ? (
        !showForm && (
          <div className={CARD + " p-4 text-ui text-muted"}>
            No scheduled tasks yet — use a template above, click <strong>+ New automation</strong>,
            or just ask OpenWorker in a session.
          </div>
        )
      ) : (
        <div className="flex flex-col gap-2.5">
          {tasks.map((t) => (
            <div
              className={CARD + " sched-card px-4 py-3 cursor-pointer hover:border-lineStrong transition-colors"}
              key={t.id}
              onClick={() => setOpenId(t.id)}
            >
              <div className="flex items-center justify-between gap-2.5 mb-1">
                <span className="text-ui font-semibold truncate">{t.title}</span>
                <button
                  className="sched-card-del"
                  title="Delete automation"
                  aria-label={`Delete ${t.title}`}
                  onClick={async (e) => {
                    e.stopPropagation();
                    await deleteAutomation(t.id);
                    refresh();
                  }}
                >
                  <Icon name="trash" size={14} />
                </button>
              </div>
              <div className="flex items-center gap-1.5 text-meta text-muted">
                <Icon name="clock" size={13} className="text-faint shrink-0" />
                {t.enabled ? t.schedule : "Paused"} · next {fmt(t.next_run)} · {t.run_count} run{t.run_count === 1 ? "" : "s"}
                {t.last_status ? ` · last ${t.last_status}` : ""}
              </div>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}

function NewAutomationForm({
  busy,
  onCancel,
  onCreate,
}: {
  busy: boolean;
  onCancel: () => void;
  onCreate: (p: { title: string; instructions: string; cron?: string; goal_mode?: string }) => void;
}) {
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [time, setTime] = useState("09:00");
  const [freq, setFreq] = useState("daily");
  const [goalMode, setGoalMode] = useState(false);

  const valid = title.trim() && instructions.trim();

  return (
    <div className={CARD + " tmpl-form p-4 mb-4"}>
      <div className="text-label uppercase tracking-[0.05em] text-faint mb-2.5">
        New automation
      </div>
      <input
        className="tmpl-input"
        placeholder="Title (e.g. Daily standup notes)"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        className="tmpl-input tmpl-textarea"
        placeholder="What should it do each run? (e.g. Summarize today's calendar and open tasks.)"
        value={instructions}
        onChange={(e) => setInstructions(e.target.value)}
      />
      <div className="tmpl-sched">
        <label className="tmpl-field">
          <span>At</span>
          <input
            type="time"
            className="tmpl-input tmpl-time"
            value={time}
            onChange={(e) => setTime(e.target.value)}
          />
        </label>
        <label className="tmpl-field">
          <span>Repeat</span>
          <select
            className="tmpl-input tmpl-select"
            value={freq}
            onChange={(e) => setFreq(e.target.value)}
          >
            <option value="daily">Every day</option>
            <option value="weekdays">Weekdays</option>
            <option value="weekends">Weekends</option>
          </select>
        </label>
      </div>
      {/* Autonomy §C1: opt-in at launch — "watch until it changes, then report once".
          Off by default so every existing automation keeps its one-shot behaviour. */}
      <label className="goal-toggle-row">
        <input
          type="checkbox"
          checked={goalMode}
          onChange={(e) => setGoalMode(e.target.checked)}
        />
        <span>持续盯梢</span>
        <span className="dim">按计划静默复查，只在目标有变化时报告一次</span>
      </label>
      <div className="tmpl-form-actions">
        <button
          className="btn-primary sm"
          disabled={!valid || busy}
          onClick={() =>
            onCreate({
              title: title.trim(),
              instructions: instructions.trim(),
              cron: toCron(time, freq),
              goal_mode: goalMode ? "watch" : "",
            })
          }
        >
          {busy ? "Creating…" : "Create automation"}
        </button>
        <button className="link" onClick={onCancel}>cancel</button>
      </div>
    </div>
  );
}

function TaskDetail({
  id,
  onBack,
  onOpenRun,
  onRunNow,
}: {
  id: string;
  onBack: () => void;
  onOpenRun: (
    sessionId: string,
    workspace: string,
    agent: string,
    task?: { id: string; title: string },
  ) => void;
  onRunNow: (taskId: string, title?: string) => void;
}) {
  const [task, setTask] = useState<Automation | null>(null);
  const [runs, setRuns] = useState<AutomationRun[]>([]);
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [instructions, setInstructions] = useState("");
  const [time, setTime] = useState("09:00");
  const [freq, setFreq] = useState("daily");
  const [goalMode, setGoalMode] = useState(false);
  const [saving, setSaving] = useState(false);

  // The seen mark AS OF opening — the "new" pills compare against this frozen value
  // while mark-seen advances the stored one (badge clears; highlights survive).
  const [seenMark, setSeenMark] = useState<number | null>(null);

  const refresh = () =>
    getAutomation(id)
      .then((d) => {
        if (!d.task) {
          // Deleted (or a stale reopen target): "Loading…" forever is a trap —
          // fall back to the overview (owner-hit 2026-07-20).
          onBack();
          return;
        }
        setTask(d.task);
        setRuns(d.runs || []);
        setSeenMark((cur) => (cur === null ? d.task?.seen_runs_at ?? 0 : cur));
      })
      .catch(() => {});
  useEffect(() => {
    setSeenMark(null);
    refresh();
    // Opening the detail IS reading it: advance the seen mark and nudge the
    // sidebar so the badge clears immediately (UX-023).
    markAutomationSeen(id)
      .then(() => announceAutomationsChanged())
      .catch(() => {});
  }, [id]);
  // Autonomy §C1 "visible": the detail used to refresh only on user actions, so
  // a live run's step/checkpoint never moved. Poll while the detail is open;
  // the global stop/finish events are consumed in App.tsx (single WS connection).
  usePolling(refresh, 5000);

  if (!task)
    return (
      <Shell>
        <div className="text-ui text-muted">Loading…</div>
      </Shell>
    );

  const startEdit = () => {
    setTitle(task.title);
    setInstructions(task.instructions);
    const { time: t, freq: f } = fromCron(task.schedule_raw?.cron);
    setTime(t);
    setFreq(f);
    setGoalMode(Boolean(task.goal_mode));
    setEditing(true);
  };
  const saveEdit = async () => {
    setSaving(true);
    try {
      await updateAutomation(id, {
        title: title.trim(),
        instructions: instructions.trim(),
        cron: toCron(time, freq),
        goal_mode: goalMode ? "watch" : "",
      });
      await refresh();
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };
  const toggle = async () => {
    await updateAutomation(id, { enabled: !task.enabled });
    refresh();
  };
  // §C1: reopening a concluded goal bumps its generation; history is never rewritten.
  const restartGoal = async () => {
    await restartAutomationGoal(id);
    await refresh();
    announceAutomationsChanged();
  };
  const openRun = (r: AutomationRun) => {
    if (!r.session_id) return;
    onOpenRun(r.session_id, task.workspace, task.agent, { id: task.id, title: task.title });
  };
  const stopRun = async (runId: string) => {
    await cancelAutomationRun(id, runId);
    refresh();
  };
  const remove = async () => {
    await deleteAutomation(id);
    announceAutomationsChanged(); // the sidebar band must not wait out its poll
    onBack();
  };

  return (
    <Shell>
      <button className="text-ui text-muted hover:text-ink mb-3" onClick={onBack}>
        ← Automations
      </button>
      <div className="sched-detail">
        <div className="sched-detail-head">
          {editing ? (
            <input
              className="tmpl-input sched-edit-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Title"
            />
          ) : (
            <h2 className="text-[20px] font-semibold tracking-tight">{task.title}</h2>
          )}
          <div className="sched-actions">
            {editing ? (
              <>
                <button className="btn-primary sm" disabled={saving || !title.trim() || !instructions.trim()} onClick={saveEdit}>
                  {saving ? "Saving…" : "Save"}
                </button>
                <button className="link" onClick={() => setEditing(false)}>cancel</button>
              </>
            ) : (
              <>
                <button className="btn-primary sm" onClick={() => onRunNow(id, task.title)}>
                  ▶ Run now
                </button>
                <button className="btn sm" onClick={startEdit}>Edit</button>
                <button className="btn sm danger-btn" onClick={remove}>
                  <Icon name="trash" size={14} /> Delete
                </button>
              </>
            )}
          </div>
        </div>

        {editing ? (
          <>
            <div className="tmpl-sched sched-edit-sched">
              <label className="tmpl-field">
                <span>At</span>
                <input type="time" className="tmpl-input tmpl-time" value={time} onChange={(e) => setTime(e.target.value)} />
              </label>
              <label className="tmpl-field">
                <span>Repeat</span>
                <select className="tmpl-input tmpl-select" value={freq} onChange={(e) => setFreq(e.target.value)}>
                  <option value="daily">Every day</option>
                  <option value="weekdays">Weekdays</option>
                  <option value="weekends">Weekends</option>
                </select>
              </label>
            </div>
            <label className="goal-toggle-row">
              <input type="checkbox" checked={goalMode} onChange={(e) => setGoalMode(e.target.checked)} />
              <span>持续盯梢</span>
              <span className="dim">按计划静默复查，只在目标有变化时报告一次</span>
            </label>
          </>
        ) : (
          <>
            <div className="conn-meta">
              <label className="switch">
                <input type="checkbox" checked={task.enabled} onChange={toggle} />
                <span className="slider" />
              </label>{" "}
              {task.enabled ? `Active · next ${fmt(task.next_run)}` : "Paused"} · {task.schedule}
            </div>
            {/* §C1: goal generation is explicit — reopening never rewrites old runs. */}
            {Boolean(task.goal_mode) && (
              <div className="goal-banner">
                <Icon name="clock" size={14} />
                <span>持续盯梢 · 第 {(task.goal_generation ?? 0) + 1} 代</span>
                <span className="dim">结案后停止复查，需要时可重启</span>
                <button className="btn sm" onClick={restartGoal}>重启目标</button>
              </div>
            )}
          </>
        )}

        <div className="sa-sub">Instructions</div>
        {editing ? (
          <textarea
            className="tmpl-input tmpl-textarea sched-edit-instr"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
          />
        ) : (
          <div className="sched-instructions">{task.instructions}</div>
        )}

        {(task.always_allowed || []).length > 0 && (
          <>
            <div className="sa-sub">Allowed without asking</div>
            <div className="dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
              Standing approvals this automation may use — everything else still asks first.
            </div>
            <div className="sched-grants" data-testid="task-grants">
              {(task.always_allowed || []).map((rule) => (
                <div className="sched-grant" key={rule.entry}>
                  <span className="sched-grant-rule">
                    <code>{rule.tool}</code>
                    {rule.target && <span className="sched-grant-target"> → {rule.target}</span>}
                  </span>
                  <button
                    className="link"
                    title="This automation will ask for approval again"
                    onClick={async () => {
                      await updateAutomation(id, { revoke: rule.entry });
                      refresh();
                    }}
                  >
                    Revoke
                  </button>
                </div>
              ))}
            </div>
          </>
        )}

        <div className="sa-sub">Runs</div>
        <div className="dim" style={{ marginBottom: 8, fontSize: 12.5 }}>
          Each run is a live conversation — open one to see what the agent did and ask a follow-up.
        </div>
        {runs.length === 0 && <div className="dim">No runs yet.</div>}
        {runs.map((r) => {
          // §C1 "visible / stoppable / take over": live steps, the cross-run goal
          // checkpoint, a real stop button, and an explicit takeover action.
          // `progress` is still 0.0 by design (no fake percentage) — the live
          // step + event count are the honest progress signal.
          const live = r.status === "running" && !r.finished_at;
          const goal = (r.checkpoint as { goal?: { state?: string } } | undefined)?.goal;
          return (
            <div
              className="sched-run open"
              key={r.run_id}
              onClick={() => openRun(r)}
              title="Open this run's conversation"
            >
              <div className="sched-run-row">
                <span>
                  {seenMark !== null && r.started_at > seenMark && (
                    <span className="run-new-pill" data-testid="run-new">new</span>
                  )}
                  {fmt(r.started_at)} · <span className={"run-" + r.status}>{r.status}</span> · {r.trigger}
                  {r.artifacts.length > 0 && <span className="dim"> · {r.artifacts.length} file(s)</span>}
                  {r.cancel_requested && <span className="dim"> · 已请求停止</span>}
                </span>
                <span className="sched-run-actions">
                  {live && (
                    <button
                      className="btn sm danger-btn"
                      title="停止本轮（不投递结果）"
                      onClick={(e) => { e.stopPropagation(); void stopRun(r.run_id); }}
                    >
                      <Icon name="stop" size={13} /> 叫停
                    </button>
                  )}
                  <button
                    className="link sched-run-go"
                    title="接管：打开本轮会话继续对话"
                    onClick={(e) => { e.stopPropagation(); openRun(r); }}
                  >
                    Open › 接管
                  </button>
                </span>
              </div>
              {live && (
                <div className="sched-progress" data-testid="run-progress">
                  <div className="sched-progress-track"><span className="sched-progress-bar" /></div>
                  <span className="dim">{r.current_step || "执行中"} · {r.event_count ?? 0} 个事件</span>
                </div>
              )}
              {goal?.state && <div className="sched-goal-state">上轮状态：{goal.state}</div>}
              {r.result_text && <div className="sched-run-peek">{r.result_text}</div>}
              {r.error && <div className="mcp-error">{r.error}</div>}
            </div>
          );
        })}
      </div>
    </Shell>
  );
}
