/**
 * useChat 冷启动在「站点口令没过」时的行为（2026-10-05 萧哥真机反馈）。
 *
 * 旧实现的毛病：第一次 /v1/sessions 拿到 401 就把 fault 定住，而 socket 又因为
 * fault 直接不拨 → 界面永久停在「凭据不对」，而用户其实只要输一次站点口令。
 * 这里钉三条：
 *  1. 带 Www-Authenticate 的 401 → fault 是 "site"（文案不该说"重新部署"）；
 *  2. 不做人工干预也会**自动重试**并在口令通过后恢复（fault 归 null、拿到 sessionId）；
 *  3. 恢复后才拨 WebSocket（一次失败不会永久锁死连接）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useChat } from "./useChat";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(t: string) { this.sent.push(t); }
  close() { this.readyState = 3; this.onclose?.(); }
  open() { this.readyState = 1; this.onopen?.(); }
}

const okJson = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const site401 = { ok: false, status: 401, headers: new Headers({ "www-authenticate": 'Basic realm="restricted"' }), json: async () => ({}) };

let sessionsCalls = 0;
let authed = false;

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  sessionsCalls = 0;
  authed = false;
  (globalThis as any).WebSocket = FakeSocket;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/v1/sessions?") || url.endsWith("/v1/sessions")) {
      sessionsCalls += 1;
      if (!authed && sessionsCalls <= 2) return site401 as never;
      authed = true;
      return okJson({ sessions: [{ session_id: "real01", title: "国子监 · 夜班", agent: "deskmate", workspace: "", model: "m", mode: "default", updated_at: new Date().toISOString(), messages: 12 }] }) as never;
    }
    if (url.includes("/messages")) return okJson({ messages: [] }) as never;
    if (url.includes("/v1/inbox")) return okJson({ items: [] }) as never;
    return okJson({}) as never;
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("冷启动遇到站点口令 401", () => {
  it("判成 site（不是 token、不是网络），并且会自动恢复", async () => {
    const { result } = renderHook(() => useChat());
    await act(async () => { vi.advanceTimersByTime(10); });

    expect(result.current.fault).toBe("site");
    expect(result.current.sessionId).toBeTruthy();
    // 口令没过时不拨 socket（避免拿着必然失败的凭据反复撞）
    expect(FakeSocket.instances).toHaveLength(0);

    // 有界自动重试：把退避时间走完，第 3 次请求就能过（桩只挡前两次）
    for (let i = 0; i < 6; i++) {
      await act(async () => { vi.advanceTimersByTime(25_000); });
      if (result.current.fault === null) break;
    }
    expect(result.current.fault).toBeNull();
    expect(sessionsCalls).toBeGreaterThan(2);

    // 恢复之后才建 socket
    await act(async () => { vi.advanceTimersByTime(10); });
    expect(FakeSocket.instances.length).toBeGreaterThan(0);
    expect(FakeSocket.instances[0].url).toContain("/ws/session/real01");
    expect(FakeSocket.instances[0].url).toContain("agent=deskmate");
  });

  it("一次成功时不产生多余重试", async () => {
    authed = true; // 口令已经过
    const { result } = renderHook(() => useChat());
    await act(async () => { vi.advanceTimersByTime(20); });
    expect(result.current.fault).toBeNull();
    expect(result.current.title).toBe("国子监 · 夜班");
    const before = sessionsCalls;
    await act(async () => { vi.advanceTimersByTime(60_000); });
    expect(sessionsCalls).toBe(before); // 没故障就不该反复打
  });
});
