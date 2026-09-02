/**
 * 花园 P2 组件测试：花园的树 / 季节粒子 / 花开分页 / 点亮。
 *
 * - GardenTree：树等级 + 浇水徽章 + 年花 + 年鉴摘要（fetch 失败静默）。
 * - GardenScene：季节粒子随 season 切换（官方小桔仍渲染）。
 * - GardenFlowers：分页（13 朵 → 2 页）、点亮按钮、已点亮标记。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GardenFlowers } from "./GardenFlowers";
import { GardenScene } from "./GardenScene";
import { GardenTree } from "./GardenTree";
import type { GardenP2 } from "./useGarden";

afterEach(cleanup);

function p2(over: Partial<GardenP2> = {}): GardenP2 {
  return {
    tree: {
      key: "sapling",
      title: "一棵花树",
      desc: "三十朵花开满枝，它长成了一棵花树。",
      count: 30,
      threshold: 30,
      next: { title: "一树繁花", threshold: 60 },
    },
    water: { watered: true, streak: 3, today: "2026-09-02" },
    year_flowers: [{ year: "2026", first_visit_date: "2026-09-02" }],
    years: { 2026: 30 },
    lit: { count: 1, ids: ["f1"] },
    archive: { count: 0, years: [] },
    ...over,
  };
}

function flower(id: string, kind = "sprout"): import("./GardenFlowers").GardenFlower {
  return {
    flower_id: id,
    kind,
    bed: "milestone",
    title: `${id} 的花`,
    quote: "",
    date: "2026-09-02",
    source_ts: "2026-09-02T10:00:00",
    mood_tint: "",
    echo: "",
  };
}

// ---------- 花园的树 ----------
describe("GardenTree", () => {
  it("渲染树等级、浇水与年花", () => {
    render(<GardenTree p2={p2()} />);
    expect(screen.getByText("花园的树 ·")).toBeTruthy();
    expect(screen.getByText("一棵花树")).toBeTruthy();
    expect(screen.getByText(/再开 30 朵/)).toBeTruthy();
    expect(screen.getByText("今天刚给花园浇过水")).toBeTruthy();
    expect(screen.getByText("连续 3 天")).toBeTruthy();
    expect(screen.getByText(/今年已开 30 朵花/)).toBeTruthy();
  });

  it("无数据时不渲染", () => {
    const { container } = render(<GardenTree p2={null} />);
    expect(container.innerHTML).toBe("");
  });

  it("有归档时展示年鉴入口，展开显示年份摘要", () => {
    render(
      <GardenTree
        p2={p2({
          archive: { count: 5, years: [{ year: "2025", count: 5, sample_title: "旧日的花" }] },
        })}
      />,
    );
    expect(screen.getByText(/花园年鉴 · 已归档 5 朵/)).toBeTruthy();
    fireEvent.click(screen.getByText(/花园年鉴/));
    expect(screen.getByText("2025 年")).toBeTruthy();
    expect(screen.getByText("5 朵")).toBeTruthy();
  });
});

// ---------- 花园场景：季节粒子 ----------
describe("GardenScene", () => {
  it("渲染季节标签与季节粒子（秋 → 落叶类）", () => {
    const { container } = render(
      <GardenScene season="autumn" daypart="day" visitCount={3} />,
    );
    expect(screen.getByText("秋日花园")).toBeTruthy();
    expect(screen.getByText("第 3 次回到花园")).toBeTruthy();
    const leaf = container.querySelector(".garden-season-leaf");
    expect(leaf).toBeTruthy();
  });

  it("冬季渲染飘雪粒子", () => {
    const { container } = render(
      <GardenScene season="winter" daypart="night" visitCount={0} />,
    );
    expect(container.querySelector(".garden-season-snow")).toBeTruthy();
  });
});

// ---------- 花开分页与点亮 ----------
describe("GardenFlowers P2", () => {
  it("超过一页时出现分页器，翻页后展示下一页", () => {
    const flowers = Array.from({ length: 13 }, (_, i) => flower(`f${i}`));
    render(<GardenFlowers flowers={flowers} stats={{ total: 13 }} />);
    expect(screen.getByText(/第 1 \/ 2 页/)).toBeTruthy();
    fireEvent.click(screen.getByText("下一页 ›"));
    expect(screen.getByText(/第 2 \/ 2 页/)).toBeTruthy();
  });

  it("点亮按钮触发 onLit；已点亮的花显示 ⭐", () => {
    const onLit = vi.fn();
    render(
      <GardenFlowers
        flowers={[flower("f1"), flower("f2")]}
        stats={{ total: 2 }}
        litIds={["f1"]}
        onLit={onLit}
      />,
    );
    // 已点亮的花带 ⭐ 标记
    expect(screen.getByTitle("已被点亮")).toBeTruthy();
    // 打开未点亮的花，点「点亮这朵花」回调
    fireEvent.click(screen.getByTitle("f2 的花"));
    fireEvent.click(screen.getByText("点亮这朵花"));
    expect(onLit).toHaveBeenCalledWith("f2");
  });
});
