# 小桔活体朋友圈（Living Companions）改造方案

> 状态：**草案 v1（2026-10-14 由逍遥与萧哥对齐前的技术预研）**
> 目标：把萧哥的「浪漫愿景」——小桔在本地有朋友，bot 们能聊天交友、以小桔为领导层、各自记忆与技能、可彼此了解分享、将来共享花园——落成一套**可执行的改造方案**。
> 原则：**不造活体，只加社交层**。小桔的 `coworker/teams` 已具备 ds-bot 的全部活体内核（真实 agent session 预生成、lead 派活、worker 执行、board/chat/journal、MCP/A2A），本方案只在其上叠加「生活化/社交化」一层，并尽量零改动上游代码以降低合流冲突。

---

## 1. 结论先行（基于已核实源码，非推测）

**关键发现**：萧哥看好的 ds-bot「负责人 + 领导团队 + 派活 + 群聊项目」范式，**小桔原生栈已经具备且已落地**，不需要移植 ds-bot（那个第三方 dsh 插件已于 2026-10-14 净卸载，且与小桔核心无关）。

证据链：
- `coworker/teams/` 是**干净上游代码**（`git diff --stat origin/main HEAD -- coworker/teams/` 零差异）；
- `propose_team` 是上游原生门禁（`coworker/agent.py:653-658`），语义即「组队 → 批准后预生成 worker 真实 session（未派活 0 token）」；
- 小桔已在 `deskmate/creation/` 把这套活体范式**落到生产**（主编 lead + 编剧/大纲/审核 worker 真实 session、派活、审核打回、定稿），阶段 0–5 全已落地（见 `docs/teams-integration-design.md`）；
- `ChatStore.post` 支持 `@handle` 唤醒、`JournalStore.append` 支持按作者/persona 过滤——社交与共享日记的技术底座已就位。

**因此本方案不是「从零造活体」，而是「激活 + 生活化」**：
1. 复用现有活体内核把 persona 变成**长期实体**（真实 session + 独立记忆 + 模型）；
2. 在实体之上叠加**社交驱动层**（闲聊、互相关心、共享花园/日记、彼此了解技能）；
3. 把小桔确立为**朋友圈领导层**（派活 + 协调），并把 A2A 外脑（Hermes/OpenCode）纳入朋友圈。

---

## 2. 现状审计（已有 vs 缺口）

| 能力（对应浪漫愿景） | 现状 | 证据 | 缺口 |
|---|---|---|---|
| A. 小桔是领导层、能派活 | ✅ 已有 | `agent.py:654` `if agent.team=="lead"`，`propose_team`/`propose_work_items` 门禁；创作流水线主编派活 | 仅限创作语境，未泛化到生活/通用协作 |
| B. bot 是长期实体，各自记忆/技能 | ✅ 上游原生 + 已落地 | `propose_team` pre-spawn；`TeamWorker(actor, persona, session_id)` | 实体目前只建在「创作团队」，无「常驻朋友圈」 |
| C. bot 们能闲聊交友 | ⚠️ 底座有，未激活 | `ChatStore.create_group` + `post(author, text, mentions=@解析)` | 无「常驻朋友群」+ 无社交驱动（谁发起闲聊？） |
| D. 共享花园/日记，bot 留言 | ⚠️ 底座有，未投影 | `JournalStore.append(actor, case, body, persona=…)` 支持按作者过滤 | garden UI 未投影 teams journal；无「bot 留言到花园」通路 |
| E. bot 间了解彼此经验/技能 | ❌ 缺 | journal 是 append-only 账本，但无「共享记忆空间」设计 | 需建共享 case + 读取 API（轻量） |
| F. 外脑朋友（小桔朋友圈含真 Agent） | ✅ 已有 | `coworker/server/opencode_a2a_server.py` + `deskmate/mcp/roundtable_server.py` | 未以「朋友」语义纳入常驻社交群 |
| G. bot 自主干活（无人值守） | ⚠️ 上游有，未合入 | `coworker/headless/runner.py`（OPE-196，origin/main v0.3.1+）`run(args)` + `--coworker <id>` | 工作区 v0.3.0 无此文件，需 cherry-pick 合入 |

**一句话**：A/B/F 已具备，C/D/E 缺「激活与投影」，G 需先合入上游。本方案改造量集中在 `deskmate/*` 新增薄层，上游 `coworker/teams/` 保持零改动。

---

## 3. 愿景 → 能力映射

| 萧哥的浪漫描述 | 落地为的可验收能力 | 复用基座 |
|---|---|---|
| 「小桔在本地有朋友了」 | 一个常驻「朋友圈」team，成员 = 各 persona 真实实体 | `TeamRegistry.create` + `propose_team` |
| 「他们可以聊天、交友」 | 朋友群内 bot↔bot、bot↔用户的闲聊会话，可 @唤醒、可主动发起 | `ChatStore.create_group` / `post` + 社交驱动 |
| 「以小桔为核心，小桔成为领导层，可以给它们派活」 | 小桔 = lead，能在朋友圈派活（创作/调研/生活事务） | 现有 lead 门禁 + 泛化 |
| 「他们有各自的记忆和技能」 | 每个 persona 独立 session（记忆）+ persona 技能集 | `TeamWorker.session_id` + persona manifest |
| 「彼此了解，分享其他 Agent 的经验、技能和记忆」 | 共享「朋友圈记忆账本」(journal 固定 case)，bot 可写可读按 persona 过滤 | `JournalStore.append/read` |
| 「将来把花园打开给他们共享，让 bot 在日记上留言」 | garden/personal-center UI 投影朋友圈 journal；bot 可经 append 留言到花园 | `JournalStore` + garden UI 投影 |
| 「bot 们甚至能自己去做事」 | 小桔/用户触发 headless run 给某 bot 派一个自主任务 | `coworker/headless/runner.run`（合入后） |

---

## 4. 改造方案（接口导向，分模块）

> 命名约定：新增代码集中在 `deskmate/companions/`（新包），薄封装已有 `coworker/teams` 单例。红线：**不修改 `coworker/teams/*`、`coworker/agent.py`、上游 persona 加载**，所有团队语义经 `deskmate/teams_client.py`（已存在薄壳）取单例，避免污染干净上游。

### 模块 1 · 朋友圈常驻 team（实体化，复用而非新建）

**目标**：把现有 persona 从「声线/创作 worker」升级为「常驻朋友圈成员」——长期存活、独立 session、能闲聊也能被派活。

- **复用**：`TeamRegistry.create(space="companions", lead_actor="小桔", lead_session=<小桔session>, workers=[TeamWorker(actor=p, persona=p, session_id=f"cmp-{p}") for p in personas], chat_enabled=True, chat_group=<gid>)`。每个成员自带 `session_id` → 独立记忆实体。
- **不预生成对话**：遵循上游 pre-spawn 哲学（未派活 0 token），成员在群里「在线」但只在被 @ 或主动发起时才耗 token（见模块 2 驱动）。
- **成员名册**：复用 `deskmate/roundtable/personas.py` 现有 persona 过滤逻辑（圆桌 7 专家 + 创作 4 角色 + 可加「纯陪伴型」新 persona，如 `companion-empathic` 等）。
- **文件清单**：
  | 文件 | 动作 | 说明 |
  |---|---|---|
  | `deskmate/companions/circle.py`（新） | 新增 | 朋友圈建团/名册/成员 persona 映射；封装 `TeamRegistry.create` + `ChatStore.create_group` |
  | `deskmate/companions/personas_loader.py` | 新增或复用 | 从 registry 取陪伴/创作/圆桌 persona，标注 `companion: true` |
  | `coworker/personas/builtin/*/manifest.md` | 小改 | 给想进朋友圈的 persona 加 `companion: true` 标记（**不改 `team:` trait**，避免 `default_surfaced=False` 副作用，沿用 `teams-integration-design.md` 阶段 2 的评估结论） |

### 模块 2 · 社交驱动层（谁发起闲聊？）—— 核心新增

ds-bot/teams 的 chat 是「被动」的（@才唤醒）。朋友圈要「活」，需要一个**常驻驱动**让 bot 主动发起话题、关心用户。

- **复用已有 `proactive_ledger`**（小桔已有主动行为账本，取代浮动层 nudges）：把「主动关心/发起闲聊」收敛进 `proactive_ledger` 模块，而非新造。
- **新增 `deskmate/companions/driver.py`**：
  - 定时 tick（小桔已有调度机制，复用，不引 launchd）：读取用户近期日记/手记/上下文 → 触发某成员 `@小桔` 或 `@用户` 抛一个自然话题（如「今天写到哪了，卡不卡？」「我读了你昨天的手记，想到一个角度…」）。
  - 成员之间互相关心：随机/情境触发 bot A `@bot B` 问近况，模拟朋友间互动。
  - **成本护栏**：复用 `TeamRegistry.count_wake` 的每小时唤醒预算（已在 `teams-integration-design.md §3.5` 落地），社交唤醒计入预算，超预算静默。
- **文件清单**：
  | 文件 | 动作 | 说明 |
  |---|---|---|
  | `deskmate/companions/driver.py`（新） | 新增 | 社交 tick + 话题生成 + 唤醒预算；调用 `chat.post` 落群聊 |
  | `deskmate/proactivity/` | 改 | 把「朋友关心/闲聊」意图接入 `proactive_ledger`（已有） |
  | `deskmate/companions/web.py`（新） | 新增 | 朋友圈前端入口/事件流（复用 `TeamChatView`） |

### 模块 3 · 共享花园 / 日记（投影 + bot 留言通路）

- **花园投影**：garden / personal-center UI 新增「朋友圈」面板，从 `JournalStore` 按 `case="garden-shared"` 读取、按 `persona`/`actor` 过滤展示——bot 的留言、用户的留言、互动态都在此可见。
- **bot 留言通路**：提供 `deskmate/companions/garden.py:bot_journal(actor, body, persona)`，内封装 `journal.append(actor=Actor(id, Role.WORKER), case="garden-shared", body=body, kind="companion_note", persona=persona, space="companions")`。bot 经此在花园留痕。
- **防滥用**：`kind` 限定、正文超长截断（`JOURNAL_BODY_LIMIT` 已强制），taint 标记区分「bot 自动生成」便于 UI 标注。
- **文件清单**：
  | 文件 | 动作 | 说明 |
  |---|---|---|
  | `deskmate/companions/garden.py`（新） | 新增 | 封装 bot→garden journal 写入 |
  | `surfaces/gui/.../GardenView.tsx` | 改（复用 TeamChatView 思路） | 朋友圈/花园投影 + bot 留言展示 |

### 模块 4 · 记忆/技能共享账本（彼此了解）

- **设计**：用 journal 的 append-only + 哈希链特性，建一个**共享记忆空间** `case="companions-memory"`，每个 bot 可：
  - 写：把自己的一段经验/技能/偏好以 `kind="experience"` / `"skill"` 落账（`journal.append(..., case="companions-memory", persona=p)`）；
  - 读：经 `JournalStore` 按 `persona` 过滤读取他人的经验（已有 `_check_access`/索引 `idx_journal_case` 支撑）。
- **为何用 journal 而非互访 memory**：journal 不可变、带哈希链、天然防篡改，作为「朋友圈共享记忆」比直接读彼此私有 session 更合规、更安全（不碰神圣数据红线）。
- **文件清单**：
  | 文件 | 动作 | 说明 |
  |---|---|---|
  | `deskmate/companions/memory_share.py`（新） | 新增 | 写经验/读他人经验，封装 `journal.append/read` |
  | `deskmate/companions/driver.py` | 改 | 启动时常驻成员互读共享账本「认识彼此」 |

### 模块 5 · 自主干活（借力上游 headless，需先合入）

- **前提**：`coworker/headless/runner.py`（OPE-196）在工作区 v0.3.0 **不存在**，需从 `origin/main` 按文件 cherry-pick（**不整仓 merge**），与已落地的 `5873dc7` 安全补丁同分支（`fork/base`）。
- **接法**：headless `run(args)` 模块级函数，经 `get_registry().ids()` + `get_agent(args.coworker)` 复用 persona 注册表与 agent 引擎；`--coworker <id>` 选朋友圈某成员跑一个任务、自动审批、留轨迹（`audit.db`）。
- **我们造的桥**：`deskmate/companions/delegate.py` 暴露 `dispatch_to_companion(persona_id, prompt)` → 内部 `from coworker.headless.runner import run` + 构造 `args(coworker=persona_id, prompt=..., mode="auto-approvals")` 子进程调用，结果回写朋友圈 `journal.append`（kind="task_result"）+ 群聊播报。
- **边界**：headless 是**任务级**（跑完退出），不是 7×24 常驻；常驻社交态仍由模块 1–4 的 teams 基座承载。两者经 delegate 桥接。

### 模块 6 · 外脑朋友（A2A/MCP peer 入圈，可选增强）

- 复用已落地的 `opencode_a2a_server.py`（OpenCode peer）+ `deskmate/mcp/roundtable_server.py`（Hermes peer）：把 Hermes/OpenCode 作为「特殊朋友」加入朋友圈 team（角色 `worker`，但标注 `external=True`），它们也能被 @、能在群聊发言、能接派活。
- **收益**：小桔朋友圈不止本地 bot，还含真实强 Agent——与萧哥「AI 圈多 Agent 社交」的长期方向一致。

---

## 5. 实施阶段（先核心社交，后增强，最小可用优先）

**阶段 0 · 上游同步（0.5d，前置）**
- 从 `origin/main` 按文件 cherry-pick `coworker/headless/*`（OPE-196）+ 确认 `5873dc7` 安全补丁无冲突；**严禁整仓 merge**。验证：`openworker run --coworker <id> --prompt "..."` 本地跑通。
- 红线：仍只改 `coworker/personas` 安全洞 + 新增 headless；`coworker/teams/*` 零改动。

**阶段 1 · 朋友圈实体化 + 群聊骨架（1–2d，最小可用）**
- `deskmate/companions/circle.py` 建常驻 team + 朋友群；`personas_loader` 标 `companion: true`。
- 验证：建团后 `chat.post` 能发首条、bot 能被 @唤醒回复（手动触发即可，先不接驱动）。

**阶段 2 · 社交驱动层（2–3d，让朋友圈「活」）**
- `driver.py` + `proactive_ledger` 接入：定时/情境触发 bot 主动闲聊、互相关心；接 wake budget 护栏。
- 验证：静置一段时间，朋友圈自动产生 bot 主动发言 + 互 @；预算超限静默。

**阶段 3 · 共享花园/日记投影（1–2d）**
- `garden.py` 写入通路 + GardenView 投影；bot 留言落 `case="garden-shared"`。
- 验证：bot 留言出现在花园；按 persona 过滤正确。

**阶段 4 · 记忆/技能共享账本（1d）**
- `memory_share.py`：经验/技能写读；启动互读认识。
- 验证：bot A 写经验 → bot B 能读到并据此互动。

**阶段 5 · 自主干活桥 + 外脑朋友（2d，增强）**
- `delegate.py` 接 headless；A2A/MCP peer 入圈。
- 验证：`dispatch_to_companion` 跑通；Hermes 在群聊中被 @回应。

---

## 6. 上游同步策略（铁律）

- **`coworker/teams/*` 保持零改动**（与 `teams-integration-design.md §7` 一致）：所有团队语义经 `deskmate/teams_client.py` 薄壳取单例，新增逻辑全在 `deskmate/companions/*`，降低与上游合流冲突。
- **headless 仅 cherry-pick 不 merge**：上游 v0.3.0→v0.3.4 共 125 提交，大量 GUI/connector 改动会冲掉 DeskMate 定制。只取 OPE-196 headless 相关文件 + 已 cherry-pick 的 persona 安全补丁。
- **`coworker/personas` 仅加标记**：`companion: true` 是新增字段（需确认 `manifest.py` 的合法 trait 白名单是否需扩；若白名单严格，改 `manifest.py` 一行 `VALID_COMPANION` 即可，属最小改动）。

---

## 7. 风险与未决项

| 项 | 风险 | 缓解 |
|---|---|---|
| 社交唤醒成本 | bot 主动闲聊累积 token | wake budget 护栏 + 低频 tick（如每日上限 N 次） |
| 神圣数据 | bot 可能误读/改写用户稿件 | journal 只存引用/片段/留言，不移动/改写原文；共享账本独立 case |
| 沉浸感 vs 噪声 | 太多 bot 发言扰民 | `proactive_ledger` 阈值 + 用户可关「朋友主动关心」 |
| 上游 headless 变动 | OPE-196 仍在演进 | cherry-pick 时锁版本，delegate 桥做适配层隔离 |
| persona 标记白名单 | `companion: true` 可能不合法 | 阶段 0 验证 `manifest.py`；不行则扩一行白名单 |
| 多 persona 记忆膨胀 | 每个 session 累积 | 遵循 pre-spawn（未派活 0 token）+ 定期 journal 摘要压缩 |

---

## 8. 开放决策点（实施前需萧哥拍板）

1. **朋友圈成员范围**：仅现有圆桌 7 专家 + 创作 4 角色？还是新增「纯陪伴型」persona（如倾听者/陪伴者）？
2. **社交主动度**：默认开「朋友主动关心」还是默认关（用户手动召唤）？
3. **花园开放程度**：bot 留言对所有成员可见（含用户） vs 仅小桔可见？
4. **外脑朋友**：是否把 Hermes/OpenCode 纳入常驻朋友圈（涉及跨进程/机器 A2A 稳定性）？
5. **headless 合入时机**：阶段 0 是否现在就 cherry-pick（可能引入上游其他连带改动需验证）？

---

*本方案遵循「先对齐再实施」原则；任何 §8 未决项在实施前需与萧哥再确认一次 tradeoff。所有接口名、状态机、文件清单均对照本地 `fork/base`（与 `origin/main` 在 `coworker/teams/` 上零差异）及 `origin/main` 的 headless 模块核实。*
