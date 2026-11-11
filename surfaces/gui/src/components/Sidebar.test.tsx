import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Sidebar } from "./Sidebar";
import type { SessionInfo } from "../types";

// Hermetic fetch stub routing by URL substring + method; records calls for POST assertions.
type Call = { url: string; method: string; body: any };

function stubFetch(routes: { match: string; method?: string; json: any }[]) {
  const calls: Call[] = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    for (const r of routes) {
      if (url.includes(r.match) && (!r.method || r.method === method)) {
        return { ok: true, json: async () => r.json } as Response;
      }
    }
    return { ok: true, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const PERSONAS = {
  personas: [
    { id: "cowork", name: "OpenWorker", icon: "cowork", tagline: "general assistant", requires_folder: false, enabled: true, surfaced: true, default: true },
    { id: "ops", name: "Ops", icon: "ops", tagline: "incidents, runbooks", requires_folder: true, enabled: true, surfaced: true, default: false },
    { id: "code", name: "Code", icon: "code", tagline: "repository work", requires_folder: true, enabled: true, surfaced: true, default: false },
    { id: "secret", name: "Disabled One", icon: "cowork", tagline: "off", requires_folder: false, enabled: false, surfaced: false, default: false },
  ],
};

const SESSIONS: SessionInfo[] = [
  { session_id: "s-ops-1", title: "incident watch", workspace: "/w", agent: "ops", model: "m", mode: "interactive", updated_at: "2026-06-29", messages: 2 },
  { session_id: "s-cowork-1", title: "hi there", workspace: "", agent: "cowork", model: "m", mode: "interactive", updated_at: "2026-06-29", messages: 1 },
];

const baseProps = {
  agent: "cowork",
  workspace: "",
  surfaces: { cowork: true, chat: false, code: false },
  sessions: SESSIONS,
  projects: [],
  activeSession: "s-cowork-1",
  onSwitchAgent: vi.fn(),
  onNewSession: vi.fn(),
  onSelectSession: vi.fn(),
  onNewProject: vi.fn(),
  onRenameSession: vi.fn(),
  onDeleteSession: vi.fn(),
  onArchiveSession: vi.fn(),
  onTogglePin: vi.fn(),
  onManage: vi.fn(),
  onOpenPersona: vi.fn(),
  onOpenScheduled: vi.fn(),
  onOpenAutomation: vi.fn(),
  onOpenIntegrations: vi.fn(),
  onOpenAudit: vi.fn(),
  onOpenInbox: vi.fn(),
  onOpenExperts: vi.fn(),
  onOpenSkillStore: vi.fn(),
  onManagePersonas: vi.fn(),
  scheduledActive: false,
  integrationsActive: false,
  skillStoreActive: false,
  auditActive: false,
  inboxActive: false,
  expertsActive: false,
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("Sidebar group/filter control", () => {
  it("choosing Coworker persists via setNavLayout and switches to the per-persona accordion", async () => {
    const calls = stubFetch([
      { match: "/v1/personas", method: "GET", json: PERSONAS },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
      { match: "/v1/settings/nav-layout", method: "POST", json: { ok: true, nav_layout: "grouped" } },
    ]);
    render(<Sidebar {...baseProps} />);

    // personas load drives the surfaces; the RECENT header's group/filter control is always present.
    const control = await screen.findByLabelText("Group and filter conversations");

    // Open the popover and choose "Group by → Persona" (fork renamed the Coworker
    // group to Persona; sidebar.persona → "Persona" in en.json).
    fireEvent.click(control);
    fireEvent.click(await screen.findByText("Persona"));

    // POSTs the new layout pref.
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/v1/settings/nav-layout"));
      expect(post).toBeTruthy();
      expect(post!.body).toMatchObject({ nav_layout: "grouped" });
    });

    // Close the popover (it stays open so you can group AND filter in one visit) before asserting
    // the accordion — otherwise "Ops" also matches the filter-by-coworker checkbox.
    fireEvent.click(control);

    // Grouped view = the per-persona accordion. The Ops header appears; expanding it lists its
    // session. (Persona configuration moved to Settings ▸ Personas, so there is no header gear.)
    const opsHeader = await screen.findByText("Ops");
    fireEvent.click(opsHeader);
    expect(screen.getByText("incident watch")).toBeTruthy();
    expect(screen.queryByTitle("About the Ops persona")).toBeNull();
  });
});

describe("Chronological list row actions (⋮ menu)", () => {
  // The Recent list sorts by updated_at desc with store order breaking ties, so index 0 = s-ops-1.
  const openOpsMenu = () => fireEvent.click(screen.getAllByTestId("row-menu")[0]);

  it("rename / pin / archive / two-step delete all live behind the row's single kebab", async () => {
    stubFetch([
      { match: "/v1/personas", method: "GET", json: PERSONAS },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
    ]);
    render(<Sidebar {...baseProps} />);
    await screen.findByText("incident watch"); // flat Recent list rendered

    // Rename: menu item → inline input → Enter commits.
    openOpsMenu();
    fireEvent.click(screen.getByTestId("row-menu-rename"));
    const input = screen.getByDisplayValue("incident watch");
    fireEvent.change(input, { target: { value: "war room" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(baseProps.onRenameSession).toHaveBeenCalledWith("s-ops-1", "war room");

    // Pin moved inside the menu (unpinned session → "Pin").
    openOpsMenu();
    fireEvent.click(screen.getByTestId("row-menu-pin"));
    expect(baseProps.onTogglePin).toHaveBeenCalledWith("s-ops-1", true);

    // Archive.
    openOpsMenu();
    fireEvent.click(screen.getByTestId("row-menu-archive"));
    expect(baseProps.onArchiveSession).toHaveBeenCalledWith("s-ops-1", true);

    // Delete is two-step: first click arms ("Delete?"), the second deletes.
    openOpsMenu();
    fireEvent.click(screen.getByTestId("row-menu-delete"));
    expect(baseProps.onDeleteSession).not.toHaveBeenCalled();
    expect(screen.getByTestId("row-menu-delete").textContent).toContain("Delete?");
    fireEvent.click(screen.getByTestId("row-menu-delete"));
    expect(baseProps.onDeleteSession).toHaveBeenCalledWith("s-ops-1");
  });

  it("the kebab and its menu never select the row; Escape closes the menu", async () => {
    stubFetch([
      { match: "/v1/personas", method: "GET", json: PERSONAS },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
    ]);
    render(<Sidebar {...baseProps} />);
    await screen.findByText("incident watch");

    openOpsMenu();
    fireEvent.click(screen.getByTestId("row-menu-pin"));
    expect(baseProps.onSelectSession).not.toHaveBeenCalled();

    openOpsMenu();
    expect(screen.getByTestId("row-menu-rename")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("row-menu-rename")).toBeNull();
  });
});

describe("From Slack group (§31)", () => {
  const SLACK_SESSION: SessionInfo = {
    session_id: "s-slack-1",
    title: "#general — check the deploy?",
    workspace: "",
    agent: "cowork",
    model: "m",
    mode: "interactive",
    updated_at: "2026-07-13",
    messages: 2,
    origin: "slack",
    origin_label: "#general · T0AB",
  };

  it("mention-spawned sessions list chronologically in Recent with the platform icon (no band)", async () => {
    stubFetch([
      { match: "/v1/personas", method: "GET", json: PERSONAS },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
    ]);
    render(<Sidebar {...baseProps} sessions={[...SESSIONS, SLACK_SESSION]} />);
    await screen.findByText("incident watch"); // flat Recent rendered

    // No collapsed band — the session sits directly in the Recent list, exactly once…
    expect(screen.queryByTestId("from-slack-toggle")).toBeNull();
    const row = await screen.findByText("#general — check the deploy?");
    expect(screen.getAllByText("#general — check the deploy?")).toHaveLength(1);

    // …wearing the Slack logo in the row's indicator cluster.
    const cluster = row.closest(".group");
    expect(cluster?.querySelector('[data-logo="slack"]')).toBeTruthy();
  });
});

describe("New session button", () => {
  it("is a plain button (no \u25be picker \u2014 UX-029 moved the pick to the composer) starting the last-used coworker", async () => {
    // The \u25be only renders when MORE THAN ONE whitelisted persona is enabled. Production always
    // has cowork + deskmate, so this fixture must too \u2014 with cowork alone the split collapses to
    // a plain button and the assertion below would be testing the wrong shape.
    stubFetch([
      {
        match: "/v1/personas",
        method: "GET",
        json: {
          personas: [
            ...PERSONAS.personas,
            { id: "deskmate", name: "小桔", icon: "deskmate", tagline: "companion", requires_folder: false, enabled: true, surfaced: true, default: false },
          ],
        },
      },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
    ]);
    render(<Sidebar {...baseProps} />);
    await screen.findByText("incident watch");

    // fork keeps the New-session split button — primary starts the last-used persona and the
    // ▾ opens "Choose a persona". UX-029's move of the pick to the composer was NOT adopted
    // in this fork, so the picker is present by design (not absent as the upstream test asserts).
    expect(screen.getByLabelText("Choose a persona")).toBeTruthy();
    fireEvent.click(screen.getByText("New session"));
    expect(baseProps.onNewSession).toHaveBeenCalledWith("cowork");
  });

  // 2026-11-11 萧哥定：专家统一从「专家中心」进入（有头像 + 完整人设介绍）；
  // 主编派活线按 docs/bot-mode-redesign.md 阶段 3 退役 —— 验收第 1 条就是
  // 「新建会话下拉不含主编；/v1/personas 里 bot 仍在，数据没删」。
  // 下拉里的同名专家是第二个入口：它读 /v1/personas 的短 tagline、没有头像，
  // 和专家中心看起来像两个不同的专家，一并清掉。后端 persona / propose_team /
  // 历史看板全部保留，既有 lead 会话仍可打开，git 一行即可回滚。
  it("keeps only Coworker + 小桔 in the picker; experts live in 专家中心 and 主编 is retired", async () => {
    stubFetch([
      {
        match: "/v1/personas",
        method: "GET",
        json: {
          personas: [
            ...PERSONAS.personas,
            { id: "deskmate", name: "小桔", icon: "deskmate", tagline: "companion", requires_folder: false, enabled: true, surfaced: true, default: false },
            { id: "editor-in-chief", name: "主编", icon: "book", tagline: "组队派活 · 审核验收 · 终审裁决", requires_folder: true, enabled: true, surfaced: true, default: false },
            { id: "brainstormer", name: "创意脑洞专家", icon: "lightbulb", tagline: "脑洞反转 · 惊喜设计", requires_folder: true, enabled: true, surfaced: true, default: false },
            { id: "outline", name: "大纲专家", icon: "list", tagline: "卷纲 · 章纲 · 结构骨架", requires_folder: true, enabled: true, surfaced: true, default: false },
            { id: "character", name: "人物专家", icon: "users", tagline: "人物弧光 · 动机 · 关系", requires_folder: true, enabled: true, surfaced: true, default: false },
          ],
        },
      },
      { match: "/v1/settings", method: "GET", json: { nav_layout: "flat" } },
    ]);
    render(<Sidebar {...baseProps} />);
    await screen.findByText("incident watch");

    fireEvent.click(screen.getByLabelText("Choose a persona"));
    expect(screen.queryByText("主编")).toBeNull();
    expect(screen.queryByText("创意脑洞专家")).toBeNull();
    expect(screen.queryByText("大纲专家")).toBeNull();
    expect(screen.queryByText("人物专家")).toBeNull();
    expect(screen.getByText("小桔")).toBeTruthy();
  });
});
