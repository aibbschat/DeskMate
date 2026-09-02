/**
 * 花园 P1 组件测试：羁绊故事簿 / 花园图鉴 / 节日专属氛围 / 花朵回响。
 *
 * - GardenStoryBook：bond 阶段 + 叙事 + 数字 + 画像栏目；无数据时不渲染。
 * - GardenFlora：图鉴网格展开、解锁计数、未解锁置灰。
 * - GardenClockOverlay：节日（中秋/春节）专属氛围文案。
 * - GardenFlowers：花朵 tip 显示小桔的回响。
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GardenClockOverlay } from "./GardenClockOverlay";
import { GardenFlora } from "./GardenFlora";
import { GardenFlowers } from "./GardenFlowers";
import { GardenStoryBook } from "./GardenStoryBook";

afterEach(cleanup);

// ---------- 羁绊故事簿 ----------
describe("GardenStoryBook", () => {
  it("渲染 bond 阶段、叙事与真实数字", () => {
    render(
      <GardenStoryBook
        story={{
          bond: { stage: "默契", title: "不用解释", narrative: "有些话你说一半，我就知道后半句。", depth: 128, progress: 0.44, maxed: false },
          numbers: { total_chats: 208, streak_days: 5, badge_count: 12, flower_count: 39 },
          portrait: [],
        }}
      />,
    );
    expect(screen.getByText("默契")).toBeTruthy();
    expect(screen.getByText(/有些话你说一半/)).toBeTruthy();
    expect(screen.getByText("208")).toBeTruthy();
    expect(screen.getByText("39")).toBeTruthy();
  });

  it("渲染画像栏目与摘要", () => {
    render(
      <GardenStoryBook
        story={{
          bond: { stage: "初识", title: "第一粒种子", narrative: "我们在花园门口第一次互相打量。", depth: 0, progress: 0, maxed: false },
          numbers: { total_chats: 0, streak_days: 0, badge_count: 0, flower_count: 0 },
          portrait: [
            { title: "写作风格", status: "ready", summary: "平均 73 字，短句多", article_count: 8 },
            { title: "决策偏好", status: "empty", summary: "", article_count: 0 },
          ],
        }}
      />,
    );
    expect(screen.getByText("它眼中的我")).toBeTruthy();
    expect(screen.getByText("写作风格")).toBeTruthy();
    expect(screen.getByText("平均 73 字，短句多")).toBeTruthy();
  });

  it("无数据时不渲染（不占位、不编造）", () => {
    const { container } = render(<GardenStoryBook story={null} />);
    expect(container.querySelector(".garden-storybook")).toBeNull();
  });
});

// ---------- 花园图鉴 ----------
describe("GardenFlora", () => {
  const flora = [
    { kind: "sprout", label: "新芽", emoji: "🌱", desc: "技能又长一截，冒个新芽。", unlocked: 23 },
    { kind: "iris", label: "鸢尾", emoji: "🪻", desc: "写下的字，开成了鸢尾。", unlocked: 0 },
  ];

  it("默认收起，点击展开图鉴网格", () => {
    render(<GardenFlora flora={flora} />);
    expect(screen.getByText(/花园图鉴/)).toBeTruthy();
    // meta 文本「1/2 种见过」被 JSX 拆成多个文本节点，按片段匹配
    expect(screen.getByText(/种见过/).textContent).toContain("1/2");
    // 未展开时卡片不可见
    expect(screen.queryByText("新芽")).toBeNull();
    fireEvent.click(screen.getByText(/花园图鉴/));
    expect(screen.getByText("新芽")).toBeTruthy();
    expect(screen.getByText("鸢尾")).toBeTruthy();
    expect(screen.getByText("23")).toBeTruthy();
  });

  it("无图鉴数据时不渲染", () => {
    const { container } = render(<GardenFlora flora={null} />);
    expect(container.querySelector(".garden-flora")).toBeNull();
  });
});

// ---------- 节日专属氛围 ----------
describe("GardenClockOverlay festival flavor", () => {
  it("中秋专属氛围", () => {
    render(
      <GardenClockOverlay
        event={{ kind: "festival", festival_name: "中秋" }}
        flowerCount={39}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/今晚的月光，也照进了你的花园/)).toBeTruthy();
  });

  it("春节专属氛围", () => {
    render(
      <GardenClockOverlay
        event={{ kind: "festival", festival_name: "春节" }}
        flowerCount={39}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/花园里挂起了红灯笼/)).toBeTruthy();
  });

  it("未命中表内节日回退通用祝福", () => {
    render(
      <GardenClockOverlay
        event={{ kind: "festival", festival_name: "双十节" }}
        flowerCount={39}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/双十节快乐/)).toBeTruthy();
  });
});

// ---------- 花朵回响 ----------
describe("GardenFlowers echo", () => {
  const flowers = [
    {
      flower_id: "a",
      kind: "sunflower",
      bed: "milestone",
      title: "第三章定稿",
      quote: "把创作圆桌的定稿收进花园",
      date: "2026-09-02",
      source_ts: "2026-09-02T10:00:00",
      mood_tint: "",
      echo: "你拍板的那一刻，向日葵就朝着你开了。",
    },
  ];

  it("展开花朵时显示小桔的回响", () => {
    render(<GardenFlowers flowers={flowers} stats={{ total: 1, by_bed: { milestone: 1 } }} />);
    // 花朵本体只显示 emoji + 日期；标题/回响在展开后的 tip 里
    const flower = screen.getByTitle("第三章定稿");
    fireEvent.click(flower);
    expect(screen.getByText("小桔")).toBeTruthy();
    expect(screen.getByText(/你拍板的那一刻/)).toBeTruthy();
  });
});
