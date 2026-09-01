/**
 * InterviewCard 冒烟测试：③b 深度访谈前端交互。
 * 覆盖：邀请气泡 → 开始 → 逐题问答 → 提交 → 完成。
 * stub 后端 /v1/deskmate/cognitive/interview/* 接口。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { InterviewCard, type InterviewPayload } from "./InterviewCard";

const payload: InterviewPayload = {
  session_id: "iv_test001",
  total: 2,
  questions: [
    {
      id: "q_a__m=explore",
      topic: "历史创作",
      question: "我想更懂你在「历史创作」上的想法。能讲一个具体的例子吗？",
      context: "从未提及",
      related_item_id: null,
      related_gap_id: "g1",
      priority: 0.5,
      mode: "explore",
    },
    {
      id: "q_b__m=clarify",
      topic: "表达观",
      question: "关于「表达观」，你之前留下过有点矛盾的说法。能跟我说说你真正的立场吗？",
      context: "旧说法",
      related_item_id: "i1",
      related_gap_id: null,
      priority: 0.8,
      mode: "clarify",
    },
  ],
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      // 第一题（explore）→ 未完成；第二题（clarify）→ 完成
      const done = (body.question_id || "").includes("q_b");
      return {
        ok: true,
        json: async () => ({
          ok: true,
          session_id: body.session_id,
          answered: done ? 2 : 1,
          total: 2,
          session_done: done,
          open_gaps: done ? 0 : 1,
        }),
      } as Response;
    }),
  );
}

function onClose() {
  /* noop */
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("InterviewCard", () => {
  it("invite → start → 逐题回答 → done", async () => {
    stubFetch();
    render(<InterviewCard interview={payload} onClose={onClose} />);

    // 邀请气泡
    expect(screen.getByText(/小桔想更懂你/)).toBeTruthy();
    expect(screen.getByText(/开始回答/)).toBeTruthy();
    fireEvent.click(screen.getByText(/开始回答/));

    // 第一题（explore 标签）
    expect(screen.getByText(/第 1 \/ 2 题/)).toBeTruthy();
    expect(screen.getByText("探索")).toBeTruthy();
    const ta = screen.getByPlaceholderText(/说点什么/);
    fireEvent.change(ta, { target: { value: "我写历史最看重人物动机合乎历史逻辑" } });
    fireEvent.click(screen.getByText("下一题"));

    // 提交后进入第二题（clarify 标签）
    await waitFor(() => expect(screen.getByText("澄清")).toBeTruthy());
    expect(screen.getByText(/第 2 \/ 2 题/)).toBeTruthy();

    const ta2 = screen.getByPlaceholderText(/说点什么/);
    fireEvent.change(ta2, { target: { value: "我的真实立场是这样" } });
    fireEvent.click(screen.getByText("完成"));

    // 完成页
    await waitFor(() => expect(screen.getByText(/谢谢你的坦诚/)).toBeTruthy());
    expect(screen.getByText(/好的/)).toBeTruthy();
  });

  it("邀请气泡可「稍后再说」关闭", async () => {
    stubFetch();
    const closed = vi.fn();
    render(<InterviewCard interview={payload} onClose={closed} />);
    fireEvent.click(screen.getByText(/稍后再说/));
    await waitFor(() => expect(closed).toHaveBeenCalled());
  });
});
