import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ScheduledView } from "./ScheduledView";

// Autonomy §C1 behavioural tests — static "is the string wired" checks only
// prove the code exists; these prove the user's click actually reaches the API.
// Hermetic fetch stub (same convention as PersonaView.test.tsx), routing by the
// most specific URL first.
type Call = { url: string; method: string; body: any };

function stubFetch(routes: { match: string; method?: string; json: any }[]) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    for (const r of routes) {
      if (url.includes(r.match) && (!r.method || r.method === method)) {
        return { ok: true, json: async () => r.json } as Response;
      }
    }
    return { ok: true, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const TASK = {
  id: "task-1",
  title: "盯一个公开页面",
  instructions: "有变化再报告",
  schedule: "Daily 09:00",
  schedule_raw: { kind: "cron", cron: "0 9 * * *" },
  workspace: "/tmp/w",
  agent: "deskmate",
  enabled: true,
  next_run: null,
  last_run: null,
  last_status: null,
  run_count: 2,
  notify_on_completion: false,
  goal_mode: "watch",
  goal_generation: 0,
  always_allowed: [],
};

const LIVE_RUN = {
  run_id: "run-1",
  task_id: "task-1",
  session_id: "__run__run-1",
  started_at: 1730000000,
  finished_at: null,
  status: "running",
  result_text: null,
  artifacts: [],
  error: null,
  trigger: "schedule",
  current_step: "第 2 步执行中",
  event_count: 7,
  checkpoint: { goal: { state: "hash=abc 截止日未变" } },
};

function routes() {
  return [
    { match: "/v1/automations/task-1/runs/run-1/cancel", method: "POST", json: { ok: true } },
    { match: "/v1/automations/task-1/seen", method: "POST", json: { ok: true } },
    { match: "/v1/automations/task-1", method: "PATCH", json: { ok: true, task: TASK } },
    { match: "/v1/automations/task-1", method: "GET", json: { task: TASK, runs: [LIVE_RUN] } },
    { match: "/v1/automations", method: "POST", json: { ok: true, task: { ...TASK, id: "task-new" } } },
    { match: "/v1/automations", method: "GET", json: { tasks: [TASK] } },
  ];
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("C1 goal-mode launch", () => {
  it("unchecked switch keeps the ordinary one-shot default (no goal_mode)", async () => {
    const calls = stubFetch(routes());
    render(<ScheduledView onOpenRun={vi.fn()} onRunNow={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /New automation/ }));
    fireEvent.change(screen.getByPlaceholderText(/Title \(e\.g\./), { target: { value: "每日摘要" } });
    fireEvent.change(screen.getByPlaceholderText(/What should it do each run/), { target: { value: "整理笔记" } });
    fireEvent.click(screen.getByRole("button", { name: "Create automation" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.includes("/v1/automations"))).toBe(true));
    const created = calls.find((c) => c.method === "POST" && c.url.includes("/v1/automations"));
    expect(created?.body.goal_mode ?? "").toBe("");
  });

  it("checking 持续盯梢 sends goal_mode=watch to the create endpoint", async () => {
    const calls = stubFetch(routes());
    const { container } = render(<ScheduledView onOpenRun={vi.fn()} onRunNow={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /New automation/ }));
    const toggle = container.querySelector(".goal-toggle-row input") as HTMLInputElement;
    expect(toggle).not.toBeNull();
    fireEvent.click(toggle);
    fireEvent.change(screen.getByPlaceholderText(/Title \(e\.g\./), { target: { value: "盯通知" } });
    fireEvent.change(screen.getByPlaceholderText(/What should it do each run/), { target: { value: "变了再报告" } });
    fireEvent.click(screen.getByRole("button", { name: "Create automation" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.body?.goal_mode === "watch")).toBe(true));
  });
});

describe("C1 run card controls", () => {
  it("a live run shows honest progress + a real 叫停 call", async () => {
    const calls = stubFetch(routes());
    render(<ScheduledView onOpenRun={vi.fn()} onRunNow={vi.fn()} initialOpenId="task-1" />);
    await waitFor(() => expect(screen.getByText(/第 2 步执行中/)).toBeTruthy());
    expect(screen.getByTestId("run-progress")).toBeTruthy();
    expect(screen.getByText(/上轮状态：hash=abc/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /叫停/ }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/runs/run-1/cancel"))).toBe(true),
    );
  });

  it("目标代次 is shown and 重启目标 sends restart_goal", async () => {
    const calls = stubFetch(routes());
    render(<ScheduledView onOpenRun={vi.fn()} onRunNow={vi.fn()} initialOpenId="task-1" />);
    await waitFor(() => expect(screen.getByText(/持续盯梢 · 第 1 代/)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "重启目标" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "PATCH" && c.body?.restart_goal === true)).toBe(true),
    );
  });
});
