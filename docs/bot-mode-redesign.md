# Bot Mode 统一改造规格：主编线 + 圆桌线合并

> 状态：**阶段 1 骨架已落地（2026-11-06），阶段 2/3 待萧哥拍板**
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
- `coordinator.py` 的「到达序并发 + 收窄答辩 + 纪要」**可作为群内一轮讨论的调度策略**保留，但不再由表单触发。
- 圆桌「阅读全书 / 圣经抽取」：昨天刚修好付费通道，萧哥正在用；阶段 3 **降级为群里的一个工具**（`@史料考据 先把这几章读了`），不是独立表单流程。
- 7 位创作专家的 manifest / 人设 / 头像 / 模型；「我的伙伴」UI 骨架。
- `docs/living-companions-plan.md` §4 模块 1「常驻实体 + 独立 session」可复用；但**模块 2「社交驱动层 / 定时主动闲聊」必须砍掉** —— 那正是 68 次唤醒的病根。

---

## 7. 分阶段

**阶段 1 · 骨架（本次已落，纯新增、不动现有流程）**
- `deskmate/bots/{__init__,roster,rooms,discussion}.py` + `tests/test_bot_mode_skeleton.py`（10 例，先红后绿）。
- 群元数据放同 base 的独立 `bots.db`（避开 `.deskmate/` 神圣目录，与 `chat.db`/`teams.db` 同目录）。
- 零改动 `coworker/teams/*`；无任何现有入口引用新包，圆桌与主编照旧可跑。

**阶段 2 · 接通运行时（需萧哥点头）**
- 群级 API：`create / list / state / send / disband / rename`，接 `TeamChatView`。
- bot 实体与 team 解耦：worker session 订阅「群」而非「team」，用户发言 → 真 Agent 各自署名回话。
- 群成员 `persona` 真填（阶段 1 已做），加每次讨论的轮次/消息门禁。
- 前端改动**必须重打包桌面 App**；云端 `xj.aibbs.chat` 另走 `deploy_web.sh`。

**阶段 3 · 两条线退役**
- 撤主编入口、下线圆桌表单；「阅读全书」迁为群内工具。
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
7. 圆桌「阅读全书」在阶段 3 前不受影响；迁移后仍走付费通道组。

---

## 9. 红线

- 只 commit `fork/base`，**不 push origin**；云端停机保号，新增 Python 文件须进 `deploy_cloud_sync.sh` 清单。
- `coworker/teams/*` 零改动（上游干净代码，合流红线）；团队语义经 `deskmate/teams_client.py` 薄壳。
- `DESKMATE_DIR` 调用时解析，禁模块级常量 / import 期副作用；持久化原子写。
- 书稿目录与 `夜不收-烧仓脑洞汇总-2026-11-06.md` 只读，正稿一字不动；`guarded_write` 硬红线不变。
- 前端改动须重打包；云端前端另走 `deploy_web.sh`；token 不构建时烘焙。

---

## 10. 待萧哥拍板

1. **圆桌是否彻底退役**：建议阶段 3 退役表单、但把「阅读全书」保留为群内工具；若想立刻全撤，阶段 2 就要先做迁移。
2. **bot 名册范围**：阶段 1 只放 7 位创作专家；主编线 6 个角色是否全部纳入（还是只留有用的几位）。
3. **群内要不要「纪要」**：Hermes 没有主持人纪要，纯对话；旧圆桌有《综合研判报告》。建议默认不出纪要，用户说「收一下」才生成。
