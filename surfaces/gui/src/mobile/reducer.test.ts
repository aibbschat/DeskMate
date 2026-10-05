/**
 * 手机端 reducer 的单测。重点是三条"错了不报错、只是行为不对"的契约：
 * 未知事件必须活下来、turn_done 才是收尾、答完的交互要回写 resolved。
 */
import { describe, expect, it } from "vitest";
import type { WsEvent } from "../types";
import { applyEvent, emptyChat, pendingInteractive, resolveLast, withLocalUser } from "./reducer";

const ev = (type: string, data: Record<string, unknown> = {}): WsEvent =>
  ({ type, data } as unknown as WsEvent);

describe("手机端对话 reducer", () => {
  it("未知事件（含服务端有、types.ts 没有的 tool_requested/team_proposed/items_proposed）不抛错也不改状态", () => {
    for (const t of ["tool_requested", "team_proposed", "items_proposed", "inbound", "iteration_end", "nonsense_event"]) {
      const s = emptyChat();
      const withMsg = applyEvent(s, ev("assistant_message", { text: "hi" }));
      expect(() => applyEvent(withMsg, ev(t))).not.toThrow();
      expect(applyEvent(withMsg, ev(t))).toBe(withMsg); // 原样返回，不制造幽灵气泡
    }
  });

  it("流式只进 streaming，assistant_message 才落成一条气泡", () => {
    let s = applyEvent(emptyChat(), ev("turn_start", { input: "在吗" }));
    s = applyEvent(s, ev("assistant_delta", { text: "你" }));
    s = applyEvent(s, ev("assistant_delta", { text: "好" }));
    expect(s.items).toHaveLength(1); // 只有那条 user
    expect(s.streaming).toBe("你好");
    s = applyEvent(s, ev("assistant_message", { text: "你好呀" }));
    expect(s.streaming).toBe("");
    expect(s.items.map((i) => i.kind)).toEqual(["user", "assistant"]);
  });

  it("turn_start 带 input 时不重复追加（前台已乐观上屏），后台投递才补一条", () => {
    let s = withLocalUser(emptyChat(), "挪到章末？");
    s = applyEvent(s, ev("turn_start", { input: "挪到章末？" }));
    expect(s.items.filter((i) => i.kind === "user")).toHaveLength(1);

    const s2 = applyEvent(emptyChat(), ev("turn_start", { input: "云端自己醒的" }));
    expect(s2.items.filter((i) => i.kind === "user")).toHaveLength(1);
  });

  it("工具行：proposed 挂 …，finished 收尾；只收到 finished（断线重连后）也补一条完结行", () => {
    let s = applyEvent(emptyChat(), ev("tool_proposed", { name: "read_file", arguments: { path: "a.md" } }));
    expect(s.toolCount).toBe(1);
    expect(s.items[0]).toMatchObject({ kind: "tool", name: "read_file", status: "…" });
    s = applyEvent(s, ev("tool_finished", { name: "read_file", status: "ok", result_preview: "…" }));
    expect(s.items[0]).toMatchObject({ status: "ok" });

    const orphan = applyEvent(emptyChat(), ev("tool_finished", { name: "grep", status: "ok" }));
    expect(orphan.items).toHaveLength(1);
    expect(orphan.items[0]).toMatchObject({ kind: "tool", name: "grep", status: "ok" });
  });

  it("审批：未答时挂在输入区上方，答完回写 resolved 后不再挂", () => {
    let s = applyEvent(emptyChat(), ev("permission_required", { name: "write_file", arguments: {}, reason: "要写稿" }));
    expect(pendingInteractive(s.items)?.kind).toBe("approval");
    s = { ...s, items: resolveLast(s.items, "approval", "once") };
    expect(pendingInteractive(s.items)).toBeNull();
    expect(s.items[0]).toMatchObject({ kind: "approval", resolved: "once" });
  });

  it("turn_done 才是收尾；error/interrupted 把半截流式落成气泡（不吞字）", () => {
    let s = applyEvent(emptyChat(), ev("turn_start", {}));
    s = applyEvent(s, ev("assistant_delta", { text: "写到一半" }));
    s = applyEvent(s, ev("turn_done", {}));
    expect(s.running).toBe(false);
    expect(s.streaming).toBe("");
    expect(s.items.some((i) => i.kind === "assistant" && i.text === "写到一半")).toBe(false); // 空 assistant_message 前不硬造

    let e = applyEvent(emptyChat(), ev("turn_start", {}));
    e = applyEvent(e, ev("assistant_delta", { text: "半截话" }));
    e = applyEvent(e, ev("error", { error: "上游超时" }));
    expect(e.running).toBe(false);
    expect(e.items.some((i) => i.kind === "assistant" && i.text === "半截话")).toBe(true);
    const tail = e.items[e.items.length - 1];
    expect(tail).toMatchObject({ kind: "notice", tone: "warn", retriable: true });
  });

  it("report 卡片：assistant_message 带 report/report_name 时落 report 条，不是纯文本", () => {
    const s = applyEvent(emptyChat(), ev("assistant_message", { text: "审阅完了", report: { a: 1 }, report_name: "r1" }));
    expect(s.items[0]).toMatchObject({ kind: "report", name: "r1", caption: "审阅完了" });
  });
});
