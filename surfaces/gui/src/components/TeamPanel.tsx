// 团队面板：把后端本来就有的 lead + worker 并行编排**看见**。
//
// 为什么需要它：`create_team` / `team_tick` 早就能让多个 worker 各自跑在独立引擎上，
// 但界面上只有 TeamChatView（聊天日志视角）——于是"小桔只能一次做一件事"这个感受一直
// 存在。后端能力其实在，缺的是这块可视化：谁在跑、跑的是哪一条、哪一条卡住了在等谁。
//
// 只读：这里不发任何指令，也不唤醒任何东西（后端的 roster 接口同样保证零副作用）。
// 派活仍然走 lead（它在对话里提议 → 你批准），这是权限边界，不在这个面板上开口子。
import { useEffect, useMemo, useState } from "react";
import { getTeamRoster, type TeamRoster, type TeamRosterItem, type TeamRosterWorker } from "../api";
import { BOARD_COLUMNS, groupIntoColumns, shortSpace, teamSummary, workerStatus } from "../teamRoster";
import { PanelHead } from "./IntegrationsView";
import { Icon } from "./Icon";

const POLL_MS = 3000;

const STATE_TONE: Record<string, string> = {
  open: "border-line text-muted",
  in_progress: "border-accent/40 text-accent bg-accentSoft",
  blocked: "border-warnInk/40 text-warnInk bg-warnSoft",
  review: "border-ok/40 text-ok bg-okSoft",
  done: "border-line text-faint",
  canceled: "border-line text-faint line-through",
};

function StatusDot({ tone }: { tone: "live" | "busy" | "idle" }) {
  const cls =
    tone === "live"
      ? "bg-ok"
      : tone === "busy"
        ? "bg-warnInk"
        : "bg-lineStrong";
  return (
    <span className="relative inline-flex w-1.5 h-1.5 shrink-0">
      <span className={"w-1.5 h-1.5 rounded-full " + cls} />
      {tone === "live" && (
        <span className="absolute inset-0 rounded-full bg-ok animate-ping opacity-60" />
      )}
    </span>
  );
}

function WorkerRow({
  worker,
  onOpen,
}: {
  worker: TeamRosterWorker;
  onOpen?: (sessionId: string, persona?: string) => void;
}) {
  const st = workerStatus(worker);
  return (
    <button
      className="w-full text-left rounded-lg border border-line bg-panel px-2.5 py-2 hover:border-lineStrong transition-colors"
      data-testid={`team-worker-${worker.actor}`}
      onClick={() => worker.session_id && onOpen?.(worker.session_id, worker.persona)}
      title={worker.reason || worker.persona}
    >
      <div className="flex items-center gap-1.5 min-w-0">
        <StatusDot tone={st.tone} />
        <span className="text-ui font-medium truncate">{worker.actor}</span>
        <span className="text-label text-faint truncate">{worker.persona}</span>
        <span className="ml-auto text-label text-muted shrink-0">{st.label}</span>
      </div>
      <div className="text-label text-faint truncate mt-0.5 pl-3" title={worker.current || undefined}>
        {worker.current || worker.reason || "没有在办的事"}
      </div>
    </button>
  );
}

function ItemCard({ item }: { item: TeamRosterItem }) {
  return (
    <div
      className="w-full text-left rounded-lg border border-line bg-panel px-2 py-1.5"
      data-testid={`team-item-${item.id}`}
    >
      <div className="text-ui leading-snug line-clamp-2">{item.title}</div>
      <div className="flex items-center gap-1.5 mt-1">
        <span className={"text-label px-1.5 py-px rounded-full border " + (STATE_TONE[item.state] || STATE_TONE.open)}>
          {item.assignee || "未指派"}
        </span>
        <span className="text-label text-faint ml-auto">#{item.id}</span>
      </div>
    </div>
  );
}

export function TeamPanel({
  sessionId,
  onOpenSession,
}: {
  sessionId: string;
  onOpenSession?: (sessionId: string, persona?: string) => void;
}) {
  const [roster, setRoster] = useState<TeamRoster | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () => {
      if (!sessionId) {
        setRoster({ team: null, role: null });
        return;
      }
      getTeamRoster(sessionId)
        .then((r) => {
          if (!alive) return;
          setRoster(r);
          setError("");
        })
        .catch(() => alive && setError("读不到团队状态（后端没响应）。"));
    };
    load();
    const t = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [sessionId]);

  const columns = useMemo(() => groupIntoColumns(roster?.items || []), [roster]);
  const summary = useMemo(
    () => (roster ? teamSummary(roster) : { workers: 0, running: 0, review: 0, blocked: 0, total: 0 }),
    [roster],
  );

  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className="max-w-5xl mx-auto px-7 py-6">
          <PanelHead
            title="团队"
            sub="小桔带的 worker 各自跑在独立的会话里。这里看的是真实运行态，不是日志回放。"
          />

          {error && (
            <div className="text-ui text-danger bg-dangerSoft border border-danger/20 rounded-lg px-3 py-2 mb-3">
              {error}
            </div>
          )}

          {roster === null ? (
            <div className="text-ui text-muted">正在读取团队状态…</div>
          ) : !roster.team ? (
            <div className="rounded-xl border border-line bg-panel px-4 py-5" data-testid="team-empty">
              <div className="text-ui font-medium">这个会话还没有团队</div>
              <p className="text-meta text-muted mt-1.5 leading-relaxed">
                团队不是手动建的：小桔要在对话里先提议一份名单（谁、干什么、为什么），
                你点头之后后端才会真正生成 worker 会话。在那之前，它一个人做事——这是设计，
                不是限制：多一个 worker 就多一份在你项目目录里的写入权。
              </p>
              <p className="text-meta text-faint mt-2 leading-relaxed">
                想让它拆活，直接说「这件事拆成几块，分给人并行做」，它会走提议闸门。
              </p>
            </div>
          ) : (
            <>
              {/* 一句话战况 */}
              <div className="flex items-center gap-2 mb-4 flex-wrap" data-testid="team-summary">
                <span className="text-meta px-2 py-0.5 rounded-full border border-line text-muted">
                  {summary.workers} 个 worker
                </span>
                <span className="text-meta px-2 py-0.5 rounded-full border border-ok/40 text-ok bg-okSoft">
                  {summary.running} 个在跑
                </span>
                {summary.review > 0 && (
                  <span className="text-meta px-2 py-0.5 rounded-full border border-ok/40 text-ok bg-okSoft">
                    {summary.review} 条待你验收
                  </span>
                )}
                {summary.blocked > 0 && (
                  <span className="text-meta px-2 py-0.5 rounded-full border border-warnInk/40 text-warnInk bg-warnSoft">
                    {summary.blocked} 条卡住
                  </span>
                )}
                <span className="text-meta px-2 py-0.5 rounded-full border border-line text-faint">
                  {summary.total} 条工作项
                </span>
                {roster.team.paused && (
                  <span className="text-meta px-2 py-0.5 rounded-full border border-warnInk/40 text-warnInk bg-warnSoft">
                    团队已暂停
                  </span>
                )}
                <span className="ml-auto text-label text-faint truncate" title={roster.team.space}>
                  {shortSpace(roster.team.space)}
                </span>
              </div>

              <div className="grid gap-5" style={{ gridTemplateColumns: "220px minmax(0, 1fr)" }}>
                {/* 左：成员（lead 在第一位 — 它也是会跑起来的那个） */}
                <div>
                  <div className="text-label text-faint uppercase tracking-wide mb-1.5">成员</div>
                  <div className="space-y-1.5">
                    <div
                      className="rounded-lg border border-accent/30 bg-accentSoft/40 px-2.5 py-2"
                      data-testid="team-lead"
                    >
                      <div className="flex items-center gap-1.5 min-w-0">
                        <StatusDot tone={roster.team.lead_running ? "live" : "idle"} />
                        <span className="text-ui font-medium truncate">{roster.team.lead_actor}</span>
                        <span className="ml-auto text-label text-accent shrink-0">lead</span>
                      </div>
                      <div className="text-label text-muted truncate mt-0.5 pl-3" title={roster.team.lead.current || undefined}>
                        {roster.team.lead.current || "统筹中"}
                      </div>
                    </div>
                    {(roster.workers || []).map((w) => (
                      <WorkerRow key={w.actor} worker={w} onOpen={onOpenSession} />
                    ))}
                    {(roster.workers || []).length === 0 && (
                      <div className="text-meta text-faint px-1 py-2">只有 lead，没有 worker。</div>
                    )}
                  </div>
                </div>

                {/* 右：看板 */}
                <div>
                  <div className="text-label text-faint uppercase tracking-wide mb-1.5">看板</div>
                  <div className="grid grid-cols-2 xl:grid-cols-3 gap-3">
                    {BOARD_COLUMNS.map((col) => {
                      const list = columns[col.state] || [];
                      return (
                        <div key={col.state} data-testid={`team-col-${col.state}`}>
                          <div className="flex items-center gap-1.5 mb-1">
                            <span className="text-label text-muted">{col.label}</span>
                            <span className="text-label text-faint tabular-nums">{list.length}</span>
                          </div>
                          <div className="space-y-1.5">
                            {list.map((it) => (
                              <ItemCard key={it.id} item={it} />
                            ))}
                            {list.length === 0 && (
                              <div className="text-label text-faint/70 border border-dashed border-line rounded-lg px-2 py-2 text-center">
                                {col.hint || "空"}
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            </>
          )}

          <p className="text-label text-faint mt-6 flex items-center gap-1.5">
            <Icon name="users" size={11} />
            每 {POLL_MS / 1000} 秒刷新一次；运行态来自后端引擎，不是界面猜的。
          </p>
        </div>
      </div>
    </main>
  );
}
