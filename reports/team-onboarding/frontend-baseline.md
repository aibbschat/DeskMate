# 两项目前端架构与组件基线（task-4）

> 角色：前端设计/前端开发（frontend-developer）
> 日期：2026-10-11
> 范围：openworker `surfaces/gui`（React + Vite + Tauri）与 ai-forum-v3 `web/`（React + Vite，只读盘点）
> 配套报告：backend-baseline.md（工程）、ui-baseline.md（视觉）、ai-interaction-baseline.md（交互）、quality-baseline.md（质检）

---

## 0. 一页结论

| 维度 | openworker GUI | ai-forum-v3 web |
|---|---|---|
| 定位 | 桌面 App（Tauri 2 壳）+ 手机 Web（第二入口）+ 云端托管同一份产物 | 纯 Web 阅读/社交端（aibbs.chat），Nginx 托管 dist |
| 路由 | **无路由库**：App.tsx（2339 行）内多组 useState 切换视图；移动端入口用 useReducer | **react-router-dom v6**，App.tsx 128 行路由表，38 个页面 + 旧路径重定向 |
| 状态管理 | 无全局库；App.tsx 一处持有约 40 个 useState（“上帝组件”）；857 处 useState 分布全库 | 无全局库；页面本地 useState + setInterval 轮询（30s/45s 多处） |
| 数据层 | `src/api.ts`（4703 行）：token 三级解析、同源默认 httpBase、WS 事件流、REST 全走模块内 fetch 包装 | `src/api.ts`（2163 行）：类型化接口 + authGet/post/patch/del，Bearer token 存 localStorage |
| 样式 | Tailwind 3（构建期，color-mix 桥接 CSS 变量）+ `styles.css` 5912 行手写 CSS；`data-theme` 明暗 + `data-skin-pre` 皮肤注册表 | **Tailwind Play CDN**（index.html 内 script，线上运行时编译）+ `tokens.css` 2505 行 CSS 变量；另有 336 处内联 style |
| i18n | react-i18next（zh/en，默认中文） | 无 i18n（硬编码中文），SEO meta 由服务端 publicPageGuard 按页注入 |
| 单测 | vitest + jsdom + testing-library，**47 个**测试文件，已进 CI（gui-unit job：tsc + vitest） | **2 个**测试文件，esbuild 手搓 runner（test:blocks / test:emotes），无断言框架 |
| E2E | Playwright **73 个** hermetic spec（网络层 mock 全部 /v1 + WS）+ e2e-live（真实后端）；CI 有 gui-e2e job | 无 |
| Lint | 无 eslint/prettier 配置（仅 tsc strict 把关） | 无 |
| CI | .github/workflows/ci.yml（gui-unit + gui-e2e）+ release.yml | **无 workflows 目录** |
| 无障碍 | aria-* 约 190 处、role 67 处、reduced-motion 有兜底 | aria-* 121 处但 role 仅 5、tabIndex 0 处；reduced-motion 10 处 |

体量：openworker GUI 约 169 tsx / 4.8 万行 tsx + 67 ts；ai-forum web 约 86 tsx / 2.1 万行。两套都是 React 18 + Vite 5 + TS 5 strict，工程底子同源，但成熟度差一个量级：**openworker 偏“工程重、架构旧”（测试厚但 App.tsx 巨石），ai-forum 偏“迭代快、工程轻”（路由清爽但零测试零 CI）**。

---

## 1. openworker GUI（surfaces/gui）基线

### 1.1 目录地图

```
surfaces/gui/
├── index.html            # 桌面/浏览器入口（含 pre-paint 主题/皮肤脚本，防首帧闪烁）
├── mobile.html           # 手机端第二入口 → dist/mobile.html（必须留 dist 根目录，见 vite.config 注释）
├── src/
│   ├── main.tsx          # 桌面入口
│   ├── App.tsx           # 2339 行“上帝组件”：视图切换 + 全部业务状态 + WS 事件分发
│   ├── api.ts            # 4703 行数据层：REST + WS + token/httpBase 解析（全应用唯一出口）
│   ├── types.ts          # 领域类型（Item/Session/Approval/WsEvent…）
│   ├── theme.ts          # 明暗主题（localStorage + data-theme，index.html 预置防闪）
│   ├── tauri.ts          # isTauri / 平台判定 / 窗口拖拽，桌面与浏览器双环境适配
│   ├── streamGate.ts / runtimeReady.ts / usage.ts / time.ts / diaryEntries.ts…  # 纯函数模块（均带同名单测）
│   ├── components/       # 160+ 组件：Sidebar/Composer/Transcript/ApprovalCard/RightRail…
│   │   ├── connectors/   # 连接器专区（Slack/Gmail/GitHub/Calendar/Custom MCP…）
│   │   ├── deskmate/     # 桌宠专区：DeskHome/MailboxPage/MemoryGraph/Experts…
│   │   │   ├── skins/    # 皮肤体系：skinRegistry.ts + skinStore.ts + SkinPicker（附单测）
│   │   │   └── emotes/   # 表情包体系：emoteRegistry/emotePack/EmotePicker
│   │   ├── garden/       # 花园场景（SVG 树/花、季节主题，useGarden 自定义 hook）
│   │   └── SkillStore/   # 技能商店
│   ├── mobile/           # 手机端整套独立实现：App/reducer/useChat/两屏（Chat/Yard）
│   ├── i18n/ + locales/  # react-i18next，zh.json / en.json
│   ├── providers/        # 模型供应商接入（ProviderSetup + logos）
│   └── test/setup.ts     # vitest 全局 setup（强制 en locale）
├── e2e/                  # 73 个 hermetic Playwright spec + fixtures.ts（mock /v1 + WS）
├── e2e-live/             # 真实后端冒烟（api-smoke/approval/fib/persistence…）
├── src-tauri/            # Tauri 2 Rust 壳：lib.rs（动态建窗、托盘、sidecar Python 进程管理）
├── vite.config.ts        # 双入口构建、env-guard（防 VITE_COWORKER_HTTP 烘进 bundle）、版本注入
├── vitest.config.ts      # jsdom + setup + styles.css?raw 白名单（皮肤同步断言用）
├── playwright*.config.ts # 默认 5199 端口 hermetic；chrome/live 两套变体
└── tailwind.config.js    # darkMode=[data-theme=dark]；颜色经 color-mix() 桥接 CSS 变量
```

### 1.2 架构要点

- **导航/路由**：无 react-router。App.tsx 用 `surface`（cowork/chat/code）、`settingsTab`、`personaViewId` 等多组 useState 充当“伪路由”；好处是零路由依赖、深链需求少，坏处是状态与视图耦合、无法 URL 表达状态。**移动端入口是反例的正解**：`src/mobile/reducer.ts` 用 useReducer 集中管理，架构明显更干净。
- **状态管理**：无 Context/zustand/redux，全部 props 下发。App.tsx 一处约 40 个 useState（会话、模型、用量、todo、inbox、桌宠、圆桌……），任何新功能都在往这个文件加状态——最大可维护性风险点。
- **数据层**：`api.ts` 是全应用唯一网络出口。
  - httpBase 解析顺序：运行时注入 `window.__COWORKER_HTTP__`（Tauri 动态端口）→ VITE env → **同源 ""**（云端托管的正确默认）；
  - token 三级：`window.__COWORKER_API_TOKEN__` → runtime-config.json → dev token 文件；每请求加 `X-OpenWorker-Token`，WS 走子协议；
  - 内置两道部署事故防线：vite build 阶段 env-guard（禁止 .env.production* 定义 VITE_COWORKER_HTTP）+ closeBundle 产物断言（httpBase 同源分支没被 minifier 折叠）；
  - 创作圆桌前缀 `/roundtable` 硬约束（漏前缀=静默 404，历史上“点开始创作没反应”）。
- **样式方案**：Tailwind 3 构建期扫描 + 大量手写 CSS（styles.css 5912 行）。设计令牌是 :root 上的 CSS 变量（--paper/--panel/--ink/--accent…），Tailwind 用 color-mix() 包装以支持 `bg-panel/70` 透明度工具类。明暗 = `data-theme`，皮肤 = `data-skin-pre`，两者正交。
- **皮肤/表情体系**（可对外复用的产品级设计）：skinRegistry 纯数据注册（新增皮肤=加一条记录+同名 CSS 选择器），skinStore 只暴露 useActiveSkin()/setActiveSkin()，渲染层不直接碰 localStorage；皮肤可联动 emote pack；预览首帧在 index.html pre-paint 脚本落地，防闪。皮肤契约里明确要求 WCAG AA 对比度。
- **窗口体系**：tauri.conf.json `app.windows: []`——窗口由 Rust 侧 WebviewWindowBuilder 动态创建（注入 sidecar 端点后再加载 SPA），关窗隐藏到托盘；capabilities 只开最小权限集（window hide/show/focus、dialog、autostart），CSP 目前为 null。
- **部署铁律**：一次 build 产出 dist/（index.html + mobile.html 双入口），桌面（tauri://）、浏览器、云端托管同一份字节（docs/DEPLOY-PARITY.md）。

### 1.3 运行 / 测试 / 构建命令

```bash
# 浏览器开发（需先起 Python sidecar，token 在 <state-dir>/sidecar-8765.token）
cd surfaces/gui && npm install && npm run dev        # 固定端口 1420（strictPort，与 tauri.conf devUrl 对齐）

# 桌面端从源码跑（Rust 工具链 + .venv）
npm run tauri dev

# 类型检查 + 单测（与 CI gui-unit 相同）
npx tsc --noEmit && npx vitest run

# E2E：hermetic（mock 全部网络，无需 Python，CI gui-e2e 相同）
npx playwright test
npx playwright test -c playwright.live.config.ts   # 真实后端冒烟

# 构建（tsc + vite build，双入口一次产出）
npm run build
```

### 1.4 组件资产盘点（节选，供 ui-designer 对照）

- **对话主干**：Transcript / Composer / Markdown / ThinkingBlock / PlanCard / ApprovalCard / ToolRequestCard / SessionIntro
- **框架件**：Sidebar / RightRail / Dropdown / SelectMenu / Toggle / Icon / brandIcons / GalleryModal / SearchModal
- **桌宠线**（deskmate/）：DeskHome / StatusPanel(V1+V2 并存) / MailboxPage / MemoryGraph(Page) / DiaryPanel / BadgeWall / ExpertsCenter / AgencyPanel / BondPanel / VaultPanel / 多个 Toast
- **场景线**（garden/）：GardenScene / GardenTree / GardenFlowers / GardenFriends / SeasonTheme（季节主题纯函数+单测）
- **连接器线**（connectors/）：各服务商 Detail 页 + AddConnectionModal + CustomMcp + ToolsDisclosure
- **重复实现信号**：CicadaAvatar / XiaojuAvatar / Mascot / CicadaMascot / XiaojuMascot / ExpertAvatar / Avatar 类共 6+ 个头像实现；StatusPanel 与 StatusPanelV2 并存。

---

## 2. ai-forum-v3 前端（web/）基线

### 2.1 目录地图

```
web/
├── index.html            # SEO 头 + Tailwind Play CDN + 内联 tailwind.config（引用 tokens.css 变量）
├── vite.config.ts        # dev 代理 /api → 127.0.0.1:3001
├── src/
│   ├── main.tsx          # BrowserRouter + tokens.css（13 行，极简）
│   ├── App.tsx           # 128 行路由表：40+ Route + 旧路径 Navigate 重定向
│   ├── api.ts            # 2163 行数据层：接口类型（Article/Agent/Comment/Provenance…）+ 请求封装
│   ├── lib.ts            # 设计令牌工具（分类色、头像本地化、头像压缩）
│   ├── entryMeta.ts      # 入口 meta 工具
│   ├── pages/            # 38 个页面（Home/Article/AgentSpace/AgentPlaza/Room/Compose/MyAgents/UserCenter/Admin*…）
│   ├── components/       # 40+ 组件（Nav/Sidebar/CommentSection/ThoughtSphere/CourierBox/Companion*…）
│   │   └── room/         # 房间线：DecoratePanel/PeerRoom/widgets/stamps/roomTheme
│   ├── onboarding/       # 新手引导（content.ts + ui.tsx）
│   ├── styles/tokens.css # 2505 行设计令牌（单一事实源，含 25 个 @media、10 个 reduced-motion）
│   └── utils/            # mdBlocks / xjEmotes / qrcode / share / toast（仅前两者有测试）
└── dist/                 # Nginx 直接托管（deploy/aibbs.chat.nginx.conf root 指向 current/web/dist）
```

### 2.2 架构要点

- **路由**：react-router-dom v6，扁平路由表集中在 App.tsx；SEO 敏感页（文章/广场/房间）由**服务端** publicPageGuard 按路由注入 meta/OG/爬虫正文——前端不做 SSR，是“SPA + 服务端按页改 meta”的务实方案。旧路径全部 Navigate 重定向，不留 404。
- **状态/实时性**：无全局状态库；实时感全靠轮询——Nav 未读 30s、NotificationBell 30s、AgentPlaza 45s、Messages 自建 pollRef、DebateLiveStage 30s。**没有一个 WebSocket/EventSource**。多组件各自 setInterval，页面切换的清理靠 useEffect return，存在叠加轮询浪费。
- **数据层**：api.ts 单文件同时承担“接口类型定义 + 请求封装 + 领域函数”，Bearer token 存 localStorage（`aiforum_token`），authGet/post/patch/del 封装了 401 与其他错误的区分（带 status 抛出）。与 openworker 的 api.ts 是同一设计模式，但没有拦截器统一超时/重试/并发去重。
- **样式**：tokens.css 是刻意的单一事实源（品牌蓝 #1d4ed8 系 + 强调红语义），但 **Tailwind 走 Play CDN**（`cdn.tailwindcss.com`）：线上由浏览器运行时 JIT，内联 tailwind.config 用 var() 引用 tokens。风险见技术债 T1。另有 336 处 `style={{ }}` 内联样式。
- **Markdown**：react-markdown 9 + remark-gfm + **rehype-sanitize**（Article/EntryDetail 均消毒，安全姿势正确）。
- **测试**：仅 2 个纯函数测试，且 runner 是 esbuild 打包后直接 node 执行（无断言库/无报告/无 watch），`npm test` 根本不存在（根 package.json 的 test 指向 server 测试）。
- **部署**：生产 Nginx 托管 `current/web/dist`（含 SPA fallback 与资产 404 诚实化逻辑），Express 仅在 dist 存在时兜底静态托管；无前端构建 CI，靠本机手动 `npm run build` 后随部署脚本上传（deploy.sh 打包甚至 exclude web/dist）。

### 2.3 运行 / 测试 / 构建命令

```bash
cd web && npm install
npm run dev        # 5173，/api 代理到 127.0.0.1:3001（后端 node --watch server/index.js）
npm run build      # vite build（无 tsc 前置！类型错误不阻断构建）
npm run test:blocks && npm run test:emotes   # 两个手搓测试，无 npm test
```

---

## 3. 对比与可复用清单

### 3.1 两套前端的天然复用点（同为 React18+Vite5+TS5 strict+react-markdown+remark-gfm）

| 资产 | 哪边成熟 | 复用建议 |
|---|---|---|
| **vitest + playwright 配置基线** | openworker | ai-forum 可整套照搬：vitest.config（jsdom+setupFiles）+ playwright hermetic fixtures（mock 全部 /api），先给 5 个高频页面上冒烟 |
| **hermetic e2e 模式**（网络层 mock，CI 无需后端） | openworker | 推荐 ai-forum 采用，解决“前端测试必须连真库”的死结 |
| **CSS 变量 token 单一事实源** | 双方都有 | openworker 的 token + Tailwind 构建期桥接（color-mix）是生产级；ai-forum 若放弃 Play CDN，可直接套用该桥接模式 |
| **皮肤/主题注册表模式**（skinRegistry/skinStore/pre-paint 防闪） | openworker | ai-forum 房间装修（roomTheme/DecoratePanel）如要主题化，可复用该数据驱动思路 |
| **类型化 api.ts + 错误带 status** | 双方同构 | 可抽公共规范：请求层统一封装超时/401 重登/重试，两边各删各的重复 |
| **i18n（react-i18next）** | openworker | ai-forum 暂无出海需求可不搬，但硬编码文案已让 Admin/用户文案散落 38 个页面 |
| **rehype-sanitize** | ai-forum | openworker Markdown.tsx 目前只靠 react-markdown 默认不渲染原始 HTML + urlTransform 白名单；若未来引入 rehype-raw 展示工件 HTML，必须同步补 sanitize |
| **移动端 useReducer 架构** | openworker mobile | 桌面端 App.tsx 拆分时可直接以 mobile/reducer.ts 为蓝本 |

### 3.2 无障碍与响应式现状

- openworker：aria/role 覆盖尚可（审批卡、inbox、侧栏均带语义），reduced-motion 有兜底；**tabIndex 仅 5 处**，键盘遍历路径不完整；styles.css 只有 8 个 @media——桌面为主 + 固定双入口，移动端靠独立 mobile.html 而非响应式。
- ai-forum：**role 仅 5、tabIndex 为 0**，Modal/下拉/点赞按钮大概率是 div onClick；tokens.css 有 25 个 @media 与 10 个 reduced-motion（移动端阅读体验有照顾），但 SPA 路由切换无焦点管理/无 scroll restore，读屏用户会“迷路”。

---

## 4. Top 10 前端技术债（按优先级）

| # | 项目 | 债项 | 影响 | 建议 |
|---|---|---|---|---|
| T1 | ai-forum | **生产环境用 Tailwind Play CDN**：第三方运行时脚本阻塞渲染、线上 JIT、无法 purge、受 CDN 可用性/版本漂移影响、与 CSP 不兼容 | 性能+稳定性+安全 | 迁移到构建期 Tailwind（照抄 openworker 的 color-mix 桥接），产物自托管 |
| T2 | ai-forum | **零 CI + 构建不过 tsc**：`npm run build` 无 tsc 前置，.github 无 workflows | 类型错误可静默上线 | build 前加 `tsc --noEmit`；加最小 CI（tsc + 既有 2 个测试 + vite build） |
| T3 | openworker | **App.tsx 2339 行上帝组件**（40+ useState，视图切换+WS 分发+业务状态全混） | 每个新功能都加剧合并冲突与回归面 | 按 mobile/reducer.ts 蓝本拆：数据（会话/用量/inbox）进 hooks，视图路由表化 |
| T4 | ai-forum | **无实时通道**：6+ 处各自 setInterval 轮询（30–45s），无统一调度 | 电量/流量浪费，多组件叠加轮询，实时性差 | 统一 poller（visibility/路由感知、合并窗口），中期评估 SSE（后端已有事件模型可对齐） |
| T5 | ai-forum | **测试真空**：全站 2 个手搓测试、无断言框架；38 个页面 0 覆盖 | 回归全靠线上肉眼巡检 | 先 vitest 覆盖 api.ts 纯函数与 3 个重交互页，再上 3–5 条 Playwright 冒烟 |
| T6 | openworker | **无 lint**：仅 tsc 把关（且 noUnusedLocals 严格，但无 eslint/react-hooks 规则） | hooks 依赖错误、坏模式无自动拦截 | 引入 eslint + react-hooks + import 排序，先 --warn-on-mode 渐进收紧 |
| T7 | openworker | **api.ts 4703 行单文件** | 新人找不到函数、改动易碰 | 按域拆分（sessions/automations/connectors/creation/events），保留总出口 re-export |
| T8 | ai-forum | **336 处内联 style** + 无组件库，Modal/Toast 等基础件各页自造 | 视觉漂移、无法统一换肤 | 抽 8–10 个基础件（Modal/Toast/Tag/Avatar/Button），token 化颜色 |
| T9 | 双方 | **重复基础组件**：openworker 6+ 个头像实现、StatusPanel V1/V2 并存；ai-forum Nav/Sidebar/NotificationBell 各自轮询 | 维护成本、行为不一致 | 建立“基础件清单”，旧实现标记 deprecated 后合并 |
| T10 | ai-forum | **无障碍垫底**：role=5/tabIndex=0、Modal 无焦点陷阱、路由切换无焦点管理；token 存 localStorage 的 XSS 面 | 合规与口碑（aibbs.chat 生产站） | 基础件落地时同步补语义与键盘路径；审计 token 存储与 httpOnly cookie 方案 |

---

## 5. 建议的后续任务拆分（供 Lead 参考）

1. 【前端基建】ai-forum 最小 CI + tsc 门禁 + Play CDN 迁移（T1/T2）——**建议作为 ai-forum 侧第一个工程任务**（修改建议以补丁/指令形式交用户执行，本沙箱只读）。
2. 【openworker】App.tsx 拆分专项：以 src/mobile/reducer.ts 为蓝本，先抽“会话数据 hook + 视图表”，不改任何视觉（T3），需 code-reviewer 配合回归 e2e。
3. 【测试】ai-forum 引入 vitest + 3 条 Playwright 冒烟（复用 openworker e2e/fixtures.ts 模式）（T5）。
4. 【体验】两项目基础组件对齐：Modal/Toast/Avatar 清单化，ai-forum 顺手补键盘可达性（T8/T10）。
5. 【实时】ai-forum 轮询收敛为统一 poller 或 SSE（T4，依赖 backend-engineer 提供事件端点）。

---

## 附：关键文件速查

- openworker GUI：`surfaces/gui/src/App.tsx`、`src/api.ts`、`src/mobile/reducer.ts`、`src/components/deskmate/skins/skinRegistry.ts`、`vite.config.ts`（env-guard 与双入口注释）、`e2e/fixtures.ts`、`.github/workflows/ci.yml`
- ai-forum web：`web/src/App.tsx`（路由表）、`web/src/api.ts`（请求层+类型）、`web/src/styles/tokens.css`（令牌）、`web/index.html`（Play CDN 现场）、`server/index.js`（dist 托管与 SPA fallback）、`deploy/aibbs.chat.nginx.conf`
