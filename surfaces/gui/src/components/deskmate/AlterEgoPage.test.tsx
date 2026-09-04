/**
 * AlterEgoPage — 「它眼中的我」画像聚合页测试。
 *
 * 覆盖 2026-09-04 新增的「懂我中心」头部：
 * - 像我度头部（小桔 + likeness_score + 了解进度 + 待澄清缺口角标）
 * - 待澄清问题列表（identity.gaps 聚合，层区分情感/认知）
 * - 栏目网格仍正常渲染（含情感栏目）
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AlterEgoPage } from "./AlterEgoPage";

function json(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

function stubFetch(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json(body)),
  );
}

const ALTER_EGO = {
  enabled: true,
  has_data: true,
  total_signals: 12,
  portrait: {
    sections: [
      {
        id: "cognitive",
        title: "它理解的我",
        icon: "🧠",
        status: "ready",
        headline: "已理解 49 条认知特征",
        articles: [{ title: "立场：历史题材选择", content: "重视明代", evidence: "置信度 90%", tags: ["立场"] }],
      },
      {
        id: "emotional",
        title: "它在乎的",
        icon: "💛",
        status: "ready",
        headline: "已捕捉 5 条情感特征",
        articles: [{ title: "回血方式", content: "写作", evidence: "置信度 83%", tags: ["回血"] }],
      },
    ],
    summary: { ready_count: 2, learning_count: 0, empty_count: 0, total_articles: 2 },
    identity: {
      likeness_score: 0.73,
      likeness_label: "正在懂你",
      understanding_progress: 0.95,
      understanding_detail: "54 条画像特征",
      core_ready: 2,
      core_total: 2,
      core_avg_confidence: 0.87,
      gaps: [
        { layer: "emotional", topic: "情感·关怀偏好", reason: "你偏好被问进度还是独处？", gap_id: "gap_1" },
        { layer: "emotional", topic: "情感·回血方式", reason: "除了史料，低落时还靠什么？", gap_id: "gap_2" },
      ],
      note: "估算分",
    },
  },
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("AlterEgoPage 懂我中心头部", () => {
  it("渲染像我度头部：百分比 + 标签 + 了解进度", async () => {
    stubFetch(ALTER_EGO);
    render(<AlterEgoPage open onClose={() => {}} />);
    // 等数据加载
    expect(await screen.findByText("小桔有多懂我")).toBeTruthy();
    expect(screen.getAllByText("73%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("正在懂你")).toBeTruthy();
    expect(screen.getByText("54 条画像特征")).toBeTruthy();
  });

  it("渲染待澄清去回答按钮（聚合 gaps）", async () => {
    stubFetch(ALTER_EGO);
    render(<AlterEgoPage open onClose={() => {}} />);
    expect(await screen.findByText(/去回答·关怀偏好/)).toBeTruthy();
    expect(screen.getByText(/去回答·回血方式/)).toBeTruthy();
  });

  it("三环绕按键正常渲染（认知/情感/侧写 + 计数）", async () => {
    stubFetch(ALTER_EGO);
    render(<AlterEgoPage open onClose={() => {}} />);
    expect(await screen.findByText(/它理解的我/)).toBeTruthy();
    expect(screen.getByText(/它在乎的/)).toBeTruthy();
    expect(screen.getByText(/它在学什么/)).toBeTruthy();
    // 认知 1 条 / 情感 1 条 / 侧写聚合其余（0）
    expect(screen.getByText("认知 · 1")).toBeTruthy();
    expect(screen.getByText("情感 · 1")).toBeTruthy();
  });

  it("点认知按键弹出维度数字弹层", async () => {
    stubFetch(ALTER_EGO);
    render(<AlterEgoPage open onClose={() => {}} />);
    // stub 里 cognitive.articles 只有一条且无 dimension → 归入 other 分组
    fireEvent.click(await screen.findByText(/它理解的我/));
    expect(screen.getByText("🧠 它理解的我 · 认知")).toBeTruthy();
    expect(screen.getByText("点这里看详情 →")).toBeTruthy();
  });

  it("无 identity 时优雅降级（不崩溃）", async () => {
    const noIdentity = {
      enabled: true,
      has_data: true,
      total_signals: 0,
      portrait: {
        sections: [],
        summary: { ready_count: 0, learning_count: 0, empty_count: 0, total_articles: 0 },
      },
    };
    stubFetch(noIdentity);
    render(<AlterEgoPage open onClose={() => {}} />);
    // 仍能渲染标题栏，无 identity 头部时 likeness 为 0 但页面不崩
    expect(await screen.findByText("它眼中的我")).toBeTruthy();
    expect(screen.getAllByText("0%").length).toBeGreaterThanOrEqual(1);
  });
});
