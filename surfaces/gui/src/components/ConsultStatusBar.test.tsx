// ConsultStatusBar（2026-11-17）：专家在后台工作时，界面必须有交代。
//
// 这条组件存在的理由很具体：咨询改成后台派发后，小桔派完活就回话，专家还要跑几分钟。
// 用户看到的若是「她说请了专家，然后没有下文」，就等于空等——所以钉住三件事：
// ① 进行中的任务显示专家名/书名/已运行时间；② 完成后如实转成「已交付」；
// ③ 没有任务时整条不渲染（别在输入框上挂一条空壳）。
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";

vi.mock("../api", () => ({
  getConsultRuns: vi.fn(async () => ({ active: [], recent: [] })),
}));

import { getConsultRuns } from "../api";
import { ConsultStatusBar } from "./ConsultStatusBar";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const RUNNING = {
  id: "c-1",
  session_id: "s1",
  book: "夜不收",
  status: "running",
  started_at: Date.now() / 1000 - 192,
  elapsed: 192,
  expert_names: ["人物专家"],
  experts: ["character"],
};

describe("ConsultStatusBar", () => {
  it("进行中的专家：显示在做什么、已跑了多久", async () => {
    vi.mocked(getConsultRuns).mockResolvedValue({ active: [RUNNING], recent: [] });
    render(<ConsultStatusBar sessionId="s1" />);
    const row = await screen.findByTestId("consult-running");
    expect(row.textContent).toContain("人物专家");
    // 还没有任何读稿记录时只说「在看这本书」——说「正在读」就成了没有依据的断言。
    expect(row.textContent).toContain("正在看《夜不收》");
    expect(row.textContent).toMatch(/已 3:1[23]/);
  });

  it("完成事件到来后转成「已交付」，不再显示为进行中", async () => {
    vi.mocked(getConsultRuns).mockResolvedValue({ active: [RUNNING], recent: [] });
    render(<ConsultStatusBar sessionId="s1" />);
    await screen.findByTestId("consult-running");

    act(() => {
      window.dispatchEvent(
        new CustomEvent("deskmate:consult", {
          detail: {
            ...RUNNING,
            status: "done",
            elapsed: 512,
            finished_at: Date.now() / 1000,
          },
        }),
      );
    });

    const done = await screen.findByTestId("consult-done");
    expect(done.textContent).toContain("已交付");
    expect(done.textContent).toContain("8:32");
    expect(screen.queryByTestId("consult-running")).toBeNull();
  });

  it("进度会动：正在读哪一章 → 读了几次在梳理", async () => {
    // 2026-11-17 萧哥指出的问题：这行以前从派出到交付永远写着「正在读《夜不收》」。
    // 现在它翻译后端记录的事实（busy / chapter / reads / chars），不是猜的百分比。
    vi.mocked(getConsultRuns).mockResolvedValue({
      active: [
        {
          ...RUNNING,
          progress: { character: { busy: "read_book", chapter: "第十一章", reads: 2, chars: 48000 } },
        },
      ],
      recent: [],
    });
    render(<ConsultStatusBar sessionId="s1" />);
    const row = await screen.findByTestId("consult-running");
    expect(row.textContent).toContain("正在读「第十一章」");

    // 读者读完、模型开始推理：同一行改成「读了几次 + 累计字数」
    act(() => {
      window.dispatchEvent(
        new CustomEvent("deskmate:consult", {
          detail: {
            ...RUNNING,
            progress: { character: { busy: "", chapter: "第十一章", reads: 3, chars: 72000 } },
          },
        }),
      );
    });
    const after = await screen.findByTestId("consult-running");
    expect(after.textContent).toContain("已读 3 次（7.2 万字）");
    expect(after.textContent).toContain("正在梳理");
    expect(after.textContent).not.toContain("正在读「第十一章」");
  });

  it("非读稿工具也要说清在干嘛：联网查证 / 查史料库", async () => {
    // 2026-11-17 web_search 事故：专家联网查证失败循环烧掉 900s，
    // 界面上却只显示「正在看稿」——失败必须看得见。
    vi.mocked(getConsultRuns).mockResolvedValue({
      active: [
        {
          ...RUNNING,
          progress: { character: { busy: "web_search", reads: 0 } },
        },
      ],
      recent: [],
    });
    render(<ConsultStatusBar sessionId="s1" />);
    const row = await screen.findByTestId("consult-running");
    expect(row.textContent).toContain("正在联网查证");

    // 联网失败挂账：不再装作「正在看稿」，把失败次数亮出来
    act(() => {
      window.dispatchEvent(
        new CustomEvent("deskmate:consult", {
          detail: {
            ...RUNNING,
            progress: {
              character: {
                busy: "",
                reads: 0,
                tools: { web_search: { calls: 5, errors: 4, last_ms: 31000 } },
              },
            },
          },
        }),
      );
    });
    const after = await screen.findByTestId("consult-running");
    expect(after.textContent).toContain("查证不顺：web_search 已失败 4 次");
  });

  it("多位专家各说各的，不合成一句「正在读稿」", async () => {
    vi.mocked(getConsultRuns).mockResolvedValue({
      active: [
        {
          ...RUNNING,
          experts: ["character", "lit-editor"],
          expert_names: ["人物专家", "文学编辑"],
          progress: {
            character: { busy: "read_book", chapter: "第十一章" },
            "lit-editor": { busy: "", reads: 1, chars: 900 } ,
          },
        },
      ],
      recent: [],
    });
    render(<ConsultStatusBar sessionId="s1" />);
    const a = await screen.findByTestId("consult-progress-character");
    const b = await screen.findByTestId("consult-progress-lit-editor");
    expect(a.textContent).toContain("人物专家 正在读「第十一章」");
    expect(b.textContent).toContain("文学编辑 已读 1 次");
  });

  it("横向对齐输入框那一列，不是贴窗口左边", async () => {
    // 2026-11-17 实盘：这条挂在 Composer 同一层（全宽），自己没带居中约束，
    // 于是贴在最左边、压在壁纸插画上。这里把几何契约钉住，别再退回全宽。
    vi.mocked(getConsultRuns).mockResolvedValue({ active: [RUNNING], recent: [] });
    const { container } = render(<ConsultStatusBar sessionId="s1" />);
    const bar = await screen.findByTestId("consult-status");
    const column = bar.parentElement as HTMLElement;
    expect(bar.className).toContain("max-w-3xl");
    expect(bar.className).toContain("mx-auto");
    // 外层补 composer-wrap 同值的左右内边距：窄窗下才跟输入框同边起。
    expect(column.className).toContain("px-6");
    expect(column.className).not.toContain("max-w-3xl");
    expect(container.firstElementChild).toBe(column);
  });

  it("没有任务时整条不渲染（不占版面、不留空壳）", async () => {
    vi.mocked(getConsultRuns).mockResolvedValue({ active: [], recent: [] });
    const { container } = render(<ConsultStatusBar sessionId="s1" />);
    await act(async () => {});
    expect(screen.queryByTestId("consult-status")).toBeNull();
    expect(container.textContent).toBe("");
  });
});
