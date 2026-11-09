import { describe, expect, it } from "vitest";
import { EXPERTS } from "./experts";
import { personaGlyph } from "../personaIcon";

// 2026-11-08 决策 A：开脑洞固定在独立专家。专家中心的卡片就是入口，
// 所以它必须有 avatar（否则会退化成首字渐变占位）、就绪、且挂到 brainstormer persona。
describe("创意脑洞专家（独立专家入口）", () => {
  it("专家中心有卡：独立 persona + 头像 + 就绪", () => {
    const e = EXPERTS.find((x) => x.id === "brainstormer");
    expect(e).toBeDefined();
    expect(e?.personaId).toBe("brainstormer");
    expect(e?.status ?? "ready").toBe("ready");
    expect(e?.avatar).toBeTruthy();
  });

  it("图标解析成 lightbulb（不是 fallback 的 sparkle）", () => {
    expect(personaGlyph("lightbulb", "knowledge")).toBe("lightbulb");
  });

  it("不再有「即将上线」占位卡（2026-11-09 萧哥定：点进去是空的，先撤）", () => {
    const soon = EXPERTS.filter((e) => (e.status ?? "ready") !== "ready");
    expect(soon).toHaveLength(0);
    for (const gone of ["character", "polish", "lore"]) {
      expect(EXPERTS.find((e) => e.id === gone)).toBeUndefined();
    }
  });
});
