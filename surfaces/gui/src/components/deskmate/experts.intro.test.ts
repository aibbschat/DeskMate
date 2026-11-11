/**
 * 会话空态名片的「同一专家一致性」（2026-11-11 萧哥定）。
 *
 * 症状：从左上角下拉打开的大纲/人物专家没有头像、介绍是短 tagline；
 * 从专家中心打开的同一位有 PNG 头像、介绍是长文。两条道其实共用同一个
 * 后端 persona，只是前端用了两套展示字段 + 一个只在专家中心写入的瞬时
 * currentExpert。现在把名片收敛到按会话 agent 派生的 expertIntroFor。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { EXPERTS, expertIntroFor, getExpertByPersonaId } from "./experts";

const BUILTIN = new Set(["cowork", "chat", "code", "deskmate", "myhelper"]);

/** App.tsx 传给 expertIntroFor 的真实入参形状（两条道完全相同）。 */
function opts(agent: string) {
  return {
    builtinAgentIds: BUILTIN,
    persona: { id: agent, name: `${agent} 后端名`, tagline: "后端短标签" },
    personaAvatar: "static://persona-avatar.png",
  };
}

describe("expertIntroFor：两条道同一张脸、同一段介绍", () => {
  it.each(EXPERTS)("$id（personaId=$personaId）走本地专家卡，介绍是长文而非 tagline", (card) => {
    const agent = card.personaId!;
    const intro = expertIntroFor(agent, opts(agent));
    expect(intro).not.toBeNull();
    expect(intro?.expert).toBeTruthy();
    expect(intro?.name).toBe(card.displayName);
    expect(intro?.description).toBe(card.description);
    expect(intro?.description).not.toBe("后端短标签");
  });

  it("同一个 persona id 无论入口瞬时状态如何，结果字节一致", () => {
    for (const card of EXPERTS) {
      const agent = card.personaId!;
      // 模拟「专家中心」与「右栏/其它入口」两次独立渲染
      const a = expertIntroFor(agent, opts(agent));
      const b = expertIntroFor(agent, {
        ...opts(agent),
        persona: { ...opts(agent).persona, name: "换个后端名也不该变" },
      });
      expect(a).toEqual(b);
    }
  });

  it("short-drama 卡的 id 与 personaId 不同，仍按 personaId 反查到同一张卡", () => {
    expect(getExpertByPersonaId("adaptation")?.id).toBe("short-drama");
    const intro = expertIntroFor("adaptation", opts("adaptation"));
    expect(intro?.expert?.id).toBe("short-drama");
    // 卡本身没有 PNG，回落静物图；两条道都传同一张，保持一致
    expect(intro?.expert?.avatar).toBe("static://persona-avatar.png");
  });

  it("编辑专家的书稿审阅提示按数据开关派生，不按 id 硬编码", () => {
    const intro = expertIntroFor("editor", opts("editor"));
    expect(intro?.hint).toContain("提交书稿");
    expect(expertIntroFor("outline", opts("outline"))?.hint).not.toContain("提交书稿");
  });

  it("没有本地卡的圆桌老专家回落静物图 + 后端 tagline", () => {
    const intro = expertIntroFor("historian", opts("historian"));
    expect(intro?.expert).toBeUndefined();
    expect(intro?.avatar).toBe("static://persona-avatar.png");
    expect(intro?.description).toBe("后端短标签");
  });

  it.each(["cowork", "chat", "code", "deskmate", "myhelper"])(
    "内置 %s 没有专家名片，交给各自专用 hero",
    (agent) => {
      expect(expertIntroFor(agent, opts(agent))).toBeNull();
    },
  );
});

describe("App.tsx 接线不回退", () => {
  it("空态名片只认 expertIntro，不再按 currentExpert 分叉成两套字段", () => {
    // vitest 的 import.meta.url 在 jsdom 下不是 file: 协议，用 cwd（surfaces/gui）定位。
    const src = readFileSync(resolve(process.cwd(), "src/App.tsx"), "utf-8");
    const start = src.indexOf("expert-intro");
    const end = src.indexOf('agent === "cowork"', start);
    expect(start).toBeGreaterThan(0);
    expect(end).toBeGreaterThan(start);
    const block = src.slice(start, end);
    expect(block).toContain("expertIntro");
    expect(block).not.toContain("currentExpert");
    expect(block).not.toContain("expertPersona.tagline");
  });
});
