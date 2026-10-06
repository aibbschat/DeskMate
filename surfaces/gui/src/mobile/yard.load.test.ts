/**
 * 花园读口的 fail-open（2026-10-06 安卓真机反馈驱动）。
 *
 * 萧哥实测：「花园日记提示，读不到日记」。云端取证：带 token 打 `/v1/deskmate/moments` 是
 * 200 + 30 条，不带 token 是 401 —— 手机冷启动时首屏请求抢在 runtime-config 落地之前发出去，
 * 拿到 401，而旧实现把两个读口绑成一荣俱荣：`/moments` 一挂，**连本来读得到的她的日记也不给看**，
 * 整屏只剩一句「读不到日记」。
 *
 * 这里钉的是修好后的不变量：谁活着谁上屏，另一路失败只降级成一行说明；
 * 两种 401 的文案绝不混（站点口令 ≠ 要重新部署）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { faultText, loadTimeline } from "./yard";

const routes = new Map<string, { status: number; body?: unknown; headers?: Record<string, string> }>();

vi.mock("../api", () => ({
  httpBase: () => "http://t",
  authedFetch: (url: string) => {
    const key = String(url).replace(/^https?:\/\/[^/]+/, "");
    const r = routes.get(key);
    if (!r) return Promise.reject(new Error(`no route ${key}`));
    return Promise.resolve(
      new Response(JSON.stringify(r.body ?? {}), {
        status: r.status,
        headers: r.headers ?? { "content-type": "application/json" },
      }),
    );
  },
}));

const SITE_401 = { status: 401, headers: { "www-authenticate": 'Basic realm="restricted"', "content-length": "0" } };
const TOKEN_401 = { status: 401, body: { error: "missing or invalid OpenWorker sidecar token" } };

/** 后端 DiaryWriter 的真实形态：`## HH:MM <!--xj:eid-->`，eid 可以缺省（存量老文件就没有） */
const HER_DAY = "# 十月五日\n## 21:03 <!--xj:e1-->\n今天他写到很晚。\n## 22:40\n又给自己倒了杯茶。";

beforeEach(() => {
  routes.clear();
});

describe("两路都活：正常合并", () => {
  beforeEach(() => {
    routes.set("/v1/deskmate/moments?limit=30", {
      status: 200,
      body: { items: [{ id: "p1", content: "我记的一句", createdAt: "2026-10-05T13:39:59.886Z", loved: false }], next_cursor: null, comments: {}, loved: [] },
    });
    routes.set("/v1/deskmate/diary", { status: 200, body: { dates: ["2026-10-04", "2026-10-05"] } });
    routes.set("/v1/deskmate/diary/2026-10-04", { status: 200, body: { content: "# 十月四日\n## 09:12\n早。" } });
    routes.set("/v1/deskmate/diary/2026-10-05", { status: 200, body: { content: HER_DAY } });
  });

  it("她的条目独立成卡、按时间倒序、没有降级说明", async () => {
    const r = await loadTimeline(30);
    expect(r.ok).toBe(true);
    expect(r.data.degraded).toBe("");
    const ids = r.data.moments.map((m) => m.id);
    // 有 eid 的用后端给的稳定 id（重排/增删都不变），没 eid 的老条目退回 日期#序号
    expect(ids).toContain("xj-e1");
    expect(ids).toContain("xj-2026-10-05#1");
    expect(ids).toContain("p1");
    // 22:40 > 21:03 > 我那句 13:39:59Z（=本地 21:39）> 10-04 09:12
    expect(r.data.moments[0].content).toContain("倒了杯茶");
    expect(r.data.moments[r.data.moments.length - 1].author).toBe("xiaoju");
    expect(r.data.moments[1].id).toBe("p1");
  });
});

describe("一路挂了：谁活着谁上屏", () => {
  it("她读得到、你的 401 —— 时间线照常出她的记录，只加一行说明（今天这条 bug 的守卫）", async () => {
    routes.set("/v1/deskmate/moments?limit=30", SITE_401);
    routes.set("/v1/deskmate/diary", { status: 200, body: { dates: ["2026-10-05"] } });
    routes.set("/v1/deskmate/diary/2026-10-05", { status: 200, body: { content: HER_DAY } });
    const r = await loadTimeline(30);
    expect(r.ok).toBe(true);
    expect(r.data.moments.length).toBe(2);
    expect(r.data.moments.every((m) => m.author === "xiaoju")).toBe(true);
    expect(r.data.degraded).toContain("你写的那部分");
  });

  it("你读得到、她的日期列表 401 —— 你的条目照常出", async () => {
    routes.set("/v1/deskmate/moments?limit=30", {
      status: 200,
      body: { items: [{ id: "p1", content: "在", createdAt: "2026-10-05T20:00:00" }], comments: {} },
    });
    routes.set("/v1/deskmate/diary", SITE_401);
    const r = await loadTimeline(30);
    expect(r.ok).toBe(true);
    expect(r.data.moments.map((m) => m.content)).toEqual(["在"]);
    expect(r.data.degraded).toContain("没读到");
  });

  it("逐日正文失败也计入降级说明（不是静默少几天）", async () => {
    routes.set("/v1/deskmate/moments?limit=30", { status: 200, body: { items: [], comments: {} } });
    routes.set("/v1/deskmate/diary", { status: 200, body: { dates: ["2026-10-05"] } });
    routes.set("/v1/deskmate/diary/2026-10-05", { status: 500, body: { detail: "boom" } });
    const r = await loadTimeline(30);
    expect(r.ok).toBe(true);
    expect(r.data.degraded).toContain("她最近");
  });
});

describe("两路都挂：才算真故障，且必须分得清是哪种", () => {
  it("站点口令没过（Caddy 的 401）→ fault=site", async () => {
    routes.set("/v1/deskmate/moments?limit=30", SITE_401);
    routes.set("/v1/deskmate/diary", SITE_401);
    const r = await loadTimeline(30);
    expect(r.ok).toBe(false);
    expect(r.fault).toBe("site");
    expect(faultText(r).hint).toContain("不需要重新部署");
  });

  it("应用 token 不对（后端的 401 JSON）→ fault=token，文案才提重新部署", async () => {
    routes.set("/v1/deskmate/moments?limit=30", TOKEN_401);
    routes.set("/v1/deskmate/diary", TOKEN_401);
    const r = await loadTimeline(30);
    expect(r.fault).toBe("token");
    expect(faultText(r).hint).toContain("重新部署");
    expect(faultText(r).hint).not.toContain("不需要重新部署");
  });

  it("网络断了 → fault=net，并且说会自动重试", async () => {
    const r = await loadTimeline(30); // 一条路由都没配 → authedFetch reject
    expect(r.ok).toBe(false);
    expect(r.fault).toBe("net");
    expect(faultText(r).title).toContain("连不上");
  });

  it("零数据态：都 200 但都是空 → ok、空数组、无降级（不许因 reduce/undefined 崩）", async () => {
    routes.set("/v1/deskmate/moments?limit=30", { status: 200, body: { items: [], comments: {} } });
    routes.set("/v1/deskmate/diary", { status: 200, body: { dates: [] } });
    const r = await loadTimeline(30);
    expect(r.ok).toBe(true);
    expect(r.data.moments).toEqual([]);
    expect(r.data.degraded).toBe("");
  });
});
