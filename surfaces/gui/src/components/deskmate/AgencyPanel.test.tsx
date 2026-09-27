/**
 * AgencyPanel 冒烟测试：L2 自主层任务面板前端交互。
 *
 * 覆盖：
 * 1. 空态（无任务）
 * 2. 待裁决提议卡渲染 + [执行][不用][以后别提] 三按钮
 * 3. 点「以后别提」→ 调 propose/resolve 且 decision=never → 提议卡消失
 * 4. 低风险已执行记录渲染（动作中文名 + 主题域 + 时间）
 *
 * stub 后端 /v1/deskmate/agency/tasks 与 /v1/deskmate/agency/propose/resolve。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AgencyPanel } from "./AgencyPanel";

const DONE_TASK = {
  id: "t_done_1",
  ts: "2026-09-06T11:41:08",
  action_type: "world_search",
  risk: "low",
  status: "done",
  topic: "历史研究",
  summary: "摄入 3 条新事实",
  detail: "【历史研究】某某讲座将于本月举办",
  count: 3,
};

const PROPOSED_TASK = {
  id: "t_prop_1",
  ts: "2026-09-06T11:45:00",
  action_type: "write_file",
  risk: "high",
  status: "proposed",
  topic: "",
  summary: "提议：写文件",
  detail: "写入文件到磁盘",
  count: 0,
};

/** P1-1 记忆主体冲突卡（2026-09-26 新增 action_type）。 */
const SUBJECT_CONFLICT_TASK = {
  id: "t_prop_subj",
  ts: "2026-09-26T10:00:00",
  action_type: "memory_subject_conflict",
  risk: "high",
  status: "proposed",
  topic: "记忆主体冲突",
  summary: "两条记忆把同一件事记在不同主体名下，需要你裁决",
  detail: "新条：小桔整理了史料库\n旧条：萧盛整理了史料库",
  count: 0,
};

/** 按 URL 路由的 fetch stub；resolveCalls 记录决议请求体，供断言。 */
function stubAgencyApi(opts: {
  tasks?: unknown[];
  pending?: unknown[];
  resolveCalls?: Array<{ id: string; decision: string }>;
  /** resolve 之后下一轮 tasks 返回什么（默认：清空提议卡） */
  afterResolve?: { tasks: unknown[]; pending: unknown[] };
}) {
  const tasks = opts.tasks ?? [];
  const pending = opts.pending ?? [];
  const resolveCalls = opts.resolveCalls ?? [];
  let resolved = false;

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/agency/tasks")) {
        const body = resolved && opts.afterResolve
          ? { tasks: opts.afterResolve.tasks, pending_proposals: opts.afterResolve.pending }
          : { tasks, pending_proposals: pending };
        return { ok: true, json: async () => body } as Response;
      }
      if (u.includes("/agency/propose/resolve")) {
        const body = JSON.parse(String(init?.body || "{}"));
        resolveCalls.push(body);
        resolved = true;
        return { ok: true, json: async () => ({ ok: true }) } as Response;
      }
      return { ok: true, json: async () => ({}) } as Response;
    }),
  );
  return resolveCalls;
}

describe("AgencyPanel", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("无任务时展示空态", async () => {
    stubAgencyApi({ tasks: [], pending: [] });
    render(<AgencyPanel />);
    await waitFor(() => expect(screen.getByText("小桔暂时在发呆……")).toBeTruthy());
    // 面板标题常驻
    expect(screen.getByText("小桔在忙")).toBeTruthy();
  });

  it("高风险提议卡渲染，带执行/不用/以后别提三按钮", async () => {
    stubAgencyApi({ tasks: [], pending: [PROPOSED_TASK] });
    render(<AgencyPanel />);
    await waitFor(() => expect(screen.getByTestId("agency-proposal")).toBeTruthy());
    // 中文动作名 + 需你确认
    expect(screen.getByText(/写文件 · 需你确认/)).toBeTruthy();
    expect(screen.getByText("执行")).toBeTruthy();
    expect(screen.getByText("不用")).toBeTruthy();
    expect(screen.getByText("以后别提")).toBeTruthy();
  });

  it("点「以后别提」发 resolve(never)，随后提议卡消失", async () => {
    const calls: Array<{ id: string; decision: string }> = [];
    stubAgencyApi({
      tasks: [],
      pending: [PROPOSED_TASK],
      resolveCalls: calls,
      afterResolve: { tasks: [], pending: [] },
    });
    render(<AgencyPanel />);
    await waitFor(() => expect(screen.getByTestId("agency-proposal")).toBeTruthy());

    fireEvent.click(screen.getByText("以后别提"));

    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]).toEqual({ id: "t_prop_1", decision: "never" });
    // 决议后重新拉取 → 提议卡不再出现
    await waitFor(() => expect(screen.queryByTestId("agency-proposal")).toBeNull());
  });

  it("低风险已执行记录渲染动作名、主题域与时间", async () => {
    stubAgencyApi({ tasks: [DONE_TASK], pending: [] });
    render(<AgencyPanel />);
    await waitFor(() => expect(screen.getByText("主动信息摄入")).toBeTruthy());
    expect(screen.getByText("历史研究")).toBeTruthy();
    expect(screen.getByText("摄入 3 条新事实")).toBeTruthy();
    // 2026-09-06T11:41:08 → 09-06 11:41
    expect(screen.getByText("09-06 11:41")).toBeTruthy();
  });

  it("记忆主体冲突卡显示中文动作名，内部键不得漏给用户", async () => {
    // 质检补（2026-09-26）：新增 action_type 若忘登记 RISK_LABEL，labelOf 会
    // 回退成内部英文键直接展示给用户。此用例锁住「记忆主体冲突」这条。
    stubAgencyApi({ tasks: [], pending: [SUBJECT_CONFLICT_TASK] });
    render(<AgencyPanel />);
    await waitFor(() => expect(screen.getByTestId("agency-proposal")).toBeTruthy());
    expect(screen.getByText(/记忆主体冲突 · 需你确认/)).toBeTruthy();
    expect(screen.queryByText(/memory_subject_conflict/)).toBeNull();
  });
});
