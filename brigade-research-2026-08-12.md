# Brigade 项目调研报告（2026-08-12）

> 仓库：github.com/spinabot/brigade ｜ 调研目的：评估其是否代表未来 Agent 方向、有无桌面端、能否安装试玩

## 一、项目定位
- 出品：Spinabot（美国新泽西），MIT 许可，TypeScript / Node 22.12+。
- 简介：「Your personal intelligence, built enterprise-grade」——一个**自托管的 AI 智能体生态系统（ecosystem，不是单个 app）**。你拥有的不是一款软件，而是一支自己的「crew（机组）」。
- 形态：**终端优先（terminal-first，官方明确无浏览器、无 Electron）**，单二进制既是常驻 WebSocket 网关（daemon）也是瘦客户端。
- 版本：1.30.0（2026-08-09 发布）。
- 热度：2545 stars / 25 forks / 12 open issues；2026-06-19 创建（约 2 个月新项目）。GitHub topics 直接挂了 `hermes`、`hermes-agent`、`openclaw`、`moltbot`、`molty`、`clawdbot` —— 与「Hermes 范式」同属一个世界观。

## 二、核心能力（它宣称的「神」在哪）
1. **Tideline 长期记忆引擎**：typed link graph（带类型的链接图）+ 三层衰减 + 混合召回（BM25 + 向量）+ 每条事实带「来源(origin)」作用域，在**存储层**隔离不同 agent / 联系人的记忆（不是靠 prompt 约束）。
2. **Pride 组织图（多智能体）**：agent 按真实 org chart 排布，框架从层级**自动派生 A2A 策略**（谁能和谁说话）；spawn 扇出有深度/数量硬上限；杀父进程级联中止子进程。
3. **Carrow 跨模型连续性**：对话中途换模型（Claude→Gemini→本地 Ollama）不丢上下文，搬运完整 JSONL transcript、协商思考层级、剥掉下家会拒的推理块。
4. **所有权是一等公民**：工具按调用粒度有权限门（渠道 peer 能列 cron 但不能建）；密钥用 `${VAR}` 引用、运行时解析、绝不回写落盘；可选 AES-256-GCM 静态加密。
5. **自主循环 + 反 slop 质量门 + 人类门控自改进**。
6. **本地优先**：无遥测、无云组件；API key 只存本机 `~/.brigade/`（权限 0600）；网关默认绑 `127.0.0.1`；BYO-model（Anthropic/OpenAI/Gemini/Ollama/兼容端点）；可复用 Claude Code / Codex 本地登录，或 Claude/ChatGPT/Copilot 订阅（keyless）。
7. 其他：56 个内置技能、Composio 1000+ 应用连接器、WhatsApp/Telegram/Slack/Discord/iMessage 渠道、Cron 调度、文档/媒体读写、MCP、HyperFrames 视频渲染（可选依赖）。
- 工程质量信号：TypeScript strict（`noUncheckedIndexedAccess`/`noImplicitOverride`）、281 个测试文件用 node:test（不用 Jest）、双存储（文件系统 / 自托管 Convex）、公开扩展 SDK。

## 三、有没有桌面端？—— 没有
- 官方明确「终端优先，无浏览器、无 Electron」，没有独立桌面 GUI 应用。
- 客户端形态只有：
  - `brigade tui`：终端 TUI 聊天界面（自动拉起网关）；
  - `brigade connect`：从另一终端连到本地网关的瘦客户端；
  - 各种聊天渠道（WhatsApp/Telegram…）；
  - 通过 MCP 把 Brigade 智能体暴露给别处。
- 结论：在你的 Mac 上它就是**终端里用的东西**，不是像咱们桌搭那样的 Tauri 桌面 .app + GUI。这是它和桌搭在「形态」上最大的差异。

## 四、我的看法：它代表未来吗？
**先给结论：它代表的「方向」大概率是未来的一部分，但 Brigade 这个具体项目是不是「那个神」，还远没到盖棺定论。**

值得认真对待的点：
- 设计范式（本地优先、own-your-data、多智能体 crew + 真实共享记忆 + 组织图治理 + BYO-model）和咱们桌搭「倾斜 Hermes」选的那条路**高度同构**——它 GitHub topics 直接挂了 hermes/openclaw/moltbot，说明它就是这一脉最新的开源代表之一。这反过来**印证我们选的方向不是野路子，是有共识的**。
- 记忆、所有权、跨模型连续性这几个「真问题」它都给了工程上说得通的答法，不是 PPT。

需要降温的点（传说没那么神）：
- 才 2 个月、2545 star 但只有 25 fork——**围观的人多、真正在上面建的人少**，低 fork 比是黄灯。
- 几个招牌特性（pluggable memory backends、tts/stt/mediaGen）仍处「roadmap / 锁定契约」，「自改进」和媒体生成目前部分是愿景。
- **没有桌面 GUI**，对非终端用户是真实天花板（而这恰恰是桌搭 .app 补的位、也是写作者可能更想要的形态）。
- 它是「headless 网关 + 瘦客户端」架构，强，但意味着常年泡在终端里。
- 赛道正在高速收敛（Claude Code、Codex、OpenAI 方向），「范式对」不等于「Brigade 赢」。

一句话：把它当**这一波「个人 Agent 操作系统」里工程质量靠前的一个开源样本**来试，价值很高；当「装了就自动变神」的魔法盒，会失望。对你最实在的卖点是——一个**你完全拥有的、带持久记忆的研究/写作机组，无云无遥测**，且能**复用你现有的 Claude Code 登录**（keyless，这点很香）。代价是：终端里用，且得自己接模型后端。

## 五、怎么装来试（已验证可在你机器跑）
- 环境：Node 22.22.2（托管）/ 22.23.1，满足 22.12+ 要求。
- 我已在 `/tmp/brigade-try` 用托管 node 装好并验证：`Brigade 1.30.0` 可运行（640 个依赖包，约 1 分钟）。
- 试跑步骤（onboarding 需你本人选 provider / 绑凭证，故未替你跑）：
  1. `cd /tmp/brigade-try`
  2. `./node_modules/.bin/brigade onboard` —— 交互向导：选存储模式、供应商、模型；可复用 Claude Code/Codex 本地登录或 Claude/ChatGPT/Copilot 订阅，也可贴 API key（密钥只留本机 `~/.brigade/`，权限 0600，无遥测）。
  3. `./node_modules/.bin/brigade tui` —— 自动拉起网关并打开终端聊天。
- 正式全局装：`npm i -g @spinabot/brigade`（需 Node 22.12+）。官网推荐 `curl -fsSL https://brigade.spinabot.com/install.sh | sh` 会自动装 Node——为不动你的 node 环境，我特意没用它。
- 数据全在 `~/.brigade/`，纯本地；不满意直接删目录即可。
- 注意：`onboard`/`tui` 涉及绑定模型凭证，**需你本人操作**；我不会在没确认的情况下替你接 Claude key。

## 六、与咱们桌搭（DeskMate / OpenWorker）形态对照
| 维度 | Brigade | 桌搭 / DeskMate |
|---|---|---|
| 形态 | 终端优先 CLI + TUI，无 GUI | Tauri + Python 桌面 .app 带 GUI |
| 内核范式 | 多智能体 crew + 组织图(Pride) + Tideline 记忆 | persona + MCP + 圣经记忆 + 圆桌协调（倾斜 Hermes）|
| 长期记忆 | Tideline（typed graph + 衰减 + 混合召回）| 圣经 v2（可核验实体 + 分层注入）|
| 多 agent | 组织图 + A2A 策略，spawn 扇出 | 圆桌协调器（persona + 外部 peer）|
| 模型 | BYO（Anthropic/OpenAI/Gemini/Ollama）+ Carrow 跨模型连续 | DeepSeek + MiMo（记忆抽取走 DeepSeek 付费）|
| 部署 | 本地 headless 网关 + WS，可常驻(launchd/systemd) | 本地 OpenWorker.app |
| 连接器 | Composio 1000+ + 聊天渠道 | MCP 标准对齐 |
| 隐私 | 无遥测、密钥 0600、可选 AES-256-GCM | 本地 |
| 桌面 GUI | 无 | 有 |

## 七、能不能让桌搭当 Brigade 的客户端？（可行性，2026-08-12 晚追加）
**结论：能，有两条路。一条很容易、一条要费点劲。**

### 路 A（容易、推荐先试）：桌搭当 MCP 客户端，吃 Brigade 的"记忆"
- 我已核对 `brigade mcp` 源码（`dist/cli/commands/mcp-cmd.js`）：它就是个 **stdio 的 MCP server**，暴露 `add` / `search` / `context` 三个工具，README 原话"Point an MCP client (an editor, a desktop assistant, etc.) at the command `brigade mcp`"——**官方就是把"桌面助手当客户端"设计进去的**。
- 桌搭（`coworker/mcp/config.py`）本身就是标准 MCP Host，配置在 `~/.config/coworker/mcp.json`，`mcpServers` 格式与 Claude Desktop/Cursor 通用、`${VAR}` 可解析、支持 stdio。两者天然对插。
- 接入写法（加到全局 `mcp.json` 的 `mcpServers` 里）：
  ```json
  {
    "brigade-memory": {
      "command": "brigade",
      "args": ["mcp"],
      "type": "stdio"
    }
  }
  ```
  （`brigade` 需全局可用；当前我们只在 `/tmp/brigade-try` 装过，需先 `npm i -g @spinabot/brigade` 落到稳定位置。Brigade 需先 `onboard` 一次让其 `~/.brigade/` 存储就绪。）
- 这条路给桌搭带来的：Brigade 的 **Tideline 长期记忆引擎**作为桌搭可用的 `memory_add`/`memory_search`/`memory_context` 工具（README 简称 add/search/context）。桌搭保留自己的 GUI、persona、写作技能、Hermes 式学习闭环，**只把"记忆底座"换成 Brigade**。标准是 Brigade 自己宣传的用法，低风险。

### 路 B（更重、真正的"聊天客户端"）：桌搭 GUI 直连 Brigade 网关（WebSocket）
- Brigade 网关是常驻 WebSocket daemon（`brigade gateway run`，默认 `127.0.0.1:7777`）。`brigade tui` / `brigade connect` 本质就是连这个 WS 的瘦客户端。
- 桌搭的 Tauri/JS 前端若去连这个 WS、复用 Brigade 原生聊天协议，就能**让桌搭窗口成为 Brigade crew 的完整聊天前端**，等价于把 `brigade tui` 替换成桌搭 GUI。
- 代价：Brigade 的 WS 协议是内部协议、官方未文档化（仅 `brigade tui`/`connect` 自用），需读源码逆出来，且版本升级可能变。比路 A 费劲、也更脆。
- 注意：**Brigade 并没有把"agent 本身"作为通用的外部 MCP server 暴露**（只有记忆 MCP，外加一个给被包裹 CLI 用的 per-turn 内部 loopback tool-plane）。所以"纯靠 MCP 当完整聊天客户端"走不通，要聊天就得走 WS（路 B）。

### 战略提醒（别把桌搭的脑子扔了）
- 两条路都只让桌搭"借"Brigade 的能力，**不建议让桌搭退化成纯壳**——那等于扔掉桌搭真正的长板（桌面 GUI + 写作技能 + persona + 倾斜 Hermes 的学习闭环）。有意思的玩法是**混合**：桌搭当漂亮前端 + 写作/技能层，Brigade 当记忆底座（路 A）或可选的"切换式 crew 引擎"（路 B）。
- 两者都纯本地，隐私不受影响；Brigade 网关默认仅 localhost，可选 bearer token 加固。
- 冗余风险：若桌搭（圣经）和 Brigade（Tideline）各存一套记忆会重复。**路 A 其实给了你"统一到 Tideline"的机会**——是否退休圣经、让 Brigade 接管记忆，是值得单独决策的点。
- 模型后端彩蛋：Brigade 能复用你已有的 Claude Code 登录（keyless），意味着桌搭走路 A/B 后，可借 Brigade 用 Claude 而无需自己管 key——与桌搭当前 DeepSeek+MiMo 形成互补。

## 八、集成方案（用户两条约束：不弱化桌搭 · Brigade 逻辑不入桌搭、前后端分离）
约束重述：① 接入后桌搭能力不减——桌搭仍是主脑/编排者，persona、圣经、圆桌、技能、GUI 全保留；② Brigade 核心逻辑不搬进桌搭，保持「Brigade 本地后端服务 + 桌搭前端客户端」分离，双方可独立升级。

### 总体架构（解耦的关键）
- Brigade 作为**独立本地后端进程**运行，对外只暴露它自带的两个稳定接口：
  - **MCP 接口**（`brigade mcp`，stdio）：提供 `memory_add` / `memory_search` / `memory_context` 记忆工具（README 简称 add/search/context）。
  - **WebSocket 网关**（`brigade gateway run`，127.0.0.1:7777）：提供聊天/crew 服务（内部协议）。
- 桌搭作为**前端客户端 + 自身主脑**：通过现成的 MCP Host 连 Brigade 的 MCP；如需聊天，桌搭前端经一个**薄适配层**连 Brigade WS。桌搭**不复制任何 Brigade 代码**，只在边界处调用其服务。
- 结果：Brigade 升级（换版本/实现）只影响它自己；桌搭升级也不碰 Brigade → 满足约束②。桌搭始终自己编排，Brigade 只是"被调用的后端服务/工具" → 满足约束①。

### Phase 1（核心、推荐先做）：MCP 记忆后端，纯增量、零侵入
- 把 Brigade 注册为桌搭的 MCP server：在 `~/.config/coworker/mcp.json` 的 `mcpServers` 加 `brigade-memory`（`command:"brigade"`, `args:["mcp"]`, `type:"stdio"`）。
- 桌搭 MCP Host 按需 spawn `brigade mcp`，自动获得 Tideline 的 `memory_add`/`memory_search`/`memory_context` 工具（README 简称 add/search/context）。
- **桌搭侧零代码改动**（复用现有 MCP 机制）；Brigade 侧零改动。
- 约束①：agent/persona/圣经/圆桌/技能全部原样保留，Brigade 仅"额外多一组记忆工具"——能力只增不减。
- 约束②：纯协议对接，无逻辑搬移；任一方升级互不影响。
- 前置：全局装 brigade（稳定位置）+ 用户本人跑一次 `onboard`（绑模型凭证，助手套代劳）。

### Phase 2（可选、较重）：桌搭 GUI 直连 Brigade 网关（完整聊天客户端）
- 仅当用户想让桌搭窗口也能直接跟 Brigade 的 crew 对话时才做。
- 做法：在桌搭前端加一个**薄 WS 适配层**（独立模块），连 `127.0.0.1:7777`，把桌搭 UI 事件 ↔ Brigade WS 协议互转。**Brigade 逻辑仍在 Brigade**，桌搭只持有协议转换代码。
- 为守约束①：Brigade 不作为桌搭主脑的替代品，而是桌搭可**按需委派**的重型多智能体后端（桌搭仍是编排者，把任务交给 Brigade crew 跑，结果回桌搭）。
- 风险：WS 协议未文档化，逆出来后随版本可能变 → 必须包在适配层 + 优雅降级（连不上退回桌搭自身），避免 Brigade 升级拖垮桌搭。建议 Phase 2 放后面、默认关闭，先验证 Phase 1。

### 记忆策略（需用户拍板，影响是否碰圣经）
- **方案 X（推荐，最贴合两条约束）：保留圣经 + 叠加 Brigade**。桌搭自身记忆（圣经）完全不动，Brigade 的 Tideline 作为"额外的长期记忆工具"存在（显式写入/检索的共享知识库）。零风险不弱化、零耦合。
- **方案 Y（统一到 Tideline、退休圣经）**：让桌搭记忆统一走 Brigade。单一记忆源，但与"解耦、互不升级影响"精神略有张力（桌搭记忆将依赖 Brigade 可用性），且需迁移。非必要不推荐。

### 落地步骤（Phase 1）
1. 全局安装 brigade 到稳定位置（`npm i -g @spinabot/brigade` 或托管 node 装后软链），确保 `brigade` 在 PATH。
2. **用户本人**运行 `brigade onboard`（选存储/供应商/模型，可复用 Claude Code 登录）。涉及凭证，助手不代劳。
3. 助手往 `~/.config/coworker/mcp.json` 写 `brigade-memory` 条目（纯新增，不改其他）。
4. 验证：桌搭能加载该 MCP server、`memory_add`/`memory_search`/`memory_context` 工具可见；写一条记忆 → 检索命中。
5. （可选）Phase 2 另议。

### 落地记录（2026-08-12 晚，Phase 1 已实施并验证）
- **安装**：`npm i @spinabot/brigade` 装入托管 node workspace（`/Users/xiaocheng/.workbuddy/binaries/node/workspace/node_modules`），**未污染系统环境**；软链 `~/.local/bin/brigade` 便于手动调用。Brigade 1.30.0 跑通。
- **配置**：`~/.config/coworker/mcp.json` **纯新增** `brigade-memory`（stdio，显式用托管 node 22.22.2 启动 `brigade.mjs mcp`，`enabled:true`、`requires_approval:false`）。**未改动任何已有 server**（novel-memory / perception / deskmate-* / fetch / granola / bailian-websearch）；JSON 校验通过。
- **协议级验证（用桌搭将使用的同一条命令）**：`initialize` 成功 → `tools/list` 返回 `memory_add` / `memory_search` / `memory_context` → `memory_add` 写入成功（返回 `stored mem_...`）→ `memory_search` **英文查询命中**返回事实。**MCP 对接完全打通，桌搭侧零代码改动。**
- **⚠️ 重要限制（中文召回弱）**：实测 `memory_search` 对**英文**查询可命中，但对**中文**查询（如"逍遥"）返回无结果。根因：Tideline 混合召回 = BM25 + 无模型 HRR，**BM25 按空白分词**，中文整句无空格→整句成一词→子串查不到；HRR 是无模型向量，也不补中文语义。README 提到"Plug in a learned embedder to upgrade"——需接中文 embedder 才能改善。
  - **对用户的含义**：作为中文写作者，Brigade 的 Tideline 开箱对中文记忆**不如桌搭的「圣经」友好**。这进一步支持**方案 X（保留圣经 + 叠加 Brigade）**——圣经作中文主记忆，Brigade 的 Tideline 作辅助/英文/结构化事实库，或仅在你接入中文 embedder 后接管。不要因接了 Brigade 就退休圣经。
- **待用户本人**：跑一次 `brigade onboard`（选存储/供应商/模型，可复用 Claude Code 登录）——涉及你的凭证，助手不代劳。注：记忆 MCP 实测**不依赖 onboard** 也能起（仅暴露记忆工具、不调 LLM）；但要让 Brigade 当完整后端、你想"试试它多神"时，必须 onboard。
- **验证残留**：验证时往 `~/.brigade/` 写了 3 条测试事实（含 1 条中文）。MCP 只暴露 add/search/context、无删除工具，目前清不掉；不影响功能，如需干净可手动清 `~/.brigade/` 存储或忽略。

## 九、最终决策（2026-08-12 深夜）：不集成，摘回

### 推演（用户得出，经核确认成立）
1. **Phase 1 只能用到记忆，且中文弱**（已实测：英文命中 / 中文无结果）→ 对一个中文写作者，增量价值为负（桌搭「圣经」对中文更友好）。
2. **Phase 2 风险大**（WS 网关协议未文档化、随版本可能变）→ 不实施则桌搭**当不了 Brigade 的桌面端**（桌面端 = 连 WS 的聊天瘦客户端，而 `brigade mcp` 只暴露记忆工具、不是聊天前端，定义上即不成立）。
3. 由 1+2 推得：**Brigade 不适合接入桌搭**；且**不做 Phase 2 就无法成为其桌面端**。

### 关键区分
- **试 Brigade ≠ 接 Brigade**。用户最初目的是"装来试试它多神"——那是独立试用（终端 `brigade tui`），与"集成进桌搭"无关，两件事分开判。
- 技术可行性 ≠ 实用价值。Phase 1 零侵入、纯增量是真，但"干净"不等于"值得"。

### 已执行动作
- 从 `~/.config/coworker/mcp.json` **摘除 `brigade-memory`**，桌搭回到接入前 8 个 server 状态（JSON 校验通过）。
- **保留 Brigade 安装**（`~/.local/bin/brigade`，1.30.0）供用户单独终端试用，不影响桌搭。
- 桌搭维持自有的 `deskmate-memory`(圣经) + Tauri GUI + 写作技能 + 倾斜 Hermes 路线，未作任何耦合改动。

### 对桌搭方向的启示（正向）
- Brigade 处于 `hermes`/`openclaw`/`moltbot` 同一谱系，**验证了我们桌搭方向有共识、非野路子**；但它本身不是要拼进来的部件。
- 若要"桌面端 Agent OS"：应**自研 Tauri+GUI**（桌搭已有），不绑 Brigade 未文档化的 WS；中文记忆「圣经」已强于 Tideline 开箱表现，无需退休或外迁。
- Phase 2 暂不实施（用户保留再议权利）。

## 十、独立试用进展（2026-08-13 凌晨，用户本人操作）
- 用户自行完成 `brigade onboard`，选 **DeepSeek** 供应商，模型 `deepseek-v4-flash`；key 已写入 `~/.brigade/brigade.json`（非 credentials 文件）。配置自检 `logs/config-health.json` 记录到位。
- 助手以 `brigade gateway run` 验证：网关正常启动，`agent model: deepseek/deepseek-v4-flash`、`listening on ws://127.0.0.1:7777`、`ready`；`/` 与 `/health` 返回 404/Not found 属正常（纯 WS 服务，不响应 HTTP GET）。
- **拉起命令（用户在自有终端执行，交互界面助手无法代跑）**：
  - 最简：`brigade tui` —— 自动拉起网关并进入终端聊天界面。
  - 或分离：`brigade gateway run &` 后台常驻 + 另开终端 `brigade connect` 连入。
- 注意：DeepSeek 调用走网络（`api.deepseek.com`）；若聊天时报模型/权限错误，多为该 key 对该模型不可用，可重跑 `brigade onboard` 换模型（目录另有 `deepseek-v4-pro`）。

## 十一、Brigade 修复 Bug A/B 并质检通过（2026-08-13）
- 用户让 Brigade（代号「拳头」）依 `brigade-fix-candidates.md` 修两条真 Bug。
- **Bug A**（`_keyword_recall` 圣经摘要盲截）：改为按查询关键词从 CHARACTERS 切分完整角色块动态选取（新增 `_split_character_blocks` / `_pick_character_blocks`），绝不再以 `[:6000]` 整段切掉 6000 字符后的角色；无关键词时按原文顺序取完整块直到预算，且首个块允许轻微超预算而不拦腰截断。
- **Bug B**（`build_fts5_index` 删章残留）：循环结束后按现存文件集合 `DELETE FROM novels / file_tracker WHERE file_path NOT IN (...)`，目录清空则清空两表，与存量 `rel_path` 格式一致（不会误删/漏删）。
- **质检（逍遥，非轻信其报告）**：读 `git diff` 确认两处真实实现；运行 Brigade 新增 7 个测试（尾角色可被检索、角色块不切半、删章后独有词不再命中、两表同清、变更文件仍更新、空目录清空）+ 全文件 `31 passed`，零回归。
- 余注：`core.py:1885/1890` 的 `chars_text[:4000]` / `new_text[:6000]` 是「一致性核验」LLM 输入截断，与圣经注入主路径及本次 Bug A 无关，不在本次范围，记此备查。
