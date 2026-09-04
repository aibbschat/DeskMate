/**
 * GardenProfileCenter 冒烟测试：沉浸式花园全景（2026-09-02 重构）。
 *
 * - 点「个人中心」直接是花园全景（不再有院门页），挂载即 POST /garden/visit
 * - 花园本身就是界面：树屋书房/花丛/花园的门/朋友的小窝/工具棚都是园里的物件
 * - 点「花园的门」→ 功能菜单（它眼中的我/长期记忆/统计与默契/勋章墙/访客模式）
 * - 点「花丛」→ 花圃面板（真实 SVG 花朵）
 * - visit 返回 triggered 事件时渲染可关闭的问候
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
      mood_tint: "顺利",
    },
    {
      flower_id: "badge:20260901",
      kind: "badge_flower",
      bed: "badge",
      title: "连续七日",
      quote: "",
      date: "2026-09-01",
      source_ts: "2026-09-01T09:00:00",
      mood_tint: "顺利",
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
      if (url.includes("/garden/storybook")) return json({ bond: {} });
      if (url.includes("/garden")) return json(GARDEN);
      if (url.includes("/growth")) return json({ level: 3, achievements: [{ unlocked: true }] });
      return json({});
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("GardenProfileCenter 沉浸式花园", () => {
  it("进入直接渲染花园全景（无院门页），并上报 visit", async () => {
    const calls = stubFetch();
    render(<GardenProfileCenter onBack={() => {}} />);

    // 挂载即上报进园
    await vi.waitFor(() => expect(calls.some((u) => u.includes("/garden/visit"))).toBe(true));

    // 没有院门页
    expect(screen.queryByText("推开院门")).toBeNull();
    expect(screen.queryByText("小桔园")).toBeNull();

    // 花园全景：季节 + 园里的物件都在画面里
    await vi.waitFor(() => {
      expect(screen.getByText("秋日花园")).toBeTruthy();
      expect(screen.getByText(/第 3 次回到花园/)).toBeTruthy();
    });
    expect(screen.getByText("花园的门")).toBeTruthy();
    expect(screen.getByText("树屋书房")).toBeTruthy();
    expect(screen.getByText("朋友的小窝")).toBeTruthy();
    expect(screen.getByText("工具棚")).toBeTruthy();
    expect(screen.getByText("花丛")).toBeTruthy();
  });

  it("点「花园的门」弹出功能菜单，各功能才显示", async () => {
    stubFetch();
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(screen.getByText("花园的门")).toBeTruthy());

    // 未点门前，功能菜单不可见
    expect(screen.queryByText("它眼中的我")).toBeNull();

    fireEvent.click(screen.getByText("花园的门"));
    await vi.waitFor(() => {
      expect(screen.getByText("它眼中的我")).toBeTruthy();
      expect(screen.getByText("长期记忆")).toBeTruthy();
      expect(screen.getByText("统计与默契")).toBeTruthy();
      expect(screen.getByText("勋章墙")).toBeTruthy();
      expect(screen.getByText("访客模式")).toBeTruthy();
    });
  });

  it("点「花丛」弹出花圃面板，显示真实 SVG 花朵", async () => {
    stubFetch();
    const { container } = render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(screen.getByText("花丛")).toBeTruthy());

    fireEvent.click(screen.getByText("花丛"));
    await vi.waitFor(() => {
      expect(screen.getByText(/朵花 · 情绪定色/)).toBeTruthy();
      expect(container.querySelectorAll(".garden-flower-svg").length).toBe(2);
    });
  });

  it("visit 返回 triggered 事件时展示可关闭的问候", async () => {
    stubFetch({ kind: "comeback", gap_days: 5, flower_delta: 2 });
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => {
      expect(screen.getByText(/好久不见，你离开了 5 天，花园新开了 2 朵花/)).toBeTruthy();
    });

    fireEvent.click(screen.getByLabelText("关闭问候"));
    await vi.waitFor(() => {
      expect(screen.queryByText(/好久不见/)).toBeNull();
    });
  });

  it("无 triggered 时无问候浮层", async () => {
    stubFetch(null);
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(screen.getByText("花园的门")).toBeTruthy());
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("点「花园的门 → 它眼中的我」打开全屏懂我中心（AlterEgoPage）而非旧面板", async () => {
    stubFetch();
    render(<GardenProfileCenter onBack={() => {}} />);
    await vi.waitFor(() => expect(screen.getByText("花园的门")).toBeTruthy());

    // 打开门 → 菜单
    fireEvent.click(screen.getByText("花园的门"));
    await vi.waitFor(() => expect(screen.getByText("它眼中的我")).toBeTruthy());

    // 点「它眼中的我」→ 应打开 AlterEgoPage（副标题「从你身上学到的」是其独有文案）
    fireEvent.click(screen.getByText("它眼中的我"));
    await vi.waitFor(() => {
      expect(screen.getByText(/从你身上学到的/)).toBeTruthy();
      // 全屏页有「关闭」按钮（旧面板用的是面板 ✕）
      expect(screen.getByText("关闭")).toBeTruthy();
    });

    // 旧面板独有的 sub 文案不应出现
    expect(screen.queryByText(/角落已就绪/)).toBeNull();
  });
});
