// The board must render a real workbench, not bare text lines.
//
// 2026-10-31: the first TeamView port arrived with none of upstream's `.team-*`
// stylesheet and half its markup, so the panel rendered as unstyled text with no
// cards, no status pills and no hierarchy — and nothing failed. These tests pin the
// structure that is visible (rows carry a title AND a status, groups carry counts,
// the detail pane opens, stats and roster render) so a stripped port cannot pass again.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    getTeamSummary: vi.fn(),
    getBoardItem: vi.fn(),
    getInbox: vi.fn(async () => []),
    resolveInboxItem: vi.fn(),
  };
});

import { getBoardItem, getTeamSummary } from "../api";
import type { TeamSummary, TeamTask, TeamWorker } from "../teamView";
import { TeamView } from "./TeamView";

const tokens = { input: 10, output: 5, cache_read: 1, cache_write: 0 };

function task(over: Partial<TeamTask> & { id: number }): TeamTask {
  return {
    title: "",
    description: "",
    criteria: "",
    state: "open",
    assignee: "",
    creator: "lead",
    refs: [],
    links: [],
    group: "queued",
    updated: 0,
    elapsed_s: 0,
    tokens: { ...tokens },
    ...over,
  } as TeamTask;
}

const worker: TeamWorker = {
  actor: "kanyu",
  role: "research",
  session_id: "w1",
  model: "m",
  workspace: "/w",
  running: true,
  tokens: { ...tokens },
  items: [1],
};

const summary = {
  team_id: "t1",
  space: "/w",
  lead_session: "lead",
  items: [
    task({ id: 1, title: "大纲v1", state: "in_progress", group: "working", assignee: "kanyu", status: "在写提纲" }),
    task({ id: 2, title: "章纲审核", state: "open", group: "queued" }),
    task({ id: 3, title: "地图考据", state: "done", group: "done" }),
  ],
  workers: [worker],
  lead: { ...worker, actor: "lead", role: "lead", session_id: "lead", running: false },
  counts: { waiting: 0, review: 0, working: 1, queued: 1, done: 1 },
  totals: {
    done: 1,
    total: 3,
    elapsed_s: 90,
    tokens: 16,
    tokens_by_kind: { ...tokens },
    asks_answered: 0,
    asks_waiting: 0,
  },
  timing: { model_ms: 10, tool_ms: 20, waited_ms: 30, queued_ms: 40 },
  asks: { approvals: { answered: 1, waiting: 0 } },
  usage_points: [],
  generated_at: 0,
} as unknown as TeamSummary;

afterEach(() => {
  cleanup(); // vitest has no auto-cleanup here; without it renders leak into each other
  vi.clearAllMocks();
});

beforeEach(() => {
  vi.mocked(getTeamSummary).mockResolvedValue(summary);
});

describe("TeamView", () => {
  it("renders task rows that carry a title AND a status line (never blank)", async () => {
    render(<TeamView sessionId="lead" teamId="t1" onOpenSession={vi.fn()} onClose={vi.fn()} />);
    const row = await screen.findByTestId("team-task-1");
    expect(row.textContent).toContain("大纲v1");
    expect(row.textContent).toContain("在写提纲");
    // the whole point of the port: no row may be an empty shell
    expect(row.textContent?.trim().length).toBeGreaterThan("大纲v1".length);
    expect(row.querySelector(".team-task-title")?.textContent).toBe("大纲v1");
    expect(row.querySelector(".board-dot")).toBeTruthy();
  });

  it("groups tasks by what they need from the human, with counts", async () => {
    render(<TeamView sessionId="lead" teamId="t1" onOpenSession={vi.fn()} onClose={vi.fn()} />);
    await screen.findByTestId("team-task-1");
    const headings = screen.getAllByText(/Working|Queued|Done/);
    expect(headings.length).toBeGreaterThanOrEqual(3);
    // counts ride the group heading
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(1);
  });

  it("opens the task detail pane when a row is clicked", async () => {
    vi.mocked(getBoardItem).mockResolvedValue({
      id: 1,
      title: "大纲v1",
      state: "in_progress",
      assignee: "kanyu",
      creator: "lead",
      description: "",
      criteria: "",
      timeline: [],
    } as never);
    render(<TeamView sessionId="lead" teamId="t1" onOpenSession={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByTestId("team-task-1"));
    const pane = await screen.findByTestId("team-task-detail");
    await waitFor(() => expect(pane.querySelector("[data-testid='board-detail']")).toBeTruthy());
    // the pane prints the title; ItemDetail must not print it a second time
    expect(pane.querySelectorAll("[data-testid='board-detail-title'], .board-detail-title").length).toBe(0);
  });

  it("shows the stats workbench: four cards, token split, asks table", async () => {
    render(<TeamView sessionId="lead" teamId="t1" onOpenSession={vi.fn()} onClose={vi.fn()} />);
    await screen.findByTestId("team-task-1");
    fireEvent.click(screen.getByRole("tab", { name: /Stats/ }));
    await waitFor(() => expect(document.querySelector(".team-stat-cards")).toBeTruthy());
    expect(document.querySelectorAll(".team-stat-cards > div").length).toBe(4);
    expect(document.querySelector(".team-asks")).toBeTruthy();
    expect(document.querySelector(".team-metric-bar")).toBeTruthy();
  });

  it("shows a searchable roster on the workers tab", async () => {
    render(<TeamView sessionId="lead" teamId="t1" onOpenSession={vi.fn()} onClose={vi.fn()} />);
    await screen.findByTestId("team-task-1");
    fireEvent.click(screen.getByRole("tab", { name: /Workers/ }));
    const row = await screen.findByTestId("team-worker-kanyu");
    expect(row.textContent).toContain("kanyu");
    expect(document.querySelector(".team-worker-search")).toBeTruthy();
  });
});
