// 权限菜单不再由 workspace 兼任开关（2026-11-10）：专家中心 / 「我的伙伴」起的
// persona 1V1 会话没有目录工作区，旧实现把菜单整个隐藏，mode 永远停在 interactive、
// 每一步都要点授权卡。契约：showModeMenu 显式控制显隐；不传时回退旧的
// workspace !== undefined 行为，保证其它调用点与既有测试不变。
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Composer, permissionMenuAvailable } from "./Composer";

const props = (extra: Partial<Parameters<typeof Composer>[0]> = {}) => ({
  mode: "interactive",
  model: "ollama:qwen2.5-coder:32b",
  models: ["ollama:qwen2.5-coder:32b"],
  running: false,
  connected: true,
  onSend: vi.fn(),
  onInterrupt: vi.fn(),
  onModeChange: vi.fn(),
  onModelChange: vi.fn(),
  ...extra,
});

afterEach(cleanup);

describe("permissionMenuAvailable", () => {
  it("只有纯对话 Chat 不显示权限菜单", () => {
    expect(permissionMenuAvailable("chat")).toBe(false);
  });

  it("工作区会话、桌搭、专家 / 伙伴 persona 1V1 都显示", () => {
    for (const a of [
      "cowork",
      "deskmate",
      "code",
      "brainstormer",
      "editor",
      "adaptation",
      "outline",
      "character",
      "editor-in-chief",
    ]) {
      expect(permissionMenuAvailable(a)).toBe(true);
    }
  });
});

describe("Composer ModeMenu 显隐", () => {
  it("专家 1V1（无 workspace）显式开启后可选「完全访问」并回调 set_mode 的值", () => {
    const onModeChange = vi.fn();
    render(<Composer {...props({ showModeMenu: true, workspace: undefined, onModeChange })} />);
    fireEvent.click(screen.getByRole("button", { name: "Mode" }));
    const menu = screen.getByTestId("mode-menu");
    expect(within(menu).getByText("Full access")).toBeTruthy();
    fireEvent.click(within(menu).getByText("Full access"));
    expect(onModeChange).toHaveBeenCalledWith("bypass-approvals");
  });

  it("showModeMenu=false 优先于已定义的 workspace", () => {
    render(<Composer {...props({ showModeMenu: false, workspace: "" })} />);
    expect(screen.queryByRole("button", { name: "Mode" })).toBeNull();
  });

  it("不传 showModeMenu 时回退旧的 workspace 门控", () => {
    const { unmount } = render(<Composer {...props({ workspace: "" })} />);
    expect(screen.getByRole("button", { name: "Mode" })).toBeTruthy();
    unmount();
    render(<Composer {...props({ workspace: undefined })} />);
    expect(screen.queryByRole("button", { name: "Mode" })).toBeNull();
  });

  it("专家 1V1 的菜单里同时带出「审批转信箱」开关", () => {
    render(<Composer {...props({ showModeMenu: true, onUnattendedChange: vi.fn() })} />);
    fireEvent.click(screen.getByRole("button", { name: "Mode" }));
    expect(within(screen.getByTestId("mode-menu")).getByRole("switch")).toBeTruthy();
  });
});
