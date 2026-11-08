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
});
