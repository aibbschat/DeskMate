import { describe, expect, it } from "vitest";
import { parseTs } from "./time";

describe("parseTs", () => {
  it("reads a UTC 'YYYY-MM-DD HH:MM:SS' stamp as UTC, not local time", () => {
    // 侧栏"刚聊完却显示 8h"的回归锁：这个串没有时区标记，直接 Date.parse 会
    // 按本地时区解读，凭空多出 UTC 偏移。
    const naive = Date.parse("2026-09-01 13:32:03");
    const parsed = parseTs("2026-09-01 13:32:03");
    expect(parsed).toBe(Date.parse("2026-09-01T13:32:03Z"));
    // 与"按本地时区解读"的结果之差，恰好等于本地 UTC 偏移（东八区 +8h）。
    // getTimezoneOffset() 返回 (UTC - local) 分钟，东八区为 -480，故取负号。
    expect(parsed - naive).toBe(-new Date().getTimezoneOffset() * 60_000);
  });

  it("leaves already-zoned ISO strings alone", () => {
    expect(parseTs("2026-09-01T13:32:03Z")).toBe(Date.parse("2026-09-01T13:32:03Z"));
    expect(parseTs("2026-09-01T13:32:03+08:00")).toBe(
      Date.parse("2026-09-01T13:32:03+08:00"),
    );
  });

  it("still parses date-only stamps (legacy sidebar rows)", () => {
    expect(parseTs("2026-06-29")).toBe(Date.parse("2026-06-29"));
    expect(parseTs("2026-06-29")).toBeGreaterThan(0);
  });

  it("returns 0 for empty / unparseable input instead of NaN", () => {
    expect(parseTs(null)).toBe(0);
    expect(parseTs(undefined)).toBe(0);
    expect(parseTs("")).toBe(0);
    expect(parseTs("not-a-date")).toBe(0);
  });

  it("passes numeric epoch seconds straight through", () => {
    expect(parseTs(1782691200000)).toBe(1782691200000);
  });
});
