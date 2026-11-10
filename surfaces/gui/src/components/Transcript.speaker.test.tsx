// 2026-11-10 萧哥拍板的三条口径：
//   1) 只改专家会话（cowork / chat / code / deskmate 仍显示小桔，全局兜底不动）
//   2) 头像一起换（专家图 / 渐变首字占位）
//   3) 桌搭三张卡片（inspire / advice / intent_discuss）保持原样
// 发言者身份的事实源是「会话 agent」，不是只在专家中心召唤时写入的 currentExpert。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Transcript } from "./Transcript";
import {
  expertSpeakerFor,
  getExpertByPersonaId,
  type SessionSpeaker,
} from "./deskmate/experts";
import type { Item } from "../types";

const BUILTIN = new Set(["cowork", "chat", "code", "deskmate", "myhelper"]);
const AVATARS: Record<string, string> = {
  adaptation: "adaptation.png",
  historian: "historian.png",
};

beforeEach(() => {
  // 与既有 TurnGroup 测试同口径：钉死 cards 风格，避免默认风格改变影响基线。
  localStorage.setItem("openwork-turn-style", "cards");
});
afterEach(() => {
  cleanup();
  localStorage.removeItem("openwork-turn-style");
});

describe("expertSpeakerFor：只改专家会话", () => {
  it("默认四件套一律不算专家", () => {
    for (const a of BUILTIN) {
      expect(
        expertSpeakerFor(a, {
          builtinAgentIds: BUILTIN,
          persona: { id: a, name: "小桔" },
          personaAvatar: "whatever.png",
        }),
      ).toBeNull();
    }
  });

  it("按 personaId 反查本地专家，short-drama ↔ adaptation 也能对上", () => {
    expect(getExpertByPersonaId("adaptation")?.id).toBe("short-drama");
    const s = expertSpeakerFor("adaptation", {
      builtinAgentIds: BUILTIN,
      personaAvatar: AVATARS.adaptation,
    });
    expect(s?.name).toBe("短剧剧本");
    expect(s?.expert?.avatar).toBe(AVATARS.adaptation);
    expect(expertSpeakerFor("brainstormer", { builtinAgentIds: BUILTIN })?.name).toBe("创意脑洞专家");
    expect(expertSpeakerFor("editor", { builtinAgentIds: BUILTIN })?.name).toBe("编辑专家");
  });

  it("圆桌等其它 persona 用后端 name + 头像表", () => {
    expect(
      expertSpeakerFor("historian", {
        builtinAgentIds: BUILTIN,
        persona: { id: "historian", name: "史料考据专家" },
        personaAvatar: AVATARS.historian,
      }),
    ).toEqual({ name: "史料考据专家", avatar: AVATARS.historian });
  });

  it("personas 还没加载时不瞎编名字，回落普通会话的小桔", () => {
    expect(expertSpeakerFor("mystery", { builtinAgentIds: BUILTIN })).toBeNull();
  });
});

const editorSpeaker: SessionSpeaker = {
  name: "编辑专家",
  expert: { ...getExpertByPersonaId("editor")!, avatar: undefined },
};
const assistant: Item[] = [{ kind: "assistant", text: "这是专家的回复" }];

describe("Transcript 气泡发言者", () => {
  it("专家会话：名字换成专家名，头像不再是小桔立绘", () => {
    const { container } = render(
      <Transcript items={assistant} speaker={editorSpeaker} onApprove={vi.fn()} />,
    );
    expect(screen.getByText("编辑专家")).toBeTruthy();
    expect(screen.queryByAltText("小桔")).toBeNull();
    expect(container.querySelector(".who .expert-avatar")).toBeTruthy();
  });

  it("普通会话：不传 speaker 时名字与立绘都保持小桔", () => {
    const { container } = render(<Transcript items={assistant} onApprove={vi.fn()} />);
    expect(screen.getByText("Xiaoju")).toBeTruthy(); // setup 强制 en；zh 是「小桔」
    expect(container.querySelector(".who img[alt='小桔']")).toBeTruthy();
  });

  it("voice / report 两种气泡同样跟随专家身份", () => {
    const items: Item[] = [
      { kind: "voice", audio: "data:audio/wav;base64,AAAA", text: "语音", seconds: 1 },
      { kind: "report", name: "审阅报告", report: {}, caption: "报告说明" },
    ];
    render(<Transcript items={items} speaker={editorSpeaker} onApprove={vi.fn()} />);
    expect(screen.getAllByText("编辑专家")).toHaveLength(2);
    expect(screen.queryByAltText("小桔")).toBeNull();
  });

  it("桌搭三张卡片保持原样：speaker 不影响灵感 / 看法 / 意图讨论", () => {
    const items: Item[] = [
      { kind: "inspire", text: "一条灵感" },
      { kind: "advice", text: "一点看法" },
      { kind: "intent_discuss", text: "一版产出", intent_id: "i1", round: 1, phase: "draft" },
    ];
    const { container } = render(
      <Transcript items={items} speaker={editorSpeaker} onApprove={vi.fn()} />,
    );
    expect(screen.getByText("💡 小桔的灵感")).toBeTruthy();
    expect(screen.getByText("🗣 小桔的看法")).toBeTruthy();
    expect(screen.getByText(/小桔的产出/)).toBeTruthy();
    expect(container.querySelectorAll(".who img[alt='小桔']")).toHaveLength(3);
  });
});

describe("编辑 / 大纲 / 人物：正式头像已补齐（2026-11-10）", () => {
  // 这三位原先没有 avatar，聊天气泡与专家中心卡片都回落成「渐变首字」占位。
  // vitest 下 PNG import 返回源文件绝对路径，故只断言「有值 + 渲染出 img」，
  // 不对路径做关键词判断（生产路径是带指纹的 /assets/xxx.png）。
  it.each(["editor", "outline", "character"])(
    "%s：注册表有图，气泡渲染 <img> 而非渐变首字占位",
    (personaId) => {
      const expert = getExpertByPersonaId(personaId)!;
      expect(expert.avatar).toBeTruthy();
      const speaker = expertSpeakerFor(personaId, { builtinAgentIds: BUILTIN })!;
      expect(speaker.expert?.avatar).toBeTruthy();
      const { container } = render(
        <Transcript items={assistant} speaker={speaker} onApprove={vi.fn()} />,
      );
      expect(container.querySelector(".who img.expert-avatar")).toBeTruthy();
      expect(container.querySelector(".who span.expert-avatar")).toBeNull();
    },
  );
});
