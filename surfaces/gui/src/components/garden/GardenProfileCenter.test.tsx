/**
 * GardenProfileCenter 冒烟测试：花园包裹层渲染 + visit 上报 + 问候浮层。
 *
 * - 进入个人中心应自动 POST /garden/visit（上报进园）
 * - 顶部场景显示季节/昼夜标签、回园次数
 * - 花开横幅显示花朵 emoji 与花圃统计
 * - 返回的 triggered 事件渲染为可关闭的问候 toast
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GardenProfileCenter } from "./GardenProfileCenter";

function json(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

const GARDEN = {
  season: { season: { key: "autumn", label: "秋" }, daypart: { key: "day", label: "昼" } },
  flowers: [
    {
      flower_id: "milestone:20260902",
      kind: "sunflower",
      bed: "milestone",
      title: "第三章定稿",
      quote: "把创作圆桌的定稿收进花园",
      date: "2026-09-02",
      source_ts: "2026-09-02T10:00:00",
      mood_tint: "#E8551A",
    },
    {
      flower_id: "badge:20260901",
      kind: "badge_flower",
      bed: "badge",
      title: "连续七日",
      quote: "",
      date: "2026-09-01",
      source_ts: "2026-09-01T09:00:00",
      mood_tint: "#FF8A3D",
    },
  ],
  stats: { total: 2, by_bed: { milestone: 1, badge: 1 } },
  log: [],
  visit_count: 3,
  last_visit_date: "2026-09-02",
};

function stubFetch(triggered: unknown = null) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/garden/visit")) {
        return json({ triggered, season: GARDEN.season, visit_count: 3 });
      }
      if (url.includes("/garden")) return json(GARDEN);
      return json({});
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("GardenProfileCenter 花园包裹层", () => {
  it("进入时上报 visit 并渲染季节/昼夜/回园/花朵", async () => {
    const calls = stubFetch();
    render(<GardenProfileCenter onBack={() => {}} />);

    // visit 上报
    await vi.waitFor(() => expect(calls.some((u) => u.includes("/garden/visit"))).toBe(true));

    // 花园场景信息
    await vi.waitFor(() => {
      expect(screen.getByText("秋日花园")).toBeTruthy();
      expect(screen.getByText(/昼/)).toBeTruthy();
      expect(screen.getByText("第 3 次回到花园")).toBeTruthy();
    });

    // 花开横幅：标题 + 数量 + 花朵 emoji + 花圃统计
    await vi.waitFor(() => {
      expect(screen.getByText(/花园里的花/)).toBeTruthy();
      expect(screen.getByText("2")).toBeTruthy();
      expect(screen.getByText("🌻")).toBeTruthy();
      expect(screen.getByText("🏅")).toBeTruthy();
    });
    expect(screen.getByText("记忆圃")).toBeTruthy();
    expect(screen.getByText(/勋章圃/)).toBeTruthy();
  });

  it("visit 返回 triggered 事件时展示可关闭的问候", async () => {
    const calls = stubFetch({
      kind: "comeback",
      gap_days: 5,
      flower_delta: 2,
    });
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(calls.some((u) => u.includes("/garden/visit"))).toBe(true));
    await vi.waitFor(() => {
      expect(screen.getByText(/好久不见，你离开了 5 天，花园新开了 2 朵花/)).toBeTruthy();
    });

    // 可一键关闭
    fireEvent.click(screen.getByLabelText("关闭问候"));
    await vi.waitFor(() => {
      expect(screen.queryByText(/好久不见/)).toBeNull();
    });
  });

  it("无 triggered 时无问候浮层", async () => {
    const calls = stubFetch(null);
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(calls.some((u) => u.includes("/garden/visit"))).toBe(true));
    expect(screen.queryByRole("status")).toBeNull();
  });
});
