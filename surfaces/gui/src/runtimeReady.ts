/**
 * 「运行时配置到位了没有」——手机端首屏必须等它（2026-10-06 真机反馈）。
 *
 * 为什么需要这道门：托管部署的 token 不烤进产物（见 docs/DEPLOY-PARITY.md），而是由
 * `mobile.html` 里一句 `fetch("runtime-config.json")` 在运行时塞进 window。这个 fetch 是异步的，
 * 而手机端两屏同时挂载，花园的 `useEffect` 一上来就发 `GET /v1/deskmate/moments`。
 * 冷启动时这一枪经常跑在 config 落地之前 → 不带 token → 后端 401（实测：带 token 200/30 条，
 * 不带来 401）→ 花园整块显示「读不到日记」。对话屏之所以看起来没事，是因为 `useChat` 有
 * 1.5s 起指数退避的有界重试，自己爬回来了；花园当时没有。
 *
 * ⚠ 别信「同步读 window 就够了」：`api.ts:55-60` 那句注释说「首个请求也能带上凭据」，
 * 实际上 config 未到时 `__COWORKER_RUNTIME_CONFIG__` 是 null，token 就是空串，请求照样 401。
 *
 * 只在手机端用（`index.html` 与桌面壳不动，守住「独立手机端、桌面零改动」那条拍板）。
 */

/** 结论会留在这里，供自检页说明「首屏那次失败是不是抢跑」。 */
declare global {
  interface Window {
    __COWORKER_CFG_READY__?: Promise<void>;
    __COWORKER_CFG_WAIT__?: "settled" | "timeout" | "no-gate";
  }
}

/**
 * 等 config settle。三种结局都要能往下走，绝不让应用卡死：
 *  - `settled`  内联脚本的 fetch 有了结论（拿到或不拿到都算）；
 *  - `timeout`  网络或脚本异常导致 promise 迟迟不 resolve，到点先放行（默认 1.5s）；
 *  - `no-gate`  根本没有这道门（桌面壳注入 window 变量、本地 dev），立即放行。
 */
export function runtimeConfigReady(timeoutMs = 1500): Promise<"settled" | "timeout" | "no-gate"> {
  const p = (globalThis as any).__COWORKER_CFG_READY__;
  const record = (why: "settled" | "timeout" | "no-gate") => {
    (globalThis as any).__COWORKER_CFG_WAIT__ = why;
    return why;
  };
  if (!(p instanceof Promise)) return Promise.resolve(record("no-gate"));
  return Promise.race<Promise<"settled" | "timeout" | "no-gate">>([
    p.then(() => record("settled")).catch(() => record("settled")),
    new Promise<"settled" | "timeout" | "no-gate">((r) => setTimeout(() => r(record("timeout")), timeoutMs)),
  ]);
}

/** token 现在拿得到吗（自检页用它说明「为什么首屏会先红一下」）。 */
export function hasRuntimeToken(): boolean {
  return !!(
    (globalThis as any).__COWORKER_API_TOKEN__ ||
    (globalThis as any).__COWORKER_RUNTIME_CONFIG__?.__COWORKER_API_TOKEN__
  );
}
