// 模型选择器按「上游厂商 × 免费/付费」分组折叠（2026-10-13 萧哥要求）。
// 旧行为：免费/付费两档平铺——免费网关 / 火山 / 小米 / OpenRouter 几百项混列，
// 更没法看。新行为契约（2026-10-18 opencode/Kilo 已删；2026-10-24 OmniRoute 免费层
// 实测全死亦删，fixture 换现存的 Ollama 本地免费网关）：
//   1) 每组一个上游厂商（后端 model_vendors 供名），档位徽标区分免费/付费；
//   2) 含当前选中项的组默认展开、小组（≤4 项）默认展开、大组默认收起；
//   3) 点组头折叠/展开（aria-expanded 跟随）；当前模型所在组置顶；
//   4) 点选项 → onModelChange。旧后端缺 model_vendors 时从 label 的 "· via X"
//      后缀兜底推导厂商名。
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Composer } from "./Composer";

const OLLAMA_VENDOR = "Ollama 本地";
const ARK_VENDOR = "Volcengine Agent Plan";
const XIAOMI_VENDOR = "Xiaomi token-plan";

const OLLAMA_MODELS = [
  "ollama:qwen2.5-coder:32b",
  "ollama:llama3.1:8b",
  "ollama:deepseek-r1:7b",
  "ollama:qwen2.5:14b",
  "ollama:phi3:mini",
];
const ARK_MODELS = [
  "ark-agent-plan-cn:deepseek-v4-flash",
  "ark-agent-plan-cn:deepseek-v4-pro",
  "ark-agent-plan-cn:doubao-seed-2.1-turbo",
  "ark-agent-plan-cn:minimax-m3",
  "ark-agent-plan-cn:kimi-k2.7-code",
];
const XIAOMI_MODELS = ["xiaomi:mimo-v2.6-pro"];

const props = (extra: Partial<Parameters<typeof Composer>[0]> = {}) => ({
  mode: "interactive",
  model: OLLAMA_MODELS[0],
  models: [...OLLAMA_MODELS, ...ARK_MODELS, ...XIAOMI_MODELS],
  modelLabels: {
    "ollama:qwen2.5-coder:32b": "Qwen2.5 Coder 32B · Ollama 本地",
    "ollama:llama3.1:8b": "Llama 3.1 8B · Ollama 本地",
    "ollama:deepseek-r1:7b": "DeepSeek R1 7B · Ollama 本地",
    "ollama:qwen2.5:14b": "Qwen2.5 14B · Ollama 本地",
    "ollama:phi3:mini": "Phi-3 Mini · Ollama 本地",
    "ark-agent-plan-cn:deepseek-v4-flash": "DeepSeek V4 Flash · Volcengine Agent Plan",
    "ark-agent-plan-cn:deepseek-v4-pro": "DeepSeek V4 Pro · Volcengine Agent Plan",
    "ark-agent-plan-cn:doubao-seed-2.1-turbo": "Doubao Seed 2.1 Turbo · Volcengine Agent Plan",
    "ark-agent-plan-cn:minimax-m3": "MiniMax M3 · Volcengine Agent Plan",
    "ark-agent-plan-cn:kimi-k2.7-code": "Kimi K2.7 Code · Volcengine Agent Plan",
    "xiaomi:mimo-v2.6-pro": "MiMo V2.6 Pro · Xiaomi token-plan",
  },
  modelTiers: {
    ...Object.fromEntries(OLLAMA_MODELS.map((m) => [m, "free"])),
    ...Object.fromEntries(ARK_MODELS.map((m) => [m, "paid"])),
    ...Object.fromEntries(XIAOMI_MODELS.map((m) => [m, "paid"])),
  },
  modelVendors: {
    ...Object.fromEntries(OLLAMA_MODELS.map((m) => [m, OLLAMA_VENDOR])),
    ...Object.fromEntries(ARK_MODELS.map((m) => [m, ARK_VENDOR])),
    ...Object.fromEntries(XIAOMI_MODELS.map((m) => [m, XIAOMI_VENDOR])),
  },
  running: false,
  connected: true,
  onSend: vi.fn(),
  onInterrupt: vi.fn(),
  onModeChange: vi.fn(),
  onModelChange: vi.fn(),
  ...extra,
});

const openPicker = () => {
  fireEvent.click(screen.getByTitle("Qwen2.5 Coder 32B · Ollama 本地"));
};

// 菜单里的查询统一收窄到 .dd-menu —— 触发 pill 与当前选项同名，不收紧会
// getMultipleElementsFoundError（pill 显示当前模型 label，菜单里也有它）。
const menu = () => {
  const el = document.querySelector(".dd-menu");
  if (!el) throw new Error("picker menu not open");
  return within(el as HTMLElement);
};

afterEach(() => {
  cleanup();
});

describe("Composer model picker — vendor × tier grouping (2026-10-13)", () => {
  it("groups by upstream vendor with a free/paid badge, current model's group first", () => {
    render(<Composer {...props()} />);
    openPicker();

    const heads = Array.from(document.querySelectorAll(".dd-group-head"));
    // 当前模型（Ollama 本地）所在组置顶；免费档整体排在付费档前。
    expect(heads.map((h) => h.querySelector(".dd-group-name")?.textContent)).toEqual([
      OLLAMA_VENDOR,
      ARK_VENDOR,
      XIAOMI_VENDOR,
    ]);
    // 档位徽标：Ollama = Free，两家付费 = Paid。
    expect(heads[0].querySelector(".dd-tier-badge")?.textContent).toBe("Free");
    expect(heads[0].querySelector(".dd-tier-badge.free")).toBeTruthy();
    expect(heads[1].querySelector(".dd-tier-badge")?.textContent).toBe("Paid");
    expect(heads[2].querySelector(".dd-tier-badge")?.textContent).toBe("Paid");
    // 组头带条目计数。
    expect(heads[0].querySelector(".dd-group-count")?.textContent).toBe("5");
    expect(heads[1].querySelector(".dd-group-count")?.textContent).toBe("5");
    expect(heads[2].querySelector(".dd-group-count")?.textContent).toBe("1");
  });

  it("the current model's group starts expanded; big groups start collapsed", () => {
    render(<Composer {...props()} />);
    openPicker();

    const heads = Array.from(document.querySelectorAll(".dd-group-head"));
    const ollamaHead = heads[0];
    const arkHead = heads[1];
    const xiaomiHead = heads[2];

    // 含当前选中项的 Ollama 组默认展开（打开先看到自己选的模型）。
    expect(ollamaHead.getAttribute("aria-expanded")).toBe("true");
    expect(menu().getByText("Qwen2.5 Coder 32B · Ollama 本地")).toBeTruthy();
    // 5 项的 Ark 大组默认收起：选项不在文档里。
    expect(arkHead.getAttribute("aria-expanded")).toBe("false");
    expect(menu().queryByText("DeepSeek V4 Flash · Volcengine Agent Plan")).toBeNull();
    // 1 项的小组（≤4）默认展开。
    expect(xiaomiHead.getAttribute("aria-expanded")).toBe("true");
    expect(menu().getByText("MiMo V2.6 Pro · Xiaomi token-plan")).toBeTruthy();
  });

  it("clicking a group header toggles collapse (aria-expanded follows)", () => {
    render(<Composer {...props()} />);
    openPicker();

    const arkHead = Array.from(document.querySelectorAll(".dd-group-head"))[1];
    expect(arkHead.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(arkHead);
    expect(arkHead.getAttribute("aria-expanded")).toBe("true");
    expect(menu().getByText("DeepSeek V4 Flash · Volcengine Agent Plan")).toBeTruthy();
    fireEvent.click(arkHead);
    expect(arkHead.getAttribute("aria-expanded")).toBe("false");
    expect(menu().queryByText("DeepSeek V4 Flash · Volcengine Agent Plan")).toBeNull();
  });

  it("clicking an option fires onModelChange with the full routed id", () => {
    const onModelChange = vi.fn();
    render(<Composer {...props({ onModelChange })} />);
    openPicker();

    fireEvent.click(menu().getByText("Qwen2.5 Coder 32B · Ollama 本地"));
    expect(onModelChange).toHaveBeenCalledWith("ollama:qwen2.5-coder:32b");
  });

  it("older backend without model_vendors: vendor falls back to the label's '· via X'", () => {
    const noVendors = props();
    delete (noVendors as Record<string, unknown>).modelVendors;
    render(<Composer {...noVendors} />);
    openPicker();

    const names = Array.from(document.querySelectorAll(".dd-group-name")).map(
      (h) => h.textContent,
    );
    // Ollama 组名来自 modelVendors，缺它时 → label 的 "· Ollama 本地" 后缀；付费组同理。
    expect(names).toContain("Ollama 本地");
    expect(names).toContain("Volcengine Agent Plan");
    expect(names).toContain("Xiaomi token-plan");
  });

  it("older backend without model_tiers: ollama: prefix still reads as free", () => {
    const noTiers = props();
    delete (noTiers as Record<string, unknown>).modelTiers;
    render(<Composer {...noTiers} />);
    openPicker();

    const heads = Array.from(document.querySelectorAll(".dd-group-head"));
    const ollamaHead = heads.find(
      (h) => h.querySelector(".dd-group-name")?.textContent === OLLAMA_VENDOR,
    );
    expect(ollamaHead?.querySelector(".dd-tier-badge")?.textContent).toBe("Free");
    const arkHead = heads.find(
      (h) => h.querySelector(".dd-group-name")?.textContent === ARK_VENDOR,
    );
    expect(arkHead?.querySelector(".dd-tier-badge")?.textContent).toBe("Paid");
  });
});
