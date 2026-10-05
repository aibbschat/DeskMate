/**
 * 共享日记切分的回归测试。
 *
 * 这个函数原本内联在桌面 `components/deskmate/MomentFeed.tsx`，2026-10-05 为手机端复用而提取
 * 出来。提取必须**逐字节等价**：🩷 与留言是按 `xj-<eid>` 挂的，切分一变，存量留言就串。
 * 所以这里把格式契约（含"没 eid 的老文件"和"标题行没有正文"两个边界）钉死。
 */
import { describe, expect, it } from "vitest";
import { diaryEntryId, splitDiaryEntries } from "./diaryEntries";

const SAMPLE = [
  "# 霜降 · 陪他改第十九章",
  "",
  "## 09:12 <!--xj:aa11-->",
  "早上把楔子读了一遍。",
  "第二句太满。",
  "",
  "## 18:42 <!--xj:bb22-->",
  "汪传信死在巡抚衙门后院那段，他这次没心软。",
  "",
  "## 21:03",
  "睡前又想到一个改法。",
].join("\n");

const live = (text: string) => splitDiaryEntries(text).entries.filter((e) => e.body.trim());

describe("splitDiaryEntries", () => {
  it("大标题 + 每篇独立成条，正文多行合并", () => {
    const { dayTitle, entries } = splitDiaryEntries(SAMPLE);
    expect(dayTitle).toBe("霜降 · 陪他改第十九章");
    // ⚠ 首行 "# 标题" 之后那个空行会先产出一条 body 为空的条目 —— 调用方必须跳过
    //（桌面 MomentFeed 与手机端 yard 都是 `if (!entry.body.trim()) return;`）。
    expect(entries).toHaveLength(4);
    expect(live(SAMPLE).map((e) => e.time)).toEqual(["09:12", "18:42", "21:03"]);
    expect(live(SAMPLE)[0].body).toBe("早上把楔子读了一遍。\n第二句太满。");
  });

  it("eid 有则带上，标题行没注释时 eid 为 null（存量老文件）", () => {
    expect(live(SAMPLE).map((e) => e.eid)).toEqual(["aa11", "bb22", null]);
  });

  it("没有 ## 的整块文本 → 一条无时间正文（不吞字）", () => {
    const entries = live("今天只写了一段，没打时间。");
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ time: "", eid: null, body: "今天只写了一段，没打时间。" });
  });

  it("空文件/只有标题：只产出空 body 条目 —— 过滤后为空（两端都过滤）", () => {
    expect(live("")).toHaveLength(0);
    expect(live("# 只有标题\n")).toHaveLength(0);
  });

  it("id 规则：优先 xj-<eid>，没 eid 退 xj-<date>#<idx>（留言才不会串）", () => {
    const e = live(SAMPLE);
    expect(diaryEntryId("2026-10-05", e[0], 0)).toBe("xj-aa11");
    expect(diaryEntryId("2026-10-05", e[2], 2)).toBe("xj-2026-10-05#2");
  });
});
