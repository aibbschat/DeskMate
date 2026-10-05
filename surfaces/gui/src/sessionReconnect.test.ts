/**
 * Session 的保活 + 自动重连（2026-10-05）。
 *
 * 缺陷背景：web 端「浏览器一直开着 → 过一段时间 → 发送键变灰、快捷键发送也无响应」，
 * 刷新浏览器即好。根因是 Session 只建一次 socket、断开后无任何恢复：onclose 把
 * `connected` 置 false 永不回真 → Composer's send button 永久 disabled；且
 * `send()` 遇 CLOSED 既不发也不入队 → 消息静默丢弃。
 *
 * 这些用例锁住修复后的三条不变量：
 *  1. 非主动关闭的断开 → 会自动重连（指数退避），`connected` 能恢复 true。
 *  2. 断线期间 send() 的消息进 outbox，重连成功后由 flush() 补发（不丢）。
 *  3. 主动 close()（切会话/卸载）→ 不再重连、无幽灵 socket。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** 最小 WebSocket 替身：可控 readyState、记录 send、暴露 open/close 触发器。 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
  }

  /** 模拟服务端/代理完成握手 */
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  /** 模拟链路被掐断（非主动 close） */
  drop() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
}

const g = globalThis as unknown as {
  WebSocket?: unknown;
  __COWORKER_API_TOKEN__?: string;
  __DESKMATE_API_TOKEN__?: string;
};

let openSpy: ReturnType<typeof vi.fn>;
let originalWS: unknown;

beforeEach(() => {
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  originalWS = g.WebSocket;
  g.WebSocket = FakeWebSocket;
  // wsBase()/httpBase() 读这些注入量，缺失会抛。
  g.__COWORKER_API_TOKEN__ = "test-token";
  g.__DESKMATE_API_TOKEN__ = "test-token";
  openSpy = vi.fn((url: string) => new FakeWebSocket(url));
  (globalThis as unknown as { openWebSocket?: unknown }).openWebSocket = openSpy;
  (g as unknown as { openWebSocket?: unknown }).openWebSocket = openSpy;
});

afterEach(() => {
  vi.useRealTimers();
  g.WebSocket = originalWS;
  delete (globalThis as unknown as { openWebSocket?: unknown }).openWebSocket;
});

/** 动态 import：让 beforeEach 里的 WebSocket/openWebSocket 注入先生效。 */
async function loadSession() {
  const mod = await import("./api");
  return mod.Session;
}

const handlers = () => ({
  onEvent: vi.fn(),
  onOpen: vi.fn(),
  onClose: vi.fn(),
});

describe("Session 保活与自动重连", () => {
  it("断开后自动重连，且 onOpen 再次触发（connected 可恢复）", async () => {
    const Session = await loadSession();
    const h = handlers();
    const s = new Session("sess-1", "/ws", "deskmate", h);

    const first = FakeWebSocket.instances[0];
    first.open();
    expect(first.url).toContain("/ws/session/sess-1");
    expect(h.onOpen).toHaveBeenCalledTimes(1);

    // 链路被掐断 → onClose 触发（UI 变灰）
    first.drop();
    expect(h.onClose).toHaveBeenCalledTimes(1);

    // 退避重连（base 500ms）→ 新 socket 建立
    await vi.advanceTimersByTimeAsync(600);
    expect(FakeWebSocket.instances.length).toBe(2);

    // 新 socket 握手成功 → connected 恢复
    FakeWebSocket.instances[1].open();
    expect(h.onOpen).toHaveBeenCalledTimes(2);
    s.close();
  });

  it("断线期间 send() 的消息不丢：重连成功后补发", async () => {
    const Session = await loadSession();
    const h = handlers();
    const s = new Session("sess-2", "/ws", "deskmate", h);
    const first = FakeWebSocket.instances[0];
    first.open();

    // 链路断开
    first.drop();

    // 用户此时仍触发发送（旧实现：CLOSED 既不发也不入队 → 静默丢弃）
    s.userMessage("断线时说的话");

    await vi.advanceTimersByTimeAsync(600);
    const second = FakeWebSocket.instances[1];
    second.open();

    // flush() 应把排队消息补发出去
    const payloads = second.sent.map((s) => JSON.parse(s));
    expect(payloads.some((p) => p.type === "user_message" && p.text === "断线时说的话")).toBe(true);
    s.close();
  });

  it("close() 之后不再重连（切会话/卸载不泄漏幽灵 socket）", async () => {
    const Session = await loadSession();
    const h = handlers();
    const s = new Session("sess-3", "/ws", "deskmate", h);
    FakeWebSocket.instances[0].open();

    s.close();
    const countAfterClose = FakeWebSocket.instances.length;

    // 给足时间 + 多次退避周期，也不该再冒出新 socket
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeWebSocket.instances.length).toBe(countAfterClose);
  });

  it("重连成功后重置退避（连续断开仍能持续恢复）", async () => {
    const Session = await loadSession();
    const h = handlers();
    const s = new Session("sess-4", "/ws", "deskmate", h);
    FakeWebSocket.instances[0].open();

    // 第一轮：退避 base
    FakeWebSocket.instances[0].drop();
    await vi.advanceTimersByTimeAsync(600);
    FakeWebSocket.instances[1].open();

    // 第二轮：因onopen 重置 attempt，退避应回到 base（而非翻倍到 1000ms）
    FakeWebSocket.instances[1].drop();
    await vi.advanceTimersByTimeAsync(600);
    expect(FakeWebSocket.instances.length).toBe(3);
    s.close();
  });

  it("周期性发 ping 保活（防代理回收空闲连接）", async () => {
    const Session = await loadSession();
    const h = handlers();
    const s = new Session("sess-5", "/ws", "deskmate", h);
    const ws = FakeWebSocket.instances[0];
    ws.open();

    await vi.advanceTimersByTimeAsync(25_000);
    const kinds = ws.sent.map((x) => JSON.parse(x).type);
    expect(kinds).toContain("ping");
    s.close();
  });
});
