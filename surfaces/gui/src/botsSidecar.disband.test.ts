// @vitest-environment jsdom
// Bot 群页面本体在 Python sidecar（deskmate/bots/web.py 的 PAGE 常量），不在 React bundle。
// 这条用例把 PAGE 搬进 jsdom 跑真实 DOM 交互：桌面 Tauri 的 WKWebView 未实现 JS 确认面板，
// 原生 confirm 恒为 false —— 这里就按那个最坏环境模拟，锁住「撤群仍能完成」。
// 项目前端 tsconfig 未装 @types/node；这三个模块只在 vitest 的 Node 运行期用到。
// @ts-ignore
import { readFileSync } from "node:fs";
// @ts-ignore
import { dirname, resolve } from "node:path";
// @ts-ignore
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const py = readFileSync(resolve(here, "../../../deskmate/bots/web.py"), "utf8");
const pageMatch = py.match(/PAGE = r?"""([\s\S]*?)"""/);
if (!pageMatch) throw new Error("从 web.py 提取 PAGE 失败");
const html = pageMatch[1].replace("__BOT_BASE__", '""');
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? "";
const body = html.match(/<body>([\s\S]*?)<\/body>/)?.[1] ?? "";

const room = {
  room_id: "r1",
  topic: "夜不收讨论",
  bot_ids: ["reader", "publisher"],
  bot_names: { reader: "读者视角", publisher: "发行部主任" },
  created_at: 1,
  last_text: "",
  count: 0,
};
const bots = [
  { id: "reader", name: "读者视角", icon: "", tagline: "", role: "" },
  { id: "publisher", name: "发行部主任", icon: "", tagline: "", role: "" },
];

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("Bot 群 · 撤群确认层", () => {
  it("宿主 confirm 恒为 false 时仍能撤群，且取消不误删", async () => {
    const calls: { url: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (u: string, opt: { method?: string } = {}) => {
        const method = opt.method ?? "GET";
        calls.push({ url: u, method });
        let data: unknown = [];
        if (u === "/api/bots") data = bots;
        else if (u === "/api/rooms" && method === "GET") data = [room];
        else if (u.endsWith("/messages")) data = [];
        else if (method === "DELETE")
          data = { room_id: "r1", disbanded_at: 1, idempotent: false };
        return {
          ok: true,
          status: 200,
          json: async () => data,
          text: async () => JSON.stringify(data),
        };
      }),
    );
    // Tauri/wry 的 WKWebView：确认面板未实现 → 静默当作「取消」。
    const nativeConfirm = vi.fn(() => false);
    vi.stubGlobal("confirm", nativeConfirm);

    document.body.innerHTML = body;
    eval(script);
    await tick();

    const disband = document.querySelector<HTMLButtonElement>("#head-disband")!;
    expect(disband.classList.contains("hidden")).toBe(false); // 有群时按钮可见

    disband.click();
    await tick();
    const dlg = document.querySelector<HTMLElement>("#disband-dlg")!;
    expect(dlg).not.toBeNull();
    expect(dlg.classList.contains("hidden")).toBe(false);
    expect(dlg.textContent).toContain("消失");
    expect(dlg.textContent).toContain("留档");
    expect(nativeConfirm).not.toHaveBeenCalled();

    document.querySelector<HTMLButtonElement>("#disband-cancel")!.click();
    await tick();
    expect(dlg.classList.contains("hidden")).toBe(true);
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);

    disband.click();
    await tick();
    document.querySelector<HTMLButtonElement>("#disband-ok")!.click();
    await tick();
    await tick();
    expect(
      calls.some((c) => c.url === "/api/rooms/r1" && c.method === "DELETE"),
    ).toBe(true);
    expect(dlg.classList.contains("hidden")).toBe(true);
    expect(nativeConfirm).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
