/**
 * skinStore 单元测试：localStorage 持久化 / 订阅通知 / 兜底回落 / DOM 副作用。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

describe("skinStore 激活态", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute("data-skin-pre");
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("初始为 default（localStorage 空时）", async () => {
    const { getActiveSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    expect(getActiveSkin()).toBe("default");
  });

  it("localStorage 有合法值时启动即读出", async () => {
    localStorage.setItem("openwork-skin", "xiaoju-orange");
    // 清掉旧模块缓存，强制重新 evaluate readInitial
    vi.resetModules();
    const { getActiveSkin } = await import("./skinStore");
    expect(getActiveSkin()).toBe("xiaoju-orange");
  });

  it("localStorage 非法值时回落 default", async () => {
    localStorage.setItem("openwork-skin", "garbage");
    vi.resetModules();
    const { getActiveSkin } = await import("./skinStore");
    expect(getActiveSkin()).toBe("default");
  });

  it("setActiveSkin 写 localStorage 并通知订阅", async () => {
    const { getActiveSkin, setActiveSkin, subscribeSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    const cb = vi.fn();
    const unsub = subscribeSkin(cb);
    setActiveSkin("xiaoju-orange");
    expect(getActiveSkin()).toBe("xiaoju-orange");
    expect(localStorage.getItem("openwork-skin")).toBe("xiaoju-orange");
    expect(cb).toHaveBeenCalledWith("xiaoju-orange");
    unsub();
  });

  it("setActiveSkin 切到当前皮肤是 no-op（不写盘不通知）", async () => {
    localStorage.setItem("openwork-skin", "xiaoju-orange");
    vi.resetModules();
    const { setActiveSkin, subscribeSkin } = await import("./skinStore");
    const cb = vi.fn();
    subscribeSkin(cb);
    setActiveSkin("xiaoju-orange"); // same
    expect(cb).not.toHaveBeenCalled();
  });

  it("setActiveSkin 非法 id 回落 default 且通知", async () => {
    localStorage.setItem("openwork-skin", "xiaoju-orange");
    vi.resetModules();
    const { getActiveSkin, setActiveSkin, subscribeSkin } = await import("./skinStore");
    const cb = vi.fn();
    subscribeSkin(cb);
    setActiveSkin("nonexistent-skin");
    expect(getActiveSkin()).toBe("default");
    // 非法 id 解析为 default；当前已是 default 之外的皮肤 → 真的切，localStorage 也写
    expect(localStorage.getItem("openwork-skin")).toBe("default");
    expect(cb).toHaveBeenCalledWith("default");
  });

  it("setActiveSkin 非法 id 但当前已是 default 时是 no-op（不写盘不通知）", async () => {
    const { setActiveSkin, subscribeSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    const cb = vi.fn();
    subscribeSkin(cb);
    setActiveSkin("garbage");
    expect(cb).not.toHaveBeenCalled();
    expect(localStorage.getItem("openwork-skin")).toBeNull();
  });

  it("应用 data-skin-pre 到 <html>", async () => {
    const { setActiveSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    expect(document.documentElement.dataset.skinPre).toBe("default");
    setActiveSkin("xiaoju-orange");
    expect(document.documentElement.dataset.skinPre).toBe("xiaoju-orange");
  });

  it("useActiveSkin 切换时重渲染", async () => {
    const { renderHook, act } = await import("@testing-library/react");
    const { useActiveSkin, setActiveSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    const { result } = renderHook(() => useActiveSkin());
    expect(result.current).toBe("default");
    act(() => setActiveSkin("xiaoju-orange"));
    expect(result.current).toBe("xiaoju-orange");
  });

  it("订阅者取消订阅后不再收到通知", async () => {
    const { setActiveSkin, subscribeSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    const cb1 = vi.fn();
    const cb2 = vi.fn();
    const unsub1 = subscribeSkin(cb1);
    subscribeSkin(cb2);
    unsub1();
    setActiveSkin("xiaoju-orange");
    expect(cb1).not.toHaveBeenCalled();
    expect(cb2).toHaveBeenCalledWith("xiaoju-orange");
  });

  it("localStorage 写盘失败也不阻塞切换（本次会话内仍生效）", async () => {
    const { setActiveSkin, getActiveSkin, _resetForTests } = await import("./skinStore");
    _resetForTests();
    // mock localStorage.setItem 抛错
    const original = localStorage.setItem;
    localStorage.setItem = vi.fn(() => {
      throw new Error("quota exceeded");
    });
    try {
      setActiveSkin("xiaoju-orange");
      expect(getActiveSkin()).toBe("xiaoju-orange");
    } finally {
      localStorage.setItem = original;
    }
  });
});
