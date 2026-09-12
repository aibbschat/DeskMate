/**
 * skinRegistry — 小桔皮肤包（产品级"皮肤地基"）。
 *
 * 设计契约（与 src/components/deskmate/emotes/emotePack.ts 同构）：
 *   ① 数据驱动：皮肤 = 注册表里一条记录；新增皮肤 = 加一条 + 写对应 CSS 选择器。
 *      不想改任何 TS 代码的话，CSS 选择器名按 `data-skin-pre` 命名即可被自动命中。
 *   ② 状态与渲染分离：渲染层只调 useActiveSkin() / setActiveSkin()，不直接读 localStorage。
 *   ③ 视觉分层：皮肤覆盖"品牌色（--accent 系）+ 装饰变量（--skin-*）"，并**可选**覆盖
 *      表面色（--paper/--panel/--line/--ink/--muted/--solid/--glass，见 SkinSurface）。
 *      只换 accent 是不够的——本应用 accent 仅作点缀，大面积视觉由中性表面色撑着。
 *      表面色省略即继承出厂中性色，故"默认皮肤"与没有这套机制时逐像素一致。
 *      改暖/改冷必须守住 WCAG AA（正文 ≥ 4.5:1）；明暗仍由 data-theme 控制，与之正交。
 *   ④ 联动：皮肤可指定 emote pack（切肤自动切表情包）。
 *   ⑤ 视觉元素扩展：用**显式自描述**命名的可选字段（hero / decorativeStamp /
 *      sceneIllustration / bannerArt / cornerOrnament 等）。**不**用通用名
 *      （image / mascot / picture / icon）——避免混淆"元素"和"实体"（09-08 教训：
 *      mascot 这个名字让 AI 一度想拿去换桌宠）。当前实现：`hero` —— 主聊天区
 *      纯景物大背景，仿 WorkBuddy 范式但只放景物不放人。
 *
 * 配套 CSS：src/styles.css 在 :root 后用
 *   :root[data-skin-pre="<id>"]  { --accent; --accent-soft; --skin-* 变量 }
 *   html[data-theme="dark"][data-skin-pre="<id>"]  { dark 下的品牌色覆盖 }
 * 声明同名选择器即可；TS 这边只管数据，不管样式——单一职责。
 *
 * 首屏防闪：index.html 内的 pre-paint 脚本读 localStorage 直接设 data-skin-pre，
 * 等 styles.css 加载完第一帧就是对的。
 */

import type { CSSProperties } from "react";

/** 品牌色（影响整站按钮/高亮/链接色，已被 Tailwind 桥接）。 */
export interface SkinBrand {
  /** 亮色主题下的主品牌色（如小桔橙光的 #F26B1F） */
  accent: string;
  /** 亮色主题下的浅色背景（按钮 hover/选中/标签底） */
  accentSoft: string;
  /** 暗色主题下的主品牌色（缺省 = accent，由各 skin 按需调亮） */
  accentDark?: string;
  /** 暗色主题下的浅色背景（缺省 = accentSoft） */
  accentSoftDark?: string;
}

/** 皮肤专属装饰色（只给小桔相关组件用，不动整站功能色）。 */
export interface SkinDecor {
  /** 主底色（卡片底/暖色调底） */
  cream: string;
  /** 暖色点缀（次按钮/装饰） */
  warm: string;
  /** 发光/聚焦色（focus ring / 高亮） */
  glow: string;
  /** 阴影色（rgba 字符串） */
  shadow: string;
}

/**
 * 表面色（可选）——决定"换肤有没有感觉"的那一层。
 *
 * 为什么必须有：本应用是中性极简 UI，`--accent` 只承担点缀（hover 描边 / 小图标 /
 * 链接色），大面积视觉由 --paper/--panel/--line/--ink 这些中性色撑着。只换 accent
 * 的话，用户感知约等于"一个按钮变色"。皮肤要成立，就得允许整块表面跟着换气质。
 *
 * 全部字段可选：省略 = 继承 :root（也就是 OpenWorker 出厂中性色），
 * 这样"默认皮肤"与加这套机制之前**逐像素一致**，不引入回归。
 *
 * ⚠ 同步约定：这里的值必须与 styles.css 的
 *   `:root[data-skin-pre="<id>"]` / `html[data-theme="dark"][data-skin-pre="<id>"]`
 * 保持一致。CSS 是渲染真相源，这里的字段供选择器预览与未来运行时注入使用。
 */
export interface SkinSurface {
  /** 亮色：应用底（窗口背景） */
  paper?: string;
  /** 亮色：面板/卡片底 */
  panel?: string;
  /** 亮色：分隔线 */
  line?: string;
  /** 亮色：主文字 */
  ink?: string;
  /** 亮色：次要文字（改暖色时务必守住 4.5:1 对比度） */
  muted?: string;
  /** 亮色：填充块（气泡/胶囊底） */
  solid?: string;
  /** 亮色：填充块上的文字 */
  onSolid?: string;
  /** 亮色：毛玻璃（顶栏/浮层），rgba 字符串 */
  glass?: string;
  /** 暗色版：同上，缺省 = 继承 html[data-theme="dark"] 的中性暗色 */
  paperDark?: string;
  panelDark?: string;
  lineDark?: string;
  inkDark?: string;
  mutedDark?: string;
  solidDark?: string;
  onSolidDark?: string;
  glassDark?: string;
}

/** 一张皮肤卡的元数据。 */
export interface SkinMeta {
  id: string;
  name: string;
  desc: string;
  tag: string;
  version: string;
  /** 品牌色 */
  brand: SkinBrand;
  /** 装饰色 */
  decor: SkinDecor;
  /** 表面色（可选）：让整块界面跟着换气质；省略 = 出厂中性色。 */
  surface?: SkinSurface;
  /** 联动 emote pack：切到本皮肤时自动 setActivePack（皮肤 store 调）。 */
  emotePack?: string;
  /** 预览色（4 色，由浅到深）—— 卡片左侧色板用 */
  preview: [string, string, string, string];
}

/** 注册表：所有可选皮肤。加新皮肤 = 追加一条 + 在 styles.css 加同名选择器。 */
export const SKIN_REGISTRY: readonly SkinMeta[] = [
  {
    id: "default",
    name: "经典桔",
    desc: "OpenWorker 默认皮肤，钴蓝品牌色 + 中性灰，工具感强、适合长时间阅读与写作。",
    tag: "默认",
    version: "1.0.0",
    brand: {
      accent: "#2563eb",          // = styles.css :root --accent（cobalt）
      accentSoft: "#e9f0fd",      // = styles.css :root --accent-soft
      accentDark: "#4c8dff",
      accentSoftDark: "#1c2a44",
    },
    decor: {
      cream: "#f5f6f7",
      warm: "#FFC24B",
      glow: "rgba(37, 99, 235, 0.35)",
      shadow: "rgba(232, 85, 26, 0.10)", // 与小桔色系保持一致（不破主色感）
    },
    preview: ["#E9F0FD", "#A8C2F8", "#4C8DFF", "#2563EB"],
  },
  {
    id: "xiaoju-orange",
    name: "小桔橙光",
    desc: "小桔专属皮肤，桔皮帽小女孩主题，更饱满的橙黄主品牌色，搭配暖底卡片与金色点缀。",
    tag: "专属",
    version: "1.0.0",
    brand: {
      accent: "#F26B1F",          // 暖橙主品牌
      accentSoft: "#FFE7CC",      // 暖橙浅底
      accentDark: "#FFA94D",      // dark 模式提亮
      accentSoftDark: "#3a2410",  // dark 暖橙深底
    },
    decor: {
      cream: "#FFF8F0",
      warm: "#F0B429",
      glow: "rgba(242, 107, 31, 0.35)",
      shadow: "rgba(242, 107, 31, 0.12)",
    },
    // 暖色表面：整套界面从"冷灰工具感"转成"暖纸写作感"。
    // 对比度已核：ink/muted on paper 均 ≥ 4.5:1（WCAG AA），不牺牲可读性。
    surface: {
      paper: "#FBF6EF",
      panel: "#FFFCF7",
      line: "#F1E4D4",
      ink: "#2A211B",
      muted: "#7A6A5C",
      solid: "#F6E9D9",
      onSolid: "#33261B",
      glass: "rgba(255, 252, 247, 0.92)",
      paperDark: "#17130F",
      panelDark: "#201A15",
      lineDark: "#33281F",
      inkDark: "#F2E9DF",
      mutedDark: "#A79483",
      solidDark: "#33281F",
      onSolidDark: "#F2E9DF",
      glassDark: "rgba(32, 26, 21, 0.92)",
    },
    emotePack: "xiaoju-orange",   // 联动表情包
    preview: ["#FFE7CC", "#FFA94D", "#F26B1F", "#C43E0A"],
  },
] as const;

/** 默认皮肤 id。 */
export const DEFAULT_SKIN_ID = "default";

/** 快速查找。 */
export function getSkinMeta(id: string): SkinMeta | undefined {
  return SKIN_REGISTRY.find((s) => s.id === id);
}

/** 校验合法性，非法回落默认。 */
export function resolveSkinId(id: string | null | undefined): string {
  if (id && getSkinMeta(id)) return id;
  return DEFAULT_SKIN_ID;
}

/** 把品牌色 + 装饰色打包成内联 CSSProperties（用于 inline style 注入或 fallback 路径）。 */
export function skinBrandVars(skin: SkinMeta, theme: "light" | "dark" = "light"): CSSProperties {
  const isDark = theme === "dark";
  const accent = isDark ? (skin.brand.accentDark ?? skin.brand.accent) : skin.brand.accent;
  const accentSoft = isDark
    ? (skin.brand.accentSoftDark ?? skin.brand.accentSoft)
    : skin.brand.accentSoft;
  return {
    "--accent": accent,
    "--accent-soft": accentSoft,
    "--skin-cream": skin.decor.cream,
    "--skin-warm": skin.decor.warm,
    "--skin-glow": skin.decor.glow,
    "--skin-shadow": skin.decor.shadow,
  } as CSSProperties;
}
