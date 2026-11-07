# Bot Mode 统一改造规格：主编线 + 圆桌线合并

> 状态：**阶段 1 / 2 / 2.5 / 2.6 均已落地（2026-11-06），并已做真机群聊实测；阶段 3 退役待决策**
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

**阶段 2.6 · 讨论能力硬伤修复（已落地，仍只动 `deskmate/bots/`）**

| 硬伤 | 修法 |
|---|---|
| `max_rounds` 是死代码（只有 1 轮接龙） | `say()` / `say_stream()` 改为真正的轮次循环；`bound_reason` 传**已完成轮数**（第 1 轮传 0），`max_rounds=3` 正好跑满 3 轮、第 4 轮收口；`max_messages=10` 优先截断。≥2 位被唤醒才开多轮，每轮至少 2 位仍在实质发言才继续，全 pass / 独白立即收口。`round_start` 事件带轮次，`turn_id` 带轮次避免同 bot 跨轮气泡覆盖 |
| 中文名没进 `format_history` | `format_history(..., names=)` 把 `author`（bot_id）映射成中文名；`rooms` 把 `room.bot_names` 一路传到 `speak` / `speak_stream`。人设里「文学编辑说…→ 接：…」的示范才对得上说话人 |
| `@中文名` 静默失效 | `resolve_mentions(..., aliases=)` 支持 `{中文名: bot_id}`；`@史料考据` 与 `@historian` 等价。`ChatStore` 自带 mentions 只认英文 handle，不影响唤醒（唤醒走本模块） |
| 显式 `pass` | `discussion.is_pass()` 只认短句里的弃权标记（不误杀「我不接受这个观点」）；pass 不落库、不生成占位消息、不参与后续轮，`say()` 返回 `passed`，流式发 `bot_pass` |
| 跨群串味 | engine / 工具锁 / workspace 的 key 从 `bot_id` 改为 `(room_id, bot_id)`；同一 bot 在 A、B 群是两份独立会话。省略 `room_id` 仍落 `_default` 桶（兼容阶段 2 调用） |
| 上下文三重冗余 | ① 刚发的当前句从群历史里排除；② 每轮开始前 `eng.messages.clear()`，群历史是唯一上下文源；③ 第 2 轮起的 prompt 不重复用户原话，只让 bot 接话。跨发言记忆由 ChatStore 群历史承载，按 room 天然隔离 |

`tests/test_bot_discussion.py`（16 例）锁住上述六项；连同阶段 1/2/2.5 共 **46 例全绿**。

**群聊实测（2026-11-06）**

- **真机**：付费通道 `deepseek-v4-flash` 起真 `TurnEngine`，`@史料考据` 定向 → SSE 200、`text/event-stream`、`X-Accel-Buffering: no`、21 个 `delta` 逐字到达、`final` 拼接与落库文本完全一致、只有 `historian` 落库。
- **真实 HTTP 多轮**：uvicorn + 可控 runtime，2 位 bot 全员讨论 → `round_start [1,2,3]`、6 条各自署名发言、6 个 `turn_id` 唯一、`done.rounds=3 / bounded=max_rounds`、落库 `1 user + 6 bot` 与 `bot_end` 一一对应。
- ⚠️ **体感结论**：真机首 token 延迟 **69.5 秒**（ark plan 通道），单次发言总耗时约 89 秒。逐字流式 + typing 占位能缓解等待感，但 3 轮 × N 位 bot 串行会把一次发言放大到数分钟 —— 阶段 3 前若要正式用，需要评估并发化（多 bot 并行起 turn）或换更快通道。
- 实测还发现模型爱在发言开头自加 `（来自 @historian · …）` 署名，已在 `_GROUP_GUIDANCE` 明令禁止；SSE 里重复的第二个 `start` 事件也已去掉。

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

1. ~~建群测试的体感结论~~ → 已实测，见 §7「群聊实测」。**遗留**：首 token 69.5s 导致的串行多轮耗时，需决定并发化 / 换通道。
2. 桌面端接入时机（接 `TeamChatView` 须重打包 App；本轮先用独立页 `9921` 验收形态，该页已具备逐 token 流式 + 多轮轮次分隔）。
3. 主编线 5 个角色（文档旧写 6 个，代码实测为 5）是否改写人设后纳入名册。
4. 名册数据源仍读 `roundtable: true` 字段；退役圆桌前须先切成独立的 `bot: true`（`roster.py:51`、`manifest.py:100-102`），否则清理旧字段会让名册变空。
5. 8 位专家人设声明的 `tools` / `mcp` / `sw-*` 技能与运行时实际只有 `read_book` / `list_books` 不一致；`historian` 等仍保留圆桌时代的「逐条报告体」输出格式。群聊层的「100~300 字」硬上限已于 11-06 18:00 撤销（见 §14），但人设里的报告体仍待收敛。

## 14. 人设补厚后的真机复测与两处收口（11-06 18:00）

萧哥反馈「测试能跑，但详细建议不够具体」→ 7 位专家人设由 144~401 字补到 833~1535 字，新增第 8 位「创意脑洞专家」`brainstormer.md`（三方向：保守／中间／激进，每个写清是什么、读者反应、前面埋什么伏笔），同步扩入 `BOT_ORDER` 与 `ROUNDTABLE_ORDER`；群聊侧另补逐 token 流式与六项讨论硬伤（`f47adb3d` / `840fc0dd`）。

**真机复测**（9921，付费通道，2 位建群、`@brainstormer` 定向只唤醒 1 位）：说「先读《夜不收》，给三个方向，要具体到伏笔埋在哪儿、读者翻到哪儿会愣住」→ 单轮、1676 字。bot 自己读到第十一章末（孔大贵死于仓门、三人漂江心、李昖被当饵），三个方向全部回收正文已有伏笔（信号弹在渡口交到宁靖手里、铜雀渡船家、白烟无粮焦味），逐一交代伏笔在第几章、读者在哪句愣住，收尾还提醒别把方也行的贫嘴写断。**「建议不够具体」已解决**。`ROLES=['bot','user']`、无纪要、书稿只读未动、撤群留档。
⚠️ 单轮 **247 秒**（多次读稿 + 长输出），比阶段 2 的 67~89 秒明显变长；多轮串行会放大，见 §13-1。

**实测翻车 → 两处收口**（新增 5 例回归，bot 群共 51 例全绿）：

1. 发言开头仍带 `（来自 @brainstormer · 创意脑洞专家）`。`840fc0dd` 只在 `_GROUP_GUIDANCE` 里「明令禁止」，本质是**请求模型自觉**，模型不遵守就漏到群里 → 加确定性 `strip_self_attribution()`，在 `speak()` 与 `speak_stream()` 两条路径的落库文本前剥离；只认开头单独一行的括号「来自 @…」或「@id：」，正文里的括号不受影响。剥的是模型自加的元数据噪声，不是语气／话术约束，不违「真实优先」。
2. 同一段还压着「一次发言简短（100~300 字）」，与「具体到能落笔」**直接矛盾**；这次模型碰巧选了人设写 1676 字，换个轮次／通道就可能被压回空泛 → 撤掉硬字数上限，改成「按人设的输出格式写足，别为压字数砍掉可执行细节；纯接话轮次才简短」。

**另补红线**：8 份专家人设（含新增 `brainstormer.md`）原本不在 `deploy_cloud_sync.sh` 的 `PERSONA_FILES` 传输数组里，但 `_reconcile` 会扫全部 `coworker/personas/**/*.md` → 复机时会出现「报不一致却只查不传」的老坑；已把 8 份补进传输清单。

**遗留（未擅自改）**：建群下限 2 位（`DiscussionLimits.min_members`，测试标「Hermes 同款硬边界」），故「单独只拉 1 位 bot」目前走不通，只能 2 位建群后 `@` 定向；markdown 加粗／分隔线等 AI 味按「真实优先」没加禁词表，留萧哥目测。

## 15. 「1676 字是不是硬上限」核实 + 两处仍在砍内容的真上限（11-06 18:30）

萧哥追问：单轮 1676 字是不是硬上限？如果是，就怪不得上轮测试没看到建设性意见，字数额度太少没法展开讲。

**核实结论（代码 + git 时间线实证，不靠推断）**：

| 项 | 事实 |
|---|---|
| 1676 字 | **不是上限**，是 §14 那次真机（撤销字数约束后）的实际产出长度。落库 `ChatStore.post` 与前端 `web.py` 均全文，无任何 `[:N]` / `_trim` |
| 上轮为什么没建设性意见 | 猜对了方向：当时 `_GROUP_GUIDANCE` 压着 **「一次发言简短（100~300 字）」**，是 prompt 级软约束（模型会遵守，但不是代码截断）。生效窗口 `b756e2b3`（14:43）→ `d1fdeb9d`（18:23），萧哥那轮测试正在窗口内。已在 §14 撤销 |
| 输出侧唯一的硬上限 | `coworker/providers/openai_provider.py:143` `DEFAULT_MAX_TOKENS = 32000`（`kwargs.setdefault`），≈ 2 万汉字以上，1676 字远未触及，不是瓶颈 |
| 不是上限但确实在砍内容 | ① `runtime.format_history` 每条群历史 `text[:600]`；② `BookShelf.max_chars = 12000` 且读不全时只说「其余未加载」，不告诉 bot 后面有哪些章节 |

**本轮顺手根治这两处**（都是「没法展开讲」的结构性根因，不是加个字数额度）：

1. **群历史撤销单条 600 字截断** → 改「总量预算 `_HISTORY_BUDGET=18000` 字 + 从最新往回取」：够装下 10 条 1676 字的完整发言，最新一条必完整（结论通常在结尾），更早的按预算裁，标注省略条数。仍守 `_HISTORY_LIMIT=20` 条。零额外 token 成本之外的线性膨胀。
2. **读稿上限不再等于「只读到开头」** → `read()` 返回里列出被截断／未加载的**章节文件名**，并写明用 `chapter` 参数填关键词续读；`read_book` docstring 同步说明。真 Agent 自己决定要不要多读几次，仍只有两个只读工具、零表单。**不盲目放大 12000 字**（单轮已 247s，放大只会更慢更贵），另给旋钮 `DESKMATE_BOT_READ_CHARS`（200~80000）由萧哥按需调。

**验证**：新增 3 例先红后绿（旧代码分别红在「长发言结尾被砍」「最新发言不完整」「未加载章节名缺失」）；bot 群 54 例全绿；扩大回归（圆桌 4 例文件 + 圣经抽取 + bot 群）77 例全绿。独立页 9921 已重启加载新代码；未重跑 247s 真机 LLM 验证（成本太高，改动为纯确定性上下文拼接，离线用例已锁）。

---

## 16. 整体方案：保质硬前提下的省时省钱（11-06 20:43 萧哥拍板，一次性改造）

萧哥定调：**「省时省钱重要，但质量是底线；没有质量更浪费，因为还得重来。两者不能兼得就选保质。」** 并明确不再一项一项改，要整体方案一次修完。

### 16.1 成本结构：钱和时间到底花在哪（全部为 11-06 本机实测）

| 实测 | 结果 |
|---|---|
| 本地 `read_book` 12341 字 | 0.041s；注入 12000 字原文首 token 2.6s ⇒ **读稿/发文件几乎免费** |
| 回答「四个字」，默认 | 106.8s / completion 7583 token / reasoning **11726 字**；`max_tokens=30` 管不住 thinking |
| 同一四字问题重复 3 次 | completion 4444 / 10828 / 14786，耗时 62 / 140 / 195s ⇒ 默认思考成本随机波动 3 倍 |
| prompt 缓存 | 稳定命中 `cached=4096/5297`（77%），省的是**便宜的输入** |
| 同题创作建议（正文 12000 字，三个方向） | 默认 **329.4s / 20208 token / reasoning 27194 字 / 正文 845 字**；`reasoning_effort=low` 23.0s / 1131 token / 636 字 / 957 字 |
| 质量同题对照 | low 具体度不降（锚点 8、落点 16、空泛 0、字数更多），但**丢了结构性洞察**：默认能把羊皮卷+李如松+龙山仓收束成「烧船不烧仓」的障眼法机制，low 只有单点局部反转且第三条出现堆砌 |

**结论**：`一次群讨论 ≈ N 位 × R 轮工具往返 × (输入 + 长思考 × 输出单价 + 正文)`。**慢和贵的真因是「为找文件反复重付长思考」与「N 位串行」，不是文件 IO、不是共享材料、不是正文字数。**

### 16.2 质量不变量（硬前提，测试逐条锁死，违反即回退）

1. **不降思考强度**：不设 `reasoning_effort`／不关 thinking／不换更弱模型／不换 provider。
2. **不压输出**：正文不截断、不设字数上限、不设 `max_tokens` 人为收紧。
3. **不减视角**：不减唤醒人数、不改 `resolve_mentions` 的默认全员语义。
4. **真 Agent 不变**：每位仍独立 persona / `TurnEngine` / session / 人设输出格式；`read_book` 保留，自主补读权不削。
5. **不压缩材料**：共享的是**原文分块 + 文件索引**，绝不做 LLM 抽取／摘要／圣经／纪要；只读走 `sacred_paths`，零表单、自然语言触发。
6. **接话语义**：第 2 轮起仍能看到完整前文（原 `say()` 语义），不改成并列独白。

### 16.3 只砍「零质量贡献」的开销 —— 一个抽象派生全部收益

新增 **`RoomMaterials`（群级共享材料面）**：把「找材料」从每个 bot 的在线自主决策，上移为**群级一次性本地 IO 准备**。所有省时省钱都从这一处派生，不在别处打补丁。

| 层 | 改造 | 省什么 / 保什么 |
|---|---|---|
| 备料 | `say`/`say_stream` 入口从人话解析书名 → `RoomMaterials.prepare()`：分类排序**正文/小说 > 大纲/人物/脑洞 > 协议/授权/简介/目录**（非创作文本默认不喂）、产出正文优先的原文分块（仍守 12000 字）+ 完整文件索引；按 room 缓存、mtime 失效、「重读」强制刷新 | 砍掉 N×R 轮 discovery 往返；**质量上升**：第一次读到的不再是授权协议（旧 `read()` 前 12000 字全是合同/粗纲/大纲，230KB 正文排第 9） |
| 注入 | `build_prompt(..., materials=...)` 把材料包作为**固定前缀**放在最前（跨 bot 字节一致）；`_GROUP_GUIDANCE` 说明可直接引用、要别的文件用 `read_book` 点名；`read_book` docstring 不再引导「留空读全书」 | 不改任何输出内容。⚠️ provider 的 prefix cache 按完整 prompt（含 system）匹配，各 bot 的 persona 不同 ⇒ **跨 bot 不保证命中，同一 bot 跨轮才可命中**（实测 77% 命中是同一请求重复，不是跨 bot）。不把缓存当成主要省钱项 |
| 调度 | 第 1 轮 N 位**并发**（各自看同一份群历史 + 同一份材料，独立出方向）；**谁先好谁先落库**，`say_stream` 用事件队列合并 N 条流陆续推；第 2 轮起回到串行接话 | 墙钟由 `N×单次` → `最慢一位`；感知延迟由「等全群」→「最快一位」；第 2 轮语义不变 |
| 回退 | `DESKMATE_BOT_PARALLEL_FIRST_ROUND=0` 回到全串行 | 语义存疑时可一键回退，不必改代码 |

**唯一一处语义变化（诚实标注，不伪装成实测）**：第 1 轮原本后发言者看得到同轮前一位的发言，并发后看不到。判断是「第 1 轮本就该各自出方向、第 2 轮才接话」，独立视角更有利灵感；但这是**推断**，必须真机同题对照 + 萧哥目测，不认可用绿灯代替质量验收。开关默认开，若目测不利立即回退。

### 16.4 验收（保质优先，绿灯 ≠ 质量守住）

- **离线先红后绿**：质量不变量逐条断言（无 `reasoning_effort`、正文第一份必须是正文、无截断、唤醒人数不变、`read_book` 仍在工具面）、材料分类排序与缓存失效、并发第 1 轮 / 串行第 2 轮语义。
- **全套回归**：bot 群 + 圆桌 4 文件 + 圣经抽取；`py_compile` / `bash -n` / `git diff --check`。
- **真机**：重启 9921 独立页，同题「烧仓接下来怎么写，给三个方向」对照 §16.1 基线（锚点 ≥ 11、空泛句 = 0、正文不截断），**结构性洞察由萧哥目测**。
- **红线**：不改圆桌与主编线；不 push origin；新 Python 文件必须同步进 `scripts/deploy_cloud_sync.sh` 清单。

### 16.5 实施后验收状态（11-06 21:00，诚实记录阻塞）

| 项 | 结果 |
|---|---|
| 离线测试 | bot 群 **68 例全绿**（新增 14 例：材料层 5 / 质量不变量 4 / 调度 4 / 错误透传 1） |
| 扩大回归 | 圆桌 4 文件 + 圣经抽取 + bot 群 **90 例全绿**；`py_compile` / `bash -n` / `git diff --check` 全过 |
| 确定性备料验收（零 LLM） | 真实书架备料 **0.196s**，第一份＝`夜不收：绝命救援.docx` 正文、授权协议已排除、索引 11 份、缓存命中；材料块 12610 字 |
| 真机错误透传 | 重启 9921 后 SSE 实测 `error_type=RateLimitError` + 完整 429 正文，不再显示「bot 发言出错」 |
| ⚠️ **真机质量对照（当时）** | 21:00 时**未跑成**：付费通道 `ark-agent-plan-cn` 返回 `429 AccountQuotaExceeded`（月度配额用尽，`2026-11-06 23:59:59 +0800` 重置）。engine 的 error payload 已实测拿到完整 429，**不是代码退化**。按保质硬前提**不擅自切未验质量的其他通道**；绿灯不能替代质量验收。**22:05 萧哥授权换套餐，已补跑完成，见 §18** |

**顺带修掉一处真缺陷（可观测性＝质量保障）**：`coworker/engine.py:589` 的 error payload 是 `{"error": ..., "error_type": ...}`，而旧 `BotRuntime._drive` 把事件全 `pass`、`speak_stream` 只找 `data["message"]` —— 429 配额超限被吞成「bot 发言出错」，看起来像模型弃权，害得排查方向差点跑偏。现两条路径都透出真实原因与 `error_type`，新增 1 例锁住。

---

## 17. 群共享材料扩成两层：把小桔已经积累的经验放进来（11-06 21:25 萧哥定调）

### 17.0 性质判定：与红线无关，红线原文不改

萧哥补集后书桶从 55 样本 / 146 万字涨到 **155 样本 / 435 万字**（净增近 290 万字），并明确：

> 「小桔的这些积累，我认为与红线无关，这是**借她的经验，用现成的经验去补群聊天的缺陷**，与圆桌的摘要是有区别的。」

这个区分成立，代码与数据双向核实：

| | 圆桌「阅读全书 / 圣经抽取」 | 本方案接入的东西 |
|---|---|---|
| 何时产生 | **本次讨论现场** | 小桔每天 16:00 逐日维护，早已落盘 |
| 怎么产生 | **跑 LLM 抽取 + 逐条引证核验**（几十次调用，昨天还被 `400 MissingSessionID` 打挂） | **纯本地只读 IO**，零 LLM |
| 是什么 | 对原文**有损压缩**成四份结构化事实（L1 还截断到 4000 字） | **逐字保真的原文留档** + 带正文引文的既成圣典 + 她的进度判断 + 435 万字统计出的文风指纹 |
| 风险 | 现场重做会编造、丢细节、引入 N 次调用 | 已经是她读过并核准过的结果，不重新生成 |

⇒ §16.2(5) 的「绝不做 LLM 抽取／摘要／圣经／纪要」**禁的是现场生成有损压缩**，本方案没有任何生成动作，**不触发该红线，红线原文一字不改**。本节只是澄清适用范围，不是放宽红线。同理不碰「不要纪要」：`author_role` 仍只有 `user` / `bot`，材料是群共享文件，不是主持人收尾。

### 17.1 缺口实证（上一轮取证结论，非推断）

`RoomMaterials.prepare()` 原先只扫书稿目录**顶层 11 个文件**，`<书>/.deskmate/` 子目录整个没进去。真实材料包逐词核验：

| 关键词 | 命中 |
|---|---|
| `CHARACTERS` / `OUTLINE` / `TIMELINE` / `NOTES` / `进度简报` / `圣典` | **全部 0** |
| `烧粮仓` / `第十一章` / `1486` / 停笔句「今晚老子就拿你们的命给孔百户献祭！」 | **全部 0** |
| `孔大贵` / `宁靖` | 61 / 66（来自正文**开头＝第一章**） |

而群里问的正是「烧仓接下来怎么写」＝**第十一章**。⇒ **bot 群在讨论一本它只看了开头、完全不知道作者今天写到哪的书**。上一轮两版都编出原文没有的物件（银簪／谚文／密令 vs 安州／木梳／马市），缺的不是思考强度，是进度事实与作者意图。

小桔那条线是真的（`automation.db: task-680bf4c4dc`「进度跟踪：夜不收」enabled=1、`cron 0 16 * * 1-5`、今天 16 点 `last_status=ok`，指令含【圣典落盘授权】）：

- 四份圣典 **11-06 16:36–16:38** 刚写（CHARACTERS 8875 / NOTES 5513 / OUTLINE 3159 / TIMELINE 1047 bytes）
- **5 份进度简报** 11-02 ~ 11-06，今天 5565 bytes：1486 段 / 97472 字符、第十一章《烧粮仓》、停笔句、孔大贵之死、日线未达
- `style_feeds/` **8 份逐字保真的新增正文 txt**，今天 10662 bytes，开头即第十一章孔大贵对话
- `timeline.jsonl` 的 `progress_tracking` **26 条**，逐日 1198→1290→1384→1438→1486 段
- `style_profile` 的 `manuscript` 桶 **updates=155 / chars=4,354,384**，`profile.summary` + `features` + 量化指纹可读

### 17.2 一个抽象，两层材料

仍用 `RoomMaterials` 这一个抽象：群里放**一份**文件，各 bot 自取。只是这份文件现在有两层，**都是既有落盘内容、都是原文／事实，没有摘要**。

| 层 | 内容 | 来源 |
|---|---|---|
| **经验层（放最前）** | ① 作者整体文风（`profile.summary` / `features` / 155 样本量化指纹，标明跨书而非某本专属）② 最新一份进度简报 ③ 最新一份逐字新增正文的**尾部**（停笔点在那里，不在开头）④ 四份圣典 | `<书>/.deskmate/` + `style_profile`（`BlobStore`） |
| **原文层（原样保留）** | 正文优先的原文分块 + 完整文件索引（§16 已验收的 12000 字） | 书稿目录顶层 |

**只增不减**：原文层一个字不删、索引不删，避免动已经验过的质量基线。经验层有独立预算 `DESKMATE_BOT_EXPERIENCE_CHARS`（默认 12000，合法 200~30000），按 ①→④ 优先级装，装不下的圣典进索引并写明可用 `chapter` 按名补读 —— 确定性截断，不是 LLM 压缩。

### 17.3 为什么这一刀同时保质、省时、省钱

| 维度 | 机制 |
|---|---|
| **质量上升** | bot 第一次发言就握着「今天写到第十一章、停在哪句、孔大贵已死、圣典里哪条带引文」，不再凭第一章硬编；文风基准让方向贴作者的半文半白说书人腔调 |
| **省时** | 零额外 LLM、零额外等待；备料仍是纯本地 IO（§16 实测 0.196s），不破坏第 1 轮并发。注入 12000 字原文首 token 实测只要 2.6s ⇒ **输入侧几乎免费，慢的是 thinking** |
| **省钱** | 不增加调用次数、不增加 thinking 轮次；只增加输入 token（便宜且同 bot 跨轮可命中缓存）。砍掉的是「bot 反复试探找进度」可能发生的往返 |
| **零表单** | 仍是一句「先读《夜不收》」触发；改稿／新简报靠 mtime 指纹自动失效，不必记着说「重读」 |

### 17.4 守卫与边界

- 全部经 `sacred_paths.guarded_dir_entry` / `guarded_assistance_read`：实测 `.md` / `.txt` 可读，`entities.db` / `fts5.db` / `*.db*` 一律被白名单拒绝。**全程只读，绝不写**（`guarded_write` 硬红线不动）。
- 显式排除 `_backup_*/`、`_snapshots/`、隐藏目录与 db —— 实测 `_backup_20261105_1700/` 里有同名旧圣典，混进去会拿昨天的事实裁判今天。
- `read_book` 只在 `chapter` 点名时才补读 `.deskmate/` 文件；**留空行为完全不变**（仍只读顶层原文），不把经验文件挤进正文优先级 0 —— 那正是 §16 修好的「合同排第一」质量事故。
- 文风是**跨书全局统计**（155 样本来自多本书），必须标明「作者整体文风，非《夜不收》专属」，不许 bot 拿它当某本书的设定。
- 圆桌与主编线零改动；不 push origin；新增 Python 文件必须进 `scripts/deploy_cloud_sync.sh` 清单。

### 17.5 验收

- **离线先红后绿**：经验层四段齐备、排除备份与 db、零 LLM 依赖、无摘要生成、mtime 失效覆盖 `.deskmate/`、按名补读、原文层一字不删、跨 bot 字节一致、总量有界。
- **确定性验收（零 LLM）**：真实书架备料，逐词核对「烧粮仓 / 第十一章 / 停笔句 / 1486 / 圣典条目 / 文风 summary」从 0 命中变为命中，并量耗时与第一份文件。
- **全套回归**：bot 群 + 圆桌 4 文件 + 圣经抽取；`py_compile` / `bash -n` / `git diff --check`。
- ~~⚠️ 真机质量对照仍受 §16.5 的 429 月度配额阻塞~~ → **22:05 萧哥授权换套餐后已补跑**，同题对照（锚点 ≥ 11、空泛句 = 0、正文不截断）与完整 SSE 群聊证据见 §18。结构性洞察仍须萧哥目测，**绿灯 ≠ 质量守住**。

### 17.6 实施后验收状态（11-06 21:40，诚实记录阻塞）

| 项 | 结果 |
|---|---|
| 先红后绿 | 旧代码 **13 红 / 2 绿**（2 绿锁的是「留空行为不变」与「红线原文不改」）；实现后 **15 例全绿** |
| bot 群全套 | **83 例全绿**（原 68 + 新 15） |
| 扩大回归 | 圆桌 4 文件 + 圣经抽取 + bot 群 **106 例全绿** |
| 静态检查 | `py_compile` / `bash -n` / `git diff --check` 全过 |
| **零 LLM 确定性验收**（真实书架） | 备料 **0.103s**；材料块 **26351 字**（经验层 12189 + 原文层 12021 + 索引 2112）。上一轮全部 **0 命中**的进度事实现在都进材料：`第十一章` 11 次、`1486` 7 次、停笔句「今晚老子就拿你们的命给孔百户献祭！」4 次、`97472` 1 次、四份圣典 / `155` 样本 / `4354384` 字文风口径齐备；`_backup_` 旧事实 / 二进制 db / 授权协议 **0 混入**；缓存命中 |
| 真机链路 | 9921 重启加载新代码，`health ok books=21`、`bots=8`；建群发同题一句，SSE 正常收口、`error_type=RateLimitError` 如实透出，**经验层未导致服务端异常** |
| ⚠️ **真机质量对照（当时）** | 21:40 时**未跑成**：配额探测仍返 `429 AccountQuotaExceeded`（`2026-11-06 23:59:59 +0800` 重置）。按保质硬前提不擅自切未验质量的通道。**22:05 萧哥授权后已用备用通道补跑完成，见 §18** |

**顺带补一条部署红线**：`experience.py` 与新引入的既有依赖 `deskmate/style_profile_store.py` 都进了 `deploy_cloud_sync.sh` 增量白名单。后者是函数内 import 且 fail-open，漏传只会**静默跳过文风段**、不报错，正是「漏传＝静默失效」的典型。`sacred_paths.py` 是 `reading.py` 既有依赖、非本轮引入，未动。

---

## 18. 备用通道实测与真机质量对照补跑（11-06 22:05 萧哥授权）

萧哥授权：「**真机跑的话，你可以选 deepseek 或者 opencode go 等其他套餐和模型**」⇒ 不再等 ark 月配额 `23:59:59` 重置。但**保质硬前提不变**：先用同题实测证明换道不伤质量，再跑完整群聊；默认通道组（`ark→小米→腾讯`）一字节不动，换道只在验收进程内注入。

### 18.1 通道探测（22:08，六条并发极短请求）

| 通道 | 结果 | 实测 |
|---|---|---|
| `ark-agent-plan-cn:deepseek-v4-flash` | 挂：429 | `AccountQuotaExceeded`，月度配额 `2026-11-06 23:59:59 +0800` 重置 |
| `xiaomi:mimo-v2.6-pro` | 挂：429 | `quota exhausted` |
| `tencent-tokenplan:hy3` | 挂：403 | 套餐包被禁用／无调用权限 |
| `deepseek:deepseek-flash` | 可用 | 1.3s，官方直连 |
| `deepseek:deepseek-v4-pro` | 可用 | 1.6s，返回 `reasoning_content` |
| `opencode-go:deepseek-v4.1-flash` | 可用 | 3.4s，须带 `User-Agent: xiaoju/1.0` + `x-opencode-session`，否则重演 400 MissingSessionID |

⇒ **组内自动回退此刻救不了场**：付费组三条同时挂（火山 429 / 小米 429 / 腾讯 403），熔断表却显示全部可用——因为 bot 运行时不走 `_call_with_breaker` 记账。这是运行时事实，不是代码退化，也说明「换通道」必须显式验质量。

### 18.2 同题单 bot 质量对照（22:11）

题目：`先读《夜不收》，@文学编辑 第十一章烧粮仓之后接下来怎么写？给三个方向`。三条并发，各自独立临时群库与 workspace，走仓库真实的 `TurnEngine` + 真实 26351 字共享材料包（`RoomMaterials`），**零产品代码改动**。

| 通道 | 耗时 | 字数 | 锚点 | 空泛句 | 质量要点 |
|---|---|---|---|---|---|
| `opencode-go:deepseek-v4.1-flash` | 57.0s | 1763 | 6 | 0 | 对齐「任务结果为零、孔大贵没回来」；三方向各带代价与占章，点出《烧粮仓》名实不符 |
| `deepseek:deepseek-v4-pro` | 115.6s | 2012 | 6 | 0 | **精确对齐停笔点 1486 段**，引 1411／1424 段；「火起守军不离岗，制度比火硬」；三向收束到「船到底回不回头」 |
| `deepseek:deepseek-flash` | **57.8s** | **2935** | **11** | 0 | **最佳**：主动 `read_book` 补读《烧仓脑洞汇总》《后半部大纲-V4-讲戏版》并识别后者已过时（孔大贵改死在第十一章）；提出「**宁靖这个誓把账记错了**」——孔大贵不是被倭兵杀死，是死于自己的错算，复仇对象错了，属跨线索结构性收束；另给三根从正文长出来的柱子、三条技术提醒、韩孝民边界 |

⇒ 选 `deepseek:deepseek-flash`。**不是因为它便宜**（官方按量计费，反而可能比 $10/月订阅的 opencode-go 贵），而是「不能兼得就选保质」下取质量最优且耗时与最快通道持平。⚠️ 短探测时它未返回 `reasoning_content`，一度被怀疑「不思考」，真机 2935 字输出证伪该推断——**判通道质量只能跑同题，不能看探测字段**。

### 18.3 完整真机 SSE 群聊（22:13–22:20，胜出通道）

9922 验收实例：`BOT_CHANNEL` 进程内注入 `rooms._default_runtime`（默认配置零改动）；3 位 bot（史料考据／文学编辑／创意脑洞），同题不 @ ⇒ 全员唤醒。

| 项 | 实测 |
|---|---|
| 墙钟 / 事件 | **401.2s**；`start 1` / `round_start 3` / `bot_start 9` / `tool 7` / `delta 7256` / `final 9` / `bot_end 9` / `done 1`；**0 error** |
| 落库 | 9 条 bot 发言、**10589 字**；锚点 23（`第十一章`3、`1486`5、`97472`1、`孔百户`2、`李如松`4、`龙山仓`3、`献祭`5） |
| 第 1 轮（并发） | 三位各自出方向，**全部对齐第十一章停笔点**：1486 段、97472 字符、「今晚老子就拿你们的命给孔百户献祭！」、孔大贵死于仓门、一粒粮没烧着 ⇒ §17 经验层生效 |
| 第 2／3 轮（串行接话） | 真接话＋互相纠错：编辑「我先核了你那句衣服」并把问题推到腰牌；创意专家驳「韩孝民是收口」→「是递价」；考据驳「四个真倭兵是四条人命」（换防≠顶替）；创意专家自报「四支 vs 三支小队」记错、考据复核确认三支；只标不改、无纪要、无主持人收尾、无一句编造，甚至主动标注「按圣典引文用，不算实锤」 |
| 结构性洞察 | **腰牌从通行证变赃证**、**撤退令写在点火之前＝军门从未指望这趟成**、「火没点着，人点着了」——与基线「烧船不烧仓」同级的跨线索收束 |

⇒ §16 的「第 1 轮并发／第 2 轮起串行接话」与 §17 的经验层，在真机都成立；绿灯之外拿到了质量证据。7 次 `tool` 事件说明 bot 确实自主补读，真 Agent 未被材料层架空。

### 18.4 边界与红线（单列）

- **换道不是产品改造**：§16.2(1)「不换 provider」约束的是默认通道，一字未改；本次仅验收进程内注入，不设 `BOT_CHANNEL` 即回 `ark→小米→腾讯`。代码与配置零改动，`git status` 无 modified。
- **不可宣称等价或更省**：基线是 ark 通道 `deepseek-v4-flash`（329.4s / 20208 token / 正文 845 字），本次是官方 `deepseek-flash`。通道换了，质量只能同题比输出，不能拿「更快」当质量结论。
- **结构性洞察最终由萧哥目测**；本节只给事实与我的判断，不代替拍板。
- 本次验收范围 3 位 bot（非全员 8 位），已覆盖第 1 轮并发与 3 轮接话；全员 8 位未重复跑，避免无谓烧钱。
- 9921 保持默认未动（ark 仍 429，`23:59:59` 重置后自动恢复）；9922 为临时群库的验收实例。
- 产物：`/tmp/bot_quality_compare_20261106-221101/result.md`、`/tmp/bot_sse_verify_20261106-221341/report.md`（含 SSE 原始流与落库全文）。

## 19. 模型链路改判 + GUI 接入（11-06 22:35 萧哥拍板，暂不打包）

### 19.1 起因：一个与直觉相反的事实

22:35 萧哥问「群聊的模型是不是跟着所选的主模型走的？如果是，不用去管他了，可以接入 GUI 了」。核实结果**不是**，而且是刻意写死的硬约束：

| 核实点 | 事实 |
|---|---|
| GUI 主模型 | `prefs.json:default_model = deepseek:deepseek-flash`（熔断表 `closed`，主聊天健康） |
| 群聊实取 | `rooms._default_runtime()` → `BotRuntime()` → `paid_credentials()` → `llm_channels.route(GROUP_PAID)`，固定 `ark → 小米 → 腾讯` |
| 主模型作用范围 | `manager.py:269/4987` `_prefs["default_model"]` → `self.model`，只进主对话会话；bot 侧一行 prefs 都不读 |
| 代码原文 | `bots/runtime.py`：「**绝不**用 `_resolve_provider()` 的 default_model」 |
| 运行时实测 | `route(GROUP_PAID)` 返回 `None`（三条全熔断）；临时群库 SSE 两条 bot 都 `error: 付费通道组无可用通道` |

熔断到期实读（`deskmate.db:llm_circuit_breaker`）：小米约 22:45（30 分钟退避）、ark `23:59:59`（配额重置）、腾讯次日约 04:15（403 长冷却）。⇒ 接入 GUI 后，23:59:59 前点群聊会直接 fail-fast；「不用管」当时**不成立**。

### 19.2 根源：旧红线的原因可以被根治，而不是绕过

11-06 圣经抽取事故里，后台裸调 `opencode-go` 网关得 `400 MissingSessionID`。当时的修法是「后台一律改走付费组，绝不跟随主模型」——**把症状当成契约**。真因是 `runtime.py` 自己拼裸 `OpenAIProvider`，绕过了 `registry` 的 `ProviderDescriptor.build()`：descriptor 本来就给 opencode-go 内置了 `User-Agent: xiaoju/1.0` + 稳定 `x-opencode-session`（`registry.py:711-714`）。

官方 `ProviderRouter`（主对话用的同一个 provider）实测：

| 主模型 | `_provider_name` | `_bare` | 网关头 |
|---|---|---|---|
| `deepseek:deepseek-flash` | `deepseek` | `deepseek-flash` | 不需要 |
| `opencode-go:deepseek-v4.1-flash` | `opencode-go` | `deepseek-v4.1-flash` | `UA=xiaoju/1.0` + `x-opencode-session=ses_…` **自动带上** |

⇒ 换成官方 Router 后，「跟随主模型」不再触发 400，旧红线的原因消失。萧哥 22:35 拍板：**改成跟随主模型**（不是加白名单、不是临时切通道）。

### 19.3 落地契约

- 优先级：`DESKMATE_BOT_MODEL=provider:model`（显式覆盖，验收/回退）> `prefs.json:default_model`（GUI 主模型）。
- 两者都把**完整 `provider:model` 字符串**交给 `ProviderRouter`，由它剥前缀、选 client、带合规头；不自拼裸 provider。
- 主模型缺失或为 `auto` 哨兵（没有具体通道可直连）→ 回付费组兜底，`/api/health` 返回 `model_source=paid_fallback` + `model_reason`，**不静默降级**。
- `model_choice()` 零网络零凭证解析，供 health 展示；`resolve_default_provider_model()` 一次解析 `(provider, model, source)`，防 provider/model 串味。
- 已缓存的 engine 沿用旧模型，新 engine 跟随新设置（与「换模型要开新会话」的既有约定一致）。
- **保质硬前提不动**：不降 reasoning、不减人数、不压字数；质量由 GUI 所选模型决定。

### 19.4 GUI 接入（新增入口，圆桌并存，暂不打包）

- 后端：`coworker/server/app.py` 用 `Mount("/bots", create_app(base_prefix="/bots"))`，插在 `/{tenant}` 通配之前（同 `/roundtable` 范例）；群库用 `RoomStore` 默认目录，与独立 9921 页同一份，切换入口不丢群。
- 页面：`web.py` 的 `create_app(..., base_prefix=...)` 把前缀注入 `PAGE` 的 `const BASE`，`api()` 与 SSE `fetch` 统一走 `url(path)`。独立页 `BASE=""`、挂载页 `BASE="/bots"`，双形态共用一份页面，不靠 JS 猜 location。
- 前端：`App.tsx` 加 surface `botroom` + iframe `${httpBase()}/bots/`；`Sidebar.tsx` 加 `nav-botroom`（`chat` 图标）；zh/en i18n；`styles.css` 与 `.roundtable-view` 共用 iframe 布局规则。
- **创作圆桌入口与后端一律未动、并存不退役**——按萧哥「先做新 bot、建群测试、成功后再让圆桌退役」的顺序，先在 GUI 里验一遍。
- **暂不打包**：只改源码并提交 `fork/base`，不跑 `build_local_app.sh`、不重打 DMG、不部署云端。桌面 App 要等下次重打包才看得见新入口；期间继续用 9921 独立页。

### 19.5 验证（实测，不靠推断）

| 项 | 结果 |
|---|---|
| `model_choice()` | `('deepseek:deepseek-flash', 'main', '')` |
| `/api/health` | `model=deepseek:deepseek-flash, model_source=main` |
| 挂载可达 | `GET /bots/` 200、`/bots/api/health` 200、`/bots/api/bots` 8 位；页面 `BASE="/bots"` |
| 独立页 | `BASE=""`，占位符无残留，SSE 路径同样带 `url()` |
| bot 群回归 | 82 例全绿（含新增 10 例、旧断言价值保留） |
| 前端 | `tsc --noEmit` 0 错；`vitest` 414 例 410 绿 / 4 红，经 HEAD `styles.css` 基线对照确认 **4 红为既有**（皮肤对比度 / CSS 同步 / 留言框），与本次改动无关；还原后 md5 一致 |
| Python 全量 | `4707 passed / 38 failed / 1 skipped`（10m43s）。38 红所在 17 个文件做 HEAD 基线对照：**HEAD 与改动版同为 31 红、FAILED 清单逐项一致 ⇒ 零新增红**，恢复后 md5 一致；全量多出的 7 红（通道/熔断/本地兜底）在子集里全绿，是既有的全量顺序污染（14:45 基线同为 38 failed） |
| 9921 真机自检（重启后） | `health.model_source=main`；2 位 bot 群聊 152.5s，`round_start 3` / `final 6` / `tool 10` / `delta 2066`、**0 error**。发言对齐第十一章《烧粮仓》停笔点与「火没真进仓」缺口，出现「糠用三遍第三遍要换手法 / 快火 vs 慢火」的互相纠错；一位自报「只翻到协议粗纲、正文没打开，别当已核」，另一位补读到第十一章引原文纠正仓门倒向 ⇒ 跟随主模型 + §17 经验层 + 真 Agent 自主补读**同时生效**。用完已撤群 |
| 部署清单 | `deskmate/bots/*.py` 9 个在 `SKILLS_TOOLS_MODULES`、`coworker/server/app.py` 在 `LOCAL_MODULES`，两者均进传输循环（537/585）与对账循环（610/670）；**无需补清单**，本次也不部署（云端停机保号） |

### 19.6 边界与红线（单列）

- §18.4 记的是 22:05 验收当时的**历史状态**（默认回付费组）；19 节起默认链路已改为跟随主模型，两节不矛盾，以本节为准。§16.2(1) 的质量不变量原文未动。
- 旧测试 `test_bot_runtime_uses_paid_channel_not_main_gateway` 语义变更，按测试红线**未删用例、断言一字未删**，改名为 `test_bot_paid_fallback_is_not_a_naked_gateway`，价值重述为「付费组兜底也不裸连网关」；新默认由 `tests/test_bot_main_model.py` 锁住。
- `app.py` 的 `except` 会吞掉 `ImportError`：云端若漏传 `deskmate/bots/*.py`，现象是「服务照常起、/bots 悄悄打不开」。已在 warning 里写明先核对清单。
- 未跑真机群聊全文重验：本次只改模型来源与入口，§18 的质量证据仍有效；**桌面 GUI 内的实际点击与 iframe 呈现，等重打包后由萧哥目测**。
- 所有提交仅在 `fork/base`，**未 push origin**。

---

## 20. 用户驱动续轮：不自动烧满 3 轮（11-07 萧哥拍板）

### 20.1 问题与前提（两处澄清，代码实证）

萧哥问「现在是默认 6 轮，跑完后这个群聊就结束吗？」：

- **不是 6 轮**：`discussion.py:21-24` 是 `MAX_MEMBERS=6 / MAX_ROUNDS=3 / MAX_MESSAGES=10`。「6」是成员上限；§18.3 那次是 3 位 × 3 轮 = 9 条发言。`bound_reason` 传**已完成轮数**，第 1 轮传 0、第 4 轮才 `3>=3` 收口 ⇒ 正好 3 轮。
- **跑完不结束群**：`say()` / `say_stream()` 只是「用户发一句 → 一次有界讨论」，`done` 只带 `rounds` / `bounded`，**不发墓碑、不改 `bot_rooms` 表**；`disband_room()` 全仓只被 `DELETE /api/rooms/{id}` 调用。跑完后群仍在 `list_rooms()`、历史全在 `chat.db`、成员与 `bot_names` 不变；再发一句开启新讨论（计数归零，群历史继续累积）。只有显式撤群才结束，且撤群也只打墓碑、不删消息、room_id 永不复用。

⇒ 萧哥的使用形态成立：**群跟着《夜不收》一直存在，直到整本写完；只要不撤，群与共享材料一直在，下次讨论不必重建群、不必让 bot 重新翻资料。**

### 20.2 「资料一直在」的准确含义（避免误解）

- `RoomMaterials.prepare()`（`materials.py:150-199`）每次讨论都重算文件 `mtime+size` 指纹，按 `room_id` 缓存；指纹变则自动重建 ⇒ **作者边写边改稿，下一轮自动带最新进度 / 正文 / 圣典 / 文风，不需要说「重读」**。纯本地 IO（实测 0.1~0.2s），零 LLM、不花思考钱。
- 缓存是**进程内**的：重启服务会重扫一次（仍是 0.1s 本地 IO，不是重新翻资料、不花 LLM）。
- bot 的 engine 每轮清空 `messages`，它**不「记住」上次读过什么** —— 靠每次确定性重建的共享材料包 + 群历史。「不需要重新翻资料」准确说是**不需要 bot 重新做 discovery 往返**（`read_book` 试探找文件）；材料包没覆盖的章节仍会自主补读（保质设计，§18.3 实测 7 次 `tool`）。
- 群历史有 `_HISTORY_BUDGET=18000` 字预算会裁很早的发言，但材料包每次从磁盘现读，不受影响。

### 20.3 改造：把轮次决定权交还用户

**只砍「零质量贡献」的自动连跑，不砍任何质量不变量**：不减唤醒人数、不降思考强度、不压输出、第 2 轮起的串行接话语义原样保留（§16.2 六条逐条不破）。**3 轮 / 10 条硬上限不取消，只是不再自动烧满。**

| 层 | 改动 |
|---|---|
| `rooms.say_stream` | 新增可选参数 `rounds` / `start_round` / `post_user` / `woken_override` / `initial_message_count`（**全有默认值，旧调用方行为不变**）。`_bound()` 用累计轮次 + 累计消息数判护栏；`done` 带 `can_continue` / `next_round` / `total_rounds` / `message_count` |
| `rooms.continue_stream` | 新入口：**不落新 user 消息**；累计消息数从**群历史**推导（最后一条 user 之后的 bot 消息数），不信任前端、不新增存储；`start_round` 让 `build_prompt` 用「第 N 轮接话」引导，且并发只在 `round_no == 1` ⇒ **续轮天然串行接话** |
| `web.py` | `/stream` 传 `rounds=1`；新增 `POST /api/rooms/{id}/continue`（同一套 SSE 事件与响应头）；composer 加「再聊一轮」按钮，`done` 驱动显隐，`consumeStream()` 让新发言与续轮共用同一套逐字渲染 |
| 材料 | 续轮传空 `text` ⇒ `prepare()` 沿用该群已备材料但**仍验 mtime 指纹**，改稿自动换新，零额外 LLM |

「到此为止」不需要额外动作：不点按钮即可；随时可再发一句开新话题。到界或全 pass 时按钮自动消失并给出人话提示。

### 20.4 验收

| 项 | 结果 |
|---|---|
| 专项回归 | `tests/test_bot_user_driven_rounds.py` **7 例**（默认 1 轮 / 续轮接上前文 / 串行非并发 / 3 轮与 10 条累计收口 / 群不撤 / 向后兼容） |
| 扩大回归 | bot 群 + 圆桌 4 文件 + 圣经抽取 **120 例全绿**；`py_compile` / `node --check` 内嵌 JS / `git diff --check` 全过 |
| 真实 HTTP 端到端（uvicorn + 离线 runtime，零 LLM） | `/stream` → `total=1, can_continue=True, next=2`；`/continue` 2 → `total=2`；`/continue` 3 → `total=3, can_continue=False`；`/continue` 4 → `bounded=max_rounds, rounds=0`（后端挡住，不依赖前端自觉）。逐字 delta 正常、`X-Accel-Buffering: no`、页面 `BASE="/bots"` 注入正确、续轮按钮存在 |
| 保质证据 | 续轮 bot 的发言明确引用了前一轮发言文本；全程只落 **1 条 user 消息**，落库 `1 user + 3 轮 × 2 bot = 7` 条；群仍在 `list_rooms()` |
| 未跑 | 真 LLM 群聊全文重验（成本太高；本次是确定性调度 + 前端渲染，离线用例已锁）。桌面 GUI 内实际点击仍待重打包后萧哥目测 |

### 20.5 边界

- 旧 `/say` 一次性端点与 `say_stream(rounds=None)` **行为不变**（仍 3 轮），回归用例不破；前端只用 `/stream` + `/continue`。
- `MAX_ROUNDS=3` 语义从「每次必然跑满」变为「一次讨论最多允许点 2 次续轮」；`MAX_MESSAGES=10` 变为跨续轮累计护栏。
- 新增 Python 文件只有测试文件，云端不需要，`deploy_cloud_sync.sh` 清单无需改动；`rooms.py` / `web.py` 是已有条目。
- 未 push origin。

---

## 21. 桌面 GUI 真机收口：`/bots` 鉴权与侧栏分组（11-07）

### 21.1 `/bots/` 必须进 sidecar token 白名单

| 项 | 内容 |
|---|---|
| 现象 | 重打包后点「Bot 群」，iframe 直接回 `{"error":"missing or invalid OpenWorker sidecar token"}` |
| 根因 | `coworker/server/app.py:require_sidecar_token` 白名单只有 `/roundtable/`。iframe 跑在 `http://127.0.0.1:<sidecar>`，父窗口是 `tauri://localhost`，**跨源**，页面里的 fetch 既读不到父窗口的内存 token，也不会自动带 `X-OpenWorker-Token` |
| 修法 | 与圆桌同类的桌面内嵌本地页待遇：放行 `/bots/` 与 `/bots`（Mount redirect）；**主 `/v1` 鉴权一字不动**，前缀精确匹配（`/bots-elsewhere` 仍 401） |
| 🔴 上轮验证盲点 | §19 用 `TestClient` 验 `/bots/` 200 时把 `COWORKER_API_TOKEN` 从 env pop 掉 —— 等于在**免鉴权环境**测路由，那个 200 是假绿。新增 `tests/test_bot_sidecar_auth.py` 在**带 token** 的 `create_app` 上跑 6 例，同时锁住「主 API 无 token 仍 401 / 错 token 401 / 对 token 放行 / 前缀不宽」 |
| 未采用的方案 | 让父窗口经 postMessage 把 token 送进 iframe、只给 `/bots/api/*` 保留鉴权：**页面 HTML 自身也会被中间件挡**，握手通道根本建立不起来（鸡生蛋）；且圆桌既有待遇就是整前缀免鉴权 |
| 边界 | 外层由本机环回监听 + 云端 Caddy basicauth 保护；**不要把 `/bots` 暴露到公网裸端口**。云端停机保号期间不涉及 |

### 21.2 左侧导航收敛为「多人协作」大分类

三个平铺入口（创作圆桌 / Bot 群 / 创作流水线）合并为一个 hover 展开的分类，侧栏少占两行。三个 surface、回调与 `data-testid` 全部保留；父项在任一子项 active 时高亮；除 hover 外也支持键盘 focus 与点击 toggle。浮层是同一包裹容器的子节点，`padding-top` 兼作 hover 桥，鼠标从按钮移到菜单不会提前收起。新增 `Sidebar.collaboration.test.tsx` 2 例锁住折叠/展开/回调/收起/active/focus。
