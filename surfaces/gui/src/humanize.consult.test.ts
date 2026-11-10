import { describe, expect, it } from "vitest";

import { humanizeTool } from "./humanize";

describe("consult_experts humanization", () => {
  it("shows who is consulting and the book, not raw args", () => {
    const line = humanizeTool("consult_experts", {
      experts: ["character", "lit-editor"],
      brief: "书：《夜不收》（作者萧盛，万历援朝背景历史小说）……后面很长",
      book: "夜不收",
    });
    expect(line.pre).toBe("请专家看下：");
    expect(line.obj).toBe("人物专家、文学编辑");
    expect(line.post).toBe(" · 《夜不收》");
  });

  it("falls back to expert ids for unknown experts", () => {
    const line = humanizeTool("consult_experts", { experts: ["nope"] });
    expect(line.obj).toBe("nope");
    expect(line.post).toBeUndefined();
  });
});
