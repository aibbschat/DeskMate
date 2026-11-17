// LLM 通道组卡片（2026-10-06，设计 §二）：两个总开关 + 熔断可见 + 切换审计。
//
// 关注三件事：① 手动切组真的调后端并改本地状态；② 限额中的通道列出来（让
// 「为什么在换模型」可见）；③ 后端不可用时静默隐藏（不破坏设置页其余部分）。
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("../../api", () => ({
  getLlmChannels: vi.fn(),
  setLlmMemoryGroup: vi.fn(async () => true),
  getLlmChannelAudit: vi.fn(async () => []),
  getProactiveHeartbeat: vi.fn(async () => null),
}));

import {
  getLlmChannels,
  getLlmChannelAudit,
  getProactiveHeartbeat,
  setLlmMemoryGroup,
} from "../../api";
import { LlmChannelsCard } from "./LlmChannelsCard";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// 免费组现状（2026-10-18 外网免费车道全撤）：仅剩云端本地 llama-server 兜底一条
// （local:qwen3-4b-instruct，小桔自己的 CVM 设施，电脑端不可用）。fixture 用真实 id。
const STATUS = {
  paid: {
    total: 3,
    available: ["ark-agent-plan-cn:deepseek-v4-flash", "xiaomi:mimo-v2.6-pro"],
    open: ["tencent-tokenplan:hy3"],
  },
  free: {
    total: 1,
    available: [],
    open: ["local:qwen3-4b-instruct"],
  },
  memory_group: "free" as const,
};

describe("LlmChannelsCard", () => {
  it("渲染两个总开关并标出当前组", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    render(<LlmChannelsCard />);

    const free = await screen.findByTestId("llm-group-free");
    const paid = screen.getByTestId("llm-group-paid");
    expect(free.getAttribute("aria-pressed")).toBe("true");
    expect(paid.getAttribute("aria-pressed")).toBe("false");
    // 两组的候选数都露出来（3 条付费 / 1 条免费）——用 within 限定到各自按钮，
    // 否则 getByText 会因两处都含"条候选"而报multiple matches。
    expect(within(paid).getByText(/3 条候选/)).toBeTruthy();
    expect(within(free).getByText(/1 条候选/)).toBeTruthy();
    // 限额提示也按组各自显示
    expect(within(paid).getByText(/1 条限额中/)).toBeTruthy();
  });

  it("点另一个组 → 调后端并切高亮", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    render(<LlmChannelsCard />);

    const paid = await screen.findByTestId("llm-group-paid");
    fireEvent.click(paid);

    expect(setLlmMemoryGroup).toHaveBeenCalledWith("paid");
    await vi.waitFor(() =>
      expect(screen.getByTestId("llm-group-paid").getAttribute("aria-pressed")).toBe(
        "true",
      ),
    );
    expect(screen.getByTestId("llm-group-free").getAttribute("aria-pressed")).toBe(
      "false",
    );
  });

  it("列出限额中的通道（付费+ 免费合并）", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    render(<LlmChannelsCard />);

    const list = await screen.findByTestId("llm-open-list");
    // hy3 与云端本地兜底都该在列
    expect(list.textContent).toContain("tencent-tokenplan:hy3");
    expect(list.textContent).toContain("local:qwen3-4b-instruct");
  });

  it("有审计时展示最近的通道切换", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    vi.mocked(getLlmChannelAudit).mockResolvedValue([
      {
        ts: "2026-10-06T21:35:12",
        event: "open",
        channel: "local:qwen3-4b-instruct",
        kind: "transient",
        detail: "upstream 500",
        until: "",
      },
    ]);
    render(<LlmChannelsCard />);
    const list = await screen.findByTestId("llm-audit-list");
    expect(list.textContent).toContain("限额");
    expect(list.textContent).toContain("qwen3-4b-instruct");
  });

  it("心跳新鲜时显示「上次思考」与今日次数", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    vi.mocked(getProactiveHeartbeat).mockResolvedValue({
      tick_age_seconds: 12,
      judge_age_seconds: 95,
      judge_calls_today: 7,
      model: "xiaomi:mimo-v2.6-pro",
      last_skip_reason: "上下文未变化，跳过重复推理",
    });
    render(<LlmChannelsCard />);
    const hb = await screen.findByTestId("proactive-heartbeat");
    expect(hb.textContent).toContain("1 分钟前");
    expect(hb.textContent).toContain("xiaomi:mimo-v2.6-pro");
    expect(hb.textContent).toContain("今日 7 次");
  });

  it("心跳超过 30 分钟没思考 → 标红提示可能断了", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    vi.mocked(getProactiveHeartbeat).mockResolvedValue({
      tick_age_seconds: 3600,
      judge_age_seconds: 3600,
      judge_calls_today: 0,
    });
    render(<LlmChannelsCard />);
    const hb = await screen.findByTestId("proactive-heartbeat");
    expect(hb.querySelector(".text-danger")).not.toBeNull();
    expect(hb.textContent).toContain("1 小时前");
  });

  it("跳过理由是整段自述时截断，别把卡片撑散", async () => {
    // 实盘证据（2026-11-17 14:47 心跳文件）：judge 把自己的整段推理塞进
    // skipped_reason，200+ 字。卡片只留开头一句。
    const long =
      "用户处于 casual 状态、正在做模型配置类技术工作（剪贴板内容涉及模型矩阵补全），" +
      "非写作场景。最近日记明确说过「不用总是来问我进度」，这是清晰的边界信号。";
    vi.mocked(getLlmChannels).mockResolvedValue(STATUS);
    vi.mocked(getProactiveHeartbeat).mockResolvedValue({
      tick_age_seconds: 5,
      judge_age_seconds: 30,
      last_skip_reason: long,
    });
    render(<LlmChannelsCard />);
    const hb = await screen.findByTestId("proactive-heartbeat");
    expect(hb.textContent).toContain("用户处于 casual 状态");
    expect(hb.textContent).toContain("…");
    expect(hb.textContent).not.toContain("这是清晰的边界信号"); // 尾部不出现 = 真截断了
  });

  it("后端拿不到状态时静默隐藏（不破坏设置页）", async () => {
    vi.mocked(getLlmChannels).mockResolvedValue(null);
    const { container } = render(<LlmChannelsCard />);
    expect(container.querySelector("[data-testid=llm-channels-card]")).toBeNull();
  });
});
