# PilotDeck 开源项目调研与借鉴分析

> 调研日期：2026-10-15
> 来源文章：今日头条《清华在 GitHub 上开源的 Agent 工作台，最近更新了 2.0 版本》
> 仓库：https://github.com/OpenBMB/PilotDeck ｜ 官网：https://pilotdeck.openbmb.cn/
> 调研方式：文章 + README + 官网 + 源码目录结构 + 关键实现文件（classifyAndRoute.ts / EdgeClawMemoryProvider.ts / CronRuntime 等）精读

---

## 一、结论先行

PilotDeck 是清华 THUNLP、面壁智能（ModelBest）、OpenBMB、AI9Stars 联合开源的**面向任务的 AI Agent 工作台 / "Agent OS"**，2026-05-28 开源，许可证 **AGPL-3.0**（⚠️ 强 copyleft），最新 `v2026.10.13-r2`（2.0）。

**核心判断**：它的底层哲学与我们小桔 / DeskMate / OpenWorker 高度重合——项目级隔离、白盒记忆、智能路由、Always-on、MCP 原生、不绑定大厂。**多数能力我们已有对标，说明路子走对了**；真正值得借鉴的是几个**工程细节层**的差距，尤其是"按单条任务难度动态路由"和"记忆的产品级可视化/trace"。同时我们也有它**完全没有**的差异化优势（零 LLM 确定性 Dream、云端多副本收敛闭环、陪伴人格）。

> ⚠️ **红线提醒**：AGPL-3.0 强 copyleft，不能直接 copy 其代码进我们的仓库（我们是 fork 自 openworker，许可证不同）。**只借鉴设计思路与算法，不复制代码**。技术栈也不同（它是 Node/TS/Electron，我们是 Python/Tauri），借鉴的是架构与算法。

---

## 二、PilotDeck 核心架构（与我们的映射）

| 维度 | PilotDeck 实现 | 我们的对应 | 差距评估 |
|---|---|---|---|
| **项目隔离** | WorkSpace 为基本单位：独立文件系统 + 记忆 + 技能，检索有界，技能随任务沉淀 | 产出按 `/OpenWorker/<project-id>/`；记忆库按项目维度（profile_items layer） | 我们记忆层 `VaultStore` 是**全局** `~/.deskmate/vault`，未按项目隔离 |
| **白盒记忆** | EdgeClaw 引擎（独立包）+ 专用前端 UI + Dream 回滚 + 每次检索落 trace | `VaultStore`(.md) + `dream.py`(零 LLM 确定性) + `.bak` 回滚 | 我们有"可恢复"，但**缺内置可视化 UI 与 per-query trace 面板** |
| **智能路由** | judge 小模型对单条任务做难度分类 → tier → 主/子模型编排 | `router.py` 免费组(Kilo)/付费组 + 会话级 `sessions.model` | 我们是**静态分组**，缺"按单任务难度动态分级"一层 |
| **Always-on** | `cron` 模块：完整定时/常驻调度 + 结果投递 + 时区 + SQLite | 云端 `xiaoju-agency` tick 7×24 + `xiaoju-sync-window` 每小时 | 我们偏场景化，它更工程化为通用调度器 |
| **多端一致** | Gateway(Web/CLI/IM) + Adapters 分层 | Web / Desktop / CLI 多端 | 已对齐 |
| **扩展协议** | `plugin.json`：MCP / Tools&Skills / Lifecycle Hooks / Custom Memory | MCP + 技能目录 | 已对齐 |

**技术栈**：Node.js 22 + TypeScript + React/Vite/Tailwind/shadcn-ui + SQLite(`better-sqlite3`/`node:sqlite`) + Electron 桌面端；原生支持 MCP 及 OpenAI/Anthropic/Google/DeepSeek/Qwen/Kimi/MiniMax 协议。

---

## 三、值得借鉴的工程细节（代码级证据）

### ① 智能路由的「judge 难度分类」—— 我们缺的关键一层 ⭐⭐⭐

**证据**：`src/router/tokenSaver/classifyAndRoute.ts`（11.6KB）+ `src/router/index.ts` 导出 `classifyAndRoute`。

实现机制：
- 取**最后一条用户消息** → 用一个**轻量 judge 模型**（`maxOutputTokens=256`、`thinking.enabled=false`、`stream=false`，极小开销）→ 生成分类 prompt → 解析出 `tier` → 选对应 `RouterModelRef`。
- **tier 即难度分层**：每个 tier 绑定一个模型（如强模型 / 弱模型），`defaultTier` 为兜底强模型。
- **fail-open 兜底**（关键可靠性设计）：judge 超时（默认 5s）/ 解析失败 / 模型错误 → 一律回落 `defaultTier`（强模型），最多 3 次重试，绝不阻塞主流程；失败信息结构化进 `TokenSaverFailure`。
- **短续接继承**：`isShortContinuation()` 检测「好 / 继续 / ok / 嗯」等 <30 字消息，直接继承上轮 `previousTier`，避免小模型把这些误判为"简单任务"。
- **凭证脱敏**：`sanitizeFailureMessage()` 自动 redact `api_key`/`token`/`authorization`，绝不把凭证写进日志或 telemetry。

**与我们对比**：我们是「免费组 vs 付费组」**静态分组** + 会话级 `default_model`/`sessions.model`，**没有按单条任务难度动态分级**。这层 judge 能让我们"强模型干难的、弱模型干简单的"真正自动发生，而不是靠用户/会话手动选组——是最值得补的一层。

### ② 记忆白盒化的「产品级 UI + trace」—— 我们可补的呈现层 ⭐⭐

**证据**：`src/context/memory/EdgeClawMemoryProvider.ts` + `src/context/memory/edgeclaw-memory-core/`（`service.ts` 37KB 核心引擎 + `ui-source/`：`index.html` + `app.js` 93KB + `app.css` 30KB + `trace-i18n.js`）。

实现机制：
- 记忆被抽成**独立引擎包 EdgeClaw**，Provider 只是 wrapper，真正的存储/检索/提取/整理在 `service.ts`。
- 有**专门的独立前端应用**（`ui-source/`）来浏览、编辑记忆——这就是"记忆看得见、改得了"的产品级落地。
- **每次记忆检索都落 trace**：`saveCaseTrace` 记录 `query`、检索意图 `intent`、是否注入 `injected`、上下文预览 `contextPreview`、assistant 回复——"白盒可追溯"的工程实现。
- 检索失败 / 捕获失败都**不破坏 agent 主流程**（fail-open）。

**与我们对比**：我们用 `.md` 文件存记忆（用户得开 Obsidian 才能看/改），Dream 有 `.bak` 回滚 + `dream_report.md` + `dream.jsonl` trace。我们有"可恢复"，但**没有内置的记忆可视化 UI 与 per-query 检索 trace 面板**。EdgeClaw 把记忆做成产品级可视化的思路，值得我们的记忆管理面板升级参考。

### ③ Always-on 的 cron 模块 —— 与云端 tick 思路一致 ⭐

**证据**：`src/cron/`（`CronRuntime`/`CronManager`/`CronTaskStore`/`CronSchedule`/`CronTimezone`）。

实现：完整定时任务引擎——create/list/update/delete/runNow/stop，带结果投递（`CronResultDelivery`）、时区解析、SQLite 存储。与我们"云端 `xiaoju-agency` tick 7×24 自跑 + 本地 hourly sync window"异曲同工；区别在于它把 Always-on 做成**通用 cron 调度**（任何任务可定时），我们是针对"小桔自主副本 + 同步"的场景化实现。借鉴点：通用调度器 + 结果投递，比我们散落的 tick 更工程化，可考虑抽象。

### ④ 办公 Skills 内置化 —— 我们有能力但没做成产品内置 skill 流 ⭐⭐⭐

PilotDeck 2.0 把 **docx / pptx / spreadsheets 三个做成生产级内置 skill**（不是"调个库"，而是 Understand→Build→Review→Deliver 完整工程闭环 + LibreOffice 逐页/逐幻灯片视觉核验 + 源保护原子交付）。**这是本次调研发现的最大真实落差**——详见下文「七、办公能力落差（代码级深挖）」。

### ⑤ 多端一致 + 扩展协议 —— 已对齐（确认方向对）⭐

Gateway/Adapters 分层（Web/CLI/Feishu）+ `plugin.json` 扩展，与我们的多端 + MCP 架构一致。**确认我们架构方向没问题**。

---

## 七、办公能力落差（代码级深挖）

> 本节是萧哥追问"办公能力有没有落差"的专项回答。结论：**在「小桔自主运行时」维度，办公能力是我们与 PilotDeck 最明显的真实短板**。

### 7.1 PilotDeck 的办公能力到底多深（证据）

三个 skill 都是 `SKILL.md + scripts/ + references/ + runtime/` 标准结构，且**不是简单调库**，而是带 QA 闭环的生产级工程：

| Skill | 能力范围 | 底层工具 | 标志性工程能力 |
|---|---|---|---|
| **docx** | 新建 / 编辑 / 检查 / 审阅 / 终稿 Word；保留源文件、修订、批注、域、分页 | `python-docx` + `lxml` + OOXML | `inspect`(摘要/搜索) → `validate` → **`render`(LibreOffice 逐页出图，人工视觉核验)** → `deliver`(原子发布+源保护+内部留恢复副本) → `compare`/`annotate`/`finalize`/`sanitize` |
| **pptx** | 新建 / 编辑 / 审阅 / 终稿 PPT；图表、表格、图片、演讲者备注、母版 | `python-pptx` / `PptxGenJS` / OOXML / LibreOffice | 同样 `validate`/`render`(逐幻灯片出图)/`compare`；**`convert-legacy`(.ppt→.pptx)** |
| **spreadsheets** | XLSX/XLS/CSV/TSV 新建 / 编辑 / 公式 / 图表 / 数据整合 | `openpyxl` / `xlsxwriter` / `pandas` / ExcelJS / LibreOffice UNO | `recalculate`(刷新公式缓存) + `validate` + `render`；`convert-legacy` |

**PilotDeck 办公 skill 的精髓**（它的 SKILL.md 反复强调的设计哲学，值得抄作业）：
1. **四/三阶段框架**：Understand（区分事实源 vs 模板 vs 视觉参考，禁止编造事实/日期/引用）→ Build（按任务选最合适库，**优先局部编辑而非重建**）→ Review（**依据"用户要求的结果"而非"工具是否跑通"来判卷**，用 render 出图做视觉证据）→ Deliver（原子发布、保护源文件）。
2. **render 视觉 QA**：用 LibreOffice 把文档/幻灯片渲染成图片，**打开全尺寸图做视觉核验**后才交付——这是它"生产级"的关键，不是"生成完就甩给用户"。
3. **源保护 + 恢复副本**：`deliver` 永远保护源文件，内部留 recovery copy；替换源需显式 `--replace-source`。
4. **诚实报告局限**：遇到图表/宏/签名等包敏感特性，先 inspect 再决定保留策略，并如实报告限制。

### 7.2 我们小桔 / DeskMate 的真实状态（逐项核查证据）

| 维度 | PilotDeck | 我们（小桔自主运行时） | 证据 |
|---|---|---|---|
| **Word 文档生成** | 通用 skill（新建+编辑任意 docx） | **只有写作流水线导出**：`deskmate/creation/docx_export.py` 把剧本/大纲 markdown→docx（经外部 `officecli`），带 `.md` 降级兜底、时间戳永不覆盖 | `docx_export.py:1-142` |
| **PPT 生成** | 完整内置 skill | **运行时内完全没有**（全仓 grep `pptx`/`python-pptx` 仅命中 docx_export 注释 + 文件服务 mime 类型，无任何生成逻辑） | grep 全仓 `*.py` |
| **Excel/CSV 生成** | 完整内置 skill | **运行时内完全没有**（仅文件服务能"展示"既有 xlsx，1 处测试 fixture） | grep 全仓 `*.py` |
| **办公 skill 注册** | 3 个内置 | `deskmate/skills` 框架**有 `office` 分类占位**，但**无任何办公 skill 落地**（`deskmate/skills/**` 下 0 个 skill 文件，grep 仅见分类常量） | `skill_store.py:59` `CATEGORIES=[...,"office",...]`；`Glob deskmate/skills/**/*.md` → 无 |
| **Python 办公库** | 全装（docx/pptx/openpyxl/pandas） | **托管运行时内全缺**（docx/pptx/openpyxl/pandas/xlsxwriter 均 MISSING），办公完全依赖外部 `officecli` 二进制 | 实测 import 探测 |
| **officecli 二进制** | （无此概念，用 python 库） | **已装** `~/.local/bin/officecli`（33MB，schema-driven，可操作 docx/xlsx/pptx）—— 这是我们能做 docx 导出的唯一抓手 | `ls -la ~/.local/bin/officecli` |
| **LibreOffice 渲染 QA** | 有（render 出图核验） | **未安装**（libreoffice/soffice 均 not found），无任何视觉 QA | `which libreoffice soffice` |
| **源保护 / 原子交付** | `deliver` 带恢复副本 | docx_export 仅"时间戳不覆盖"，无源保护/恢复副本/compare 机制 | `docx_export.py` |

### 7.3 关键判断：落差在哪、不在哪

**真实落差（小桔自主运行时）：**
1. **PPT / Excel 生成能力为零**——这是与 PilotDeck 最刺眼的差距。用户让小桔"做个 PPT/表格"，自主运行时做不到。
2. **Word 仅写作流水线可用**，不是"通用文档 skill"——做不了报告/信函/论文等任意 docx，也**不能编辑已有 docx**。
3. **没有 render 视觉 QA**——生成完直接交付，无法像 PilotDeck 那样"先看图再交"。
4. **没有源保护/恢复副本/compare** 的工程闭环。
5. **Python 办公库托管运行时不装**，办公完全系于外部 `officecli` 二进制是否在场。

**但要注意的边界（避免误判）：**
- **平台侧我们其实很强**：WorkBuddy 内置 `workbuddy-docx` / `workbuddy-pptx` / `workbuddy-sheet-generation` / `workbuddy-sheetagent` 四个全家桶 skill（多阶段 writer→formatter→converter、HTML 高保真），**论成熟度不输甚至超过 PilotDeck**。问题在于：这些 skill 是**平台（你我这次对话用的助手）能力，没有被接进小桔的自主桌面运行时**——即小桔在云端 tick / Tauri App 里自主跑时，调不到它们。
- `coworker/tools/shell.py` 已把 `~/.local/bin` 加进 agent 的 PATH，说明后端 agent **本就打算**让 skill 通过 shell 调 `officecli` 做办公——只是**没有对应的"办公 skill 包"去驱动它**，等于"铺了路没发车"。

### 7.4 借鉴与落地建议

| 优先级 | 借鉴项 | 落地建议 |
|---|---|---|
| **P1 ✅ 已落地** | 把办公做成"产品内置 skill" | 在 `deskmate/skills` 的 `office` 分类下，基于已装的 `officecli` 落地 **docx / pptx / xlsx 三个 skill 包**（`office-word` / `office-excel` / `office-pptx`，SKILL.md 驱动 agent 调 officecli）。`office-word` 已与既有 `office-doc`（读稿专长）分工互补；`seed_office.py` 幂等装入活目录。详见第九章 |
| **P1 ✅ 已落地（替代方案）** | 渲染视觉 QA（不装 LibreOffice） | 改用 `officecli view html` 做视觉 QA（分页/层级/留白直接在 HTML 预览，**Mac 无需装 LibreOffice**），已写进三个技能的四阶段「核验」步与交付门禁，对标 PilotDeck 的 `validate`+`render` 模式但零外部依赖 |
| **P2** | 源保护 + 原子交付 + 恢复副本 | 办公 skill 的 deliver 步骤：保护源文件、写临时候选、原子替换、内部留 recovery copy（对标 PilotDeck `deliver --replace-source`） |
| **P2** | 平台办公 skill 接进小桔自主运行时 | 评估把 WorkBuddy 的 docx/pptx/sheet 全家桶，或以 MCP / skill 包形式，暴露给小桔的自主 agent（云端 tick、Tauri App），让"自主做 PPT/表格"成为可能 |
| 观察 | 诚实报告局限 + 禁止编造事实 | PilotDeck SKILL.md 的"区分事实源/不编造/如实报告包敏感特性限制"原则，直接抄进我们的办公 skill 规范 |

> ⚠️ 注意：PilotDeck 办公 skill 是 AGPL-3.0，其 SKILL.md / scripts 不可直接复制进仓库；我们借鉴的是**工程框架（四阶段 + render QA + 源保护）**，实现走自己的 `officecli` / 平台 skill。

---

## 四、我们已领先 / 已对标（给信心）

- **记忆 Dream**：我们 `dream.py` 是**零 LLM 确定性**去重 + 矛盾消解 + `.bak` 回滚，比 PilotDeck 用 LLM 归纳更稳、更省、更可控（不会"越整理越乱"），且已带 `.bak` 回滚与 `dream.jsonl` 追溯。
- **多副本云端收敛闭环**：PilotDeck 偏单机/桌面，未见等价的「本地 + 云端双副本 LWW 收敛」实现，这是我们的**差异化优势**。
- **不绑定大厂 + 多 provider**：两边都做。
- **「真实优先」陪伴人格哲学**：PilotDeck 是工具向，无陪伴人格，这是小桔独有。

---

## 五、优先级借鉴清单

| 优先级 | 借鉴项 | 落地建议 |
|---|---|---|
| **P0** | judge 难度分类路由 | 在 `router.py` / picker 上加「任务难度 judge」层：轻量模型分类 → 选免费组/付费组/特定模型；fail-open 兜底（judge 失败回落强模型） |
| **P1** | 记忆可视化 + trace 面板 | 升级记忆管理面板：加 per-query 检索 trace（意图/注入/预览）+ 记忆编辑 UI（现靠 Obsidian） |
| **P2** | 通用 cron 调度抽象 | 把 `xiaoju-agency` tick 抽象成可配置 cron 调度器 + 结果投递 |
| **P1 ✅ 已落地** | 办公 skill 内置化（最大真实短板） | 在 `deskmate/skills` 的 `office` 分类下，基于已装 `officecli` 落地 **docx/pptx/xlsx 三个 skill 包**（通用文档，非仅写作流水线）；用 `officecli view html` 做视觉 QA（**不装 LibreOffice**）；源保护/原子交付/恢复副本已嵌进技能红线与交付门禁；平台 docx/pptx/sheet 全家桶接进小桔自主运行时——待下一步评估（见第九章） |
| 观察 | EdgeClaw 记忆引擎 | 关注其开源演进，必要时参考其服务化 / 独立包拆分思路 |

---

## 六、红线（不因借鉴松动）

- ⚠️ **AGPL-3.0**：只借鉴设计思路与算法，不复制代码。
- ⚠️ **技术栈差异**：Node/TS 借鉴的是架构，不是实现。
- ⚠️ 我们的「真实优先」人格红线、神圣稿件红线、本地↔云端代码一致红线，不因借鉴而松动。

---

## 八、P0 动态路由 judge 层已落地（2026-10-15 执行 · 代码级）

> 萧哥确认执行，先把 P0（动态路由）做了。已写代码 + 单测 + 全量回归验证，**零新红**。

### 8.1 落地方案（与 PilotDeck 的取舍，先讲清楚）

PilotDeck 用一个小号 **judge LLM** 分类难度。我们第一版用**启发式分类器**，理由：
- 这是个"省钱"功能，再为它每轮发一次 LLM 调用是自相矛盾（零延迟、零成本更好）；
- 确定性、fail-open 由构造保证（分类失败/异常永远回落强模型，绝不卡主流程）；
- 结构上预留 `backend="llm"` 扩展点，需要时再接小号 judge 模型（忠实复刻 PilotDeck 的升级路径）。

### 8.2 改动文件

| 文件 | 改动 |
|---|---|
| **`coworker/providers/dynamic_route.py`**（新增） | 纯函数 judge 层：`classify_difficulty`（启发式，代码/长文/推理/工具/长度多信号）→ simple/complex；`select_group_for_turn`（免费组/付费组）；`resolve_auto_model`（"auto" 哨兵→具体通道，免费组全熔回落付费组）；`is_short_continuation`（短续接继承上轮分组，对齐 PilotDeck）；`last_user_text`。全部 fail-open |
| **`coworker/providers/router.py`** | `ProviderRouter` 加 `dynamic_routing` 开关 + `_resolve_auto()` 中央钩子：`complete/stream` 仅当 `model=="auto"` 且开关开才动态选组，否则**完全透传**（默认零行为改动、不越权替用户换模型）；记录 `_last_dynamic_group` 供续接继承 |
| **`coworker/server/manager.py`** | 构造 `ProviderRouter` 时读 prefs `dynamic_routing`；顺手把 `self._data_base = base` 提前（修了 3 个依赖它的预设失败） |
| **`coworker/agent.py`** | CLI/standalone 构造处接 `DESKMATE_DYNAMIC_ROUTING` 环境变量开关（默认关） |
| **`tests/test_dynamic_route.py`**（新增） | 23 个单测：覆盖 last_user_text / 短续接 / 启发式分类 / 选组 / 解析 / router 接线（monkeypatch 隔离真实 provider） |

### 8.3 接入方式（opt-in，默认零影响）

- 用户把会话模型设为 **`"auto"`** 且开启总开关（`prefs.json` 写 `"dynamic_routing": true`，或 CLI 设 `DESKMATE_DYNAMIC_ROUTING=1`）→ 才按 turn 动态选免费组(Kilo)/付费组(ark→小米→腾讯)。
- 复用既有 `deskmate/llm_channels.route(group)` 落地具体通道，免费组全熔自动回落付费组。
- 默认（用户没设 `auto` / 没开开关）**完全透传**，现有行为零改动。

### 8.4 验证结果（trust-but-verify）

- 新增 23 单测全绿。
- 受影响测试 140 全绿（router/provider/engine/circuit_breaker 等）。
- **全量套件回归**：带改动 44 failed / 基线 47 failed → **零新红**（反而少 3 个，因 `_data_base` 提前修好预设失败）。

### 8.5 下一步（P1 办公 skill 骨架，✅ 已执行）

见第九章。基于已装 `officecli` 在 `deskmate/skills` 的 `office` 分类下落地 docx/pptx/xlsx 三个 skill 包（含神圣稿件红线 + 四阶段框架 + 交付门禁）；视觉 QA 采用 `officecli view html`（不装 LibreOffice）；源保护/原子交付/恢复副本已嵌入技能规范。

---

## 九、P1 办公 skill 骨架已落地（2026-10-15 执行 · 代码级）

> 萧哥确认前端不做、先让后端 auto 跑几天，直接进办公任务。已写技能包 + 幂等 seed 脚本 + 单测，验证通过，**零新红**。

### 9.1 落地方案（与 PilotDeck 的取舍，先讲清楚）

PilotDeck 的办公 skill 是 AGPL-3.0，其 SKILL.md / scripts **不可直接复制**（红线）。我们借鉴的是**工程框架**——四阶段（理解→构建→核验→交付）+ 视觉 QA + 源保护——实现走自己的 `officecli`（已装 `~/.local/bin/officecli`，schema-driven）。

关键设计决策：
- **视觉 QA 不装 LibreOffice**：`officecli view html` 自带 HTML 预览，Mac 上直接看分页/层级/留白，零外部依赖，比"装 LibreOffice render"更轻。
- **深度参考而非复制**：officecli 自带生产级 `imported/officecli-*` skill（含完整命令 schema）。但裸导入无神圣稿件护栏，会被小桔绕过红线。决策：删除裸导入，改由**自有带红线技能 + `officecli help <element>` 权威兜底**驱动（属性名/枚举不确定时先 help 再动手，不猜）。
- **与既有 `office-doc` 互补**：`office-doc`（64 次使用、读稿专长）保留；新 `office-word` 的 trigger 收窄为"新建/重排/生成/编辑且非读取原稿"，避免匹配器混淆。

### 9.2 改动/新增文件

| 文件 | 改动 |
|---|---|
| **`deskmate/skills/seeds/office/office-word.md`**（新增） | Word 通用文档技能：从零生成/在副本上编辑报告/信件/提纲/合同/纪要。含 ⛔ 红线（sacred_paths / is_author_manuscript / 副本 / 真实优先 / .bak）、四阶段框架、Shell 纪律、通用流程、关键能力速查、交付门禁、诚实边界 |
| **`deskmate/skills/seeds/office/office-excel.md`**（新增） | Excel 技能：import CSV/TSV、cell/sheet/table/chart/pivottable/conditionalformatting 等；同样含红线 + 四阶段 + 交付门禁 |
| **`deskmate/skills/seeds/office/office-pptx.md`**（新增） | PPT 技能：slide（layout/渐变/图片背景）、shape、notes、chart、table、picture；同样含红线 + 四阶段 + 交付门禁 |
| **`deskmate/skills/seed_office.py`**（新增） | 幂等 seed 脚本：把 `seeds/office/*.md` 复制到 `~/.deskmate/skills/office/`（受 `DESKMATE_SKILLS_DIR` 影响）；已存在则跳过（非破坏）；支持 `--force` 覆盖同步仓库更新；可作 `ensure_office_skills()` 模块函数调用 |
| **`tests/test_office_skills_seed.py`**（新增） | 5 个单测：seed 源存在、装入幂等（首次装 3 / 二次跳过）、装入后 store 可见、嵌入神圣稿件护栏、嵌入四阶段框架 |
| `~/.deskmate/skills/office/`（活目录） | 已实际 seed 三技能；另含既有 `office-doc.md`（25KB 读稿专长）、`pdf-official.md` |
| `~/.deskmate/skills/imported/` | 删除裸 `officecli-{docx,xlsx,pptx}`（缺神圣稿件红线，会被绕过） |

### 9.3 三个技能的统一契约

- **触发（trigger）**：用户要新建/重排/生成/编辑 Word/Excel/PPT，且非"读取/查阅原稿"。
- **四阶段框架**：理解（`view outline`）→ 构建（`create/open` + 小步增量）→ 核验（`view html`/`issues`/`validate`）→ 交付（`save/close` + 留 `.bak`）。
- **交付门禁**（任一不过 = REJECT）：`validate` 无 schema 错误 + token 泄漏检查（无 `{{}}`/`<TODO>`/`xxxx`/`lorem` 残留）+ 活字段检查（如 docx 页码用 `field=page` 而非静态假页码）。
- **红线**：神圣稿件绝不触碰（原稿正文只读、一律副本操作）；新产出写新路径；真实优先不编造；破坏性操作前留 `.bak`。

### 9.4 验证结果（trust-but-verify）

- 新增 5 单测全绿（`tests/test_office_skills_seed.py`）。
- 技能相关回归 **98 passed, 0 failed, 4 warnings**（含既有 98 技能目录扫描）。
- 三技能已实际装入活目录，小桔在自主运行时（云端 tick / Tauri App）即可匹配调用。

### 9.5 待办 / 未决（本轮收尾后）

- **端到端验证**：✅ 已做（萧哥令"先端到端跑一次"）。实测 `officecli` v1.0.157 真实生成 `季度综述.docx` / `演示.pptx` / `数据.xlsx`，三交付门禁全过（schema / token 泄漏 0 / docx 活页码 / xlsx 公式实算 4070）；实踩并修复 zsh 陷阱（路径 `[N]` 须单引号、`()` 值须引号），已补进三技能 Shell 纪律。
- **源保护 / 原子交付 / 恢复副本**：✅ 已做成可调用的统一工具（对标 PilotDeck `deliver --replace-source`）。新增 `deskmate/skills/tools/office_safe_deliver.py`（`protect`/`deliver`/`restore` 三子命令：编辑前滚动备份 `.bak`、原子替换用同 fs 临时文件 + `os.replace`、恢复副本）；三技能交付步已改为调用它；8 单测全绿。已加进 `deploy_cloud_sync.sh` 云端同步清单（红线：新 Python 文件须同步上云）。
- **平台 docx/pptx/sheet 全家桶接进自主运行时**：✅ 已确认接进。小桔自主运行时经 `coworker/agent.py:_deskmate_skill_index()` → `SkillManager.build_skill_index()` 扫描 `~/.deskmate/skills` 并注入 `<available_skills>` 系统 prompt；实测索引已含 `office-word`/`office-excel`/`office-pptx`/`office-doc`，且「做个 PPT / 整理成 Excel / 新建 Word 报告」类请求能命中对应技能。
  - ⚠️ 边界说明：WorkBuddy 平台私有的 HTML 高保真全家桶（`workbuddy-docx`/`workbuddy-pptx`/`workbuddy-sheet-*`）不在本仓库内，本仓库接进的是 **officecli 基座的办公套件（docx/pptx/xlsx）**——已可真实生成、可自主调用。若需平台级高保真管线，属独立移植工作，不在本次范围。
  - 新增 `tests/test_office_skills_runtime_wiring.py` 锁回归（索引可见 + 请求命中）。
- **仓库接线（未决）**：`seed_office` 是否接 CI 自动 seed、`.gitignore` 如何处理 seed vs 活目录，未定。
- ⚠️ 以上改动均只在本地、未 push（按红线只走 backup/fork/base）。

### 9.6 本轮收尾新增 / 改动文件（2026-10-15 续）

| 文件 | 改动 |
|---|---|
| **`deskmate/skills/tools/office_safe_deliver.py`**（新增） | 源保护 + 原子替换 + 恢复副本工具（CLI：`protect`/`deliver`/`restore`）；同 fs 临时文件 + `os.replace`，绝不半截写；滚动 `.bak` 保留最近 5 份 |
| **`deskmate/skills/seeds/office/office-word.md`**（改） | 交付步改调 `office_safe_deliver`；Shell 纪律补 zsh 双陷阱（`[N]` 路径须单引号、`()` 值须引号） |
| **`deskmate/skills/seeds/office/office-excel.md`**（改） | 同上 |
| **`deskmate/skills/seeds/office/office-pptx.md`**（改） | 同上 |
| **`scripts/deploy_cloud_sync.sh`**（改） | 新增 `SKILLS_TOOLS_MODULES`，把 `office_safe_deliver.py` 纳入云端同步清单（传输 3c + md5 核对 7 + 预检 ALL_FILES） |
| **`tests/test_office_safe_deliver.py`**（新增） | 7 个单测：protect 建/.bak 滚动/noop、deliver 原子替换/拒空候选/无候选仅保护、restore |
| **`tests/test_office_skills_runtime_wiring.py`**（新增） | 2 个单测：office 三技能入运行时索引 + 被「做 PPT/表格/Word」请求命中（P2 接进回归护栏） |

**验证**：办公相关测试 14 passed（deliver 7 + wiring 2 + seed 5）；全量技能回归 98 零破坏。
