/**
 * 首屏那道门（src/runtimeReady.ts）。
 *
 * 钉的是「等待只为不抢跑，绝不为等待而卡死」：resolve 才算有结论，
 * promise 不存在 / 不 resolve / 抛错都必须放行。
 */
import { afterEach, describe, expect, it } from "vitest";
import { runtimeConfigReady } from "./runtimeReady";

const g = globalThis as any;

afterEach(() => {
  delete g.__COWORKER_CFG_READY__;
  delete g.__COWORKER_CFG_WAIT__;
});

describe("runtimeConfigReady", () => {
  it("入口没挂这道门（本地 dev / 桌面壳直注入 window 变量）→ 立即放行 no-gate", async () => {
    await expect(runtimeConfigReady(1000)).resolves.toBe("no-gate");
    expect(g.__COWORKER_CFG_WAIT__).toBe("no-gate");
  });

  it("config 有结论（哪怕什么都没拿到）→ settled 放行", async () => {
    let done: () => void = () => {};
    g.__COWORKER_CFG_READY__ = new Promise<void>((r) => {
      done = r;
    });
    const p = runtimeConfigReady(1000);
    expect(await Promise.race([p.then(() => "early"), Promise.resolve("waiting")])).toBe("waiting");
    done();
    await expect(p).resolves.toBe("settled");
    expect(g.__COWORKER_CFG_WAIT__).toBe("settled");
  });

  it("promise 永远不 resolve（内联脚本被 CSP 挡了之类）→ 到点先放行，绝不白屏", async () => {
    g.__COWORKER_CFG_READY__ = new Promise<void>(() => {});
    await expect(runtimeConfigReady(10)).resolves.toBe("timeout");
    expect(g.__COWORKER_CFG_WAIT__).toBe("timeout");
  });

  it("ready promise 被 reject 也算有结论（不能把启动挂在一个错误上）", async () => {
    g.__COWORKER_CFG_READY__ = Promise.reject(new Error("boom"));
    await expect(runtimeConfigReady(1000)).resolves.toBe("settled");
  });

  it("非 Promise 的脏值（别的脚本抢过这个名字）→ 当没有这道门", async () => {
    g.__COWORKER_CFG_READY__ = true;
    await expect(runtimeConfigReady(1000)).resolves.toBe("no-gate");
  });
});
