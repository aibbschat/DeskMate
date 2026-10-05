/**
 * 记一笔的乐观上屏逻辑（2026-10-05 真机反馈驱动）。
 *
 * 萧哥实测：「在花园写日记，提交后没反应，写下的那行话也还在输入框里，没变化……
 * 等等，发上去了，可能比较慢，也没提示」。根因是 POST 要现场生成小桔回复（几秒），
 * 而旧实现等返回才清空草稿、才刷新列表 → 这段时间里界面完全静默。
 * 这几个断言钉的是修好后的不变量：**点下去的那一刻，字就离开输入框、卡片就在时间线上**。
 */
import { describe, expect, it } from "vitest";
import { markFailed, mergeCommentResult, mergePostResult, mergeRefresh, pendingMoment, type Moment } from "./yard";

const base = (over: Partial<Moment> = {}): Moment =>
  ({
    id: "x1",
    author: "xiaoju",
    date: "2026-10-05",
    at: "2026-10-05T18:42:00",
    content: "她的一条",
    loved: false,
    archived: false,
    comments: [],
    ...over,
  }) as Moment;

describe("记一笔：乐观上屏与合并", () => {
  it("pendingMoment 立刻可用：id 唯一、状态 pending、内容就是原文", () => {
    const a = pendingMoment("今天写废了四千字");
    expect(a.state).toBe("pending");
    expect(a.content).toBe("今天写废了四千字");
    expect(a.author).toBe("me");
    expect(a.id).not.toBe(pendingMoment("x").id);
  });

  it("后端返回后：pending 换成真条目，小桔回复挂在留言里，状态字段消失", () => {
    const pend = pendingMoment("草稿");
    const list = [pend, base()];
    const out = mergePostResult(list, pend.id, {
      post: { id: "p1", content: "草稿", createdAt: "2026-10-05T20:58:00", loved: true },
      comment: { id: "c1", author: "xiaoju", text: "废掉的四千字里有一句留下了", createdAt: "2026-10-05T20:58:05" },
    });
    expect(out).toHaveLength(2);
    expect(out[0].id).toBe("p1");
    expect(out[0].state).toBeUndefined();
    expect(out[0].loved).toBe(true);
    expect(out[0].comments).toEqual([
      { id: "c1", author: "xiaoju", text: "废掉的四千字里有一句留下了", at: "2026-10-05T20:58:05" },
    ]);
  });

  it("后端没给 post（异常/旧包）→ 标成 failed，绝不静默消失", () => {
    const pend = pendingMoment("草稿");
    const out = mergePostResult([pend, base()], pend.id, {});
    expect(out[0].state).toBe("failed");
    expect(out[0].content).toBe("草稿");
    expect(out).toHaveLength(2);
  });

  it("pending 已被后台刷新换掉时，真条目插到最前而不是丢", () => {
    const real = base({ id: "p1", author: "me", content: "草稿" });
    const out = mergePostResult([real], "pending-gone", {
      post: { id: "p2", content: "草稿二", createdAt: "2026-10-05T21:00:00", loved: false },
    });
    expect(out[0].id).toBe("p2");
    expect(out).toHaveLength(2);
  });

  it("markFailed 只动那一条", () => {
    const pend = pendingMoment("a");
    const out = markFailed([pend, base()], pend.id);
    expect(out[0].state).toBe("failed");
    expect(out[1].state).toBeUndefined();
  });

  it("后台刷新不许把刚发的那条冲掉（读后写宽限 15s）", () => {
    const now = Date.now();
    const justPosted = base({ id: "p9", author: "me", content: "刚记的", at: new Date(now - 3000).toISOString() });
    const pend = pendingMoment("还在记的");
    const oldPost = base({ id: "p0", author: "me", at: new Date(now - 60_000).toISOString() });
    const out = mergeRefresh([base()], [pend, justPosted, oldPost], now);
    expect(out.map((m) => m.id)).toContain("p9");
    expect(out.map((m) => m.id)).toContain(pend.id);
    expect(out.map((m) => m.id)).not.toContain("p0"); // 超窗口的老条目以服务端为准
    expect(out[0].id).toBe(pend.id); // 最新的在最上
  });

  it("服务端已经有这条时，不重复保留本地版", () => {
    const server = base({ id: "p9", author: "me", content: "服务端版" });
    const local = base({ id: "p9", author: "me", content: "本地版", state: "pending" });
    const out = mergeRefresh([server], [local]);
    expect(out.filter((m) => m.id === "p9")).toHaveLength(1);
    expect(out.find((m) => m.id === "p9")?.content).toBe("服务端版");
  });

  it("留言：本地先追加，后端返回再补小桔的自动回复（不重复覆盖我那条）", () => {
    const mine = base({ id: "m1", author: "me", comments: [] });
    const step1 = mergeCommentResult([mine], "m1", {
      comment: { id: "local-1", author: "me", text: "回一句", createdAt: "2026-10-05T21:01:00" },
    });
    expect(step1[0].comments).toHaveLength(1);
    const step2 = mergeCommentResult(step1, "m1", {
      comment: { id: "c9", author: "me", text: "回一句", createdAt: "2026-10-05T21:01:02" },
      reply: { id: "c10", author: "xiaoju", text: "收到", createdAt: "2026-10-05T21:01:04" },
    });
    expect(step2[0].comments.map((c) => c.author)).toEqual(["me", "me", "xiaoju"]);
    // 别的条目不受影响
    expect(mergeCommentResult([mine], "other", { comment: { id: "z", text: "t" } })[0].comments).toHaveLength(0);
  });
});
