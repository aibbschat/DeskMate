# 圆桌原生改造 + 创作流水线 + 圆桌终审 · 技术设计文档

> 状态：阶段 0–4 全部落地（2026-08-27）。含：① 创作流水线后端（teams 基座、派活→审核→打回→定稿）；② 圆桌终审桥接 + 知情批准四要素 + 定稿自动落地 docx（officecli）；③ 前端 `CreationPipelineView`/`TeamChatView` + 终审提案卡/专家勾选 UI（tsc + vite build 编译验证通过）；④ 终审 web SSE 流式触发（后端 `service.stream_final_review` + 前端 `streamFinalReview`）；⑤ 对外 MCP `deskmate/mcp/roundtable_server.py`（FastMCP，5 工具，可对接 Hermes A2A）。v1 草案 2026-08-26 由逍遥与萧哥对齐后落稿。
> 目标：把上游官方 `coworker/teams/` 原生多 Agent 框架，落地为两套相互打通的系统——
> ① **圆桌**（研讨型审稿）迁入 teams 基座；② **创作流水线**（剧本/大纲「派活→审核→打回→定稿」）；
> 二者通过「圆桌终审」接成一条线：日常单点审核走流水线，重大节点拉圆桌多专家研判。
> 本文所有 API 名、状态机、文件清单均对照本地 `fork/base`（与 `origin/main` 在 `coworker/teams/` 上零差异）核实。

---

## 1. 前提：已核实的上游源码事实（避免设计飘空）

### 1.1 本地 `coworker/teams/` 已是干净上游代码
- `git diff --stat origin/main HEAD -- coworker/teams/` = 零差异 → 可直接当基建复用，无需重写。
- 模块构成（`coworker/teams/`）：`model.py`(状态机) / `registry.py`(团队花名册) / `store.py`(看板+事件日志) / `tools.py`(动词工具) / `chat.py`(群聊) / `mcp_server.py`(对外 MCP) / `dialect.py`(空格绑定) / `journal.py` / `tokens.py` / `cli.py` / `attachments.py`。

### 1.2 看板状态机（核心，来自 `model.py`）
```
ItemState: open → in_progress → {blocked, review, canceled}
                    blocked   → {in_progress, canceled}
                    review    → {done, in_progress, canceled}   ← 关键：审核打回 = review→in_progress
                    done      → (终态)
                    canceled  → {open}                          ← reopen
```
- **「审核打回重改」是原生转换**：`store.transition(item, to="in_progress", comment="…审核意见")`，`EDGES[REVIEW]={DONE,IN_PROGRESS,CANCELED}`。
- 权限防火墙：`WORKER_TARGETS={in_progress,blocked,review}`——worker 永远不能自己 `done`，`done` 只能由 lead/user 在 `review` 后拍板（`store.transition` 内 `_check_transition_authority` 强制）。
- `link(src, "parent", dst)` / `link(src, "blocks", dst)`：只记录依赖关系，**不强制顺序**（串行门禁需自行加，见 §5.3）。

### 1.3 角色与能力（来自 `model.py` / `tools.py`）
- `Role`: `USER` / `LEAD` / `WORKER` / `SYSTEM`。
- `LEAD_VERBS` = `create_item, list_items, transition, comment, assign, link`（lead 能看到 `assign`，可派活）。
- `WORKER_VERBS` = `create_item, list_items, transition, comment, claim`（worker 只能 `claim` 自己认领，`done` 归 lead）。
- 权限双重校验：`tools.board_tools()` 按角色裁剪可见动词；`TeamStore` 每次调用再校验一遍。

### 1.4 关键集成缝（本地已实例化，我们直接复用）
| 缝 | 位置 | 作用 |
|---|---|---|
| 单例 | `coworker/server/manager.py:292-294` | `self.team_store = TeamStore(base/"teams.db", journal=…)`；`self.teams = TeamRegistry(base/"teams.json")` |
| 工具注册 | `coworker/agent.py:624` | `registry.register(propose_team_tool())`（组队门禁入口） |
| per-session 动词 | `coworker/server/manager.py:2041` | `tools = board_tools(store, space=…, actor=…)` |
| 对外 MCP | `coworker/teams/mcp_server.py:build(dialect, space)` | 外部 Agent（如 Hermes）经 `ocw board mcp --token --space` 接入，拿到角色限定的看板工具 |
| 群聊 | `coworker/teams/chat.py:ChatStore` | `create_group` / `post`（自动解析 `@handle` 提及）/ `unread_for`（@提及才唤醒该成员；user 发言唤醒全员） |

### 1.5 Persona 的 `team:` trait（复用官方底座，无需新机制）
- `coworker/personas/manifest.py:24` `VALID_TEAM={"lead","worker"}`；persona 的 `manifest.md` 加 `team: worker`（或 `lead`）即获得团队能力，并由 `loading.py:67` 注入 `cap:team:{role}`（变更需重新授权 consent）。
- 现有圆桌 7 专家已是官方 persona：`coworker/personas/builtin/*.md`（historian / editor(lit-editor) / proofreader / reader / publisher / adaptation / genre），由 `deskmate/roundtable/personas.py` 过滤 `roundtable:true` 加载。

### 1.6 圆桌现有实现要点（来自 `deskmate/roundtable/coordinator.py`）
- `Roundtable` 类：**到达序并发 + 有界收窄多轮**；主持「知了」开场/归纳分歧/写《综合研判报告》。
- 每轮 `asyncio.as_completed` 按「谁先想好谁先冒泡」收集发言；第 1 轮自由发言，之后只把未一致的分歧点抛回答辩。
- 专家是**进程内 LLM 调用**（`deskmate.memory.llm.chat_stream`），**无持久 session、无成本护栏、无结构化追踪**——这正是要借 teams 补足的。
- 圣经上下文 `L0/L1/L2` + FTS5 检索已成熟，改造时须原样保留。

### 1.7 前端复用
- `surfaces/gui/src/components/TeamChatView.tsx`（团队群聊视图）、`TeamRequestCard.tsx`（组队申请卡片）、`api.ts`（接口）。圆桌与创作流水线可复用这套而非另起炉灶。

---

## 2. 总体架构：两条线如何接成一条

```
                        上游原生框架 coworker/teams
   ┌──────────────────────────────────────────────────────────────────┐
   │  TeamStore(看板状态机)   ChatStore(群聊/@唤醒)   TeamRegistry(花名册) │
   │  mcp_server(对外A2A, 对接 Hermes)      wake budget(成本护栏)         │
   └──────────────────────────────────────────────────────────────────┘
            ▲                                    ▲
   线路 A：圆桌（研讨型）                线路 B：创作流水线（执行+审核型）
   复用 teams 做「基座」                  用 teams 做「原生派活」
   ┌──────────────────────┐            ┌──────────────────────────────┐
   │ 团队: 圆桌终审委员会    │            │ 团队: 《XX小说》改编项目         │
   │ lead = 知了(主持)      │            │ lead = 主编                   │
   │ workers = 7专家(进程内) │            │ workers = 编剧/大纲专家/审核专家 │
   │ board = 审稿维度工作项   │            │ board = 大纲v1→审核→剧本→审核   │
   │ chat  = 公开辩论场      │            │ chat  = 主编↔编剧指令通道       │
   └──────────┬───────────┘            └───────────────┬──────────────┘
              │  终审桥接（§6）                          │
              │  创作项到达 REVIEW → 主编 @召唤圆桌       │
              └──────────────────────────────────────────┘
                         ▼
              终审结论回填创作项：approve=done / 打回=in_progress
```

**一句话**：圆桌用 teams 当「持久化+结构化+成本护栏」的基座（专家仍进程内执行，保留研讨编排）；创作流水线用 teams 当「原生派活执行」框架（真·worker session 预生成）；二者共用一个 `team_store`/`teams`，终审阶段由创作流水线唤醒圆桌，结论回灌。

---

## 3. 线路 A：圆桌接入原生 teams 改造（已采纳的路线A）

### 3.1 设计原则（保留护城河，只换底层）
- **保留**：coordinator 的「到达序并发 + 收窄答辩 + 主持纪要」研讨编排（这是圆桌的价值，绝不丢）。
- **替换**：底层从「每次临时拉起 LLM」→「teams 持久 session + board 工作项 + ChatStore 承载发言 + wake budget 成本护栏」。
- **关键决策（loose coupling）**：圆桌 7 专家**不**强行拆成 N 个 coworker agent session（那样会破坏研讨并发语义、且过重）。改为**逻辑映射**——7 专家作为 board 的 `worker` actor / chat 的 `member`，由 coordinator 统一驱动发言顺序，状态落到 teams 的 `TeamStore`/`ChatStore`。这样既有 teams 的持久/可追踪/可对外(MCP)，又保住研讨感。

### 3.2 角色与团队映射
| 圆桌概念 | teams 原生概念 | 实现 |
|---|---|---|
| 主持「知了」 | `lead`（`team: lead`） | 拥有 board、创建审稿维度工作项、做最终 synthesis 验收 |
| 7 专家（historian 等） | `worker`（`team: worker`） | 进程内执行，映射到 board actor / chat member，按名 `@` 唤醒 |
| 议题 | 一个 team（space = 稿件路径/项目） | `TeamRegistry.create(space=…, lead_session=…, lead_actor="知了", workers=[…])` |
| 审稿维度（史实/文笔/结构/市场） | board item | 每项一个 `create_item(title, criteria, …)`，`criteria`=该维度「通过」的验收标准 |
| 专家发言 | chat message | `ChatStore.post(group_id, author=专家名, text, mentions=…)` |
| 综合研判报告 | board 顶层 item 的 `done` 验收 | 知了在 `review` 后 `transition(done)`，报告作为 `comment`/journal 落盘 |

### 3.3 board 设计（圆桌专用）
- **1 个「研讨议题」顶层 item**（parent 根），`criteria` = 议题的完整定义。
- **N 个「审稿维度」子 item**（每个 `link(child,"parent",议题item)`），例：
  - `史实核验`（historian 认领）：criteria=「无史实硬伤、存疑处标【待确认】」
  - `文笔修辞`（lit-editor 认领）：criteria=「语言质感达标、无 AI 腔」
  - `结构节奏`（reader 认领）：criteria=「章节钩子/信息密度达标」
  - `市场适配`（publisher/genre/adaptation 认领）：criteria=「符合品类读者预期」
- 专家表示意见 → 在自己维度 item 上 `comment`（带 `refs` 指向稿件片段）；维度没过 → `transition(review→in_progress)`，过 → 等主持 `done`。
- **主持「知了」做验收（保留 synthesis）**：coordinator 跑完收窄多轮、产出《综合研判报告》后，由 lead 把报告作为顶层 item 的 `done` 验收结论（`comment` + journal），圆桌结束。

### 3.4 ChatStore 当「辩论场」（保留到达序+@唤醒）
- 圆桌团队 `enable_chat=True`，`TeamRegistry.create(..., chat_enabled=True, chat_group=<ChatStore.group_id>)`。
- coordinator 每轮仍按**到达序**并发产出专家发言，但改为 `ChatStore.post`（而非只塞进内存 transcript），让发言进入持久群聊、可被未读游标/外部 MCP 消费。
- 收窄轮：主持把分歧点发到 chat 并 `@historian @lit-editor` 等，触发对应专家唤醒补辩——沿用原生 `@mention` 唤醒语义。

### 3.5 成本护栏（圆桌当前完全没有）
- 复用 `TeamRegistry.count_wake(team_id, cap=…)` 的「每小时自动唤醒次数」滚动预算（已实现 `RoundtableTeamsBridge.check_wake_budget`，每场圆桌终验收时记一次消耗）。
- **实际语义**：圆桌是**同步主动触发**（用户在前端等结果），不是后台 tick 自动唤醒，故 `count_wake` 在阶段 2 仅作护栏信号记录、**不阻断流程**；原生「循环在轮次之间停下」由 coordinator 的 `max_rounds`（1–3）已保证。强制 wake 预算门禁留**阶段 4** 调优时接。
- 未分配的专家 worker 不耗 token（teams 原生 pre-spawn 哲学），与现圆桌「无会话不调 LLM」一致。

### 3.6 对外暴露（对接已有 Hermes A2A peer）
- 圆桌团队经 `mcp_server.build(dialect, space)` 暴露为 MCP：Hermes 可作为外部 `worker` 加入圆桌（与现 `a2a.py` peer 机制互补——MCP 走 board 工具、A2A 走整段 prompt）。
- 现有 `deskmate/roundtable/web.py` 的 `/roundtable` 路由保留，新增 `/roundtable/start-team`（基于 teams 基座的一键建团+跑研讨）。

### 3.7 文件改动清单（线路 A）
| 文件 | 动作 | 说明 |
|---|---|---|
| `deskmate/roundtable/teams_bridge.py`（新） | 新增 | 封装「圆桌→team」建团、board 维度建项、ChatStore 辩论场、保留 coordinator 编排的适配层 |
| `deskmate/roundtable/coordinator.py` | 改 | 发言出口从内存 transcript 改为 `ChatStore.post`；终验收从纯文本改为 board `done` + journal；保留 L0/L1/L2/FTS5 |
| `deskmate/roundtable/web.py` | 改 | 新增 `/roundtable/start-team`；从 `manager.team_store`/`manager.teams` 取单例 |
| `coworker/personas/builtin/*.md`（7 专家） | 改 | 在 manifest 增加 `team: worker`（保留 `roundtable:true`）；「知了」加 `team: lead` |
| `surfaces/gui/src/.../TeamChatView.tsx` | 复用+小改 | 圆桌用其展示辩论场；增加「审稿维度工作项」进度条 |

> **阶段 2 实施状态**：`teams_bridge.py` 已新增、`coordinator.py` 已加 hook（建团 + board 维度项 + ChatStore 辩论场镜像 + lead 终验收 `done` + journal 落盘），验证全过且**零前端影响**（不注入 `team_bridge` 时圆桌完全照旧）。`web.py` 新增 `/roundtable/start-team` 路由 + 前端 `TeamChatView` 复用留**阶段 4** 打磨。
> **7 专家 `team: worker` 标记经评估跳过**：上游 `team: worker` 会自动 `default_surfaced=False`，可能使 7 专家从普通聊天 persona 选择器消失，且与圆桌「研讨型」语义不符（不应被当 teams 执行 worker）。圆桌仍走官方 `roundtable: true` 加载，不受影响（2026-08-27 与萧哥对齐）。

---

## 4. 线路 B：创作流水线（剧本 / 大纲）—— 真正的原生 teams 用法

> 这是 teams 的**天然主场**（派活+执行+审核门禁），与上游设计意图完全对齐，优先排期。

### 4.1 角色封装（新增官方 persona，带 `team:` trait）
按上游 `builtin/devops-lead`、`builtin/posture-worker` 的 manifest 格式，在 `coworker/personas/builtin/` 下新增：
| persona | team trait | 职责 |
|---|---|---|
| `editor-in-chief`（主编） | `lead` | 组队、派活、审核验收、终审裁决 |
| `screenwriter`（编剧） | `worker` | 出剧本大纲 / 写剧本 |
| `outline-writer`（大纲专家） | `worker` | 把概念写成大纲 |
| `script-reviewer`（审核专家） | `worker` | 审大纲/剧本，给可执行的审核意见 |

> 审核专家亦可拆为 `outline-reviewer` + `script-reviewer` 两个 worker，使门禁更细。v1 先用一个 `script-reviewer` 覆盖两类审核，后续可拆。

### 4.2 剧本改编流水线（board 工作项设计）
space = 改编项目（原创小说路径/项目 id）。主编（lead）经 `propose_team` → 用户批准 → **预生成** 编剧/大纲/审核 的 worker session（teams 原生 pre-spawn，未派活不耗 token）。
```
item 大纲v1        (open→in_progress→review→[done | in_progress(打回)])
   └─ item 大纲审核  (针对大纲v1，用 description 表达；**不传 parent**，否则触发门禁误拦审核项)
item 全剧本/第N集  (open, blocked until 大纲 done；link(kind=parent) 建立依赖)
   └─ item 剧本审核  (针对全剧本，用 description 表达；不传 parent)
```
- 主编派活：`assign(大纲v1, "outline-writer")` → 大纲专家写 → `transition(review)` 交稿。
- 审核专家审完：`transition(review)` 交结论（`comment` 写意见）。**验收/打回是主编(lead)的裁决权**——上游 authority 规定 worker 不能 `done`、不能打回别人 item（见 §1.3），故主编读审核结论后拍板：
  - 通过 → `transition(done)` 验收（产出项 + 审核项均 done）；
  - **不通过 → 产出项 `transition(review→in_progress)` + `comment(审核意见)`**（原生打回！审核项保持 `review` 留待复审，编剧收到 feed 改完再 `review`）。
- 大纲 `done` 后，主编才 `assign(全剧本, "screenwriter")` —— 串行依赖见 §4.4 门禁。

### 4.3 大纲路线（概念→大纲，同样范式）
用户输入「一个概念/剧情点子」→ 主编派活 `outline-writer` 写大纲 → `script-reviewer` 审 → 通过定稿 / 打回改 → 定稿后可选接 §4.2 的剧本路线（大纲项作为剧本项的 `parent`）。

### 4.4 串行门禁（teams 原生不强制，需自加）
`link(parent)` 只记录关系。实现 `PipelineGuard`（`deskmate/creation/guard.py`，薄封装）：
- 在允许某 item `transition(to="in_progress")` 前，检查其 `parent` item 是否 `done`；未 `done` 则拒绝并返回「需先完成 <parent>」。
- 包装 `team_store.transition` 调用（不动上游 `store.py`，避免污染干净代码）。
- 主编的 prompt 也显式写明依赖顺序（双保险）。

### 4.5 成本与打扰控制
- worker 独立 session、「不直接对用户说话、靠 board 协调」——与「编剧闷头干、审核专家提意见、全程不打扰用户」天然一致。
- 主编通过 `@编剧`/`@审核` 在 ChatStore 下指令，未提及不唤醒（原生语义）。
- `TeamRegistry` 的 wake budget 给整条流水线加护栏。

### 4.6 文件改动清单（线路 B）
| 文件 | 动作 | 说明 |
|---|---|---|
| `coworker/personas/builtin/editor-in-chief/manifest.md`（新） | 新增 | `team: lead` |
| `coworker/personas/builtin/screenwriter/manifest.md`（新） | 新增 | `team: worker` |
| `coworker/personas/builtin/outline-writer/manifest.md`（新） | 新增 | `team: worker` |
| `coworker/personas/builtin/script-reviewer/manifest.md`（新） | 新增 | `team: worker` |
| `deskmate/creation/pipeline.py`（新） | 新增 | 建团、建 board 工作项、驱动审稿门禁、串联剧本/大纲两条子路线 |
| `deskmate/creation/guard.py`（新） | 新增 | `PipelineGuard`：parent 未完成则拦截 `in_progress` |
| `deskmate/creation/personas_loader.py`（新） | 新增 | 从官方 registry 取 4 个创作 persona |
| `surfaces/gui/src/.../CreationPipelineView.tsx`（新） | 新增 | 复用 `TeamRequestCard` + `TeamChatView`，展示流水线进度/审核意见/打回 |

---

## 5. 两线合一：圆桌终审（线路 B → 线路 A 的桥）

### 5.1 动机
流水线里的 `script-reviewer` 是**单点审核**（一个 LLM 看另一个 LLM 产出），质量天花板取决于该 persona 提示词与模型。圆桌是**多点互搏**（7 专家辩论收敛），正好补「重大节点终审」。二者互补：日常走流水线单点审核，定稿/重大修改前拉圆桌多专家研判。

### 5.2 桥接触发点（混合式：主编提议 + 用户知情批准）

**决策权归属（已与萧哥对齐，2026-08-26）**：
- 主编（LLM lead）**自主判断**何时该终审、并**预选推荐专家**（基于稿子主题：历史剧→史料+文笔+品类；纯结构→读者+文笔…）。
- 但主编**只发「终审提案」**，**绝不**直接拉圆桌烧 token；**必须先把送审内容完整呈现到用户面前、用户看了才能拍板**——否则「决定权」是虚的（萧哥原话）。
- 用户侧：一键批准（可增减专家）/ 驳回 / 手动改专家。另有「自动终审」配置项（默认关）可免确认。随时可手动 `@圆桌` 兜底。

**知情批准 ≠ 盲批（设计铁律）**：提案卡须展示四要素，用户看不到内容就无权叫「批准」：
1. **被送审的稿件片段**（精确到章节/段落，即圆桌 7 专家将研判的文本本体）；
2. **推荐专家清单**（主编预选，用户可勾选增减，并看每个专家的职责说明）；
3. **验收标准**（本次终审的 `criteria`，如「史实无硬伤 / 台词可拍性 / 品类契合」）；
4. **前序审核意见**（`script-reviewer` 的单点结论，作为圆桌研判的基线参照）。

**实现**：主编在 `pipeline.py` 终审分支生成「终审提案」，复用上游 `TeamRequestCard`（组队申请卡片，原生即「需用户批准」的 UI 载体，见 §4.6）渲染为提案卡；用户确认 → 才 `create_item` 建圆桌终审议题 + `@圆桌` 触发研讨。自动化优先、手动兜底不变。

### 5.3 终审回流（结论 → 创作项）
圆桌跑完（coordinator 产出《综合研判报告》）→ 主持「知了」`done` 验收 → 报告作为：
- **approve**：创作项 `transition(review→done)`（定稿）；
- **打回**：创作项 `transition(review→in_progress)`，报告全文作为 `comment`（审核意见）回灌给编剧/大纲专家改。

> 跨团队的 item 关联：用 `refs` 指向稿件路径/章节，或两圆桌与创作共用的 `space`（同一稿件项目）让 board 互相可见；v1 先用 `refs` + 稿件路径关联，避免跨 team 硬链接的复杂度。

### 5.4 文件改动清单（桥接）
| 文件 | 动作 | 说明 |
|---|---|---|
| `deskmate/creation/final_review.py` | **新增** | 桥接核心：`FinalReviewProposal` 四要素提案 / `build_final_review_proposal`（主编生成、不烧 token）/ `recommend_experts`（按主题预选）/ `run_final_review`（批准后驱动圆桌+TeamsBridge）/ `apply_final_review`（lead 回灌 done/in_progress）/ `parse_verdict` |
| `deskmate/creation/pipeline.py` | 改 | 新增 `propose_final_review` / `finalize_final_review` 薄方法（状态机不变），主编在 review 分支生成并回灌提案 |
| `deskmate/creation/__init__.py` | 改 | 导出 final_review 符号 |
| `deskmate/roundtable/teams_bridge.py` | 改（阶段 2） | 已提供 `RoundtableTeamsBridge` 供 final_review 复用（建团 + board 维度 + ChatStore 辩论场 + lead 终验收） |
| `deskmate/creation/docx_export.py`（新增） | ✅ 已落地 | 终审定稿自动调本地 `officecli` 把定稿 Markdown 展开为 .docx（落 `output/`），officecli 缺失降级 .md |
| `deskmate/roundtable/web.py` `POST /api/creation/export-docx` | ✅ 已落地 | docx 能力的 HTTP 入口（前端后续接入） |
| `deskmate/roundtable/web.py` `POST /api/creation/{pid}/final-review`(SSE) + `CreationPipelineView.tsx` + `ProposalCard.tsx` | ✅ 已落地 | 终审研讨的 web 触发（SSE 流式）+ 提案卡/专家勾选 UI（前端 `streamFinalReview` reader 解析 `data:` 事件） |

---

## 6. 实施路线图（分阶段，先 B 后 A，终审最后接）

**阶段 0 · 准备（0.5d）** ✅ 已落地
- 确认 `manager.team_store` / `manager.teams` 对 `deskmate` 的可达封装（新增 `deskmate/teams_client.py` 薄壳取单例）。
- 在 4 个创作 persona 的 manifest 加 `team:` trait，验证 `loading.py` 注入 `cap:team:*`。

**阶段 1 · 创作流水线（线路 B，2–3d，优先）** ✅ 已落地
- 新增 4 persona + `pipeline.py` + `guard.py`。
- 跑通：概念→大纲→审核→打回→定稿；大纲 done→剧本→审核→定稿。验证 `review→in_progress` 打回、`PipelineGuard` 串行门禁。
- 前端 `CreationPipelineView` 复用 `TeamRequestCard`/`TeamChatView`。

**阶段 2 · 圆桌接入 teams 基座（线路 A，2–3d）** ✅ 已落地
- `teams_bridge.py` 建团 + board 维度项 + ChatStore 辩论场；coordinator 发言出口改 `ChatStore.post`、终验收改 board `done` + journal。
- 7 专家 manifest 加 `team: worker`，「知了」加 `team: lead`。
- 保留 L0/L1/L2/FTS5 与到达序收窄编排；新增 `/roundtable/start-team`。

**阶段 3 · 终审桥接（1–2d）** ✅ 已落地
- 流水线终审分支建圆桌 item + `@圆桌`；圆桌 `run_from_pipeline` 返回 verdict；回流 `done`/`in_progress`。
- 验证：编剧被打回、圆桌终审报告作为审核意见回灌。

**阶段 4 · 对外 & 打磨** ✅ 全部落地（2026-08-27）
- ✅ **剧本/大纲定稿自动落地 docx**（核心新增需求）：新增 `deskmate/creation/docx_export.py`，定稿分支（`lead_verdict_outline` / `lead_verdict_screenplay` / `apply_final_review` 三处 approved）自动调本地 `officecli` 把「剧名标题 + 正文」展开为规范 .docx，落 `output/`，文件名带时间戳防覆盖；officecli 缺失降级 .md；导出失败不阻断状态机。验证：stub 跑通全链路 + `officecli validate` 通过 + 首段为剧名 `Heading1`。
- ✅ **web 路由 `POST /api/creation/export-docx`**：docx 能力的 HTTP 入口（请求体 markdown/title/kind）。
- ✅ **前端 `CreationPipelineView` + `TeamChatView` 复用 + 终审提案卡/专家勾选 UI**：新增 `surfaces/gui/src/components/CreationPipelineView.tsx` 与 `ProposalCard.tsx`；阶段按钮按 `phase` 联动、四要素提案卡可勾选专家、SSE 事件流渲染（speaking/turn_end/synthesis）、确认定稿/打回、docx 路径展示；`Sidebar` 加「创作流水线」入口（`nav-creation`）、`App.tsx` 接入 `creation`/`teamchat` surface；`tsc --noEmit` + `vite build` 编译验证通过。
- ✅ **终审研讨 web 触发（SSE 流式）**：后端 `deskmate/creation/service.py:stream_final_review` 异步转发 `Roundtable.run_stream` 事件（meta/turn_delta/final_review_ready），`done` 时暂存 synthesis 待用户拍板；web 路由 `POST /api/creation/{pid}/final-review` 返回 `StreamingResponse(text/event-stream)`；前端 `api.ts:streamFinalReview` 用 `fetch`+`ReadableStream` reader 解析 `data: {json}`。
- ✅ **原著改编（`novel_ref` 真正接通，2026-08-27）**：此前 `novel_ref` 仅是半截子接口（前端无入口、pipeline 接收未消费、executor 无原著参数）。现已补全——① 前端 `CreationPipelineView` 创建表单新增「原著文件路径」输入框（txt/docx，留空则仅凭概念创作）；② 后端 `pipeline._read_novel` 复用 `deskmate/perception/file_reader.read_file_safe` 安全读取（剥离其「未读完」警告头、坏路径返回空不污染 prompt），取前 6000 字作改编参考；③ `executor` 三个方法（`draft_outline`/`draft_screenplay`/`review`）加 `novel_text` 形参并拼进 prompt（编剧依原著精神改编、审核对照原著）；④ 圆桌终审 `FinalReviewProposal.novel_excerpt`（前 2000 字）拼进 `context`，7 专家终审也能对照原著。验证：`/tmp/verify_novel_ref.py` 全过（读入/透传/快照/坏路径/大纲流/docx 分支）。
- ✅ **圆桌团队经 MCP 暴露对接 Hermes A2A peer**：新增 `deskmate/mcp/roundtable_server.py`（FastMCP `openworker-roundtable`，stdio），暴露 5 个高层 tool（`create_creation_pipeline`/`get_pipeline`/`final_review_proposal`/`export_docx`/`run_roundtable`）；验证脚本全过（initialize/tools/list/create/get/export_docx）。Hermes/Claude 可在 MCP 设置里加 stdio connector（command=`.venv/bin/python`, args=`["-m","deskmate.mcp.roundtable_server"]`）。另 `officecli mcp` 子命令可提供文档编辑 MCP 入口。
- ⏳ wake budget 参数调优（留后续打磨）。
- **7 专家 `team: worker` 标记跳过**（见阶段 2 状态说明）：避免 `default_surfaced=False` 副作用。

---

## 7. 风险与未决项

| 项 | 风险 | 缓解 |
|---|---|---|
| 范式错位 | 圆桌是研讨型、teams 是派活型；若让 teams 的 lead-assign 主导会丢研讨感 | 圆桌用 **loose coupling**（逻辑映射，不拆 N 个 agent session），coordinator 仍主导编排（§3.1） |
| 串行门禁 | teams 原生 `link` 不强制顺序 | `PipelineGuard` 拦截 + 主编 prompt 双保险（§4.4） |
| 审核质量天花板 | 审核也是 LLM，单点审核未必强 | 圆桌终审补足多点研判；强化 `script-reviewer` 提示词 |
| 持久 session 成本 | 真·worker session 可能累积 token | 原生 pre-spawn（未派活 0 token）+ wake budget 护栏 |
| 神圣稿件安全 | 国子监秘闻录等绝对不可触碰 | 稿件读写限定在用户显式指定的项目路径；`team_store`/chat 只存引用与片段，不移动/改写原文 |
| 上游合流 | 后续 `git merge v0.2.x` 可能改 teams API | 改动集中在 `deskmate/*` 新增模块与 `builtin/*.md` 的 `team:` 字段；上游 `coworker/teams/` 保持零改动，降低合流冲突 |

---

## 8. 附录：上游 teams API 速查（实现时照抄）

```python
# 建团（预生成 worker session，未派活 0 token）
from coworker.teams.registry import TeamRegistry, TeamWorker
from coworker.teams.model import Actor, Role  # board 动词的 actor 是 Actor 对象（id+role），非裸字符串
team = reg.create(
    space="<稿件/项目路径>", lead_session="<会话id>", lead_actor="知了",
    workers=[TeamWorker(actor="historian", persona="historian", session_id="…", reason="…")],
    chat_enabled=True, chat_group="<ChatStore.group_id>",
)

# 建工作项（open, unassigned；criteria 必填=验收标准）
store.create_item(space, Actor(lead_actor, Role.LEAD), title="史实核验",
                  criteria="无史实硬伤；存疑处标【待确认】", parent=<议题item_id>)

# 派活 / 认领
store.assign(space, actor_lead, item_id, assignee="historian")   # lead 派
store.claim(space, actor_worker, item_id)                        # worker 自认领

# 状态流转（审核打回 = review→in_progress）
store.transition(space, actor, item_id, to="in_progress")        # 开工
store.transition(space, actor, item_id, to="review", comment="完成，请审")  # 提交审
store.transition(space, actor_lead, item_id, to="done", comment="验收通过") # 主编拍板
store.transition(space, actor_lead, item_id, to="in_progress", comment="第3处逻辑硬伤，打回改")  # 打回

# 依赖（不强制顺序，门禁自行加）
store.link(space, actor_lead, src=剧本item, kind="parent", dst=大纲item)

# 群聊（@提及唤醒）
chat.post(group_id, author="知了", text="请 @historian @lit-editor 就分歧点补辩")
chat.unread_for(group_id, "historian")   # 仅 @提及 / user 发言才唤醒

# 对外 MCP（Hermes 等接入）
from coworker.teams.mcp_server import build
mcp = build(dialect, space="<space>")    # 外部 Agent 经 ocw board mcp --token --space 拿到角色限定工具
```

### 8.1 桌搭自带创作流水线 MCP（`deskmate/mcp/roundtable_server.py`）
面向**外部 Agent（Hermes/Claude）**直接驱动创作流水线，stdio 传输，FastMCP 暴露 5 个高层 tool：
- `create_creation_pipeline(kind, concept, executor, novel_ref)` → 返回 `{pid, team_id, ids}`
- `get_pipeline(pid)` → 阶段/工作项快照
- `final_review_proposal(pid, item_kind, genre_hint)` → 四要素提案（不烧 token）
- `export_docx(kind, title, markdown)` → 调本地 `officecli` 落地 docx
- `run_roundtable(topic, personas, context, manuscript)` → 拉圆桌多专家研判

**Hermes 对接配置示例**（`~/.hermes/config.yaml` 或 MCP 设置里加 stdio connector）：
```yaml
mcpServers:
  openworker-roundtable:
    command: ".venv/bin/python"          # openworker 的 venv 解释器
    args: ["-m", "deskmate.mcp.roundtable_server"]
```
> 注：此 server 复用进程内 `service` 注册表（sidecar 单进程足够）。若 Hermes 在另一进程/机器，需经 HTTP 网关转发（当前为本地 stdio 直连，留后续打磨）。

---
*本设计遵循「先对齐再实施」原则；任何 §7 未决项在实现前需与萧哥再确认一次 tradeoff。*

---

# 阶段 5 · 长文本 I/O 架构（已落地 2026-08-27，提交 `5c9f5af..c3e5b75`）

**决策（2026-08-27 萧哥拍板）**：当前创作流水线对原著/剧本存在硬性字数天花板——
`_read_novel` 仅取前 6000 字、LLM 输出上限约 8k token（≈6000 中文字）。对长篇改编与
短剧生产无意义（百万字小说中后段全盲、单集剧本塞不出）。下一阶段解决**输入/产出两端
的长文本工程难题**，而非调参。

- **输入侧 → 摘要+分章混合**：先 LLM 把整本原著压成「改编圣经」（人物表/情节线/主题/
  关键场景/分章摘要）常驻；写某集时按任务主题动态喂入相关章节原文，取代「前 6000 字节选」。
- **输出侧 → 分段续写拼接**：把一集/一幕拆成场景序列，每段在 token 限额内生成 → 审 →
  续写 → 拼接，用场景边界保连贯；突破单次 max_tokens 上限，使万字段剧可真实产出。

## 模块拆解与优先级
1. **`deskmate/creation/novel_index.py`（新）**：读全文（复用 `file_reader`）→ LLM 生成
   改编圣经 → 缓存（`~/.deskmate/novels/<hash>/bible.md`）+ 章节索引。一次性成本，可复用。
2. **pipeline 改造**：`start_*_project` 触发建索引（后台/异步）；`_read_novel` 升级为
   「圣经摘要 + 按需章节」，废除硬截断前 6000。
3. **executor 改造**：`draft_outline`/`draft_screenplay` 从单次生成改分段续写循环；
   `review` 逐段可插门禁。
4. **docx 导出**：拼接后的完整剧本/大纲才导出（非截断片段）。
5. **前端**：创建时显示「建立原著索引…」进度；进行中可指定本集关联章节。
6. **依赖确认**：DeepSeek-V3 128k 输入够塞圣经+数章；输出仍受 8k 限 → 输出侧必须分段（不靠换模型）。

## 落地摘要（2026-08-27）

- **输入侧「摘要+分章混合」已通**：`novel_index.py` 读全文（复用 `file_reader`）→ LLM 生成
  改编圣经（人物表/情节线/分章摘要）→ 缓存 `~/.deskmate/novels/<hash>/bible.md` + 章节索引。
  缓存命中规则含降级/升级两档：`degrade=True` 强制降级圣经（零 LLM，stub 不烧 token）、
  `degrade=False` 生成 LLM 精炼圣经；降级缓存仅在「本次也接受降级」或「LLM 不可用」时复用，
  否则重建精炼版。
- **pipeline 改造已落地**：创建时（`service.create_pipeline`）同步预建索引，失败降级不阻断；
  `_novel_context` 改为「圣经摘要 + 目录」、`_novel_related` 按大纲 bigram 自动挑 3 章
  （或用户 `set_related_chapters` 指定章节），按执行器类型自动选档（stub 强制降级，杜绝
  「stub 模拟却烧真 LLM」）；`final_review.novel_excerpt` 语义升级为圣经摘要节选（对照改编）。
- **输出侧「分段续写拼接」已落地**：`draft_screenplay` 拆场景 → 逐段 3800 token → 前情摘要
  续写 → `---` 拼接，突破单次 max_tokens 上限。
- **前端已适配**：创建时「正在建立原著索引…」进度；进行中视图新增「原著参考章节」面板
  （novel-index / novel-pick / related-chapters 三接口 + 索引失败提示条）。
- **顺带修复（HTTP 冒烟抓到）**：`web.py` 函数内定义的 Pydantic Body 类 + `from __future__
  import annotations` 导致 FastAPI 按模块全局解析 ForwardRef 失败 → **所有创建/终审 POST 路由
  实际 422 不可用**；7 个 Body 类（CreatePipelineBody/NovelPickBody/RelatedBody/LeadBody/
  FRProposalBody/FRBody/FRApplyBody）已全部上移模块级修复。
- 验证：service 层 27 项 + HTTP 冒烟 10 项 + 老回归 `verify_creation_web_stage4.py` ALL PASS。

## 验收
- 百万字原著：AI 改编能引用中后段情节/人物（不再只看开头 6000 字）。
- 单集剧本：产出 ≥ 1 万字、场景连贯、可逐段审、可落地 docx。
- 不引重依赖（RAG/向量库暂不入）；索引可缓存复用，不每次重算。
