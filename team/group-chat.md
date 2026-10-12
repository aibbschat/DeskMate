# 双项目研发团队 · 群频道

> 建群时间：2026-10-11 · 建群人：lead
> 用途：openworker 与 ai-forum-v3 两个项目开发维护团队的日常协作频道。
> 说明：本 harness 没有群聊 API，本文件即「群」——主频道（本文件）+ 每人一个席位文件 `team/group-chat/<name>.md`。

---

## 一、成员名录

| 席位 | target | 角色 | 职责一句话 |
|---|---|---|---|
| 群主 | `lead` | Team Lead | 派单、拍板、对外汇报 |
| 1 号位 | `backend-engineer` | 工程开发 | Python 引擎 / Express 后端 / 数据库 / 发布链路 / 部署 |
| 2 号位 | `ui-designer` | UI 设计师 | 设计 Token / 视觉规范 / 走查 / 改版方案 |
| 3 号位 | `ai-interaction` | AI 交互设计 | 审批回路 / 人设 / 记忆体验 / 多 Agent 社交交互 |
| 4 号位 | `frontend-developer` | 前端设计开发 | React/Vite/Tauri GUI / ai-forum 阅读端 / 组件与前端测试 |
| 5 号位 | `code-reviewer` | 代码质检 | 评审 / 测试基线 / CI / 发布准入门槛 |

## 二、群规（全员必读）

1. **开工必读频道**：每次被唤醒开工前，先 read 本文件最新 60 行 + 自己的席位文件，再动手。
2. **@人 = send_message + 频道留痕**：要找某人协作，用 send_message 直接发给对方（这是真正的私聊/拉人），随后在本文件「三、@与协作帖」补一条记录（写清 @谁、什么事、结论），让全群可见。需要群主知悉的，抄送 `lead`。
3. **任务状态只认任务板**：频道里只讨论、不改状态。认领/完成一律走共享任务板（team_task_*）。
4. **同文件必先喊话**：两人以上可能改动同一文件时，先在频道喊一声并等对方回「+1」再动手。改他人产出的文件用 edit 定点改，**禁止整文件覆盖**。
5. **决议落档**：讨论出的结论由群主或指定人写进「五、决议区」，成为后续派单依据；没落档的讨论视为未决。
6. **ai-forum-v3 只读铁律**：沙箱只允许写 /Users/xiaocheng/workspace/openworker。对 ai-forum-v3 的一切修改建议，产出为补丁/操作指令放在本工作区，由群主转交用户执行。此铁律在群里讨论时同样有效。
7. **中文汇报**：频道与产出一律中文。

## 三、@与协作帖

> 新帖加在最上面，格式：`### [时间] @发起人 → @对象：事由（结论）`

### [2026-10-11 15:55] @lead → 全员：建群通知
本群建立。请各席位把自己的名片写进 `team/group-chat/<席位>.md`（见「四、席位墙」），写明：专长、当前负责/可接的任务、可以被谁以什么事打扰、协作偏好。写完在本帖下回一行确认。

- [backend-engineer] 已入群，席位 team/group-chat/backend-engineer.md
- [frontend-developer] 已入群，席位 team/group-chat/frontend-developer.md
- [ai-interaction] 已入群，席位 team/group-chat/ai-interaction.md
- [ui-designer] 已入群，席位 team/group-chat/ui-designer.md
- [code-reviewer] 已入群，席位 team/group-chat/code-reviewer.md

## 四、席位墙

> 每人维护自己的 `team/group-chat/<name>.md`，写名片 + 个人待办 + 想在群里公开的事。别人要看某人在忙什么，直接读他的席位文件。

- [x] backend-engineer.md — 已写（名片 + 个人待办）
- [x] ui-designer.md — 已写（名片 + 个人待办）
- [x] ai-interaction.md — 已写（名片 + 个人待办）
- [x] frontend-developer.md — 已写（名片 + 个人待办）
- [x] code-reviewer.md — 已写（名片 + 个人待办）

## 五、决议区

> 只有这里的内容是「已拍板」。派单、复盘以此为准。

### [2026-10-11] 团队组建完成，首轮盘点交付
- 5 名成员全部到位，入职盘点 5/5 完成，产出见 `reports/team-onboarding/`。
- P0 候选（待用户拍板优先级）：ai-forum-v3 最小 CI 补丁、openworker 已知红 xfail 挂账、核实 backup 仓库 Actions 是否启用。

## 六、老板发言区（用户 @xiaocheng 专属）

> 老板想在群里说话，两种方式：
> ① 最省事：直接在 DeepSeek Harness 里告诉 lead（群主）你要说什么，lead 原文转发到群里并带回回复；
> ② 自己写：把话写在下面这个区域，然后跟 lead 说一声「群里有我的话」，lead 负责唤醒相关成员。
> 群规对老板的唯一要求：点名谁、要什么结果，写清楚就行，其他交给 lead。

（此处为老板发言区，成员勿在此写内容）

## 七、站会区（可选，晨会/晚报）

> 每人一行：昨天做了什么 / 今天做什么 / 卡在哪。群主汇总后写进决议区或任务板。
