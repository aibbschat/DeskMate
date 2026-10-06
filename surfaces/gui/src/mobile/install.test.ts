/**
 * 「装到主屏」的判定与文案（2026-10-06 安卓真机反馈驱动）。
 *
 * 钉三件事：
 *  1. manifest 的 Content-Type 不是 JSON 类就必须判失败——今天安卓装不上正是这个原因
 *     （Caddy 不认 .webmanifest 扩展名，响应干脆没有 Content-Type；iOS 不看 manifest 所以没露馅）。
 *  2. 拿不到安装弹窗时，文案要给**可照抄的手动路径**，不能只说"不支持"。
 *  3. 已经装过了就别再喊"可以装"。
 */
import { describe, expect, it } from "vitest";
import { installCopyOf, installStateOf, manifestOk, probeManifestFrom } from "./install";

const env = (over: Partial<Parameters<typeof installStateOf>[0]> = {}) =>
  ({ standalone: false, canPrompt: false, ua: "", ...over });

const UA = {
  androidChrome:
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
  androidEdge:
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36 EdgA/129.0.0.0",
  samsung:
    "Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/115.0.0.0 Mobile Safari/537.36",
  iosSafari:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  wechat:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.50",
  desktop: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
};

describe("Content-Type 判据（今天这个 bug 的守卫）", () => {
  it("空的 / octet-stream / 文本都算无效", () => {
    expect(manifestOk(200, "")).toBe(false); // ← Caddy 不认扩展名时就是这个形态
    expect(manifestOk(200, "application/octet-stream")).toBe(false);
    expect(manifestOk(200, "text/html")).toBe(false);
  });

  it("manifest+json 与 application/json 都算有效", () => {
    expect(manifestOk(200, "application/manifest+json")).toBe(true);
    expect(manifestOk(200, "application/manifest+json; charset=utf-8")).toBe(true);
    expect(manifestOk(200, "application/json")).toBe(true);
  });

  it("不是 200 一律无效（哪怕类型对）", () => {
    expect(manifestOk(401, "application/manifest+json")).toBe(false);
  });

  it("自检文案要把「类型不对」和「拿不到」分开说，并点明后果", () => {
    const noType = probeManifestFrom({ url: "/m.webmanifest", status: 200, type: "" });
    expect(noType.ok).toBe(false);
    expect(noType.note).toContain("不是 JSON 类");
    expect(noType.note).toContain("安装入口");

    const blocked = probeManifestFrom({ url: "/m.webmanifest", status: 401, type: "" });
    expect(blocked.ok).toBe(false);
    expect(blocked.note).toContain("HTTP 401");

    const good = probeManifestFrom({ url: "/m.webmanifest", status: 200, type: "application/manifest+json" });
    expect(good.ok).toBe(true);
    expect(good.note).toBe("正常");
  });
});

describe("安装状态判定", () => {
  it("已经 standalone → installed，且优先级最高（有弹窗也不催装）", () => {
    expect(installStateOf(env({ standalone: true, canPrompt: true, ua: UA.androidChrome }))).toBe("installed");
  });

  it("浏览器给了弹窗 → prompt，不论什么内核", () => {
    expect(installStateOf(env({ canPrompt: true, ua: UA.desktop }))).toBe("prompt");
  });

  it("微信内置优先认出来，别教人去点一个不存在的菜单", () => {
    expect(installStateOf(env({ ua: UA.wechat }))).toBe("embedded");
  });

  it("iOS Safari → ios；安卓 Chrome/Edge/三星 → android；桌面 → other", () => {
    expect(installStateOf(env({ ua: UA.iosSafari }))).toBe("ios");
    expect(installStateOf(env({ ua: UA.androidChrome }))).toBe("android");
    expect(installStateOf(env({ ua: UA.androidEdge }))).toBe("android");
    expect(installStateOf(env({ ua: UA.samsung }))).toBe("android");
    expect(installStateOf(env({ ua: UA.desktop }))).toBe("other");
    expect(installStateOf(env({ ua: "" }))).toBe("other");
  });
});

describe("文案从用户角度写，且给出可照抄的路径", () => {
  it("安卓没给弹窗时，说清菜单里叫什么（Edge/三星叫法不同，要给全），并点破 Edge 安卓压根没有入口", () => {
    const c = installCopyOf("android");
    expect(c.hint).toContain("安装应用");
    expect(c.hint).toContain("Edge");
    expect(c.hint).toContain("三星");
    expect(c.hint).toContain("Chrome");
    expect(c.action).toBeNull();
  });

  it("有弹窗时按钮就是主路径，别再让人去翻菜单", () => {
    const c = installCopyOf("prompt");
    expect(c.action).toBe("prompt");
    expect(c.title).toContain("可以装");
  });

  it("iOS 说分享菜单；微信说换浏览器；都不给假按钮", () => {
    expect(installCopyOf("ios").hint).toContain("分享");
    expect(installCopyOf("embedded").hint).toContain("浏览器打开");
  });

  it("绝不承诺没做的能力（第一批明确不上推送：不能说「她会主动通知你」）", () => {
    for (const s of ["installed", "prompt", "embedded", "ios", "android", "other"] as const) {
      expect(installCopyOf(s).hint).not.toMatch(/会主动(通知|推送)|给你推送|推送通知你/);
    }
  });

  it("已装态不再喊安装", () => {
    const c = installCopyOf("installed");
    expect(c.title).toContain("已经在");
    expect(c.action).toBeNull();
  });
});
