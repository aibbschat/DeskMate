import { describe, expect, it } from "vitest";
import { askEvidence, askPhrase } from "./askPhrase";

describe("审批中文措辞", () => {
  it("常见工具都有中文说法，且绝不漏成英文", () => {
    expect(askPhrase("write_file", { path: "_小桔批注/第11章.md" })).toBe("新建/覆盖 第11章.md");
    expect(askPhrase("replace_in_file", { path: "/a/b/OUTLINE.md" })).toBe("改 OUTLINE.md");
    expect(askPhrase("run_shell", { description: "列出目录内容" })).toBe("跑一条命令 · 列出目录内容");
    expect(askPhrase("web_search", { query: "明实录 巡抚衙门" })).toBe("上网搜「明实录 巡抚衙门」");
    expect(askPhrase("browser_click", {})).toBe("操作浏览器");
  });

  it("未知工具兜底成可读中文，不返回空、不返回 undefined", () => {
    expect(askPhrase("some_exotic_tool", {})).toBe("用一下 some_exotic_tool");
    expect(askPhrase("", {})).toBe("做一件事");
  });

  it("动到删除/移动这类路径时，话要说重一点（提醒看清路径）", () => {
    expect(askPhrase("delete_file", { path: "/x/y.md" })).toContain("请看清路径");
  });

  it("证据行挑最有用的一个字段，超长截断", () => {
    expect(askEvidence({ path: "a/b.md", command: "ls" })).toBe("a/b.md");
    expect(askEvidence({ command: "x".repeat(300) }).length).toBeLessThanOrEqual(121);
    expect(askEvidence({})).toBe("");
  });
});
