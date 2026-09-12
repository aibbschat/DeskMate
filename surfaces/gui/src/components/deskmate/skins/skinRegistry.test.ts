/**
 * skinRegistry 单元测试：注册表/查找/品牌色变量映射。
 * 参照 src/components/garden/SeasonTheme.test.ts 范式。
 */
import { describe, expect, it } from "vitest";
// Vite 的 ?raw 导入：把 styles.css 当文本读进来做"CSS ↔ registry 同步"断言，
// 不依赖 node:fs（本项目未装 @types/node）。
import stylesCss from "../../../styles.css?raw";
import {
  DEFAULT_SKIN_ID,
  SKIN_REGISTRY,
  getSkinMeta,
  resolveSkinId,
  skinBrandVars,
} from "./skinRegistry";

describe("skinRegistry 注册表", () => {
  it("含 default + xiaoju-orange 至少两张", () => {
    const ids = SKIN_REGISTRY.map((s) => s.id);
    expect(ids).toContain("default");
    expect(ids).toContain("xiaoju-orange");
  });

  it("id 唯一", () => {
    const ids = SKIN_REGISTRY.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("每张皮肤都有完整字段（id/name/desc/tag/version/brand/decor/preview）", () => {
    for (const s of SKIN_REGISTRY) {
      expect(s.id).toBeTruthy();
      expect(s.name).toBeTruthy();
      expect(s.desc).toBeTruthy();
      expect(s.tag).toBeTruthy();
      expect(s.version).toBeTruthy();
      expect(s.brand.accent).toMatch(/^#[0-9a-fA-F]{3,8}$/);
      expect(s.brand.accentSoft).toMatch(/^#[0-9a-fA-F]{3,8}$/);
      expect(s.decor.cream).toBeTruthy();
      expect(s.decor.warm).toMatch(/^#[0-9a-fA-F]{3,8}$/);
      expect(s.decor.glow).toBeTruthy();
      expect(s.decor.shadow).toBeTruthy();
      expect(s.preview).toHaveLength(4);
    }
  });
});

describe("getSkinMeta / resolveSkinId", () => {
  it("getSkinMeta 命中/未命中", () => {
    expect(getSkinMeta("default")?.id).toBe("default");
    expect(getSkinMeta("xiaoju-orange")?.id).toBe("xiaoju-orange");
    expect(getSkinMeta("nonexistent")).toBeUndefined();
  });

  it("resolveSkinId 非法回落 default", () => {
    expect(resolveSkinId("default")).toBe("default");
    expect(resolveSkinId("xiaoju-orange")).toBe("xiaoju-orange");
    expect(resolveSkinId("nonexistent")).toBe(DEFAULT_SKIN_ID);
    expect(resolveSkinId(null)).toBe(DEFAULT_SKIN_ID);
    expect(resolveSkinId(undefined)).toBe(DEFAULT_SKIN_ID);
    expect(resolveSkinId("")).toBe(DEFAULT_SKIN_ID);
  });
});

describe("skinBrandVars 品牌色变量映射", () => {
  it("light 主题：返回 --accent / --accent-soft / --skin-* 5 个变量", () => {
    const orange = getSkinMeta("xiaoju-orange")!;
    const vars = skinBrandVars(orange, "light") as Record<string, string>;
    expect(vars["--accent"]).toBe("#F26B1F");
    expect(vars["--accent-soft"]).toBe("#FFE7CC");
    expect(vars["--skin-cream"]).toBe(orange.decor.cream);
    expect(vars["--skin-warm"]).toBe(orange.decor.warm);
    expect(vars["--skin-glow"]).toBe(orange.decor.glow);
    expect(vars["--skin-shadow"]).toBe(orange.decor.shadow);
  });

  it("dark 主题：xiaoju-orange 有专属提亮色（accentDark / accentSoftDark）", () => {
    const orange = getSkinMeta("xiaoju-orange")!;
    const vars = skinBrandVars(orange, "dark") as Record<string, string>;
    expect(vars["--accent"]).toBe(orange.brand.accentDark);
    expect(vars["--accent-soft"]).toBe(orange.brand.accentSoftDark);
  });

  it("dark 主题：缺省 brandDark 时回落到 light accent", () => {
    // 构造一个无 dark 字段的 skin
    const fake = {
      id: "fake",
      name: "",
      desc: "",
      tag: "",
      version: "1.0.0",
      brand: { accent: "#123456", accentSoft: "#abcdef" },
      decor: { cream: "", warm: "", glow: "", shadow: "" },
      preview: ["", "", "", ""] as [string, string, string, string],
    };
    const vars = skinBrandVars(fake, "dark") as Record<string, string>;
    expect(vars["--accent"]).toBe("#123456");
    expect(vars["--accent-soft"]).toBe("#abcdef");
  });
});

/* ------------------------------------------------------------------ *
 * 表面色护栏：皮肤一旦改了 --paper/--ink 这层，就必须守住可读性，
 * 且 CSS 与 registry 两处必须同步（否则预览卡片会骗人）。
 * ------------------------------------------------------------------ */

function _srgb(c: number): number {
  const v = c / 255;
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

/** WCAG 相对亮度。 */
function _lum(hex: string): number {
  let h = hex.replace("#", "").trim();
  if (h.length === 3) h = h.split("").map((x) => x + x).join("");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b);
}

/** WCAG 对比度（1 ~ 21）。 */
function _contrast(a: string, b: string): number {
  const la = _lum(a);
  const lb = _lum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

describe("表面色可读性（WCAG AA 护栏）", () => {
  const skinned = SKIN_REGISTRY.filter((s) => s.surface);

  it("有表面色的皮肤：亮/暗下 ink 对 paper ≥ 4.5:1", () => {
    expect(skinned.length).toBeGreaterThan(0); // 保证这条护栏真的在跑
    for (const s of skinned) {
      const sf = s.surface!;
      if (sf.paper && sf.ink) {
        expect(_contrast(sf.ink, sf.paper), `${s.id} light ink/paper`).toBeGreaterThanOrEqual(4.5);
      }
      if (sf.paperDark && sf.inkDark) {
        expect(_contrast(sf.inkDark, sf.paperDark), `${s.id} dark ink/paper`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("有表面色的皮肤：亮/暗下 muted（次要文字）对 paper ≥ 4.5:1", () => {
    for (const s of skinned) {
      const sf = s.surface!;
      if (sf.paper && sf.muted) {
        expect(_contrast(sf.muted, sf.paper), `${s.id} light muted/paper`).toBeGreaterThanOrEqual(4.5);
      }
      if (sf.paperDark && sf.mutedDark) {
        expect(_contrast(sf.mutedDark, sf.paperDark), `${s.id} dark muted/paper`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it("填充块 solid / onSolid 自身成对可读 ≥ 4.5:1", () => {
    for (const s of skinned) {
      const sf = s.surface!;
      if (sf.solid && sf.onSolid) {
        expect(_contrast(sf.onSolid, sf.solid), `${s.id} light solid`).toBeGreaterThanOrEqual(4.5);
      }
      if (sf.solidDark && sf.onSolidDark) {
        expect(_contrast(sf.onSolidDark, sf.solidDark), `${s.id} dark solid`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe("CSS ↔ registry 同步护栏", () => {
  /** 取 styles.css 中某选择器的声明块文本。 */
  function cssBlock(selector: string): string {
    const i = stylesCss.indexOf(selector);
    if (i < 0) return "";
    const start = stylesCss.indexOf("{", i);
    const end = stylesCss.indexOf("}", start);
    return start < 0 || end < 0 ? "" : stylesCss.slice(start + 1, end);
  }

  function cssVar(block: string, name: string): string | undefined {
    const m = block.match(new RegExp(`(?:^|[;\\s])${name}\\s*:\\s*([^;]+)`));
    return m ? m[1].trim() : undefined;
  }

  it("registry 里声明的表面色必须在 styles.css 同 id 选择器里出现（防两处漂移）", () => {
    for (const s of SKIN_REGISTRY) {
      if (!s.surface) continue;
      const light = cssBlock(`:root[data-theme="light"][data-skin-pre="${s.id}"]`);
      const dark = cssBlock(`html[data-theme="dark"][data-skin-pre="${s.id}"]`);
      const sf = s.surface;
      if (sf.paper) expect(cssVar(light, "--paper"), `${s.id} --paper`).toBe(sf.paper);
      if (sf.panel) expect(cssVar(light, "--panel"), `${s.id} --panel`).toBe(sf.panel);
      if (sf.ink) expect(cssVar(light, "--ink"), `${s.id} --ink`).toBe(sf.ink);
      if (sf.paperDark) expect(cssVar(dark, "--paper"), `${s.id} dark --paper`).toBe(sf.paperDark);
      if (sf.panelDark) expect(cssVar(dark, "--panel"), `${s.id} dark --panel`).toBe(sf.panelDark);
      if (sf.inkDark) expect(cssVar(dark, "--ink"), `${s.id} dark --ink`).toBe(sf.inkDark);
    }
  });

  it("表面色选择器必须带 data-theme 限定（否则 dark 下会被浅色洗白）", () => {
    for (const s of SKIN_REGISTRY) {
      if (!s.surface) continue;
      // 禁止出现「不带主题限定的 --paper 覆盖」
      const bare = `:root[data-skin-pre="${s.id}"]`;
      const bareBlock = cssBlock(bare);
      expect(bareBlock.includes("--paper"), `${s.id} 不应在无限定选择器里覆盖 --paper`).toBe(false);
    }
  });
});

/* 注：v1.9.8 装回的 hero 字段护栏（SkinHero 视觉元素护栏）已随 v1.9.9 撤回同步删除。
   视觉元素字段命名规范已写进 MEMORY（hero / decorativeStamp / sceneIllustration / bannerArt
   等显式自描述名；禁用通用名 mascot/image/picture/icon）。下次再装 hero 时把这些护栏
   按以下三块重新加回：
     ① 字段名白名单（已声明 + 显式自描述扩展位）
     ② hero 必为纯景物（alt 不含人物关键词 + src 是图片扩展名）
     ③ 必填校验（src 是 Vite ?url 产出的 / 开头路径；alt 必填）*/
