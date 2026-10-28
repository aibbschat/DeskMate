// Agent-teams 看板的纯函数：把后端的 items[] 折成看板的列，把 worker 的运行态折成人话。
//
// 为什么单独成文件：面板是在轮询里渲染的，任何"顺手在 render 里算"的判断都应该可测，
// 否则状态机和排序规则只能靠肉眼在 UI 里验。这里不 import React。
import type { TeamRoster, TeamRosterItem, TeamRosterWorker } from "./api";

/** 看板的四列（canceled 不占列，收进"已归档"计数里）。顺序＝一件事从左走到右。 */
export const BOARD_COLUMNS = [
  { state: "open", label: "待办", hint: "还没人认领" },
  { state: "in_progress", label: "进行中", hint: "worker 正在做" },
  { state: "blocked", label: "卡住", hint: "需要有人解开" },
  { state: "review", label: "待审", hint: "等你或 lead 验收" },
  { state: "done", label: "已完成", hint: "" },
] as const;

export type BoardColumn = (typeof BOARD_COLUMNS)[number];

/** 按列折叠。列内按 id 升序（先派的任务在上，稳定不跳）。 */
export function groupIntoColumns(items: TeamRosterItem[]): Record<string, TeamRosterItem[]> {
  const out: Record<string, TeamRosterItem[]> = {};
  for (const col of BOARD_COLUMNS) out[col.state] = [];
  for (const item of items) {
    // 未知状态（后端加了新状态而前端还没更新）落到待办，宁可多显示不可消失。
    (out[item.state] || out.open).push(item);
  }
  for (const key of Object.keys(out)) out[key].sort((a, b) => a.id - b.id);
  return out;
}

/** worker 现在到底在干嘛。running 是服务端给的（引擎真在跑），其余是看板推断。 */
export function workerStatus(w: TeamRosterWorker): { label: string; tone: "live" | "busy" | "idle" } {
  if (w.running) return { label: w.current ? "运行中" : "思考中", tone: "live" };
  if ((w.counts?.in_progress || 0) > 0) return { label: "已接手，等唤醒", tone: "busy" };
  if ((w.counts?.blocked || 0) > 0) return { label: "卡住", tone: "busy" };
  return { label: "空闲", tone: "idle" };
}

/** 团队的一句话战况：几个在跑、几个待审。面板顶部和侧栏徽标共用。 */
export function teamSummary(r: TeamRoster): {
  workers: number;
  running: number;
  review: number;
  blocked: number;
  total: number;
} {
  const workers = r.workers || [];
  const counts = r.counts || {};
  return {
    workers: workers.length,
    running: workers.filter((w) => w.running).length + (r.team?.lead_running ? 1 : 0),
    review: counts.review || 0,
    blocked: counts.blocked || 0,
    total: Object.values(counts).reduce((a, b) => a + b, 0),
  };
}

/** 工作区路径太长，面板只显示最后两段（"项目/小说" 而不是 /Users/x/…/项目/小说）。 */
export function shortSpace(space: string): string {
  const parts = (space || "").split("/").filter(Boolean);
  return parts.slice(-2).join("/") || space || "";
}
