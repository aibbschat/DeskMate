/**
 * 坏类名护栏（2026-11-17）：方括号掉了的 Tailwind 类静默失效，编译、测试、类型检查
 * 全都不报错，只是样式回落——最难发现的一种 bug。
 *
 * 实锤来源：`text-x]`（原意 `text-[11.5px]` 一类）在 SettingsView 与 LlmChannelsCard
 * 里共 12 处存活了许久（`94229c67` / `dda3563b` 引入），期间没人发现：说明文字反而
 * 比卡片标题还大，因为那个类压根没生效、字号一直在继承父级。
 *
 * 判据很窄，只盯「工具前缀 + 值 + ] 但值前面没有 [」这一种形态：
 * - `text-[11.5px]` 不命中（有 `[`）
 * - `text-x]` 命中（`text-` 后面直接 `x]`）
 * 别扩大成通用 CSS 检查，否则会把 JSX 表达式里的数组索引、正则全扫进来。
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, ".");

/** 会被误写的工具前缀（方括号任意值最常用的那批）。 */
const PREFIXES = [
  "text",
  "bg",
  "border",
  "ring",
  "outline",
  "p",
  "px",
  "py",
  "pt",
  "pb",
  "pl",
  "pr",
  "m",
  "mx",
  "my",
  "mt",
  "mb",
  "ml",
  "mr",
  "w",
  "h",
  "min-w",
  "min-h",
  "max-w",
  "max-h",
  "gap",
  "rounded",
  "leading",
  "tracking",
  "opacity",
  "shadow",
  "z",
  "top",
  "left",
  "right",
  "bottom",
  "grid-cols",
  "flex-basis",
];

/** `text-x]` / `bg-paper]`：前缀后面是「值 + ]」，且值前没有 `[`。 */
const BROKEN = new RegExp(
  `(?:^|[\\s"'\`{])((?:${PREFIXES.join("|")})-[A-Za-z0-9._%/-]+\\])`,
  "g",
);

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === "node_modules") continue;
      walk(full, out);
    } else if (/\.(tsx?|css)$/.test(name) && !name.endsWith(".test.ts") && !name.endsWith(".test.tsx")) {
      out.push(full);
    }
  }
  return out;
}

describe("坏类名护栏", () => {
  it("源码里不存在「方括号掉了」的 Tailwind 类", () => {
    const hits: string[] = [];
    for (const file of walk(SRC)) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(BROKEN)) {
        const line = text.slice(0, m.index ?? 0).split("\n").length;
        hits.push(`${relative(SRC, file)}:${line} → ${m[1]}`);
      }
    }
    expect(hits).toEqual([]);
  });

  it("护栏自身认得出实锤过的形态（防它变成永真）", () => {
    // 若正则写坏，上一条会永远绿；这两条钉住判据本身。
    const scan = (s: string) => [...s.matchAll(BROKEN)].map((m) => m[1]);
    expect(scan('className="text-x] text-muted"')).toEqual(["text-x]"]);
    expect(scan('className="text-[11.5px] text-muted"')).toEqual([]);
    expect(scan('className="bg-paper]"')).toEqual(["bg-paper]"]);
  });
});
