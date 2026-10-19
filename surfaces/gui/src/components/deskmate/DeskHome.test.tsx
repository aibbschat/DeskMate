/**
 * DeskHome 测试：开聊即递（2026-10-19 修复首页主动开场白断链）。
 *
 * 覆盖：
 * 1. 有待办 → 挂载即拉 GET handoff，并**让她先开口**（POST speak 把条目落进会话）
 * 2. 今日已递过的条目（delivered_at 为今日）不复述——不追重
 * 3. 无待办 → 不调用 speak（静默，不阻塞首页）
 * 4. localStorage 按日去重：同一天重新挂载不重复说
 * 5. speak 失败 → 不放 localStorage（允许后续重试）
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { DeskHome } from "./DeskHome";

interface SpeakCall {
  id: string;
  session_id: string;
}

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}T08:00:00`;
}

/** stub 后端：GET handoff 返待办，POST speak 记账；agency 面板返空态。 */
function stubHandoffApi(opts: { items?: unknown[]; speakOk?: boolean }) {
  const speakCalls: SpeakCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/proactive/handoff/speak")) {
        speakCalls.push(JSON.parse(String(init?.body || "{}")));
        return {
          ok: opts.speakOk !== false,
          json: async () => ({ ok: opts.speakOk !== false }),
        } as Response;
      }
      if (u.includes("/proactive/handoff")) {
        return {
          ok: true,
          json: async () => ({ text: "对了，有几件事想跟你说", items: opts.items ?? [] }),
        } as Response;
      }
      // AgencyPanel 等其它面板：空态
      return { ok: true, json: async () => ({ tasks: [], pending_proposals: [] }) } as Response;
    }),
  );
  return speakCalls;
}

const ITEM = {
  id: "cloud_audit:plot_contradiction:abc123",
  source: "cloud_audit",
  kind: "plot_contradiction",
  content: "你《国子监秘闻录》第二章的伏笔和第五章对不上",
  evidence: "第二章：……；第五章：……",
  understanding: "她在核对双线伏笔时发现的",
  status: "pending",
  confidence: 0.9,
  created_at: "2026-10-19T07:00:00",
  source_node: "mac",
};

describe("DeskHome 开聊即递", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("有待办 → 挂载即让她先开口（POST speak 落会话）", async () => {
    const speakCalls = stubHandoffApi({ items: [ITEM] });

    render(<DeskHome sessionId="sess-1" />);

    await waitFor(() => expect(speakCalls.length).toBe(1));
    expect(speakCalls[0]).toEqual({
      id: ITEM.id,
      session_id: "sess-1",
    });
    // 说成功 → 按日记账（重启/重挂载不重复）
    const spoken = JSON.parse(window.localStorage.getItem("dm:handoff-spoken") || "{}");
    expect(spoken.ids).toEqual([ITEM.id]);
    expect(spoken.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("今日已递过的条目不复述（不追重）", async () => {
    const speakCalls = stubHandoffApi({
      items: [{ ...ITEM, delivered_at: todayISO() }],
    });

    render(<DeskHome sessionId="sess-2" />);

    // 给足异步窗口，确认始终不调 speak
    await new Promise((r) => setTimeout(r, 50));
    expect(speakCalls.length).toBe(0);
  });

  it("无待办 → 静默，不调用 speak", async () => {
    const speakCalls = stubHandoffApi({ items: [] });

    render(<DeskHome sessionId="sess-3" />);

    await new Promise((r) => setTimeout(r, 50));
    expect(speakCalls.length).toBe(0);
  });

  it("同一天重新挂载不重复说（localStorage 按日去重）", async () => {
    const speakCalls = stubHandoffApi({ items: [ITEM] });

    const first = render(<DeskHome sessionId="sess-4" />);
    await waitFor(() => expect(speakCalls.length).toBe(1));
    first.unmount();

    render(<DeskHome sessionId="sess-4" />);
    await new Promise((r) => setTimeout(r, 50));
    expect(speakCalls.length).toBe(1); // 没有第二次
  });

  it("speak 失败 → 不记账（允许重试），也不崩", async () => {
    const speakCalls = stubHandoffApi({ items: [ITEM], speakOk: false });

    render(<DeskHome sessionId="sess-5" />);

    await waitFor(() => expect(speakCalls.length).toBe(1));
    expect(window.localStorage.getItem("dm:handoff-spoken")).toBeNull();
  });
});
