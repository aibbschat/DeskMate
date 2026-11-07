import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Sidebar } from "./Sidebar";

// 侧栏 mount 时会读 personas / settings；这里只给最小 stub，避免真出网。
function stubFetch() {
  const fn = vi.fn(async (url: string) => {
    if (url.includes("/v1/personas")) {
      return {
        ok: true,
        json: async () => ({
          personas: [
            { id: "cowork", name: "OpenWorker", icon: "cowork", tagline: "", requires_folder: false, enabled: true, surfaced: true, default: true },
          ],
        }),
      } as Response;
    }
    return { ok: true, json: async () => ({ nav_layout: "flat" }) } as Response;
  });
  vi.stubGlobal("fetch", fn);
}

const baseProps = {
  agent: "cowork",
  workspace: "",
  surfaces: { cowork: true, chat: false, code: false },
  sessions: [],
  projects: [],
  activeSession: "",
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

describe("Sidebar 多人协作分组", () => {
  it("三个入口收纳进一个分类，hover 才展开，点击子项走原回调", () => {
    stubFetch();
    const onOpenRoundtable = vi.fn();
    const onOpenBotRoom = vi.fn();
    const onOpenCreation = vi.fn();
    render(
      <Sidebar
        {...baseProps}
        onOpenRoundtable={onOpenRoundtable}
        onOpenBotRoom={onOpenBotRoom}
        onOpenCreation={onOpenCreation}
      />,
    );

    const group = screen.getByTestId("nav-collaboration");
    const toggle = screen.getByTestId("nav-collaboration-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // 折叠态：三个入口不再平铺在侧栏里（这是「导航太满」要解决的本体）。
    expect(screen.queryByTestId("nav-roundtable")).toBeNull();
    expect(screen.queryByTestId("nav-botroom")).toBeNull();
    expect(screen.queryByTestId("nav-creation")).toBeNull();

    fireEvent.mouseEnter(group);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("nav-collaboration-menu")).toBeTruthy();
    expect(screen.getByTestId("nav-roundtable")).toBeTruthy();
    expect(screen.getByTestId("nav-botroom")).toBeTruthy();
    expect(screen.getByTestId("nav-creation")).toBeTruthy();

    // 真实用法：每点一个子项菜单就收起，想点另一个要重新 hover。
    for (const [testId, spy] of [
      ["nav-botroom", onOpenBotRoom],
      ["nav-roundtable", onOpenRoundtable],
      ["nav-creation", onOpenCreation],
    ] as const) {
      fireEvent.click(screen.getByTestId(testId));
      expect(spy).toHaveBeenCalledTimes(1);
      expect(screen.queryByTestId("nav-collaboration-menu")).toBeNull();
      fireEvent.mouseLeave(group);
      fireEvent.mouseEnter(group);
      expect(screen.getByTestId("nav-collaboration-menu")).toBeTruthy();
    }

    fireEvent.mouseLeave(group);
    expect(screen.queryByTestId("nav-collaboration-menu")).toBeNull();
  });

  it("任一子项 active 时父分类高亮，键盘 focus 也能展开", () => {
    stubFetch();
    render(
      <Sidebar
        {...baseProps}
        botRoomActive
        onOpenRoundtable={vi.fn()}
        onOpenBotRoom={vi.fn()}
        onOpenCreation={vi.fn()}
      />,
    );
    const group = screen.getByTestId("nav-collaboration");
    const toggle = screen.getByTestId("nav-collaboration-toggle");
    expect(toggle.className).toContain("bg-paper");

    fireEvent.focus(group);
    expect(screen.getByTestId("nav-collaboration-menu")).toBeTruthy();
  });
});
