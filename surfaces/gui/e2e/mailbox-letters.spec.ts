// 信箱 · 三栏收件箱（萧哥 2026-10-09 第二轮拍板：乙的纸 + 三栏的骨架）。
// 钉四件事：① 默认哪栏都不展开（打开信箱不再是一桌子纸）；② 标题列只给首句截断，
// 正文要点开才有；③ 信纸仍是案 A 的纸（稿纸不得长成 IM 输入框）；④ 处置过的信
// **当着面移到归档**，不再一按就蒸发。三张截图（浅/深/窄）落 test-results/ 供过目。
import { expect, test } from "@playwright/test";
import { mockApi } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByTestId("nav-mailbox").click();
});

test("默认态：三栏都在，但哪栏都没展开", async ({ page }) => {
  await expect(page.getByTestId("mb-cat-unread")).toContainText("2"); // 未读 2
  await expect(page.getByTestId("mb-cat-read")).toContainText("1"); // 已读 1
  await expect(page.getByTestId("mb-cat-archived")).toContainText("1"); // 归档 1
  // 没点分类前，标题列与信纸都只有指引，不是一桌子纸
  await expect(page.locator(".dm-mb-list")).toContainText("左边挑一栏");
  await expect(page.locator(".dm-mb-read")).toContainText("挑一封");
  await expect(page.locator(".dm-mb-read .dm-sheet")).toHaveCount(0);
  await expect(page.locator(".dm-mb-row")).toHaveCount(0);
});

test("点未读 → 只出标题；点标题 → 才摊开信纸，并自动算已读", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  const rows = page.getByTestId("mb-row");
  await expect(rows).toHaveCount(2);
  // 标题 = 正文首句截断，整篇正文不在这一步露出
  await expect(rows.first()).toContainText("看你昨晚灯还亮着");
  await expect(rows.first()).not.toContainText("她的理解");

  await rows.first().click();
  const sheet = page.locator(".dm-mb-read .dm-sheet").first();
  await expect(sheet.locator(".dm-letter-sig")).toHaveText("小桔：");
  await expect(sheet.locator(".dm-letter-by")).toHaveText("—— 小桔");
  await expect(sheet.locator(".dm-letter-date")).toContainText("2026-10-09");
  await expect(sheet).toHaveCSS("border-radius", "3px");
  // 点开即已读 → 未读计数掉到 1
  await expect(page.getByTestId("mb-cat-unread")).toContainText("1");
});

test("信纸上的表情渲染成贴纸，不露 emote: 符号", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  await page.getByTestId("mb-row").first().click();
  const body = page.locator(".dm-letter-body").first();
  await expect(body.locator("img")).toHaveCount(1);
  await expect(body).not.toContainText("emote:");
});

test("回信 = 稿纸：印好的抬头 + 横线 + 邮票 + 寄出（不许长成 IM 输入框）", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  await page.getByTestId("mb-row").first().click();
  await page.getByRole("button", { name: "回信" }).click();

  // 往来各一张纸，微微右移一档（同一件事的后续，不是一条流）
  const leaves = page.locator(".dm-mb-read .dm-letter-msg");
  await expect(leaves).toHaveCount(2);
  await expect(leaves.first().locator(".dm-letter-msg-from")).toHaveText("萧哥 提笔：");
  await expect(leaves.first().locator(".dm-letter-msg-date")).toContainText("2026-10-09 11:02");

  const ta = page.locator(".dm-letter-compose textarea");
  await expect(page.locator(".dm-compose-hi")).toHaveText("小桔：");
  await expect(page.locator(".dm-stamp")).toContainText("邮");
  await expect(ta).toHaveCSS("border-top-width", "0px");
  await expect(ta).toHaveCSS("border-radius", "0px");
  await expect(ta).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  await expect(ta).toHaveCSS("line-height", "28px");
  // 处置那排小字在纸外面且不带边框
  await expect(page.locator(".dm-letter-btn").first()).toHaveCSS("border-top-width", "0px");
});

test("寄出一句 → 她的回信落在同一摞纸上（同一引擎，非另开通道）", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  await page.getByTestId("mb-row").first().click();
  await page.getByRole("button", { name: "回信" }).click();
  await page.locator(".dm-letter-compose textarea").fill("先只给我看第三处。");
  await page.getByRole("button", { name: "寄出" }).click();
  await expect(page.locator(".dm-letter-msg-user")).toHaveCount(2);
  await expect(page.locator(".dm-letter-msg-assistant").last()).toContainText("Echo:");
});

test("「不用了」不是蒸发：它当着面移到归档，还带得上架原因", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  const first = page.getByTestId("mb-row").first();
  const title = (await first.locator(".dm-mb-row-t").innerText()).slice(0, 10);
  await first.click();
  await page.getByRole("button", { name: "不用了" }).click();

  // 重拉后：未读少一封（2 → 1），归档多一封（1 → 2），且那封标着「不用了」
  await expect(page.getByTestId("mb-cat-unread")).toContainText("1");
  await page.getByTestId("mb-cat-archived").click();
  const rows = page.getByTestId("mb-row");
  await expect(rows).toHaveCount(2);
  const moved = rows.filter({ hasText: title });
  await expect(moved).toHaveCount(1);
  await expect(moved.locator(".dm-mb-row-chip")).toHaveText("不用了");
});

test("三栏各自能滚：右栏信纸超出屏幕也拉得动", async ({ page }) => {
  await page.getByTestId("mb-cat-unread").click();
  await page.getByTestId("mb-row").first().click();
  await expect(page.locator(".dm-mb-read")).toHaveCSS("overflow-y", "auto");
  await expect(page.locator(".dm-mb-list")).toHaveCSS("overflow-y", "auto");
});

test("三张截图：浅底 / 深底 / 窄屏", async ({ page }) => {
  await page.setViewportSize({ width: 1180, height: 760 });
  await page.getByTestId("mb-cat-unread").click();
  await page.getByTestId("mb-row").first().click();
  await page.getByRole("button", { name: "回信" }).click();
  await page.screenshot({ path: "test-results/mailbox-3col-light.png" });

  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.waitForTimeout(150);
  await page.screenshot({ path: "test-results/mailbox-3col-dark.png" });

  // 窄屏取 860：这是桌面壳真会被挤到的宽度（应用侧栏 ~250 + 信箱 ~610），能看清
  // 「塌成目录+列表 → 点开换正文」的切换。再窄就该用手机端 PWA 了，不在本页职责内。
  await page.setViewportSize({ width: 860, height: 900 });
  await page.waitForTimeout(150);
  await page.screenshot({ path: "test-results/mailbox-3col-narrow.png" });

  await page.getByRole("button", { name: "回列表" }).click();
  await page.waitForTimeout(120);
  await page.screenshot({ path: "test-results/mailbox-3col-narrow-list.png" });
});
