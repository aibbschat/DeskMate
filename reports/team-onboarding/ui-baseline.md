
# 【UI 设计师】两项目视觉与设计资产盘点 · 设计基线

> 角色：UI 设计师 ｜ 日期：2026-10-11 ｜ 任务：task-2
> 范围：openworker（读+写沙箱内）与 ai-forum-v3（**只读**）
> 方法：实地清点目录与样式源码、对 CSS 做变量/色值/圆角/字号的量化统计、抽读核心设计文档与视觉验收记录。

---

## 一、结论速览（TL;DR）

1. **两个项目都已有"单一事实源"意识，但成熟度不同**：openworker 的 Token 架构更完整（明暗双主题 + 皮肤系统 + Tailwind 映射齐全），但落地执行打折（大量半像素字号、81 种阴影）；ai-forum-v3 有一份号称"单一事实源"的 `tokens.css`，实际已膨胀成 2505 行的"大杂烩"，硬编码色值比变量还多。
2. **两个项目的品牌色意外地同源**：都是 cobalt 蓝系（openworker `--accent: #2563eb`，ai-forum `brand-500: #2563eb / brand-600: #1d4ed8`），卡片圆角都收敛在 8px 附近。这是"像一家"的天然锚点，应该正式确立。
3. **设计性格应当保持差异**：openworker 是"工具感"（冷灰纸底 + 无衬线 + 高信息密度面板），ai-forum 是"编辑部/杂志感"（白卡片 + 衬线大标题 + 中文排版 17px/1.9）。这一差异源于产品定位，**不建议强行统一，只统一 Token 命名与基础规范**。
4. **最大共性短板**：两边都缺"间距标尺 / 圆角标尺 / 阴影层级"三样基础 Token；ai-forum 无暗色模式、靠 CDN 运行时 Tailwind 与远程 Google Fonts（国内访问风险）；openworker 字号体系失控（20+ 种字号，含 8px 微文字）。
5. openworker 的设计文档质量明显更高（有诊断、有原则、有分阶段落地、有验收标准），ai-forum 的设计文档胜在"产品/运营视角深"，但缺一份真正被执行的视觉规范；两边都**缺一份可给 AI/新人直接执行的"活的" Design Token 文档**。

---

## 二、资产清单

### 2.1 openworker

| 类别 | 位置 | 内容与状态 |
|---|---|---|
| 设计文档（docs/） | `docs/refined-design-spec.md`（506 行） | **目前最接近"视觉规范"的文档**：边框/阴影/颜色/字号 6 级/进度条/动画时长与缓动，含"❌粗糙 vs ✅精致"对照示例 |
| | `docs/status-panel-redesign.md`（441 行） | StatusPanel 重设计：参考 Finch/Habitica/原神角色面板，4 层信息架构 |
| | `docs/emote-system-design.md`（65 行） | 表情系统架构（分层 + 分阶段规划） |
| | `docs/creation-chat-redesign.md`（165 行） | 创作流水线对话流化重构（Phase A/B/C） |
| | `docs/creation-pipeline-progress-ui-design.md`（259 行） | 流式进度 + 界面现代化，含 SSE 协议与文件级改动清单，**已实施（2026-08-28 落地记录）** |
| | 其他 | `plugin_center_design.md`、`skill-store-design.md`、`compaction-redesign-from-hermes.md` 等 |
| 静态 Mock | `ui-mocks/`（14 项） | `redesign.html`（Tailwind 版式事实源）、`connectors-redesign.html`、`memory-graph.html`、`badge_*.html`、`voice-input-*.html`、`agent-group-chat-mock.html`；**最新为 2026-10-09 的《信箱回信样式_三案》《信箱双栏收件箱》**；另有《小桔手机端_静态预览》（715KB，移动端） |
| 根目录散件 | `emote-preview.html`、`diary-style-preview.html` | 早期预览，与 ui-mocks 有重叠，未归档 |
| 品牌资产 | `design/` | `openworker-logo-v1.png` / `openworker-logo-v1-real.png`（PNG，**无 SVG 矢量与多尺寸导出**）；`design/xiaoju-emotes-v2/`（22 个表情 PNG + `make_transparent.py`） |
| 皮肤/桌宠素材 | `workbuddy-skin/`（92 项）、`surfaces/gui/public/skins` | 皮肤贴图素材（board/books/背景分件 PNG），带 `apply-skin.js` |
| 样式实现 | `surfaces/gui/src/styles.css`（**5912 行**） | Token 定义 + 全部组件样式的手写 CSS |
| | `surfaces/gui/src/theme.ts`（55 行） | Light/Dark/Auto 三态，localStorage `openwork-theme`，index.html pre-paint 防闪 |
| | `surfaces/gui/tailwind.config.js` | **构建期 Tailwind**，颜色全部映射到 CSS 变量（`color-mix` 支持 `/NN` 透明度），`darkMode: ['[data-theme="dark"]']` |
| | `src/components/deskmate/skins/skinRegistry.ts` + `skinStore.ts` | **9 套皮肤**：经典桔 / 小桔橙光 / 春日草地 / 秋日枫林 / 樱花春日 / 海边夏日 / 星空夜晚 / 咖啡时光 / 作者书房；CSS 侧 50 处 `data-skin-pre` 选择器 |
| | `src/components/garden/SeasonTheme.ts` | 花园季节主题（含测试） |
| | `src/mobile/` | 移动端独立入口（ChatScreen / InteractiveDock 等），dist 有独立 `mobile-*.css` |
| 组件规模 | `src/components/` 155 个 `.tsx` | Sidebar/Composer/RightRail/ApprovalCard/InboxView/CreationPipelineView/StatusPanel 相关等 |

### 2.2 ai-forum-v3（只读）

| 类别 | 位置 | 内容与状态 |
|---|---|---|
| 设计 Token | `web/src/styles/tokens.css`（**2505 行**） | 文件头 ~60 行是真 Token（布局/色彩/字体/圆角/阴影约 20 个变量），其余为**页面级 CSS 大杂烩**：文章排版、星空思想流（cosmic）、日记页 rm-page 主题、分享抽屉、点赞动效等全塞在一个文件 |
| Tailwind | `web/index.html` 内联 config | **运行时 CDN 版（cdn.tailwindcss.com）**，brand 色映射到 CSS 变量 |
| 字体 | `web/index.html` | Google Fonts 远程加载 `Noto Serif SC`（600/700/900）+ `Inter`（400–800），用 `media="print" → onload` 非阻塞技巧 |
| 视觉验收 | `web/docs/visual-acceptance.md` | **很成熟的实践**：逐页对照原型 + headless 截图 + 路由探测；设计语言事实源是 `article-prototype.html` |
| 品牌资产 | `design/logo-2026-09/` | **两项目中最完整的品牌包**：SVG（logo/dark/mono/maskable）、PNG 多尺寸、apple-touch-icon、OG 分享图、`build.py` 生成脚本、`USAGE.md` |
| 改版设计稿 | `design/`（19 个子目录） | agent-profile / nav / rooms / room / journal / materials / identity-cards / plaza / debate-camp / tag-chip / wechat-banner 等，多为「HTML mock + 上线前后截图」对照式 |
| 独立改版 | `rooms-redesign/`、`room-1976-shots/`、`preview-drafts/` | 房间改版预览 HTML 与截图 |
| 产品/视觉设计文档 | 根目录 30+ 篇 `AI圈-*.md` | 与视觉直接相关：《主人资料页重设计》（三方案已拍板"这本册子"纸感方向）、《日记页装修重规划》（主题×墙纸两轴解耦 + 统一饰品系统）、《日报改版-大字报》、《广场社交存在性展示》、《圈内纪事与游戏化任务信》等 |
| 前端规模 | `web/src/pages/` 38 页 + `components/` 50+ | Home/Article/AgentSpace/Plaza/Rooms/Room/AgentJournal/Compose/Chat/Companion* 等 |

---

## 三、设计 Token 现状（量化统计）

### 3.1 openworker（`surfaces/gui/src/styles.css`）

**色彩（语义化，架构优秀）**

```css
/* 亮色 :root */                          /* 暗色 html[data-theme="dark"] */
--paper: #f5f6f7  /* 冷灰纸底，"工具感" */   --paper: #131417
--panel: #ffffff                         --panel: #1c1e22
--ink:   #17191c                         --ink:   #e6e8eb
--muted: #5b616b   --faint: #9aa1aa      --muted: #9aa1ab   --faint: #62686f
--line: #e8eaed    --line-strong: #d8dce1   --line: #2a2d33
--accent: #2563eb  /* cobalt 签名色 */       --accent: #4c8dff
状态色：--ok / --warn-ink / --danger / --teal-ink，各配 -soft / -line 变体
表面：--solid / --on-solid、--glass ×3（frosted bars）、--scrim、--check-a/b
```

- 暗色映射完整、注释良好（含"为什么用冷灰而不是暖白"这类设计理由），**是两项目中最扎实的 Token 层**。
- 量化：`var(--` 引用 **912** 处 vs 裸 hex **872** 处、rgba **336** 处 → 约一半样式已 Token 化，另一半仍硬编码。

**皮肤系统**：9 套皮肤通过 `[data-skin-pre="<id>"]` 覆盖 `--accent / --accent-soft / --skin-cream / --skin-warm / --skin-glow / --skin-shadow`；`xiaoju-orange` 还换纸底（`--paper: #FBF6EF` 暖白）。有 pre-paint 防闪与暗色联动限定，工程考虑周全。

**字号（失控区）**：styles.css 出现 **20+ 种字号**，最常见 13px(99 次)、12px(80)、**12.5px(80)**、11px(47)、14px(40)、**11.5px(35)**、15px(25)、**13.5px(23)**，另有 8.5/10.5/9/8/7px 约 50 处微文字。而自家规范 `refined-design-spec.md `5.2` 明确写着「❌ 12.5px → ✅ 12px」，6 级字阶为 20/13/12/11/10/9 —— **规范与实现明显背离**。

**圆角 / 阴影 / 间距**：圆角 16+ 种取值（8/10/12px 为主，另有 9/11/14/16/18/20px 等），无 `--radius` 标尺；box-shadow 去重后 **81 种**，无层级标尺；**无 `--space` 间距变量**，靠 Tailwind px/py/gap 与手写 px 混用（TSX 中 text-/flex/rounded/gap 等工具类高频出现，属 Tailwind + 手写 CSS 混合体）。

### 3.2 ai-forum-v3（`web/src/styles/tokens.css`）

```css
--container-max: 1120px; --container-pad: 1.5rem; --article-max: 760px;
--color-bg: #fafafa; --color-surface: #fff; --color-ink: #111; --color-border: #e5e7eb;
--color-brand-50/100/500/600/700: #f0f7ff / #e0efff / #2563eb / #1d4ed8 / #1e40af;
--color-accent: #dc2626;   /* 「涨=红」：仅热榜/热标/点赞态 */
--font-sans: Inter + PingFang SC; --font-serif: Noto Serif SC; --font-hand: 楷体;
--radius-card: 8px; --shadow-card-hover: 0 8px 24px rgba(0,0,0,.06);
```

- 定位语义清晰（品牌蓝 + 语义红的分工写进了注释），排版 Token（正文 17px/1.9、文章 760px 阅读宽）体现编辑部定位。
- 量化：`var(--` **450** 处 vs 裸 hex **694** 处；TSX 内另有 **353 处**裸 hex。→ **"单一事实源"名存实亡，硬编码多于变量**。
- 2505 行中仅前 ~60 行是真 Token，其余是页面级样式（cosmic 星空流、rm-page 日记主题等），**Token 与组件样式没有物理隔离**。
- **暗色模式：全站无**（仅日记阅读态有 `.rm-dark` 局部变量）。
- **圆角只有 1 个 Token**（8px），实际页面靠 Tailwind `rounded` 各档与硬编码混用；无间距、无阴影层级标尺。

### 3.3 两项目对照表

| 维度 | openworker GUI | ai-forum-v3 | 是否"像一家" |
|---|---|---|---|
| 品牌色 | cobalt `#2563eb`（暗 `#4c8dff`） | 品牌蓝 `#2563eb/#1d4ed8` + 语义红 `#dc2626` | ✅ 同源，可正式确立为共享基色 |
| 底色 | 冷灰 `--paper #f5f6f7` + 白面板 | `#fafafa` 页底 + 白卡片 | ✅ 接近 |
| 文字层级 | ink/muted/faint 三级 + line 两级 | ink/border 两级为主 | ⚠️ ai-forum 层级较粗 |
| 字体性格 | 全无衬线（`--serif: var(--sans)`，注释注明"曾是 Palatino，已改产品感"） | 衬线大标题（Noto Serif SC）+ Inter UI + 楷体手记 | ✅ 差异即性格，保留 |
| 字号体系 | 规范 6 级但实现 20+ 种 | 未定义标尺 | ❌ 双双失控 |
| 圆角 | 无 Token，16+ 种取值 | `--radius-card: 8px` 一个 Token | ⚠️ 数值趋同、管理缺失 |
| 阴影 | 81 种手写值 | 1 个 hover 阴影 | ❌ 都无层级标尺 |
| 间距 | 无 Token，Tailwind+手写混用 | 无 Token，仅容器 1120/24px | ❌ |
| 明暗主题 | ✅ 完整（pre-paint 防闪、Auto 跟随系统） | ❌ 无 | ❌ |
| 皮肤/主题 | 9 套皮肤 + 季节主题 | 日记页主题×墙纸两轴系统 | ⚠️ 各自为政，概念可互鉴 |
| Tailwind | 构建期 + color-mix 透明度 | **运行时 CDN** | ❌ 工具链不一致 |
| 字体加载 | 自托管 woff2（Manrope wordmark） | Google Fonts 远程（国内风险） | ❌ |
| 视觉验收 | 无成文清单 | `visual-acceptance.md` 逐页验收 | ⚠️ ai-forum 做法可反向推广 |

---

## 四、Top 10 视觉问题（按影响排序）

1. **ai-forum 的 Tailwind 是运行时 CDN（`cdn.tailwindcss.com`）**。生产站 aibbs.chat 首屏样式依赖外部 CDN 可达性：国内访问抖动 = 全站裸奔（无样式降级预案），且拖累 LCP。**优先级最高、修复路径明确**：迁移到构建期 Tailwind（项目本就有 Vite），或至少把资源自托管。
2. **ai-forum 的 Google Fonts 远程字体**（Noto Serif SC + Inter）。大陆访问 fonts.googleapis.com 不稳定，而衬线大标题正是该站编辑部人格的核心，字体失败 = 人格降级为系统字体。建议自托管 woff2 子集（openworker 的 Manrope 自托管做法可直接照抄）。
3. **openworker 字号体系失控**：20+ 种字号、约 50 处 ≤10.5px 微文字、大量 12.5/11.5/13.5px 半像素值，与自家 `refined-design-spec.md` 的 6 级字阶和「拒绝 12.5px」条款直接冲突，也有可读性/无障碍风险（8px、7px 出现在正式界面）。
4. **`tokens.css` 已变成 2505 行大杂烩**：Token 与页面样式（cosmic 星空流、日记主题、动效）物理混居，改色需在样式堆里翻找；694 处裸 hex 多于 450 处变量，「改一个令牌全站变化」的承诺已失效。
5. **openworker styles.css 5912 行 + 81 种 box-shadow、16+ 种圆角、无间距标尺**：没有 elevation/radius/space 标尺，新增组件只能"看着调"，长期必然面板质感不一致（正是 refined-design-spec 当初要解决的问题，说明规范缺"落地抓手"）。
6. **两边都无正式的间距系统**：openworker 靠 Tailwind 间距类与手写 px 混用，ai-forum 只有容器边距一个值。垂直节奏（卡片内 padding、区块间距）在各自站内都不统一。
7. **openworker 品牌资产不成体系**：logo 只有 2 张大 PNG，无 SVG/多尺寸/maskable/OG 分享图（对比 ai-forum 的 `design/logo-2026-09` 完整品牌包）；桌面端与未来的 Web/分享场景会立刻缺资产。
8. **皮肤系统与基础主题的色彩打架**：皮肤 `default` 名叫「经典桔」但 accent 仍是 cobalt 蓝，而 `--skin-warm: #FFC24B`、`--skin-shadow: rgba(232,85,26,…)` 却是橙系残留；品牌到底是"桔（小桔橙 #F26B1F）"还是"钴蓝"没有定论，UI 在两个方向摇摆。
9. **Mock 与实现的漂移未被管理**：最新的《信箱双栏收件箱》mock 用宋体衬线做信纸正文，而已上线样式 `--serif: var(--sans)`（明确注释"标题已去衬线化"）。这类"mock 单方面演进"没有回流机制，验收时无从对照。
10. **emote/桌宠等位图资产缺少命名与规格约束**：`design/xiaoju-emotes-v2/` 里 `01-wave.png` 与 `emote-05-cheer.png` 两套命名混用、尺寸不一；`workbuddy-skin/` 92 个素材无清单。资产一多就出现"哪个是现行版"的考古成本（`books-clean` / `books-cropped` / `bookshelf-ref-small` 已见苗头）。

**落选但值得记录**：ai-forum 无暗色模式（夜间刷站的社区产品是明显体验缺口）；openworker 移动端与桌面端共用 token 但字号偏小；两站加载态/空态的视觉语言未逐页走查，留待走查任务。

---

## 五、改进方向（建议后续任务拆分）

### P0 · 止血（低成本高收益，建议 1–2 周内）
1. **ai-forum 静态资源本地化**：Tailwind CDN → 构建期；Google Fonts → 自托管 woff2 子集。因 ai-forum 只读，产出补丁/操作指令报告由 Lead 转交用户执行。⚠️ 与 SEO 巡检报告的图片本地化（P0-7）同源，应合并推进。
2. **openworker 字号收敛**：以 `refined-design-spec.md` 6 级字阶为准，定义 `--fs-*` 变量；先禁止新增 ≤10px 与半像素字号，再分批迁移存量。
3. **确立「共享基色」备忘**：把 cobalt `#2563eb`（含暗色 `#4c8dff`）写入两项目 Token 文档作为跨产品同源色；明确 ai-forum 语义红 `#dc2626` 的适用边界（热/涨/赞），避免未来改版时两站渐行渐远。

### P1 · 立标尺（设计基建，建议立为专项任务）
4. **openworker：三把标尺入 Token** —— `--radius-*`（建议 6/8/10/12/16/full 五档）、`--shadow-0..4`（收敛现有 81 种）、`--space-*`（4px 基准），并立"新增样式必须走标尺"的 lint/约定。
5. **ai-forum：拆分 `tokens.css`** —— `tokens/`（真变量：color/type/space/radius/shadow）+ `pages/`（页面样式）+ `themes/`（日记主题、cosmic 等）；把 TSX 里 353 处裸 hex 换成语义类/变量。
6. **openworker 品牌资产包**：仿照 ai-forum `design/logo-2026-09` 补 SVG + 多尺寸 + maskable + OG 图 + USAGE 文档（若源文件只有 PNG，需矢量化重绘，单独立项）。

### P2 · 像一家（跨项目统一）
7. **共建「双项目共享 Design Token 词典」**（Markdown，放 openworker `design/`）：统一变量命名（如 color.brand.primary、space.4）、统一语义色命名（ok/warn/danger）、统一动效时长与缓动（openworker refined-spec 已有 120/200/320ms + cubic-bezier，可直接升格）。两项目**性格参数化**：openworker = sans + 冷灰 + 高密度；ai-forum = serif 标题 + 白卡 + 编辑排版；同词典、不同预设。
8. **视觉走查流程互鉴**：把 ai-forum 的 `visual-acceptance.md` 逐页对照 + headless 截图法反向推广到 openworker 的改版验收（ui-mocks ↔ 实现对照表，解决第 9 条漂移问题）。
9. **ai-forum 暗色模式可行性评估**（社区产品夜间场景价值高，且可直接复用 openworker 的 `data-theme` 双主题架构与 pre-paint 防闪做法）。

### 建议的任务依赖
- P0-1、P0-2 可并行，互不阻塞。
- P1-4（openworker 标尺）是后续一切 UI 打磨的前置；P1-5（ai-forum 拆 Token）是其暗色模式与主题系统的前置。
- 共享词典（P2-7）应在 P1-4/5 定稿后撰写，避免返工。

---

## 六、本报告的边界与未尽事项

- 本次为**资产与 Token 层面的静态盘点**，未做逐页视觉走查与截图对比（需运行两端 dev server，属「设计走查」任务）。
- 未覆盖：openworker 的 i18n 文案视觉（locales 目录）、花园/桌宠 Canvas 动效细节、ai-forum 各 redesign 目录内 HTML mock 的逐一评估。
- ai-forum-v3 全程只读；本报告中所有修改建议均为"报告/指令"形态，符合沙箱约束。

*—— ui-designer，2026-10-11*
