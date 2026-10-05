import { describe, expect, it } from "vitest";
import type { SessionInfo } from "../types";
import { classify401, connFaultLabel, faultCopy, newSessionId, pickSession } from "./session";

const s = (over: Partial<SessionInfo>): SessionInfo =>
  ({
    session_id: "x",
    workspace: "",
    agent: "deskmate",
    model: "m",
    mode: "default",
    updated_at: null,
    messages: 1,
    ...over,
  }) as SessionInfo;

describe("手机端冷启动选会话", () => {
  it("没有任何可续的会话时返回 null（调用方才去 mint 新 id）", () => {
    expect(pickSession([])).toBeNull();
    expect(pickSession([s({ agent: "cowork" })])).toBeNull();
    expect(pickSession([s({ archived: true })])).toBeNull();
    expect(pickSession([s({ messages: 0 })])).toBeNull(); // 开过没说话的壳
  });

  it("取最近活跃的那条，置顶优先", () => {
    const list = [
      s({ session_id: "old", updated_at: "2026-10-01T10:00:00Z" }),
      s({ session_id: "new", updated_at: "2026-10-05T10:00:00Z" }),
      s({ session_id: "pinned-old", updated_at: "2026-09-20T10:00:00Z", pinned: true }),
    ];
    expect(pickSession(list)?.session_id).toBe("pinned-old");
    expect(pickSession(list.filter((x) => !x.pinned))?.session_id).toBe("new");
  });

  it("时间缺失/非法也不崩，等值时按 id 稳定排（同一输入两次选出同一条）", () => {
    const list = [s({ session_id: "b", updated_at: "not-a-date" }), s({ session_id: "a", updated_at: null })];
    expect(pickSession(list)?.session_id).toBe("a");
    expect(pickSession(list)?.session_id).toBe("a");
  });

  it("新会话 id 是 12 位、两次不同", () => {
    const a = newSessionId();
    expect(a).toHaveLength(12);
    expect(new Set([a, newSessionId()]).size).toBe(2);
  });

  it("1008 说「凭据不对」，其它说「连不上服务器」——两种故障不能混成一句话", () => {
    expect(connFaultLabel(1008).title).toContain("凭据");
    expect(connFaultLabel().title).toContain("连不上");
  });

  // 2026-10-05 真机：Caddy 的 401 被我误报成「重新部署前端」，把人带偏了一次。
  describe("401 分类（云端实测的两种响应形状）", () => {
    const h = (init: Record<string, string>) => new Headers(init);
    it("Caddy：带 Www-Authenticate: Basic → site（不是部署问题）", () => {
      expect(classify401({ status: 401, headers: h({ "www-authenticate": 'Basic realm="restricted"' }) })).toBe("site");
    });
    it("后端：JSON 错误、无 Www-Authenticate → token", () => {
      expect(classify401({ status: 401, headers: h({ "content-type": "application/json" }) })).toBe("token");
    });
    it("site 的文案必须明说「不用重新部署」，只有 token 才让人去重新部署", () => {
      expect(faultCopy("site")!.title).toContain("站点口令");
      expect(faultCopy("site")!.hint).toContain("不需要重新部署"); // 反过来告诉他，别再被带偏
      expect(faultCopy("token")!.hint).toContain("需重新部署前端");
      expect(faultCopy("token")!.hint).not.toMatch(/不需要重新部署/);
      expect(faultCopy(null)).toBeNull();
      expect(faultCopy("net")!.title).toContain("连不上");
    });
  });
});
