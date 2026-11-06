# Bot Mode 统一改造规格：主编线 + 圆桌线合并

> 状态：**阶段 1 骨架 + 阶段 2 真 Agent 运行时 + 阶段 2.5 原生流式群聊均已落地（2026-11-06）；阶段 3 退役待建群测试通过后执行**
> 萧哥 11-06 14:15 三个决策：① 先做新 bot 并建群测试，成功后圆桌才退役；「先读《夜不收》」是自然语言，bot 自动去读，**不做「阅读全书」工具按钮**；② 质疑 7 位专家「不是真 Agent 能用吗」（核实结论见 §11）；③ **不要纪要**。
> 发起：萧哥 11-06 判断「圆桌在设计时是超前的，但现在已经落后了；不应该坚持用那一套东西去套现在的新东西」。两条线一起重做成新 bot 形式。
> 参照物：Grok Bot（xAI）、Hermes Agent Bot Mode（Nous Research，本机 `~/.hermes/hermes-agent` 有源码）。

---

## 1. 结论先行

**不改造圆桌，也不改造主编；两条线一起退役，换成一套新的 bot 形态。**

- 旧圆桌 = **人设 + 表单 + 选人 + 点开始** 的批处理器（死的）。
- 旧主编 = **真 Agent + 看板 criteria + 审核打回 + lead 裁决 + 定时 tick** 的自治流水线（失控且烧钱）。
- 新形态 = **bot 常驻（真 Agent 身份）+ 群临时（可建可撤）+ 自然语言驱动 + 硬上限护栏**。

关键判断修正：真 Agent **不是**问题。今天 68 次唤醒/小时来自「自治调度层」（多实例 × 定时 tick × 自动裁决），不是 session 身份本身。上游 `manager.py:2858` 明写 pre-spawn worker session **state on disk、zero tokens**，bot 一直在 = 0 花费，被 @ 才花。

---

## 2. 参照物实证（非印象）

### 2.1 Grok Bot（xAI，2026-08）
- 每个 bot 是**有名字、有持久状态的 AI 同事**，专属云电脑（浏览器 / 文件系统 / 终端），跨会话记忆累积不归零。
- 多个 bot 可放进同一群聊，**自主协商分工、互相传递任务所有权**，用户只在关键节点被召唤。
- 通过 `@bot` 交互；bot 甚至可以创建新的 bot 给自己组队。

### 2.2 Hermes Bot Mode（本地源码核实，比二手报道更可信）
| 概念 | 实现 | 本地证据 |
|---|---|---|
| bot = profile | 每个 bot 一个隔离目录 `~/.hermes/profiles/<id>/`，含 config / memory / skills / 凭据 / 聊天史 / SOUL.md / 固定模型 | 本机实存 `dapao`、`xiaoju` 两个 profile |
| 名册 | 左侧 Bots tab，一行一个 bot；**零新存储层**，"a bot is just a Hermes profile" | `apps/desktop/src/plugins/hermes-bots/roster-pane-*.ts` |
| 群 | `hosted_rooms` 表 + append-only event log；`create_room` / `disband_room` / `rename_room` / `list_rooms` 一等公民 | `gateway/hosted_rooms.py:871/1141/933/920` |
| 撤群 | 打 `disbanded_at` 墓碑 + `hosted_room_retired_ids` 永久 retired id（**room_id 永不复用**，90 天保留），**不删消息** | `gateway/hosted_rooms.py:99/115/1123` |
| 群 RPC | `groups.create / send / disband / state / log / stop / rename / context / retry` —— **没有任何议题表单** | `gateway/session_group_controls.py:33-45` |
| 自然语言 | 用户消息不 @ = 全员；`@` 谁谁答；`@all/@everyone`；bot 可 `pass` | `gateway/hosted_room_discussion.py:342/762/767` |
| 硬上限 | `MAX_DISCUSSION_MEMBERS=6 / MIN=2 / MAX_ROUNDS=3 / MAX_MESSAGES=10`，到界返回 `bounded` | `gateway/hosted_room_discussion.py:28-31/855` |
| 无新引擎 | 就是 profile / cron / CLI handoff 原语上面一层 UI；无 daemon、无新存储 | 官方定位："Desktop UI over the profile primitive" |

共同哲学（Teknium 原话）：**"Build a specialist Bot once to use it forever."**

---

## 3. 我们仓库的实证：活的运行时其实已经存在

| | 死的（圆桌） | 活的（原生 teams 群聊） |
|---|---|---|
| 入口 | `web.py:129 StreamBody` 五字段：topic / context / manuscript_path / chapter_range / personas / peers / max_rounds | `TeamChatView.tsx` 输入框 |
| 触发 | 填好表单 → 选人 → **点开始** | 直接打一句话 |
| 发言者 | `coordinator.py` 进程内 `chat_stream` **人设**，有界 2 轮，无独立 session | `post_team_chat` → `ChatStore.post(author_role="user")` → `unread_for` → 每个 worker **真实 session** 各自署名回话 |
| 唤醒 | 一次性批跑 | 用户发言唤醒全员（`chat.py:173`）；@ 定向唤醒 |
| 产物 | `teams_bridge.setup()` 建 board 维度项 + 群，`finalize()` 走 open→in_progress→review→done 状态机 | 只有群里的消息 |
| 接口 | `/api/stream` | `app.py:1272/1276` `GET\|POST /v1/teams/{id}/chat` |

`chat.db` 实存 **11 个群全是 `圆桌终审委员会·final-review:creation:xxx:3`**，成员 `persona` 字段**全为空串**，且 `ChatStore` 只有 `create_group/get_group`，**没有 `list_groups` / `delete_group`** → 只能建不能撤。这就是「死」的物证。

`docs/teams-integration-design.md §3.1` 也写明：圆桌当初被**有意**设计成 "loose coupling、7 专家不预生成真实 session" —— 2026-08 是先进的研讨编排，2026-11 就是表单批处理器。萧哥的判断成立。

---

## 4. 统一形态定义（五条，验收以此为准）

1. **bot 常驻**：bot 是有名字 / 人格 / 独立记忆 / 模型的实体，一次建好永久用；名册直接读官方 PersonaRegistry，零新存储层。群解散后 bot 及其记忆仍在。
2. **群临时**：一个话题一个群，可建、可列、可撤、可改名；撤群打墓碑、不删消息、room_id 永不复用。群不是常在物。
3. **自然语言驱动**：开群只需给个话题名，之后在群里说人话。不 @ = 全员，@ 谁谁答，bot 可以 pass。**没有稿件路径 / 章节范围 / 讨论方向 / 轮次表单，没有「开始」按钮。**
4. **有界护栏**：2~6 个成员、3 轮、10 条消息硬上限；不 @ 不调用、群散即停；无定时 tick、无自动派活、无 lead 自动裁决。
5. **不造新引擎**：复用官方 PersonaRegistry / ChatStore / worker session / `count_wake`；`coworker/teams/*` 保持零改动，新逻辑全在 `deskmate/bots/`。

---

## 5. 概念映射

| Bot Mode 概念 | 落点 | 状态 |
|---|---|---|
| bot = profile | `coworker/personas/builtin/*/manifest.md` + `create_team` pre-spawn session | ✅ 现成 |
| 名册（Bots tab） | `deskmate/bots/roster.py`（投影 registry）+ 右侧「我的伙伴」`RoundtableContacts.tsx` | ✅ 阶段 1 已落 |
| 群房间 | `deskmate/bots/rooms.py`（`bots.db` 元数据 + 官方 `ChatStore` 消息） | ✅ 阶段 1 已落 |
| groups.* RPC | 待接 `app.py` / `roundtable/web.py` 的群级路由 | ⬜ 阶段 2 |
| 自然语言发言 | `RoomStore.post` → `ChatStore.post`（@ 解析 + 未读唤醒原生） | ✅ 阶段 1 已落 |
| 硬上限 | `deskmate/bots/discussion.py` + `TEAM_WAKE_CAP_PER_HOUR` | ✅ 阶段 1 已落 |
| 群聊 UI | `TeamChatView.tsx`（作者分组 / @ 高亮 / composer） | ✅ 现成，待接新路由 |
| 独立 worker session 唤醒 | `manager.py:3185 _drain_team_member` | ⬜ 阶段 2 需从 team 解耦 |

---

## 6. 拆什么、留什么

**拆（阶段 2/3）**
- `Sidebar.tsx:1386` 新建会话白名单里的 `editor-in-chief`（连带 `Sidebar.test.tsx` 断言）。
- 主编线的看板 criteria / 审核打回 / lead 自动裁决 / 定时 tick 唤醒 / 自动派活；`editor-in-chief` 等 manifest 的 `team:` 自治语义。
- 圆桌 `/api/stream` 的表单 + 点开始 + `coordinator` 人设编排 + `teams_bridge` 造 board 项与僵尸群。
- 三个 `paused=true` 的僵尸团队（已在 11-06 13:40 可逆止血，备份 `~/.config/coworker/teams.json.bak.20261106-1340`）。

**留（别误删）**
- 真 Agent session 引擎（pre-spawn 0 token）——这正是 bot 的载体。
- `coordinator.py` 的「到达序 + 有界轮次」可作为群内调度策略参考；**纪要 / 主持人收尾一律不做**（萧哥 11-06 决策 3）。
- 圆桌「阅读全书 / 圣经抽取」昨天刚修好付费通道，萧哥正在用，**阶段 3 前保持原样不动**；新形态里不设这个工具 —— 群里是真 Agent，说「先读《夜不收》」它自己调只读工具读稿（萧哥 11-06 决策 1）。
- 7 位创作专家的 manifest / 人设 / 头像 / 模型；「我的伙伴」UI 骨架。
- `docs/living-companions-plan.md` §4 模块 1「常驻实体 + 独立 session」可复用；但**模块 2「社交驱动层 / 定时主动闲聊」必须砍掉** —— 那正是 68 次唤醒的病根。

---

## 7. 分阶段

**阶段 1 · 骨架（本次已落，纯新增、不动现有流程）**
- `deskmate/bots/{__init__,roster,rooms,discussion}.py` + `tests/test_bot_mode_skeleton.py`（10 例，先红后绿）。
- 群元数据放同 base 的独立 `bots.db`（避开 `.deskmate/` 神圣目录，与 `chat.db`/`teams.db` 同目录）。
- 零改动 `coworker/teams/*`；无任何现有入口引用新包，圆桌与主编照旧可跑。

**阶段 2 · 真 Agent 运行时（已落地，独立可测，不动桌面端）**
- `deskmate/bots/reading.py`：自然语言书名 → 只读书稿；目录遍历 `guarded_dir_entry`、正文 `guarded_assistance_read`，路径必须仍在 books_root 下，输出上限 12000 字。**只有 `list_books` / `read_book` 两个工具**。
- `deskmate/bots/runtime.py`：7 位专家经官方 `PersonaRegistry.agent()` + `build_engine()` 起**真 TurnEngine**；`dataclasses.replace` 换只读 tool_factory 并收紧 registry；provider 走 `llm_channels.route(GROUP_PAID)`（**不跟随主模型 opencode-go**）；`max_iterations` + 群历史 20 条 + 一轮一句，成本有硬上界。
- `rooms.py::say()`：说一句 → `resolve_mentions` 定唤醒名单 → 各 bot 自主跑一个 turn → `ChatStore.post(author_role="bot")` 各自署名；后发言者看得到前面发言，但不递归唤醒、不生成纪要。
- `deskmate/bots/web.py`：**独立群聊页**（`python -m deskmate.bots.web --port 9921`），可点建群/说话/撤群，本轮**不必重打包桌面 App**。
- `tests/test_bot_mode_runtime.py`（11 例，先红后绿）：锁住自主读稿、只读工具面、付费通道、无表单、无纪要、可撤群。
- 尚未做：接进桌面端 `TeamChatView`（前端改动须重打包）、云端 `deploy_web.sh`。

**阶段 2.5 · 原生流式群聊（已落地，仍只动 `deskmate/bots/`）**

关键判断：**不需要新建一套 LLM 流**。`runtime.py:186` 起的官方 `TurnEngine` 本来就逐 token yield `ASSISTANT_DELTA`（`coworker/engine.py:559-570`），旧的 `_drive` 用一行 `pass` 把事件全丢了、再从 `messages` 捞整段文本。所以改造 = **不丢事件 + 加一条 SSE 管道 + 前端增量渲染**。

- `runtime.py::speak_stream()`：同一 engine / 同一 prompt / 同一份常驻记忆，把 `ASSISTANT_DELTA` → `delta`、`TOOL_STARTED` → `tool`，末尾给权威 `final`；被 `aclose()`（客户端断开）时调 `engine.request_interrupt()` 真正停流，避免「页面关了还在烧 token」。
- `rooms.py::say_stream()`：`start` → 每 bot 的 `bot_start` / `delta*` / `bot_end`（或 `bot_pass`）→ `done`；**只在 bot 说完后 `ChatStore.post` 落库一次**，`bot_end` 的文本与落库完全一致（不双写、不留半成品）。原 `say()` 保留，旧契约与 21 例回归不破。
- `web.py`：`POST /api/rooms/{id}/stream` 标准 SSE（`X-Accel-Buffering: no` / `no-cache`），`create_app(..., runtime=...)` 可注入便于离线测试；内嵌页改用 `fetch().body.getReader()` 分帧 + `textContent += d.text` 逐字生长气泡，`bot_end` 用权威文本覆盖，结束再按落库消息重绘一次保证一致。
- `tests/test_bot_stream.py`（9 例）：锁 delta 拼接 == final、自主读稿可见、断开掐流、@ 定向 / pass、SSE 帧与响应头、真 TurnEngine 集成、旧 `/say` 不破。
- 仍然**不重打包桌面 App**：9921 独立页即可验收逐字体感；`coworker/teams/*` 零改动、`ChatStore` 只读复用、凭证仍走 `route(GROUP_PAID)` + SecretStore。
- 若要"最原生"（主 GUI 的 `/ws/session` WebSocket + `App.tsx` 逐字渲染），属阶段 3 接进桌面端时再做，本轮不动 `coworker/server/app.py`。

**阶段 3 · 两条线退役（建群测试通过后才动）**
- 撤主编入口、下线圆桌表单；**不做任何「阅读全书」迁移**（真 Agent 自然语言读稿已覆盖）。
- 主编辑 6 个 manifest 标 `bot: true` 纳入名册（不改 `team:` trait，避免 `default_surfaced=False` 副作用）。
- 清理 11 个僵尸群（**只打墓碑不物理删消息**）。

---

## 8. 验收

1. 新建会话下拉不含主编；`/v1/personas` 里 bot 仍在，数据没删。
2. 建群只需话题 + 成员，无表单字段；成员 `persona` 非空。
3. 群里发一句不带 @ → 全员各自署名的独立消息，不是一条综合回复。
4. `@历史` 只唤醒该 bot；bot 可 pass；3 轮 / 10 条到界即 `bounded`。
5. 撤群后 `list_rooms()` 不再出现，但历史消息可查；room_id 不可复用；重复撤群幂等。
6. 三个团队保持 `paused`，重启 app 不复活；看板 30 项与那份汇总 md 仍可查。
7. 群里说「先读《夜不收》」→ bot 自主调用只读工具、正文进上下文、各自署名发言；**不存在「阅读全书」按钮或表单**。
8. 群消息里没有纪要 / 总结 / 主持人消息；`author_role` 只有 `user` 与 `bot`。
9. 圆桌「阅读全书」在阶段 3 前不受影响（本轮零改动圆桌）。

---

## 9. 红线

- 只 commit `fork/base`，**不 push origin**；云端停机保号，新增 Python 文件须进 `deploy_cloud_sync.sh` 清单。
- `coworker/teams/*` 零改动（上游干净代码，合流红线）；团队语义经 `deskmate/teams_client.py` 薄壳。
- `DESKMATE_DIR` 调用时解析，禁模块级常量 / import 期副作用；持久化原子写。
- 书稿目录与 `夜不收-烧仓脑洞汇总-2026-11-06.md` 只读，正稿一字不动；`guarded_write` 硬红线不变。
- 前端改动须重打包；云端前端另走 `deploy_web.sh`；token 不构建时烘焙。

---

## 10. 萧哥的三个决策（11-06 14:15，已定）

| # | 决策 | 落地 |
|---|---|---|
| 1 | **先做新 bot、建群测试，成功后圆桌才退役**；「先读《夜不收》」用自然语言，bot 自动去读，不要这个工具 | 阶段 2 已做真 Agent + 只读读稿；阶段 3 挂起等测试结论；圆桌本轮零改动 |
| 2 | **「7 位专家不是说不是真 Agent 吗？能用吗」** | 见 §11：能用，但要起 engine，不是复用旧圆桌的人设注入 |
| 3 | **不要纪要** | `rooms.say` 只产 `user` / `bot` 两类消息，无任何总结态；有回归硬锁 |

## 11. 「7 位专家能不能用」核实结论（代码实证）

| 核实项 | 结果 |
|---|---|
| manifest 形态 | `coworker/personas/builtin/{historian,lit-editor,...}.md` 与小桔 `deskmate.md` **同构**：有 `tools: [files, search, shell, todo]`、`default_permission_mode`、`mcp: [novel-memory, deskmate-memory]`。是完整 persona，不是纯人设卡 |
| 旧圆桌为什么"不是真 Agent" | `roundtable/coordinator.py:585` 只把 `system_prompt` 文本塞进 `chat_stream()` 纯 prompt 注入 —— 没起 engine、没工具、不能自己决定读稿。`chat.db` 11 个僵尸群成员 `persona` 全为空串是物证 |
| 改造后 | `PersonaRegistry.agent()` → 官方 `build_engine()` → 真 TurnEngine；实测「先读《夜不收》…」模型**自主**发起 `read_book` 5 次、还自己分章节试，正文进上下文后给出署名发言 |
| 主编线 6 个 manifest 能否直接并入 | **不能直接用**：它们写死「只听命于主编 / 不直接对用户说话 / 走看板派活」，拉进群会重新去派活。要纳入必须改写人设，属阶段 3 范围 |
| 质量风险（未处理） | 7 位专家正文字数仅 **144~401 字**（`historian` 401、`reader` 144），而 `deskmate` 10613 字、`editor-in-chief` 1510 字。人设偏薄是「输出幼稚」的潜在来源之一，形态跑通后建议补厚，本轮未动 |

## 12. 成本对照（11-06 实测）

| | 旧主编真 Agent（自治调度） | 新 bot 群（阶段 2） |
|---|---|---|
| 后台 | 本小时 68 次唤醒、`sleep_until` 排到 13:45 | **0**，不说话就不调用 |
| 一次讨论 | 3 lead + 9 worker 常驻、看板 30 项、互相推翻 | 一句话 → 每 bot 1 turn（内部工具循环 ≤ `max_iterations`），无看板 |
| 工具面 | shell / 写文件 / 审批 / A2A / 外发 | **只有 2 个只读读稿工具** |
| 护栏 | 无 | 6 成员 / 一轮一句 / 10 消息 / 群历史 20 条 / 读稿 12000 字 / 付费通道熔断换道 |

## 13. 仍待定

1. 建群测试的**体感结论**：bot 发言质量是否够专业（7 位专家人设偏薄，见 §11）。
2. 桌面端接入时机（接 `TeamChatView` 须重打包 App；本轮先用独立页 `9921` 验收形态，该页已具备逐 token 流式）。
3. 主编线 6 个角色是否改写人设后纳入名册。
