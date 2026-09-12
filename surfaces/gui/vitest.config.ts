import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Standalone test config (kept separate from vite.config.ts so the production `vite build` is
// untouched). Reused by later frontend phases — add new `*.test.tsx` files under src/.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    // Forces the `en` locale so official English-centric assertions resolve (see
    // src/test/setup.ts). Without it, components render raw i18n keys in tests.
    setupFiles: ["./src/test/setup.ts"],
    // By default vitest stubs every CSS import to "" — including `?raw`, which the skin
    // tests use to assert styles.css stays in sync with skinRegistry.ts. Whitelist just
    // that file so `import stylesCss from ".../styles.css?raw"` yields real text.
    css: { include: [/styles\.css/] },
  },
});
