/**
 * MomentFeed 冒烟测试：微博样式双人时间线（后端接通版）。
 * stub 后端 API：moments / diary / post / comment / love。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MomentFeed } from "./MomentFeed";

function stubFetch() {
  const routes: Record<string, unknown> = {
    "/v1/deskmate/moments": { userPosts: [], comments: {}, loved: [] },
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
});
