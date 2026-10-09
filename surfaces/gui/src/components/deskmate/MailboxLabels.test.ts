/**
 * 信箱抬头的人话标签（MailboxPage 纯函数）。
 *
 * 钉一条口径：界面上**永远不出现** `cloud_audit` / `emotional_care` 这类工程键名
 * ——落不到映射就回退「她的话」（萧哥：用户可见文案一律从用户角度写，禁写工程实现）。
 */

import { describe, expect, it } from "vitest";
import {
  letterGroup,
  letterLabel,
  letterTitle,
  monthKey,
  monthLabel,
  shortHandle,
} from "./MailboxPage";
import type { MailboxLetter } from "../../api";

function letter(over: Partial<MailboxLetter>): MailboxLetter {
  return {
    id: "cloud_audit:emotional_care:abcd",
    source: "cloud_audit",
    kind: "emotional_care",
    content: "",
    evidence: "",
    understanding: "",
    status: "pending",
    read_at: null,
    read: false,
    created_at: "",
    mailbox_session_id: null,
    ...over,
  } as MailboxLetter;
}

describe("letterLabel", () => {
  it("把已知的来源/种类翻成人话", () => {
    expect(letterLabel(letter({}))).toBe("她留意到 · 她想关心你");
    expect(letterLabel(letter({ source: "mobile_voice", kind: "idea_capture" }))).toBe(
      "手机那头 · 灵感"
    );
  });

  it("来源与种类同名时不重复念一遍", () => {
    expect(letterLabel(letter({ source: "care", kind: "care" }))).toBe("关怀");
  });

  it("未知种类绝不把 snake_case 键名甩给用户", () => {
    const l = letterLabel(letter({ source: "cloud_audit", kind: "some_future_kind" }));
    expect(l).toBe("她留意到");
    expect(l).not.toContain("some_future_kind");
    expect(letterLabel(letter({ source: "weird_source", kind: "weird_kind" }))).toBe("她的话");
  });
});

describe("shortHandle", () => {
  it("取信 id 末段前 6 位当回查句柄", () => {
    expect(shortHandle("cloud_audit:emotional_care:abcdef12")).toBe("abcdef");
  });
});

describe("letterTitle", () => {
  it("标题 = 正文首句截断（信没有主题行，不为此加字段、也不让她现编）", () => {
    expect(letterTitle(letter({ content: "看你昨晚灯还亮着。第三章那处时间线对不上。" }))).toBe(
      "看你昨晚灯还亮着"
    );
  });

  it("一句到底的长正文按 42 字切，带省略号", () => {
    const t = letterTitle(letter({ content: "一".repeat(60) }));
    expect(t.length).toBeLessThanOrEqual(43); // 42 + …
    expect(t.endsWith("…")).toBe(true);
  });

  it("换行/空白折叠成一行，不破坏列表行高", () => {
    expect(letterTitle(letter({ content: "第一段\n\n第二段" }))).toBe("第一段");
  });

  it("空信也不崩", () => {
    expect(letterTitle(letter({ content: "   " }))).toBe("（这封没写字）");
  });
});

describe("letterGroup", () => {
  it("按「她为什么写」归组，供已读/归档筛选", () => {
    expect(letterGroup(letter({ kind: "emotional_care" }))).toBe("关怀");
    expect(letterGroup(letter({ kind: "plot_contradiction" }))).toBe("书稿");
    expect(letterGroup(letter({ kind: "idea_capture" }))).toBe("灵感");
    expect(letterGroup(letter({ kind: "information_support" }))).toBe("资料");
  });

  it("认不出的种类进「别的」，不露出工程键名", () => {
    expect(letterGroup(letter({ kind: "some_future_kind" }))).toBe("别的");
  });
});

describe("monthKey / monthLabel", () => {
  it("取 created_at 的年月当分桶键", () => {
    expect(monthKey("2026-10-09T10:34:13")).toBe("2026-10");
    expect(monthKey("")).toBe("");
  });

  it("当月标出来，别的只写年月", () => {
    expect(monthLabel("2026-10", "2026-10")).toContain("本月");
    expect(monthLabel("2026-09", "2026-10")).toBe("2026年9月");
    expect(monthLabel("", "2026-10")).toBe("更早");
  });
});
