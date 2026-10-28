import { describe, expect, it } from "vitest";
import type { TeamRoster, TeamRosterItem } from "./api";
import { groupIntoColumns, shortSpace, teamSummary, workerStatus } from "./teamRoster";

const item = (id: number, state: string, assignee = "writer"): TeamRosterItem => ({
  id,
  title: `t${id}`,
  state,
  assignee,
  updated_at: "",
});

const roster = (over: Partial<TeamRoster> = {}): TeamRoster => ({
  team: {
    team_id: "team1",
    space: "/Users/x/项目/小说",
    lead_session: "lead1",
    lead_actor: "cowork:lead",
    paused: false,
    chat_enabled: false,
    created_at: "",
    lead_running: false,
    lead: { counts: {}, current: "" },
  },
  role: "lead",
  workers: [],
  counts: {},
  items: [],
  ...over,
});

describe("teamRoster", () => {
  it("folds items into board columns, ordered by id inside a column", () => {
    const cols = groupIntoColumns([item(3, "open"), item(1, "open"), item(2, "review")]);
    expect(cols.open.map((i) => i.id)).toEqual([1, 3]);
    expect(cols.review.map((i) => i.id)).toEqual([2]);
    expect(cols.done).toEqual([]);
  });

  it("parks an unknown state under 待办 instead of dropping the item", () => {
    // 后端加了新状态而前端还没跟上时，宁可多显示一条，也不能让它凭空消失。
    const cols = groupIntoColumns([item(1, "escalated")]);
    expect(cols.open.map((i) => i.id)).toEqual([1]);
  });

  it("reads live from the engine first, then infers from the board", () => {
    const base = {
      actor: "writer",
      persona: "historian",
      session_id: "s1",
      model: "",
      reason: "",
      current: "写第一章",
    };
    expect(workerStatus({ ...base, running: true, counts: {} }).tone).toBe("live");
    // 引擎没在跑，但手里有 in_progress 的活 → 等唤醒，不是空闲
    expect(workerStatus({ ...base, running: false, counts: { in_progress: 1 } }).tone).toBe("busy");
    expect(workerStatus({ ...base, running: false, counts: { blocked: 1 } }).label).toBe("卡住");
    expect(workerStatus({ ...base, running: false, counts: {} }).tone).toBe("idle");
  });

  it("counts the lead as running too — it is a real engine", () => {
    const r = roster({
      team: { ...roster().team!, lead_running: true },
      workers: [
        { actor: "a", persona: "p", session_id: "s1", model: "", reason: "", running: true, counts: {}, current: "" },
        { actor: "b", persona: "p", session_id: "s2", model: "", reason: "", running: false, counts: {}, current: "" },
      ],
      counts: { review: 2, blocked: 1, done: 3 },
    });
    expect(teamSummary(r)).toMatchObject({ workers: 2, running: 2, review: 2, blocked: 1, total: 6 });
  });

  it("shortens a workspace path to its last two segments", () => {
    expect(shortSpace("/Users/x/项目/小说")).toBe("项目/小说");
    expect(shortSpace("")).toBe("");
  });
});
