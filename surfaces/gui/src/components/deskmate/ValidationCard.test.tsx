/**
 * ValidationCard 冒烟测试：认知验证（Turing test）前端交互。
 * 覆盖：邀请气泡 → 开始 → 情景展示 + AI 代答 + 用户回答 → 提交 → 相似度结果。
 * stub 后端 /v1/deskmate/cognitive/validation/* 接口。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ValidationCard, type ValidationPayload } from "./ValidationCard";

const payload: ValidationPayload = {
  scenario: {
    id: "val_test001",
    domain: "史料真实性与创作",
    scenario: "一个平台向你约稿：要求你把作品里某个历史人物改成更讨喜的结局，稿费翻倍。但你觉得这会违背你对「史料真实性与创作」的理解。你会怎么处理？",
    question: "在「创作约稿」情境下，你会怎么做？为什么？",
    related_items: ["stance_1"],
  },
  note: "已生成情境，等待 AI 回答与你的回答后评估。",
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => {
      return {
        ok: true,
        json: async () => ({
          scenario: payload.scenario,
          evaluation: {
            overall_score: 0.72,
            dimension_scores: { stance: 0.8, argument: 0.7, value: 0.75, expression: 0.6 },
            divergence_points: [],
            feedback: "AI 的回答与你高度一致——它确实懂你在这类情境下的选择。",
          },
          applied: true,
          note: "验证完成，已反哺画像。",
        }),
      } as Response;
    }),
  );
}

function onClose() {
  // no-op
}

describe("ValidationCard", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("invite 阶段展示邀请气泡，可进入验证", () => {
    render(<ValidationCard validation={payload} onClose={onClose} />);
    expect(screen.getByText("小桔想考考自己")).toBeTruthy();
    expect(screen.getByText("开始验证")).toBeTruthy();
  });

  it("开始后展示情景、AI 代答草稿与用户回答输入框", () => {
    render(<ValidationCard validation={payload} onClose={onClose} />);
    fireEvent.click(screen.getByText("开始验证"));
    expect(screen.getByText(/一个你没遇到过的新情境/)).toBeTruthy();
    // 情景文本
    expect(screen.getByText(/平台向你约稿/)).toBeTruthy();
    // AI 代答草稿（本地生成，无需等待后端）
    expect(screen.getByText(/不会轻易让步/)).toBeTruthy();
    // 用户回答输入
    expect(screen.getByPlaceholderText("如果是你，会怎么做、为什么？")).toBeTruthy();
  });

  it("提交后展示相似度结果（像我度环 + 四维条 + 反馈）", async () => {
    stubFetch();
    render(<ValidationCard validation={payload} onClose={onClose} />);
    fireEvent.click(screen.getByText("开始验证"));
    // 填用户回答（AI 代答已自动生成）
    fireEvent.change(screen.getByPlaceholderText("如果是你，会怎么做、为什么？"), {
      target: { value: "不改。史料真实是底线，不能为商业要求扭曲历史结局。" },
    });
    fireEvent.click(screen.getByText("提交验证"));
    await waitFor(() => expect(screen.getByText("验证结果")).toBeTruthy());
    // 像我度 72%
    expect(screen.getByText("72%")).toBeTruthy();
    // 反馈文案
    expect(screen.getByText(/高度一致/)).toBeTruthy();
    // 四维标签
    expect(screen.getByText("立场一致")).toBeTruthy();
    expect(screen.getByText("论证模式")).toBeTruthy();
  });

  it("未填用户回答时提交给出提示", () => {
    render(<ValidationCard validation={payload} onClose={onClose} />);
    fireEvent.click(screen.getByText("开始验证"));
    fireEvent.click(screen.getByText("提交验证"));
    expect(screen.getByText(/两边都填一下/)).toBeTruthy();
  });

  it("null 负载时不渲染", () => {
    const { container } = render(<ValidationCard validation={null} onClose={onClose} />);
    expect(container.innerHTML).toBe("");
  });
});
