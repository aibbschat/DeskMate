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
    // polish / lore 仍是当年被撤掉的空占位，不许回潮。
    for (const gone of ["polish", "lore"]) {
      expect(EXPERTS.find((e) => e.id === gone)).toBeUndefined();
    }
    // character 已于 11-09 做成真专家（persona + 私有笔记本 + 正式头像），
    // 不再是占位卡 —— 旧断言把它和占位卡一起判死，属语义过期，改为锁住
    // 「回来时必须是就绪、有 persona、有头像的真卡」这个价值。
    const character = EXPERTS.find((e) => e.id === "character");
    expect(character?.status ?? "ready").toBe("ready");
    expect(character?.personaId).toBe("character");
    expect(character?.avatar).toBeTruthy();
  });
});
