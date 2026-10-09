// 信箱 · 案 A「一摞信纸」（萧哥 2026-10-09 拍板）。
// 视觉与结构一起钉：每封是一张纸（抬头 + 正文 + 署名落款），往来是它下面另外的纸，
// 回信是一张印好抬头的稿纸 —— **不是聊天**：稿纸不得有 IM 输入框的边框/圆角，
// 纸上不钉按钮。三张截图（浅底 / 深底 / 窄屏）落在 test-results/ 供过目。
import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByTestId("nav-mailbox").click();
});

test("信箱 = 一摞信纸：抬头、署名落款、按未读数计数", async ({ page }) => {
  const letters = page.locator(".dm-letter");
  await expect(letters).toHaveCount(3);
  // 每张纸：抬头「小桔：」+ 落款「—— 小桔」+ 日期
  const first = letters.first();
  await expect(first.locator(".dm-letter-sig")).toHaveText("小桔：");
  await expect(first.locator(".dm-letter-by")).toHaveText("—— 小桔");
  await expect(first.locator(".dm-letter-date")).toContainText("2026-10-09");
  // 页头未读数（fixture 里 2 封未读）
  await expect(page.locator(".dm-mailbox-unread")).toHaveText("2 封未读");
  // 纸是 3px 直角压边，不是聊天的 14px 圆角卡片
  await expect(first.locator(".dm-sheet")).toHaveCSS("border-radius", "3px");
});

test("回信 = 稿纸：印好的抬头 + 横线纸 + 邮票 + 寄出（不许长成 IM 输入框）", async ({ page }) => {
  const first = page.locator(".dm-letter").first();
  await first.getByRole("button", { name: "回信" }).click();

  // 往来各占一张纸，两个声音靠抬头与署名区分（没有气泡底色）
  const thread = first.locator(".dm-letter-thread .dm-sheet");
  await expect(thread).toHaveCount(2);
  await expect(thread.nth(0).locator(".dm-letter-msg-from")).toHaveText("萧哥 提笔：");
  await expect(thread.nth(1).locator(".dm-letter-msg-from")).toHaveText("小桔：");
  // 落款日期来自后端的**秒**级 ts（渲染成 2026-…，不是 1970/万年）
  await expect(thread.nth(0).locator(".dm-letter-msg-date")).toContainText("-");

  const sheet = first.locator(".dm-letter-compose");
  await expect(sheet.locator(".dm-compose-hi")).toHaveText("小桔：");
  await expect(sheet.locator(".dm-stamp")).toContainText("邮");
  const ta = sheet.locator("textarea");
  // 稿纸没有输入框的那张脸：零边框、零圆角、透明底，靠横线分格
  await expect(ta).toHaveCSS("border-top-width", "0px");
  await expect(ta).toHaveCSS("border-radius", "0px");
  await expect(ta).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(ta).toHaveCSS("line-height", "28px");

  // 处置那排小字在纸外面，且不带边框（纸上不钉按钮）
  const btn = first.locator(".dm-letter-actions .dm-letter-btn").first();
  await expect(btn).toHaveCSS("border-top-width", "0px");
});

test("标已读 → 纸压暗、未读减一", async ({ page }) => {
  const first = page.locator(".dm-letter").first();
  await first.getByRole("button", { name: "标已读" }).click();
  await expect(first).toHaveClass(/dm-letter-read/);
  await expect(page.locator(".dm-mailbox-unread")).toHaveText("1 封未读");
});

test("寄出一句 → 她的回信落在同一摞纸上（同一引擎，非另开通道）", async ({ page }) => {
  const first = page.locator(".dm-letter").first();
  await first.getByRole("button", { name: "回信" }).click();
  await first.locator(".dm-letter-compose textarea").fill("先只给我看第三处。");
  await first.getByRole("button", { name: "寄出" }).click();
  // 本地先落他这张纸，随后她的回信（假 agent 经同一 WS 回来）追加在下面
  await expect(first.locator(".dm-letter-msg-user")).toHaveCount(2);
  await expect(first.locator(".dm-letter-msg-assistant").last()).toContainText("Echo:");
});

test("三张截图：浅底 / 深底 / 窄屏", async ({ page }) => {
  // 截图要含稿纸末尾那排（邮票 + 寄出），先把视口拉高，免得元素被裁在半路。
  await page.setViewportSize({ width: 900, height: 1500 });
  const box = page.locator(".dm-mailbox");
  await page.locator(".dm-letter").first().getByRole("button", { name: "回信" }).click();
  await expect(box.locator(".dm-stamp")).toBeInViewport();
  await box.screenshot({ path: "test-results/mailbox-a-light.png" });

  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.waitForTimeout(150);
  await box.screenshot({ path: "test-results/mailbox-a-dark.png" });

  await page.setViewportSize({ width: 392, height: 1200 });
  await page.waitForTimeout(150);
  await box.screenshot({ path: "test-results/mailbox-a-narrow.png" });
});
