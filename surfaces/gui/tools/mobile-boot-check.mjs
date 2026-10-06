/**
 * 首屏时序的回归自检：慢配置 + 「不带 token 就 401」的桩后端，用真 Chrome 跑 dist/。
 *
 * 复现 2026-10-06 安卓那句「读不到日记」的机理：
 *   - `/runtime-config.json` 延迟 DELAY 毫秒才回（真手机网络上这就是几百毫秒）；
 *   - `/v1/deskmate/*` 只认 `X-OpenWorker-Token`，没带一律 401（与云端 uvicorn 一致）；
 *   - 花园在 mount 的 useEffect 里就发 `/moments`。
 * 没有 `__COWORKER_CFG_READY__` 那道门时，这一枪必然抢在 token 之前 → 401 → 整屏「读不到日记」。
 *
 * 用法（在 surfaces/gui 下）：
 *   npm run build
 *   node tools/mobile-boot-check.mjs                 # 期望 PASS
 *   node tools/mobile-boot-check.mjs --gate=off      # 反向对照：把门关掉，期望 FAIL（证明这条测试真抓得到 bug）
 *   node tools/mobile-boot-check.mjs --delay=900
 *
 * `--gate=off` 靠 route 拦截把 runtime-config 的延迟拉到 60s（等价于"门没起作用"），
 * 只动网络桩，不改产物 —— 所以它能对任意一次构建做对照。
 * 只读 dist/ 与本机端口，不碰云端、不写任何用户数据。
 */
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const GUI = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DIST = path.join(GUI, "dist");
const arg = (name, dft) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`));
  return hit ? hit.split("=")[1] : dft;
};
const TOKEN = "stub-token-please-ignore";
const DELAY = Number(arg("delay", 700));
const GATE_OFF = arg("gate", "on") === "off";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json",
};
const MY_POSTS = [
  { id: "p1", content: "今天写废了四千字", createdAt: "2026-10-05T20:58:00.000Z", loved: false },
  { id: "p2", content: "把她那句记进了备忘", createdAt: "2026-10-05T19:10:00.000Z", loved: true },
  { id: "p3", content: "早，先跑五百字", createdAt: "2026-10-04T01:20:00.000Z", loved: false },
];
const HER_DIARY = "# 十月五日\n## 21:03 <!--xj:e1-->\n他今天写到很晚，中间去厨房站了一会儿。\n## 22:40\n又给自己倒了杯茶。";

function handle(req, res) {
  const url = decodeURIComponent((req.url || "/").split("?")[0]);
  const authed = req.headers["x-openworker-token"] === TOKEN;
  const json = (code, obj, headers = {}) => {
    res.writeHead(code, { "content-type": "application/json", ...headers });
    res.end(JSON.stringify(obj));
  };
  if (url === "/runtime-config.json") {
    // 慢：这一延迟就是手机上首屏会抢跑的那几百毫秒
    return setTimeout(() => json(200, { __COWORKER_API_TOKEN__: TOKEN }), DELAY);
  }
  if (url === "/v1/health") return json(200, { status: "ok", model: "stub", default_workspace: null });
  if (url.startsWith("/v1/deskmate")) {
    if (!authed) return json(401, { error: "missing or invalid OpenWorker sidecar token" });
    if (url.startsWith("/v1/deskmate/moments")) return json(200, { items: MY_POSTS, next_cursor: null, comments: {}, loved: [] });
    if (url === "/v1/deskmate/diary") return json(200, { dates: ["2026-10-04", "2026-10-05"] });
    if (url === "/v1/deskmate/diary/2026-10-05") return json(200, { content: HER_DIARY });
    if (url === "/v1/deskmate/diary/2026-10-04") return json(200, { content: "# 十月四日\n## 09:12\n早。" });
    return json(200, {});
  }
  if (url.startsWith("/v1/") || url.startsWith("/ws")) return json(200, { sessions: [], items: [], messages: [] });
  const file = path.join(DIST, url === "/" ? "index.html" : url);
  if (!file.startsWith(DIST) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404);
    return res.end("nope");
  }
  res.writeHead(200, { "content-type": TYPES[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}

const srv = http.createServer(handle);
await new Promise((r) => srv.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${srv.address().port}`;
console.log(`桩后端 ${base}｜runtime-config 延迟 ${DELAY}ms｜/v1/deskmate 不带 token 一律 401｜门=${GATE_OFF ? "关掉（对照）" : "在"}`);

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
const calls = [];
page.on("request", (r) => {
  const u = new URL(r.url()).pathname;
  if (u.startsWith("/v1/")) calls.push(`${u} token=${r.headers()["x-openworker-token"] ? "有" : "无"}`);
});
if (GATE_OFF) {
  // 反向对照：把 runtime-config 的响应拖到 60 秒 —— 首屏拿不到 token，等价于"门没起作用"
  await page.route((u) => u.pathname === "/runtime-config.json", async (route) => {
    await new Promise((r) => setTimeout(r, 60_000));
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ __COWORKER_API_TOKEN__: TOKEN }) });
  });
}
await page.goto(`${base}/mobile.html#yard`, { waitUntil: "load" });
await page.waitForTimeout(GATE_OFF ? 4000 : DELAY + 2500);

// 只量花园那一屏：对话屏的 socket 在桩里永远是坏的（桩不是真 WS），不能混进来
const pane = page.locator(".m-pane:not(.hide)");
const fault = await pane.locator(".m-fault").count();
const faultText = fault ? (await pane.locator(".m-fault").first().innerText()).replace(/\s+/g, " ") : "";
const names = await pane.locator(".m-post-n").allInnerTexts();
const body = await pane.locator(".m-scroll").first().innerText();
await browser.close();
srv.close();

const her = names.filter((n) => n.includes("小桔")).length;
const mine = names.filter((n) => n.includes("萧盛")).length;
const raced = calls.filter((c) => /moments|diary/.test(c) && c.includes("token=无"));
console.log(`\n花园读口（顺序即证据）：\n  ${calls.join("\n  ")}`);
console.log(`\n时间线卡片：她 ${her} 条（应 3）· 你 ${mine} 条（应 3）；故障条 ${fault}${fault ? ` → "${faultText}"` : ""}`);

if (GATE_OFF) {
  // 对照组要看到的正是"坏了"：抢跑 + 故障条（或降级说明）
  const broke = raced.length > 0 || fault > 0 || her === 0;
  console.log(broke ? "\n对照组如预期失败 —— 说明这条自检真的抓得住抢跑" : "\n对照组竟然没坏：这条自检无效，别拿它当守卫");
  process.exit(broke ? 0 : 1);
}
const ok = fault === 0 && her === 3 && mine === 3 && raced.length === 0 && body.includes("倒了杯茶");
console.log(
  ok
    ? "\nPASS —— 没有抢跑、她的日记与你的条目一起上屏、没有「读不到日记」"
    : `\nFAIL —— 不抢跑:${raced.length === 0} 两路都读到:${her === 3 && mine === 3}(她${her}/你${mine}) 无故障:${fault === 0}`,
);
if (!ok) console.log(`（花园正文前 200 字：${body.slice(0, 200).replace(/\s+/g, " ")}）`);
process.exit(ok ? 0 : 1);
