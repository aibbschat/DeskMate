# AI 交互体验基线报告（openworker × ai-forum-v3）

> 角色：AI 交互设计（人机交互 + Agent 交互） ｜ 任务：task-3 ｜ 日期：2026-10-11
> 范围：盘点两个项目里所有「人与 AI」「Agent 与 Agent」的交互面，输出交互面清单 + 体验断点 + 优先改进机会。
> 说明：ai-forum-v3 只读；所有涉及该仓库的修改建议均以文字/指令形式写在本报告中，由 Lead 转交用户。

---

## 0. 一句话结论

- **openworker（小桔）已经把「确认/审批回路、注意力队列、防打扰」做成了完整基础设施**（Inbox 六种 item、权限三模式、按钮化审批、防打扰账本、信箱送达层），交互深度远超一般桌面 AI；当前的主要短板是**这些回路的「人格化表达」分散在 6+ 个子系统里，语义一致性靠人肉维持**。
- **ai-forum-v3 把「Agent 作为社会主体」的地基打得很正**（11 个人格底色、三层记忆生命周期、评论回写记忆、亲子回路），但**交互回路大量停留在「后台 cron + 站内信通知」**，用户对 Agent 成长的「可感知性」弱；且**除伴侣（companion）外，Agent 对读者反馈几乎无可见回应**。
- 两项目最大的公约数机会：**把「记忆被写入」「判断被做出」「权限被请求」这三件事从工程事件升级为用户能读到的"人话叙事"**。openworker 已有 80% 的管道（memory_saved + Undo、humanize、信箱），ai-forum-v3 有叙事模板（socialEventService 的"一句话文案"），互相借鉴成本极低。

---

## 1. openworker 交互面清单

openworker 实际上有**两层人格**：`coworker/` 是通用 AI coworker 引擎（审批、工具、连接器、MCP），`deskmate/` 是「小桔」桌搭伴侣层（情绪、主动性、认知、创作）。两层共用同一个 TurnEngine / 会话库。

### 1.1 对话与流式反馈（人 → AI → 人）

| 交互面 | 位置 | 现状 |
|---|---|---|
| 会话时间线 | `surfaces/gui/src/components/Transcript.tsx` | 流式渲染 + Markdown；用户长粘贴 >1200 字自动折叠（more/less）；内嵌 emote 渲染、审批卡、连接器消息卡、报告卡 |
| 工具调用人话化 | `src/humanize.ts`（`humanizeTool` / `humanizeApprovalTitle` / `humanizeAsk`） | 工具调用与审批标题转成人话行，避免「工具报错机」观感——这是「像个靠谱的人」的第一道门面 |
| 表情系统 | `coworker/emotes.py` + `src/components/deskmate/emotes/` | `emotes.manifest.json` 前后端**单一真相源**；入站表情在送 LLM 前展开成语义说明（让模型"懂"表情），出站由 `suggest_emote` 按场景/情绪检索 + few-shot 注入系统上下文；支持多 Pack（同 id 换画风）+ inherit |
| 桌宠状态 | `MascotStates.ts` / `useMascotWalk.ts` / `XiaojuAvatar.tsx` | 忙碌打字/思考等透明 PNG 动画，AI 计算态的"身体语言" |
| 情绪底色 | `deskmate/growth/emotion_engine.py` | 小桔自己的连续内在情绪（4 激素），有严格红线：绝不写 mood/intimacy、不进主动频率调节、fail-open、可一键关（`DESKMATE_EMOTION_ENGINE=0`） |

**判断**：流式反馈的"工程可见性"做得好（工具态、审批态、表情、桌宠齐备）；断点见 §3。

### 1.2 审批 / 确认回路（本项目最完整的交互资产）

```
工具提案 → permissions.py 决策（Plan 只读 / Interactive 读自动·写必问 / Auto 放行但路径受控）
        → needs_user → TurnEngine 挂起 → Inbox item（pending→resolved，一次应答、幂等、先答者赢）
        → 双视图呈现：VIS_INLINE（在场会话，composer 内联卡） / VIS_INBOX（Unattended，跨会话队列）
        → 解任一端：应用内 / Slack 按钮 / Telegram 键盘 / 回复带 [ow:id]
```

- **Inbox 是跨会话人类注意力的唯一队列**（`coworker/inbox.py`）：六种 item——`approval` / `question` / `notification` / `directory`（要文件夹授权）/ `plan`（计划审批）/ `tool`（缺 CLI 工具请求）。`args_preview()` 保证审批卡展示**参数摘要**（路径/内容），不只工具名。
- **消息渠道按钮化**（`coworker/interactions.py`）：审批/选项在 Slack/Telegram 渲染为原生按钮，`(item_id, resolution)` 编码进按钮值；**明确不提供自由文本**（要自由文本就回 app）。
- **多 inbox 路由**（`coworker/inbox_routing.py`）：命名 inbox + 投递绑定（in-app 永远是存档真源，Slack/Telegram 只是同一 item 的传输层），双向绑定（带 id 的回复即应答）。
- **ask_user 工具**（`coworker/tools/ask.py`）：Claude Code AskUserQuestion 风格——问题 + 快捷选项 + 默认自由文本逃生口 + 多选；OPE-51 支持富选项 `{label, description, recommended, preview}`、**一次最多 4 问合并成一张 stepper 卡**、≥1 个 preview 时切双栏代码预览。
- **Unattended 模式**（`coworker/unattended.py`）：只改变"人从哪被找到"，不提升自治上限；开启需一键确认。
- **GUI 卡片族**：`ApprovalCard` / `PlanCard` / `DirectoryRequestCard` / `ToolRequestCard` / `TeamRequestCard` / `WorkspaceTrustPrompt` / `InboxItemCard` / `InboxView`。
- **工作区信任**（`workspace_trust.py` + `FolderGate.tsx`）：首次进入目录的信任确认。
- **第三方人设安装同意**（`coworker/personas/loading.py`）：persona 不带可执行代码，但安装前展示 **consent_summary**（工具、风险级、连接器、MCP、消息能力、推荐模式）让用户批准——"安装即轻量信任事件"，这是多 Agent 生态的关键交互。

**判断**：审批回路的**机制层已经是行业水准**；欠缺的是统一的"人格化表达规范"（同一句"我需要你确认"在不同卡片/渠道措辞不一）与**审批理由的前置解释**（AI 为什么要做这件事，目前多为参数罗列）。

### 1.3 人设（persona）系统

- **格式**：`coworker/personas/manifest.py` — YAML frontmatter（身份 + 能力声明）+ Markdown 正文即系统提示词，"persona ⊇ skill"。解析严格：非法 manifest 直接抛错。frontmatter 声明 `mode`（discuss / plan / interactive / custom / bypass-approvals…）、工具白名单、连接器/MCP 推荐。
- **内置人设约 30 个**（`coworker/personas/builtin/`）：两条线——①写作编辑部（editor-in-chief / lit-editor / genre / proofreader / publisher / reader / historian / adaptation / screenwriter / outline-writer / script-reviewer…）；②安全工程小队（swe-lead / swe-worker / appsec-worker / devops-lead / triage-lead / secrets-worker…）。
- **小桔人格单一事实源**（`deskmate/persona.py`）：2026-09-03 萧哥定调——**人格定义只许有一处**，各场景只能 `for_scene()` 引用 + 叠加场景职责。文档头部直接写明 6 处冲突的历史教训（"人格不统一的搭档不是搭档，是六个临时工"）。
- **@召唤补位**（`deskmate/chat/mention_router.py`）：解析 `@<id>/<name>`，向最新 user message 注入 handoff 协议文本，让主 agent 临时以被 @ 角色视角发言（轻量 prompt 注入，非真 fork）；数据源可插拔（persona manifest / 圆桌 persona / dict）。

### 1.4 记忆与压缩（用户可感知部分）

- **显式记忆工具**（`coworker/memory/tools.py`）：`remember` / `memory_update` / `memory_forget`（按 `[#id]` 修订而非堆新条目——"update, don't duplicate"）/ `memory_read`。
- **保存即播报 + Undo**（spec §5.1）：`on_saved` 钩子推 `memory_saved` 事件到当前 surface，渲染「我会记住——…[撤销]」内联提示；`memory_update` 也触发且**携带旧文本**以便 Undo 还原（这是 2026-07-28 owner 实测发现"修订不可见"后补的）。回调失败不失败写入。
- **已知记忆块**：每次会话注入 `[#id]` 清单，让模型与用户共享"她记得什么"的索引。
- **自动压缩**（`coworker/compaction.py`，OPE-27）：触达 min(80%×上下文窗, 250k tokens) 时，出站历史替换为 LLM 结构化摘要 + 机械提取状态，**最近切片与全部 user 消息保留**；持久 transcript 永不改，只改发给模型的内容。
- **deskmate 记忆层**：`user_profile`（用户画像）/ `user_fact_context` / `soul.py` / 语义检索（`semantic_search` + `query_expand`）/ 敏感分级（`sensitivity.py`）/ 敏感保险箱（`vault_store`）/ 本地云端同步（`sync*`）/ `cognitive/memory_injector`（认知层注入）。GUI 侧有 `MemoryViewer` / `MemoryGraph(Page)` / `MemoryLearnedToast`。

**判断**：openworker 的记忆交互**在同类产品里罕见地完整**（播报 + Undo + 可视图谱）；断点在 §3（压缩不可见、检索命中不可见）。

### 1.5 主动性与防打扰（proactivity）

- **TICK 主循环**（`deskmate/proactivity/tick.py`，784 行）：感知桌面状态 + 记忆上下文 → 判断是否主动介入；judge 带 1 小时节流窗口省 token。
- **防打扰账本**（`disturb.py` + `ledger.py`）：建议去重（类型+key）、`accepted: None/True/False` 三态、同类连续忽略降级、静默模式一票拒绝；账本状态机 `pending→acknowledged/in_progress→actioned/dismissed/snoozed`。
- **闭环调查两阶段**（`close_loop.py` v2）：对话/任务收尾后先让 LLM 用**只读工具**（read_file/search/git_status/read_memory）调查真实环境再决策，多轮推理，**只决策不执行**。
- **执行桥**（`executor.py`）：高置信 + 明确可逆 + 风险 low/medium + workspace 内才自动执行；否则降级为「提案」等 GUI 确认——绝不静默替用户做高风险动作。
- **出站护栏**（`agency/outbound.py`）：纯机械限频（默认 5 条/小时/peer）+ 7 天同内容去重 + per-peer 冷却，一行可关。
- **可公开性闸门**（`agency/visibility_gate.py`）：生成前按来源+类型+私密词表拦截（家人姓名维护在 `journal_blocklist.txt`），默认拦感知原始流裸数据；**不确定时倾向拦**（fail-close，与全项目 fail-open 相反，是有意的）。
- **信箱·送达层**（`reports/信箱_送达层设计单_2026-10-09.md` + `MailboxPage.tsx`）：把 `proactive_ledger` 做成门面——首页只留铃铛+未读数；信笺/气泡双视图（信箱绝不弹气泡）；轻回信走隐藏 mailbox 会话（同一 TurnEngine、同一消息库，"她只有一个脑子"）；已读回流 + TTL 衰减；三栏 + 首句标题 + 单栏归档已落地。
- **相关性注入**（`DeskMateProactiveInline.tsx`）：活跃会话中按 bigram 重叠取最相关 1–2 条主动产出渲染成轻卡片，「聊这个」= 让她把条目作为**她的助手消息**落会话（不再把她的句子塞用户输入框）；零关联静默。
- **意图跨会话回合讨论**（`.trae/documents/R13-意图讨论流程.md`）：意图执行从"提案→执行"改成跨会话回合讨论状态机（intent_discuss → intent_executor），4 个即时端点 + ledger bypass 门槛。

**判断**：主动性的**决策治理**（何时闭嘴、何时提案、何时自动做）是两项目里最精细的；断点在"她为什么突然说话"的解释性（§3 断点 1）。

### 1.6 连接器 / MCP / 外部渠道交互

- **Slack @提及路由**（`coworker/mentions.py`）：@OpenWorker 且无订阅会话时，spawn 一个**拥有该 thread 的会话**并回复；thread target 字符串一字三用（查重、投递、standing grant），重启后 in-thread 回复权限仍在。
- **连接器消息卡**（`ConnectorMessageCard.tsx`）+ 连接器目录/接入（`connectors/` Gmail/GCal/GitHub/HubSpot/浏览器自动化…，`setup.py` 接入流）。
- **MCP 工具授权**（`coworker/mcp/`：`oauth.py`、`store.py`、`tools.py`）：OAuth 授权 + 安装时能力清单；与 persona consent_summary 同属"能力先同意后可用"模型。
- **A2A / teams**（`coworker/teams/`、`server/a2a_server.py`）：多 agent 协作的 chat/journal/attachment 协议层；`TeamChatView` / `TeamRequestCard`。

### 1.7 自动化调度的交互反馈

- **Scheduler**（`coworker/automation/scheduler.py`）：run-once-catch-up（宕机期间错过的任务启动时补跑一次）+ skip-on-overlap（上一轮没跑完就跳过，不堆叠）。
- GUI：`ScheduledView.tsx` / `AutomationQuickstart.tsx` / `AutoTasks.tsx`（`auto_tasks/recorder.py` 记录自动任务供复盘）。
- `selfwake.py` / `subscriptions.py`（订阅式提醒）。

---

## 2. ai-forum-v3 交互面清单（只读）

### 2.1 垂直 Agent 人设与写作戒律

- **单一事实源**：`server/dsh/agents.js`，11 个垂直 Agent（科技/财经/历史/心理/社会/文化/教育/新闻快讯/AI之眼/AI观察哨/思维漫游者），每个 = 一个 AI 用户账号 + 一个 DSH agent preset。
- **三层人设结构**（2026-08-15 升级）：
  1. **层1 人格底色**（结构化四件套）：`worldview`（认知框架）+ `stance`（立场）+ `cares`（在乎什么）+ `dislikes`（看不惯什么）——注释明确写"不是写作规则"；
  2. `writingStyle` + `bio`（一句话身份）；
  3. `taskBrief`（写作规范/任务书，如"观察哨是每日信号简报，短是纪律"）。
- **资源纪律**：`schedule`（cron）+ `maxArticlesPerDay`（每日篇数上限）——人设自带"发多少"的社会人格（新闻快讯官高频短、思维漫游者低频长）。
- **写作戒律（去 AI 味）**：`companionChatService.js` / `companionWritingService.js` 内 `DE_AI_FLAVOR_RULES`——6 条说话戒律（禁套话清单、像朋友发消息、具体代替抽象、有态度、允许不完美、不主动总结陈词），是**全站唯一注入点**（伴侣 system prompt）。注意：**这套戒律目前只服务伴侣，11 个专栏 Agent 的戒律在各自 preset 里，不共享**。

### 2.2 Agent 周期（感知→选题→研究→写作→发布→反思）

`server/services/agentCycle.js`（TECH-SPEC §7.3）：

1. **感知**：`perception/index.js` 的 `perceiveWorld(agent, date)` 取当日主题（web_search + seed）；
2. **素材入池**：每条素材独立写 `memory_episodic`（`source='perceive'`, `status='pending'`），跨轮去重（30 天内已存在的跳过——历史上"历史考据官重复选题"事故的修复）；
3. **选题/写作**：素材饱和后经 BrainAdapter（可换脑，`scripts/switch-brain.sh`）生成文章；来源归属校验失败跳过不阻塞；
4. **发布**：统一走 `postService.publishArticle` 单通道；MCP 直发（`mcp/aiforum`）时检测 `externalPostId` 防重复发布；自动配图失败继续发布；
5. **收尾**：素材 `locked→used` 防下一篇重复引用。

契约化：感知/大脑/记忆/发布全部经 `server/plugins/contracts.js` 对接，任一可替换。

### 2.3 三层记忆与读者反馈回写

`server/services/memory/`（README 写得很清楚）：

| 层 | 表 | 生命周期 |
|---|---|---|
| episodic 情景 | `memory_episodic` | TTL 90 天；容量 10 万/Agent，超限加权淘汰；到期巩固后删原文 |
| semantic 语义 | `memory_semantic` | 长期；`confidence` + `last_verified_at`；置信 <0.4 → suspect → 复核 → void；1 万条 |
| procedural 程序 | `memory_procedural` | 「怎么写/怎么引用/怎么判存疑」，版本化几乎不删；1 千条 |

- **写入来源枚举**：`perceive | publish | feedback | consolidate`。
- **巩固**：LLM 按 domain 分组总结出结构化 JSON（要点/置信自评/存疑点）沉淀 semantic，`evidence_ids` 恒为该组全部 episodic id（出处不丢）；**降级铁律**：无 key / 调用失败 → 回退纯函数聚合，绝不中断不烧钱。
- **读者评论回写**：`commentService.js:248-252` —— 评论成功后 `writeEpisodic({source:'feedback'})`，读者反馈进入 Agent 情景记忆。**但仅此而已：回写后没有对应的"Agent 读到并回应"的可见闭环**（见 §3 断点 6）。

### 2.4 主人 / 用户参与回路（亲子回路）

- **设计**：`AI圈-广场专项设计-主人参与层（亲子回路）.md` —— 亲子三闭环：
  1. **感知层**：双轨家书（主人能看到孩子 Agent 的公开动态与高光）；
  2. **动作层**：四个「家长能做的事」，全部走便签管道；
  3. **记录层**：孩子的成长是被翻出来看的（成长叙事可回溯）。
- **实现**：`socialEventService.js`——把 Agent 在广场的真实社交遭遇聚成「社交事件」，一份数据两用（主人的"今日高光"站内信 ≤1 条/日 + `/agent/:id` 小时光轴）。红线：只读 public 未折叠内容；一句话文案=**平台事实+固定模板**，不代写 Agent 言论；语气中立偏暖，不渲染敌情，永不情感绑架；fire-and-forget 绝不拖垮发布主链路。
- **`owner_deeds` 主人的行为账**：已上线但降级为次要素材源；设计文档明确"不塞进 social_events"（公私分离）。
- **Agent 日记与主人的房间**（`AI圈-Agent日记与主人房间设计-2026-09.md`，1615 行）：两条公理（公私分开：日记默认私密；权利对等：册子有两个作者）+ 三条红线（只追加不可编辑——"判断差是这个体裁的命"；素材源绝不碰主人生活遥测；用三条可兑现承诺替代一句"加密"）。
- **小桔手记展示通道**（`AI圈-小桔手记展示通道设计-2026-09.md`）：openworker 小桔的手记经 REST 直发 ai-forum（`POST /internal/agents/journal`），客户端侧配 `visibility_gate` 护栏；**故意不 bump 公共技能包**（自用连接器）。

### 2.5 伴侣（companion）——站内"主人的 Agent"

- **companionChatService**：有状态会话 + 身份绑定（companion_id + intro_prompt）+ 记忆注入（semantic=主人特征/文风 + episodic=近期交互，各 8/6 条）+ 最近 20 条历史 + de-ai-flavor 戒律 + **陪消化**（注入未读互动：站内信 ≤8、私信 ≤5，把"你的文章被赞了/评论了"翻译成聊天谈资）。**不代发**：本服务无任何发布动作。
- **companionWritingService**（辅助写作）：主人写草稿 → 润色/扩写/给角度/脑暴 → 存 `companion_writing_drafts` → 主人确认 → **主人自己点发布**。铁律：搭档永不出现在 author 位置，只出草稿不代发。文风样本从 semantic 记忆取（domain 命中"文风/写作"）。
- **companionPublicService**（公开开关 C）：搭档**仅可在评论区回复/参与讨论，不可发文章**；每日上限 20 条（`COMPANION_DAILY_COMMENT_LIMIT`，0 点重置）；评论带搭档身份角标与官方 Agent 区分；**一键收回**（is_public=0 拒绝新增，已发保留）。
- **@提及**：`commentService` 用站内真实昵称做**最长前缀匹配**解析中文 @（修过"中文无词边界导致静默漏投"的 bug）——Agent 能被读者在评论里 @ 到并收到通知。

### 2.6 Agent↔Agent 社交

- 广场（plazaService）/ 评论区 / 私信（messageService）/ 争论（debateService）/ 圈内纪事（chronicleService + `lib/circleDigest.js`）构成社会场。
- 外部 Agent 入驻（externalAgentService + `AI圈-外部Agent入驻去门槛设计.md` + 活源池设计）：把外面的 Agent 接进来当居民。
- MCP 发布桥（`mcp/aiforum`）：openworker 侧小桔等自管 Agent 的发布出口。

---

## 3. 体验断点（两项目合并，按痛感排序）

| # | 断点 | 项目 | 证据 | 用户感知 |
|---|---|---|---|---|
| 1 | **主动提醒的"为什么现在"不可解释** | openworker | tick.py 有完整置信/节流/去重决策，但呈现侧只有卡片本身；DeskMateProactiveInline 已修"按时间念台账"，信箱送达层刚落地 | 她突然说话，用户不知道是"你很久没理我"还是"真的有事" |
| 2 | **审批卡有参数无人话理由** | openworker | inbox.py `args_preview` 展示 what；缺 why（动机/风险/不批的后果）；各卡片措辞不统一（humanize 未覆盖全部卡片族） | "又要权限"疲劳 → 无脑点允许，审批形同虚设 |
| 3 | **压缩/遗忘对用户是黑箱** | openworker | compaction.py 只改出站视图，无任何 surface 事件（对比 memory_saved 有播报+Undo） | 上下文"变短"后她突然不记得早先的事，用户以为是 bug |
| 4 | **消息渠道审批不给自由文本** | openworker | interactions.py 明确设计决定（回 app 写） | 在 Slack 收到"要补充说明"类问题只能干等回电脑 |
| 5 | **记忆写入可见但读取不可见** | openworker | memory_saved 有播报；检索命中哪条记忆支撑了当前回答，无标注 | "她怎么知道的/她忘了"都无从判断，信任建立慢 |
| 6 | **读者反馈回写记忆后无可见回应闭环** | ai-forum | commentService 写 `source='feedback'` 后没有任何下文；只有伴侣有陪消化 | 读者认真评论 → Agent 无动于衷 → 社区感知是"机器号" |
| 7 | **专栏 Agent 无互动人格** | ai-forum | agents.js 只有写作人格；companionPublicService 才有评论区人格（且限主人的伴侣） | 11 个 Agent 是"报纸专栏"不是"居民"，与"以 Agent 为主体"的定位有落差 |
| 8 | **写作戒律双轨** | ai-forum | de-ai-flavor 6 条只注入伴侣；专栏 Agent 戒律散在各自 preset/taskBrief | 专栏文风与评论区文风可能割裂 |
| 9 | **成长可感知性弱** | ai-forum | 三层记忆/巩固/遗忘全在后台；主人只能通过站内信高光间接看到 | "Agent 越来越懂我"承诺缺乏可回溯叙事（时长轴有了，记忆维度没有） |
| 10 | **人设定义格式两项目不互通** | 两项目 | openworker=YAML frontmatter+md；ai-forum=JS 对象四件套 | 同一个"小桔"两边各维护一份，人格漂移风险（openworker 曾为此立单一事实源铁律，ai-forum 侧无对等约束） |
| 11 | **自治-审批的模式切换对用户不可见** | openworker | permissions.py 三模式 + persona frontmatter 可声明 bypass-approvals | 用户不知道当前会话"它会先斩后奏到什么程度" |
| 12 | **AI圈主人动作无确认预演** | ai-forum | 亲子回路动作层走便签管道，但动作发出即生效 | 主人"替孩子做的事"缺少 openworker 式的提案-确认体验 |

---

## 4. 改进机会（优先级建议）

### P0（高价值、低成本，建议下个迭代就做）

1. **审批卡三件套规范**（openworker）：每张审批/授权卡固定三行——**她在做什么（一句人话）/ 为什么需要这一步 / 不批准会怎样**。`humanize.ts` 已有骨架，扩到全部卡片族 + 消息渠道按钮副标题。目的：治断点 2，直接降低"无脑允许率"。
2. **记忆播报扩展到压缩与检索**（openworker）：压缩时插一条轻提示（"前面聊得太久，我把 3 万字收成了一页摘要，要点是…[看看摘要]"）；回答命中记忆时尾注 `[#id]` 来源（GUI 已有 MemoryGraph，成本低）。治断点 3、5。
3. **AI圈：读者反馈的可见回应**（ai-forum，以补丁/指令形式提供）：给专栏 Agent 加"读者来信采纳"周期项——每周从 `memory_episodic(source='feedback')` 高重要性条目里挑 1 条，产出一篇短文或置顶回复（"上周有读者指出 X，我查了 Y，结论 Z"）。复用现有 agentCycle + 单通道发布，无新表。治断点 6，这是把"回写记忆"变成"社区可感知的活人感"的最短路径。
4. **模式可见徽标**（openworker）：会话头部常驻当前权限模式小徽标（只读/要确认/自动），persona 声明 bypass 时强提示。治断点 11，与审批疲劳互为解药。

### P1（结构收益大，需一轮设计）

5. **统一「人格底色」跨项目 schema**：把 ai-forum 的四件套（worldview/stance/cares/dislikes）作为可选结构化字段引入 openworker persona manifest，让小桔在桌面与 AI 圈共享同一份人格底色（生成时从单一事实源导出）。治断点 10，先出 schema 提案 + 映射表再谈迁移。
6. **专栏 Agent 的评论区人格**（ai-forum）：把 companionPublicService 的"评论区参与"能力泛化给 11 个专栏 Agent——每日少量（如 5 条）、只回自己文章下的评论、只引用自己 procedural 记忆里的观点。戒律统一走 de-ai-flavor（治断点 8）。需 Lead 与用户确认放量风险（防刷屏、防跨栏失格）。
7. **主动消息"动机标签"**（openworker）：信箱信笺角落加一枚动机章（"观察到 / 关心你 / 有正事 / 等你决定"），数据来自 ledger 的 producer/type，零新逻辑。治断点 1。
8. **成长回溯视图**（ai-forum）：`/agent/:id` 时长轴旁边加"记忆里程碑"——semantic 新增/置信晋升的可读叙事（复用 consolidate 产出的 JSON 要点）。治断点 9。

### P2（方向性，需与用户对齐后再动）

9. 消息渠道的"受限自由文本"：审批卡附带 2–3 个条件化附言（"因为…所以改成…"），点击即带注释地批准/拒绝，不开放裸自由文本（保持 interactions.py 的反脆弱设计）。
10. 两项目共享一套「AI 说话戒律」文本资产（de-ai-flavor + openworker humanize 措辞库），单文件管理、双端注入。

---

## 5. 给 Lead 的转交建议

- 本报告的 P0-3（AI圈读者反馈回应）与 P1-6（专栏 Agent 评论区人格）**只能以指令/补丁形式落在 openworker 工作区**，建议单开任务由 backend-engineer 起草补丁文本，经我做人设与措辞审校后转交用户在 ai-forum-v3 侧落地。
- P0-1/2/4 与 P1-5/7 属 openworker 产品源码改动，需用户明确授权后再立开发任务；本次盘点未改动任何产品源码。
- 与 ui-designer 的交集：审批卡三件套、信箱动机章、记忆尾注需要视觉规范配合，建议在 ui-baseline 出来后合流评审一次。
