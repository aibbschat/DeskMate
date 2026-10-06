/**
 * 手机端启动顺序的静态守卫（读源文件，不跑浏览器）。
 *
 * 为什么用文本断言而不是渲染测试：这轮的三个 bug 全都发生在 **HTML 内联脚本与 React 的先后**上，
 * 渲染测试反而测不到（React 一起跑就看不出谁先谁后）：
 *  1. token 由异步 `fetch("runtime-config.json")` 注入，两屏一挂载就发请求 → 抢跑拿 401
 *     （花园整屏「读不到日记」）；
 *  2. `beforeinstallprompt` 只发一次，等 React 挂监听就晚了 → 应用内安装按钮永远出不来；
 *  3. manifest 必须挂上且用相对路径（`base: "./"`，绝对路径换部署位置就指到别的站去了）。
 * 这三条一旦有人改动 mobile.html 就会悄悄退化，所以钉在测试里。
 */
import { describe, expect, it } from "vitest";
// 与 skinRegistry.test.ts 同一套路：?raw 把源文件当文本读进来，断言的是**源码里的顺序**，
// 不需要 node 的 fs（本项目没装 @types/node），也不会真去执行入口。
import html from "../../mobile.html?raw";
import main from "./main.tsx?raw";

const indexOfFirst = (s: string, s2: string) => s.indexOf(s2);

describe("mobile.html 的启动顺序", () => {
  it("挂了 manifest，路径必须是云端 Caddy 免口令豁免的那一个", () => {
    // 整站套着 Basic Auth，只有 @pwameta 匹配的 /m.webmanifest 与 /m-icon-* 不查口令；
    // 换名字忘了同步豁免 → Chrome 拉 manifest 拿到 401 → 安卓连安装入口都不会出现。
    const link = html.match(/<link rel="manifest" href="([^"]+)"/);
    expect(link).not.toBeNull();
    expect(link![1]).toBe("/m.webmanifest");
    expect(html).toMatch(/rel="apple-touch-icon" href="\/m-icon-180\.png"/);
  });

  it("内联脚本先置 __COWORKER_CFG_READY__，再加载应用入口", () => {
    const ready = indexOfFirst(html, "__COWORKER_CFG_READY__");
    const entry = indexOfFirst(html, '<script type="module"');
    expect(ready).toBeGreaterThan(-1);
    expect(entry).toBeGreaterThan(ready);
  });

  it("config fetch 的每条结局都放行这道门（成功/非200/抛错），绝不把启动挂在一个 promise 上", () => {
    const cfg = html.slice(indexOfFirst(html, "__COWORKER_CFG_READY__"), indexOfFirst(html, '<script type="module"'));
    expect(cfg).toMatch(/catch/);
    expect(cfg.match(/settle\(\)/g)?.length).toBeGreaterThanOrEqual(2); // settle-on-settle + settle-on-throw
  });

  it("beforeinstallprompt 的监听在入口脚本之前（事件只发一次，晚挂就永远错过）", () => {
    const bip = indexOfFirst(html, "beforeinstallprompt");
    const entry = indexOfFirst(html, '<script type="module"');
    expect(bip).toBeGreaterThan(-1);
    expect(bip).toBeLessThan(entry);
    expect(html.slice(bip, entry)).toMatch(/preventDefault/); // 必须同步 preventDefault，否则事件作废
  });
});

describe("main.tsx 的启动顺序", () => {
  it("渲染在等这道门，而不是直接 render", () => {
    expect(main).toMatch(/runtimeConfigReady\(\)/);
    const gate = indexOfFirst(main, "runtimeConfigReady()");
    const renderCall = indexOfFirst(main, "ReactDOM.createRoot");
    expect(gate).toBeGreaterThan(renderCall); // createRoot 只出现在被门挡住的那个 render() 里
  });

  it("首屏占位（#boot）随渲染一起撤，不是模块一加载就撤", () => {
    const boot = indexOfFirst(main, 'getElementById("boot")');
    expect(main.slice(0, boot)).toContain("function render");
  });
});
