/**
 * MomentFeed 冒烟测试：微博样式双人时间线（后端接通版）。
 * stub 后端 API：moments / diary / post / comment / love。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MomentFeed } from "./MomentFeed";

function stubFetch() {
  const routes: Record<string, unknown> = {
    "/v1/deskmate/moments": { items: [], next_cursor: null, comments: {} },
    "/v1/deskmate/diary": { dates: [] },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const u = String(url);
      const method = (init?.method || "GET").toUpperCase();
      if (method === "POST" && u.includes("/moments/post")) {
        const body = JSON.parse(String(init?.body || "{}"));
        return {
          ok: true,
          json: async () => ({
            post: { id: "p1", content: body.content, createdAt: "2026-08-31T15:00:00" },
            reply: "这条我好好收着。",
          }),
        } as Response;
      }
      if (method === "POST" && u.includes("/moments/comment")) {
        const body = JSON.parse(String(init?.body || "{}"));
        return {
          ok: true,
          json: async () => ({
            comment: { id: "c1", author: "me", text: body.text, createdAt: "2026-08-31T15:01:00" },
          }),
        } as Response;
      }
      if (method === "POST" && u.includes("/love")) {
        return {
          ok: true,
          json: async () => ({ loved: true, memorized: true, message: "已悄悄记进记忆" }),
        } as Response;
      }
      if (method === "POST" && u.includes("/archive")) {
        const body = JSON.parse(String(init?.body || "{}"));
        return {
          ok: true,
          json: async () => ({ ok: true, archived: !!body.archived }),
        } as Response;
      }
      const hit = Object.entries(routes).find(([k]) => u.includes(k));
      return { ok: true, json: async () => (hit ? hit[1] : {}) } as Response;
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

async function writeDiary(text: string) {
  fireEvent.click(screen.getByText(/我也写一条/));
  const ta = screen.getByPlaceholderText(/写下今天发生的事/);
  fireEvent.change(ta, { target: { value: text } });
  await waitFor(() => expect((screen.getByText("发布") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByText("发布"));
}

describe("MomentFeed 微博时间线（后端版）", () => {
  it("渲染标题与写日记入口", () => {
    stubFetch();
    render(<MomentFeed />);
    expect(screen.getByText("我们的日记")).toBeTruthy();
    expect(screen.getByText(/我也写一条/)).toBeTruthy();
  });

  it("用户发日记：卡片出现，小桔真回复随附（展开留言可见）", async () => {
    stubFetch();
    render(<MomentFeed />);
    await writeDiary("今天把文稿定稿了，很开心。");
    expect(await screen.findByText(/今天把文稿定稿了/)).toBeTruthy();
    fireEvent.click(screen.getByText(/条留言/));
    expect(await screen.findByText("这条我好好收着。")).toBeTruthy();
  });

  it("🩷 落记忆：点击后变为已记进记忆", async () => {
    stubFetch();
    render(<MomentFeed />);
    await writeDiary("今天路过那棵桂花树。");
    await screen.findByText(/今天路过那棵桂花树/);
    fireEvent.click(screen.getByTitle("记进记忆"));
    expect(await screen.findByText("已记进记忆")).toBeTruthy();
    expect(await screen.findByText("已悄悄记进记忆")).toBeTruthy();
  });

  it("可在日记下留言并发送", async () => {
    stubFetch();
    render(<MomentFeed />);
    await writeDiary("晚安，今天先睡了。");
    await screen.findByText(/晚安，今天先睡了/);
    fireEvent.click(screen.getByText(/条留言|留言/));
    const input = screen.getByPlaceholderText("回复…");
    fireEvent.change(input, { target: { value: "明天见" } });
    fireEvent.click(screen.getByText("发送"));
    expect(await screen.findByText("明天见")).toBeTruthy();
  });

  it("cursor 分页：首屏渲染后点加载更多追加下一页，到底后按钮消失", async () => {
    const page1 = {
      items: [
        { id: "p1", content: "第一页甲", createdAt: "2026-08-31T12:00:00", loved: false, comments: [] },
        { id: "p2", content: "第一页乙", createdAt: "2026-08-30T12:00:00", loved: false, comments: [] },
      ],
      next_cursor: "2026-08-30T12:00:00|p2",
      comments: {},
    };
    const page2 = {
      items: [{ id: "p3", content: "第二页丙", createdAt: "2026-08-29T12:00:00", loved: false, comments: [] }],
      next_cursor: null,
      comments: {},
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL) => {
        const u = String(url);
        if (u.includes("/moments?") && u.includes("cursor=")) {
          return { ok: true, json: async () => page2 } as Response;
        }
        if (u.includes("/v1/deskmate/moments")) {
          return { ok: true, json: async () => page1 } as Response;
        }
        if (u.includes("/v1/deskmate/diary")) {
          return { ok: true, json: async () => ({ dates: [] }) } as Response;
        }
        return { ok: true, json: async () => ({}) } as Response;
      }),
    );
    render(<MomentFeed />);
    expect(await screen.findByText("第一页甲")).toBeTruthy();
    expect(screen.getByText("第一页乙")).toBeTruthy();
    expect(screen.getByText("加载更多日记")).toBeTruthy();
    fireEvent.click(screen.getByText("加载更多日记"));
    expect(await screen.findByText("第二页丙")).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("加载更多日记")).toBeNull());
  });

  it("每条日记可独立归档：归档后离开时间线收进「已归档」区，可取消归档恢复", async () => {
    const moments = {
      items: [
        { id: "p1", content: "要归档的这条", createdAt: "2026-08-31T12:00:00", loved: false, archived: false, comments: [] },
        { id: "p2", content: "留在时间线的这条", createdAt: "2026-08-30T12:00:00", loved: false, archived: false, comments: [] },
      ],
      next_cursor: null,
      comments: {},
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
        const u = String(url);
        const method = (init?.method || "GET").toUpperCase();
        if (method === "POST" && u.includes("/archive")) {
          const body = JSON.parse(String(init?.body || "{}"));
          return { ok: true, json: async () => ({ ok: true, archived: !!body.archived }) } as Response;
        }
        if (u.includes("/v1/deskmate/moments")) {
          return { ok: true, json: async () => moments } as Response;
        }
        if (u.includes("/v1/deskmate/diary")) {
          return { ok: true, json: async () => ({ dates: [] }) } as Response;
        }
        return { ok: true, json: async () => ({}) } as Response;
      }),
    );
    render(<MomentFeed />);
    expect(await screen.findByText("要归档的这条")).toBeTruthy();
    expect(screen.getByText("留在时间线的这条")).toBeTruthy();
    // 点第一条的「归档」（时间线按时间倒序，p1 在最前）
    fireEvent.click(screen.getAllByTitle("归档这条")[0]);
    await waitFor(() => expect(screen.queryByText("要归档的这条")).toBeNull());
    expect(await screen.findByText(/已归档（1）/)).toBeTruthy();
    // 展开已归档区
    fireEvent.click(screen.getByText(/已归档（1）/));
    expect(await screen.findByText("要归档的这条")).toBeTruthy();
    // 取消归档 → 回到时间线，已归档区消失
    fireEvent.click(screen.getByTitle("取消归档"));
    await waitFor(() => expect(screen.queryByText(/已归档（/)).toBeNull());
    expect(await screen.findByText("要归档的这条")).toBeTruthy();
  });
});
