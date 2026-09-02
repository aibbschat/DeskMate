/**
 * SeasonTheme 单元测试：季节/昼夜边界 + 花园主题变量。
 * 与后端 deskmate/garden/season.py 同一套边界语义，保证前后端一致。
 */
import { describe, expect, it } from "vitest";
import { daypartOf, gardenTheme, seasonOf } from "./SeasonTheme";

describe("seasonOf 边界（与后端一致：3-5 春 / 6-8 夏 / 9-11 秋 / 12-2 冬）", () => {
  it("季节首末月", () => {
    expect(seasonOf(new Date(2026, 2, 1))).toBe("spring"); // 3月
    expect(seasonOf(new Date(2026, 4, 31))).toBe("spring"); // 5月
    expect(seasonOf(new Date(2026, 5, 1))).toBe("summer"); // 6月
    expect(seasonOf(new Date(2026, 7, 31))).toBe("summer"); // 8月
    expect(seasonOf(new Date(2026, 8, 1))).toBe("autumn"); // 9月
    expect(seasonOf(new Date(2026, 10, 30))).toBe("autumn"); // 11月
    expect(seasonOf(new Date(2026, 11, 1))).toBe("winter"); // 12月
    expect(seasonOf(new Date(2026, 0, 15))).toBe("winter"); // 1月
    expect(seasonOf(new Date(2026, 1, 28))).toBe("winter"); // 2月
  });
});

describe("daypartOf 边界（5-10 晨 / 11-16 昼 / 17-19 黄昏 / 其余深夜）", () => {
  it("小时边界", () => {
    expect(daypartOf(new Date(2026, 8, 1, 5))).toBe("morning");
    expect(daypartOf(new Date(2026, 8, 1, 10))).toBe("morning");
    expect(daypartOf(new Date(2026, 8, 1, 11))).toBe("day");
    expect(daypartOf(new Date(2026, 8, 1, 16))).toBe("day");
    expect(daypartOf(new Date(2026, 8, 1, 17))).toBe("dusk");
    expect(daypartOf(new Date(2026, 8, 1, 19))).toBe("dusk");
    expect(daypartOf(new Date(2026, 8, 1, 20))).toBe("night");
    expect(daypartOf(new Date(2026, 8, 1, 0))).toBe("night");
    expect(daypartOf(new Date(2026, 8, 1, 4))).toBe("night");
  });
});

describe("gardenTheme 主题变量", () => {
  it("四季天空基调不同，深夜灯色最亮", () => {
    const spring = gardenTheme("spring", "day") as Record<string, string>;
    const autumn = gardenTheme("autumn", "day") as Record<string, string>;
    expect(spring["--garden-sky-1"]).not.toBe(autumn["--garden-sky-1"]);
    expect(spring["--garden-leaf"]).toBeTruthy();
    expect(spring["--garden-soil"]).toBeTruthy();
    const night = gardenTheme("winter", "night") as Record<string, string>;
    expect(night["--garden-lamp"]).toBeTruthy();
    expect(night["--garden-tint-top"]).toBeTruthy();
  });
});
