import { describe, expect, it } from "vitest";
import type { InboxItem } from "./api";
import type { Item } from "./types";
import { dedupeGates, expireOpenGates, hasOpenGates, reconcileResolvedGates, retireFinishedGate } from "./gateReconciliation";

const approval = (over: Partial<Extract<Item, { kind: "approval" }>> = {}): Item => ({
  kind: "approval",
  name: "run_shell",
  args: { command: "ls" },
  reason: "",
  ...over,
});

const inbox = (over: Partial<InboxItem> = {}): InboxItem => ({
  id: "i1",
  session_id: "s1",
  kind: "approval",
  title: "t",
  body: "b",
  state: "resolved",
  resolution: "once",
  inbox: "inbox",
  created_at: "",
  resolved_at: "",
  ...over,
});

describe("gateReconciliation", () => {
  describe("retireFinishedGate", () => {
    it("retires the approval card whose tool call just finished", () => {
      const items = [approval({ toolCallId: "call-1" }), approval({ toolCallId: "call-2" })];
      const out = retireFinishedGate(items, "run_shell", "call-1");
      expect(out).toHaveLength(1);
      expect((out[0] as { toolCallId?: string }).toolCallId).toBe("call-2");
    });

    it("never matches by title when the id is missing", () => {
      // 同一个工具、同样的参数，但那是**另一张**卡：不给 id 就什么都不动。
      const items = [approval()];
      expect(retireFinishedGate(items, "run_shell", undefined)).toBe(items);
    });

    it("keeps the answered card — only the still-clickable one is a zombie", () => {
      // 答过的卡是这一轮的历史（带"已批准"chip）；删了等于凭空少一段记录。
      const items = [approval({ toolCallId: "call-1", resolved: "once" })];
      expect(retireFinishedGate(items, "run_shell", "call-1")).toBe(items);
    });

    it("retires propose_team's gate by kind, not by name alone", () => {
      const items: Item[] = [
        { kind: "teamreq", members: [{ persona: "historian" }], toolCallId: "call-t" },
        approval({ name: "propose_team", toolCallId: "call-t" }),
      ];
      const out = retireFinishedGate(items, "propose_team", "call-t");
      expect(out).toHaveLength(0);
    });
  });

  describe("reconcileResolvedGates", () => {
    it("drops a card the Inbox already resolved", () => {
      const items = [approval({ toolCallId: "call-1" }), approval({ toolCallId: "call-2" })];
      const out = reconcileResolvedGates(items, [inbox({ tool_call_id: "call-1" })]);
      expect(out).toHaveLength(1);
      expect((out[0] as { toolCallId?: string }).toolCallId).toBe("call-2");
    });

    it("does nothing when the Inbox has no resolved rows (absence is not proof)", () => {
      const items = [approval({ toolCallId: "call-1" })];
      expect(reconcileResolvedGates(items, [inbox({ state: "pending", tool_call_id: "call-1" })])).toBe(items);
      expect(reconcileResolvedGates(items, [])).toBe(items);
    });

    it("leaves non-gate items alone even if an id matches", () => {
      const items: Item[] = [{ kind: "tool", id: "call-1", name: "run_shell", args: {}, status: "ok" }];
      expect(reconcileResolvedGates(items, [inbox({ tool_call_id: "call-1" })])).toBe(items);
    });
  });

  describe("dedupeGates", () => {
    it("keeps only the first open card for a tool call (replay/reconnect)", () => {
      const out = dedupeGates([approval({ toolCallId: "call-1" }), approval({ toolCallId: "call-1" })]);
      expect(out).toHaveLength(1);
    });

    it("keeps answered history — only open duplicates collapse", () => {
      const out = dedupeGates([
        approval({ toolCallId: "call-1", resolved: "once" }),
        approval({ toolCallId: "call-1" }),
      ]);
      expect(out).toHaveLength(2);
    });

    it("returns the same array when nothing was dropped (no needless rerender)", () => {
      const items = [approval({ toolCallId: "call-1" })];
      expect(dedupeGates(items)).toBe(items);
    });
  });

  describe("expireOpenGates", () => {
    it("expires an unanswered approval instead of pretending it was approved", () => {
      const out = expireOpenGates([approval()]) as Extract<Item, { kind: "approval" }>[];
      expect(out[0].resolved).toBe("expired");
    });

    it("does not touch already-resolved cards", () => {
      const items = [approval({ resolved: "once" })];
      expect(expireOpenGates(items)).toBe(items);
    });

    it("reports whether an extra resolved-list fetch is worth it", () => {
      expect(hasOpenGates([approval({ toolCallId: "call-1" })])).toBe(true);
      expect(hasOpenGates([approval({ toolCallId: "call-1", resolved: "deny" })])).toBe(false);
      expect(hasOpenGates([approval()])).toBe(false);
    });
  });
});
