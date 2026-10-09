import baseConfig from "./playwright.config";
import { defineConfig, devices } from "@playwright/test";

/**
 * 用**系统 Chrome**跑 e2e 的一份覆盖配置：`npx playwright test -c playwright.chrome.config.ts`。
 * 为什么要它：Playwright 自带的 chromium 要从 CDN 下载，国内网经常十几分钟不落地；
 * 装了 Chrome/Edge 的机器可以直接借来跑，用例与断言一字不改。
 * 只覆盖 projects（浏览器通道），webServer/testDir/reporter 全部沿用基配。
 */
export default defineConfig({
  ...baseConfig,
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
});
