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
  {
    id: "spring-grass",
    name: "春日草地",
    desc: "自然治愈系皮肤，草木绿主品牌色，搭配浅草色表面与清新柔和的视觉感受。",
    tag: "治愈",
    version: "1.0.0",
    brand: {
      accent: "#6A994E",          // 草木绿主品牌
      accentSoft: "#E8F0E0",      // 浅绿底
      accentDark: "#8AB86E",      // dark 模式提亮
      accentSoftDark: "#2A3A20",  // dark 模式深绿底
    },
    decor: {
      cream: "#F0F5E8",
      warm: "#A7C957",
      glow: "rgba(106, 153, 78, 0.30)",
      shadow: "rgba(106, 153, 78, 0.12)",
    },
    // 自然绿表面：整套界面从"冷灰工具感"转成"清新自然感"
    surface: {
      paper: "#F5F8F0",
      panel: "#FFFFFF",
      line: "#E0E8D4",
      ink: "#2D3428",
      muted: "#5A6352",
      solid: "#E8F0E0",
      onSolid: "#2D3428",
      glass: "rgba(255, 255, 255, 0.92)",
      paperDark: "#1A1F15",
      panelDark: "#242A1C",
      lineDark: "#353D28",
      inkDark: "#E0E8D4",
      mutedDark: "#9AA88A",
      solidDark: "#353D28",
      onSolidDark: "#E0E8D4",
      glassDark: "rgba(36, 42, 28, 0.92)",
    },
    preview: ["#E8F0E0", "#A7C957", "#6A994E", "#4A7A2E"],
  },
  {
    id: "autumn-maple",
    name: "秋日枫林",
    desc: "温暖秋日主题，橙红枫叶主品牌色，暖黄底色搭配散落枫叶，右下角趴着橘猫，治愈温暖。",
    tag: "季节",
    version: "1.0.0",
    brand: {
      accent: "#D97706",
      accentSoft: "#FED7AA",
      accentDark: "#F59E0B",
      accentSoftDark: "#431407",
    },
    decor: {
      cream: "#FFF7ED",
      warm: "#EA580C",
      glow: "rgba(217, 119, 6, 0.30)",
      shadow: "rgba(217, 119, 6, 0.12)",
    },
    surface: {
      paper: "#FFF7ED",
      panel: "#FFFFFF",
      line: "#FED7AA",
      ink: "#431407",
      muted: "#9A3412",
      solid: "#FFEDD5",
      onSolid: "#431407",
      glass: "rgba(255, 247, 237, 0.92)",
      paperDark: "#1C1006",
      panelDark: "#291809",
      lineDark: "#452A12",
      inkDark: "#FED7AA",
      mutedDark: "#B45309",
      solidDark: "#452A12",
      onSolidDark: "#FED7AA",
      glassDark: "rgba(41, 24, 9, 0.92)",
    },
    preview: ["#FFEDD5", "#F59E0B", "#D97706", "#9A3412"],
  },
  {
    id: "cherry-blossom",
    name: "樱花春日",
    desc: "浪漫樱花主题，粉色樱花主品牌色，淡粉底色搭配飘落樱花，右下角趴着白猫，温柔清新。",
    tag: "季节",
    version: "1.0.0",
    brand: {
      accent: "#EC4899",
      accentSoft: "#FCE7F3",
      accentDark: "#F472B6",
      accentSoftDark: "#500724",
    },
    decor: {
      cream: "#FDF2F8",
      warm: "#F472B6",
      glow: "rgba(236, 72, 153, 0.30)",
      shadow: "rgba(236, 72, 153, 0.12)",
    },
    surface: {
      paper: "#FDF2F8",
      panel: "#FFFFFF",
      line: "#FBCFE8",
      ink: "#500724",
      muted: "#BE185D",
      solid: "#FCE7F3",
      onSolid: "#500724",
      glass: "rgba(253, 242, 248, 0.92)",
      paperDark: "#1F0A14",
      panelDark: "#2E1020",
      lineDark: "#4A1932",
      inkDark: "#FBCFE8",
      mutedDark: "#EC4899",
      solidDark: "#4A1932",
      onSolidDark: "#FBCFE8",
      glassDark: "rgba(46, 16, 32, 0.92)",
    },
    preview: ["#FCE7F3", "#F472B6", "#EC4899", "#BE185D"],
  },
  {
    id: "summer-beach",
    name: "海边夏日",
    desc: "清爽夏日主题，天蓝主品牌色，沙滩蓝海底色搭配白云海浪，右下角趴着小狗，凉快清新。",
    tag: "季节",
    version: "1.0.0",
    brand: {
      accent: "#0EA5E9",
      accentSoft: "#E0F2FE",
      accentDark: "#38BDF8",
      accentSoftDark: "#082F49",
    },
    decor: {
      cream: "#F0F9FF",
      warm: "#38BDF8",
      glow: "rgba(14, 165, 233, 0.30)",
      shadow: "rgba(14, 165, 233, 0.12)",
    },
    surface: {
      paper: "#F0F9FF",
      panel: "#FFFFFF",
      line: "#BAE6FD",
      ink: "#082F49",
      muted: "#0369A1",
      solid: "#E0F2FE",
      onSolid: "#082F49",
      glass: "rgba(240, 249, 255, 0.92)",
      paperDark: "#04141F",
      panelDark: "#0A2538",
      lineDark: "#0C4A6E",
      inkDark: "#BAE6FD",
      mutedDark: "#0EA5E9",
      solidDark: "#0C4A6E",
      onSolidDark: "#BAE6FD",
      glassDark: "rgba(10, 37, 56, 0.92)",
    },
    preview: ["#E0F2FE", "#38BDF8", "#0EA5E9", "#0369A1"],
  },
  {
    id: "starry-night",
    name: "星空夜晚",
    desc: "静谧星空主题，靛蓝主品牌色，深蓝星空搭配月亮星星，右下角站着猫头鹰，安静治愈。",
    tag: "治愈",
    version: "1.0.0",
    brand: {
      accent: "#6366F1",
      accentSoft: "#E0E7FF",
      accentDark: "#818CF8",
      accentSoftDark: "#1E1B4B",
    },
    decor: {
      cream: "#EEF2FF",
      warm: "#818CF8",
      glow: "rgba(99, 102, 241, 0.30)",
      shadow: "rgba(99, 102, 241, 0.12)",
    },
    surface: {
      paper: "#EEF2FF",
      panel: "#FFFFFF",
      line: "#C7D2FE",
      ink: "#1E1B4B",
      muted: "#4338CA",
      solid: "#E0E7FF",
      onSolid: "#1E1B4B",
      glass: "rgba(238, 242, 255, 0.92)",
      paperDark: "#0F0E24",
      panelDark: "#1E1B4B",
      lineDark: "#312E81",
      inkDark: "#C7D2FE",
      mutedDark: "#6366F1",
      solidDark: "#312E81",
      onSolidDark: "#C7D2FE",
      glassDark: "rgba(30, 27, 75, 0.92)",
    },
    preview: ["#E0E7FF", "#818CF8", "#6366F1", "#4338CA"],
  },
  {
    id: "coffee-time",
    name: "咖啡时光",
    desc: "慵懒咖啡主题，深棕主品牌色，暖棕色调搭配咖啡杯书籍，右下角趴着小狐狸，温暖舒适。",
    tag: "治愈",
    version: "1.0.0",
    brand: {
      accent: "#92400E",
      accentSoft: "#FEF3C7",
      accentDark: "#B45309",
      accentSoftDark: "#451A03",
    },
    decor: {
      cream: "#FFFBEB",
      warm: "#B45309",
      glow: "rgba(146, 64, 14, 0.30)",
      shadow: "rgba(146, 64, 14, 0.12)",
    },
    surface: {
      paper: "#FFFBEB",
      panel: "#FFFFFF",
      line: "#FDE68A",
      ink: "#451A03",
      muted: "#92400E",
      solid: "#FEF3C7",
      onSolid: "#451A03",
      glass: "rgba(255, 251, 235, 0.92)",
      paperDark: "#1C1005",
      panelDark: "#291809",
      lineDark: "#452A12",
      inkDark: "#FDE68A",
      mutedDark: "#B45309",
      solidDark: "#452A12",
      onSolidDark: "#FDE68A",
      glassDark: "rgba(41, 24, 9, 0.92)",
    },
    preview: ["#FEF3C7", "#B45309", "#92400E", "#78350F"],
  },
  {
    id: "author-bookshelf",
    name: "作者书房",
    desc: "作者专属书房主题，暖木书架陈列着萧盛老师出版的全部著作（真实书封），暖光台灯与绿萝点缀，书香满怀。",
    tag: "专属",
    version: "1.0.0",
    brand: {
      accent: "#B0762F",          // 书卷暖棕主品牌
      accentSoft: "#F5E7CE",      // 米金浅底
      accentDark: "#D8A74E",      // dark 模式提亮
      accentSoftDark: "#3A2A14",  // dark 模式深棕底
    },
    decor: {
      cream: "#FBF6EC",
      warm: "#C4882E",
      glow: "rgba(176, 118, 47, 0.30)",
      shadow: "rgba(176, 118, 47, 0.12)",
    },
    // 暖木书房表面：书卷气暖纸感，对比度已核（ink/muted on paper 均 ≥ 4.5:1）
    surface: {
      paper: "#FBF6EC",
      panel: "#FFFDF7",
      line: "#EDE2CE",
      ink: "#3B3226",
      muted: "#7A6B56",
      solid: "#F3E9D7",
      onSolid: "#3B3226",
      glass: "rgba(255, 253, 247, 0.92)",
      paperDark: "#1A150F",
      panelDark: "#241D15",
      lineDark: "#352A1C",
      inkDark: "#F0E6D3",
      mutedDark: "#AFA08A",
      solidDark: "#352A1C",
      onSolidDark: "#F0E6D3",
      glassDark: "rgba(36, 29, 21, 0.92)",
    },
    preview: ["#F5E7CE", "#D8A74E", "#B0762F", "#6E441A"],
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
