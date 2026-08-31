/**
 * 冒烟测试：首页瘦身（DeskHome）+ 个人中心（ProfileCenter）。
 *
 * 验证两点：
 * 1. DeskHome 只保留功能入口（文稿编校 / 召唤专家）+ 个人中心入口，
 *    私人化 chip（小桔日记 / 记忆同步 / 了解度 等）已全部移除。
 * 2. ProfileCenter 五个 Tab（概览 / 长期记忆 / 小桔日记 / 勋章 / 设置）可渲染。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { DeskHome } from "./DeskHome";
import { ProfileCenter } from "./ProfileCenter";

// 所有面板 fetch 一律返回空 payload，组件静默降级即可正常渲染。
function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, json: async () => ({}) }) as Response),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("首页瘦身（DeskHome）", () => {
  it("保留功能入口与个人中心入口", () => {
    stubFetch();
    render(<DeskHome onOpenProfile={() => {}} />);
    expect(screen.getByText("个人中心")).toBeTruthy();
    expect(screen.getByText("召唤专家")).toBeTruthy();
    expect(screen.getByText("Manuscript editing")).toBeTruthy();
  });

  it("私人化入口已从首页移除", () => {
    stubFetch();
    render(<DeskHome onOpenProfile={() => {}} />);
    expect(screen.queryByText("小桔日记")).toBeNull();
    expect(screen.queryByText("记忆同步")).toBeNull();
    expect(screen.queryByText("了解度")).toBeNull();
    expect(screen.queryByText("小桔是什么")).toBeNull();
    expect(screen.queryByText("个人分身")).toBeNull();
    expect(screen.queryByText("长期知识")).toBeNull();
    expect(screen.queryByText("它在学什么")).toBeNull();
  });
});

describe("个人中心（ProfileCenter）", () => {
  it("渲染五个 Tab", () => {
    stubFetch();
    render(<ProfileCenter onBack={() => {}} />);
    expect(screen.getByRole("tab", { name: /概览/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /长期记忆/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /小桔日记/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /勋章/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /设置/ })).toBeTruthy();
  });

  it("默认落在概览页（hero 展示小桔园）", () => {
    stubFetch();
    render(<ProfileCenter onBack={() => {}} />);
    expect(screen.getByText("这是你的小桔园")).toBeTruthy();
    expect(screen.getByText("最近记住的事")).toBeTruthy();
  });
});
