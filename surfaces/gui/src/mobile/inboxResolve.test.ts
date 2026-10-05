/**
 * 钉住收件箱 REST 的 resolution 词表。
 *
 * 这套词表和 WS 那套**不一样**：`coworker/inbox.py:393-405` 是
 * `"always"→ALWAYS_TOOL`、`"allow"→ONCE`、**其它一律 DENY**。
 * 也就是说手机端如果把 WS 的 "once" 直接发过来，服务端不认识 → 判成拒绝，
 * 而且不报错、不提示——用户看到的是"我点了允许，她怎么还是没动"。
 * 这几个断言存在的唯一理由就是防这个。
 */
import { describe, expect, it } from "vitest";
import type { InboxItem } from "../api";
import { actionsFor, resolutionFor } from "./inboxResolve";

const item = (over: Partial<InboxItem>): InboxItem =>
  ({
    id: "i1",
    session_id: "s1",
    kind: "approval",
    title: "Run `write_file`?",
    body: "要写稿",
    state: "pending",
    resolution: null,
    inbox: "default",
    created_at: "",
    resolved_at: null,
    ...over,
  }) as InboxItem;

describe("收件箱 resolution 词表", () => {
  it("审批：允许=allow、拒绝=deny（绝不能是 WS 那套 once）", () => {
    const a = item({});
    expect(resolutionFor(a, "allow")).toBe("allow");
    expect(resolutionFor(a, "deny")).toBe("deny");
    expect(resolutionFor(a, "allow")).not.toBe("once");
    expect(resolutionFor(a, "nope")).toBeNull();
  });

  it("手机端审批只给两档：不给 always_tool / always_command 这类大范围放行", () => {
    const keys = actionsFor(item({})).map((x) => x.key);
    expect(keys).toEqual(["allow", "deny"]);
  });

  it("目录授权：JSON {granted,path,writable}，与桌面 InboxItemCard 同形", () => {
    const d = item({ kind: "directory", data: { path: "/Users/x/Documents", writable: false } });
    expect(JSON.parse(resolutionFor(d, "grant")!)).toEqual({
      granted: true,
      path: "/Users/x/Documents",
      writable: false,
    });
    expect(JSON.parse(resolutionFor(d, "deny")!)).toEqual({ granted: false });
    // primary（设为工作区）时才强制 writable=true
    const p = item({ kind: "directory", data: { path: "/w", primary: true } });
    expect(JSON.parse(resolutionFor(p, "grant")!)).toEqual({ granted: true, path: "/w", writable: true });
  });

  it("计划：批准带 mode=interactive，拒绝带空 feedback", () => {
    const pl = item({ kind: "plan" });
    expect(JSON.parse(resolutionFor(pl, "yes")!)).toEqual({ approved: true, mode: "interactive" });
    expect(JSON.parse(resolutionFor(pl, "no")!)).toEqual({ approved: false, feedback: "" });
  });

  it("通知：知道了 = seen", () => {
    expect(resolutionFor(item({ kind: "notification" }), "seen")).toBe("seen");
  });

  it("提问：点选项 = 选项文案；没有的索引返回 null（不发空串）", () => {
    const q = item({ kind: "question", options: ["留", "砍掉"], allow_text: true });
    expect(resolutionFor(q, "q0")).toBe("留");
    expect(resolutionFor(q, "q1")).toBe("砍掉");
    expect(resolutionFor(q, "q9")).toBeNull();
    expect(actionsFor(q).map((a) => a.label)).toEqual(["留", "砍掉"]);
  });
});
