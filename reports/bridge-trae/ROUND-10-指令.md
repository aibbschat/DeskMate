# ROUND-10 指令 · 反思 R2（裁决落地）+ 教学回流（裁决/反馈自动写 lessons）

> 给 Trae Work 的执行指令。承接 ROUND-09（反思层 R1，已收口 commit `86c2dd4`）：
> R1 把「回看变化 → 裁决 maintain/revise/overturn + 理由」落进 `item_history`（`action='reflected'`）
> 并注入上下文（`build_reflection_context`），但**裁决只留痕、没真正改变条目本身**——
> `revise/overturn` 对底层 worldview/profile 条目是「 inert 」的，等于反思了却没改看法。
> 本工单补齐这条闭环，并把「教学」从「手动/LLM 预置」接到**真实用户裁决/反馈**上
> （江河设计 §四.3④「教学层」，设计明确写「本次设计的核心」）。
>
> 指挥员=萧哥，QC=WorkBuddy 读真实 diff + 跑测试出判词。
> 设计基线：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md` §三（反思）/ §四（自主·教学）。

---

## 一、目标（两段，本工单一次上）

1. **反思 R2 · 裁决落地**：`revise/overturn` 不只是写一条 `reflected` 痕，还要**真正影响对应 worldview/profile 条目的可信度/状态**，
   让「她改了看法」在数据与召回里真实可见；`maintain` 不变。
2. **教学回流**：每次用户裁决提议卡（`resolve_proposal`）后**自动写一条 `lessons`**（决策 + 派生规则 + 适用范围）；
   stretch：聊天中用户显式纠错也写 lesson。让「裁决不留教」彻底闭合——设计 §四.4 硬规矩「拦必留教」。

---

## 二、现状（真实代码，已核对）

- `deskmate/agency/behaviors/reflection.py`（R1，已 commit）：
  - `parse_verdicts()`（L191）正确解析 编号/裁决/理由 三组，越界/缺理由丢弃；
  - `run_reflection()`（L229）调 `store.record_reflection(item_id, verdict, reason, subject_kind)`（L320）
    写 `action='reflected'`，**但不对任何 profile_items 条目做任何写操作** → 裁决 inert。
- `deskmate/cognitive/store.py`：
  - `record_reflection()`（L1089）：写 `item_history`，硬断言 `reason` 空返回 False；
  - `get_reflections_for_injection()`（L1127）：读 `action='reflected'` 注入，**R1 已接通**；
  - `add_lesson(content, scope='recent'|'permanent', source='manual')`（L1024）：md5 幂等去重，
    **目前只被手动/LLM 预置调用，无用户裁决 feeder**；
  - `get_lessons_for_injection()`（L1049）：`permanent` 全取 + `recent` 限 3 + char_cap 1200，**已接通**（R3）。
- `deskmate/agency/engine.py`：
  - `resolve_proposal(record_id, decision)`（L371）：**只改 `task_log` 状态**（done/proposed/executed/rejected/never），
    **不写任何 `lessons`**；除个别特例外「拦下就只是拦下，没学到东西」（设计 §四.2 已点名）。
- **存量债（顺带修）**：`tests/test_worldview_reflection.py` 2 failed 根因
  `table profile_items has no column named sync_uuid`（R1 同步表引入的夹具债，反复撞）；
  修复方式：fixture 里跑 `sss.migrate()` 补 sync 列，不动被测逻辑。

---

## 三、设计

### 3.1 反思 R2 · 裁决落地（`apply_reflection_verdict`）
- 新增 `store.apply_reflection_verdict(item_id, verdict, reason) -> bool`：
  - `maintain` → 直接返回 True（R1 已留 `reflected` 痕，无需再动）；
  - `revise` → ① 若 `profile_items` 有 `confidence` 列：将该条目 confidence ×0.6（下调，保留旧值 fail-open：
    无该列则跳过这步，仅留痕）；② 在 `item_history` 写 `action='reflected_apply'`，
    `new_value='revise'`，`reason=reason`（透传）；
  - `overturn` → ① 若 `profile_items` 有 `active`/`status` 类列：置为非活跃（无则跳过）；
    ② 写 `item_history` `action='reflected_apply'`，`new_value='overturn'`，`reason=reason`；
  - **fail-open**：列不存在 / 写库异常 → 仅告警返回 False，绝不抛、绝不阻断 tick。
- `run_reflection()`（reflection.py L317-324）在 `record_reflection` 之后，对 `revise/overturn` 的 verdict
  **追加调用** `apply_reflection_verdict`；`maintain` 不调。
- **防自循环**：`reflected_apply` 不在 `_CHANGE_ACTIONS`（reflection.py L50-53）里，
  故 R2 的落地写不会被下一轮反思当成「新变化」反复追问（与 `reflected` 同处理）。
- **防被覆盖吞掉**：确认 `worldview_reflection._persist_worldview`（worldview_reflection.py L514-518）
  覆盖 worldview 条目时**只动该条目自身列，不删 `item_history` 的 `reflected`/`reflected_apply` 行**
  （R1 已用独立行规避；本工单加一条测试钉死：覆盖后 `get_reflections_for_injection()` 仍返回原反思）。

### 3.2 教学回流（`resolve_proposal` 写 lessons）
- 扩 `engine.resolve_proposal(record_id, decision)`（L371）：裁决成功后**追加一步**
  `store.add_lesson(content, scope, source='adjudication')`：
  - `content` 由 `(action_type / 动作标题, decision, user_note, 派生规则)` 拼成一句人话，
    例如："`AI圈发帖`：本次放行（你没拦）→ 这类自主表达可以继续"` / "`查私人资料`：本次拦下（你说涉及隐私）→ 这类事先问"`；
  - `scope`：`never`→`'permanent'`（永久规则），`executed`/`rejected`→`'recent'`（近期经验，限 N 条注入）；
  - `user_note`：若 `rec.extra` 里带用户批注则取用，否则留空（规则仍从 decision 派生）。
- **fail-open**：`add_lesson` 自身已 fail-open（空内容/写库异常返回 False）；`resolve_proposal` 调用处包 try，
  写 lesson 失败不影响主裁决状态变更。
- **stretch（可选，非验收必需）**：聊天流里检测用户显式纠错（「不对/你错了/别这样/以后别」等白名单触发词）
  → 调 `add_lesson(decision='rejected', ...)`。先做 `resolve_proposal` 主链路，stretch 单独标注。

### 3.3 观察点（来自 R9 QC，留作监控项，非本工单必改）
- `reflection.py` L289 调 `chat_memory`（免费记忆组，会降级推理模型）。反思解析是文本正则非 JSON，
  最坏 `bailed=True`（返回 `llm_error`）不落痕不假成功；调用量极小（7 天 1 次）基本不撞配额。
  **若生产日志出现 `reflection: ... llm_error` 频发**，改 `chat` 或 OmniRoute 普通模型，不动架构——记一笔即可。

---

## 四、落点（真实文件 + 函数）

| 项 | 文件 | 改动 |
|---|---|---|
| R2 落地 | `deskmate/cognitive/store.py` | 新增 `apply_reflection_verdict(item_id, verdict, reason)`（confidence 下调 / active 置否 / 写 `reflected_apply` 痕，全 fail-open） |
| R2 接线 | `deskmate/agency/behaviors/reflection.py` L317-324 | `record_reflection` 后，对 `revise/overturn` 追加 `apply_reflection_verdict` |
| R2 防覆盖 | `deskmate/agency/behaviors/worldview_reflection.py` L514-518 | 确认覆盖不删 `item_history` 反思行（加测试钉死） |
| 教学回流 | `deskmate/agency/engine.py` `resolve_proposal` L371 | 裁决后 `try: store.add_lesson(...)`（fail-open） |
| 存量债 | `tests/test_worldview_reflection.py` | fixture 补 `sss.migrate()` 还 `sync_uuid` 债（2 failed → pass） |
| 测试 | `tests/test_p3_reflection.py`（+新文件 `tests/test_teaching_feedback.py`） | R2 落地 + 教学回流 断言；全套目标 ≥ 当前 111 passed + 新增零破坏 |

---

## 五、验收判据（WorkBuddy QC 用，结论先行 + 表格）

1. **反思 R2 落地**：注入一条 `revise` verdict（`item_id` 指向真实 profile_items/worldview 条目）→
   断言：① 该条目 `confidence` 下降（若有列）；② `item_history` 出现 `action='reflected_apply'` 且 `reason` 透传；
   ③ `get_reflections_for_injection()` 仍返回原 `reflected` 反思（不被覆盖吞）；④ `maintain` 不触发任何写。
2. **教学回流**：模拟 `resolve_proposal(record_id, 'executed'|'rejected'|'never')` →
   断言 `lessons` 表各出现 1 行、对应 `decision` 与 `scope`（`never`→permanent）正确；
   `get_lessons_for_injection()` 能取到（permanent 常驻 / recent 进最近 N）。
3. **防自循环**：对 R2 落地的 `reflected_apply` 行，下一轮 `_recent_changes`（reflection 口径）**不应**把它当素材。
4. **零回归**：`pytest` 全套（含修好的 `test_worldview_reflection`）**零红**；R9 的 111 passed 不被破。
5. **红线守住**：不碰 `origin` / 不 `--force`；新写不破坏 schema（无 `ALTER TABLE` 破坏性）；fail-open 全程。

---

## 六、红线（施工约束，违反即打回）

- **出网/数据**：绝不读写神圣稿件（`.deskmate/` 原文稿）；原子写走临时文件 + `os.replace`（沿用 `reflection.py:_save_json` 风）。
- **fail-open**：`apply_reflection_verdict` / `resolve_proposal` 写 lesson 任意异常 → 仅告警返回，绝不抛、绝不拖崩 L2 tick。
- **同步**：若 R2 新增字段需跨端，只同步明文（沿用 `SYNC_TABLES` 约定）；本工单预期**不新增跨端表**。
- **测试先红后绿**：新增断言前先写失败用例，再补实现；全套零破坏才算 PASS。
- **交付后**：commit 进 `fork/base`，**不推 origin、不 --force**；交回时附「真实 diff 摘要 + 测试输出」给 WorkBuddy 出 QC。

---

## 附：后续轮次预告（非本工单范围）
- **ROUND-11 · P3 自主闭环**：`intents` 表（意图成为一等公民）+ 提案来源放开到意图（非白名单）+ `lessons` 注入链接到 `<memory-context>` 同层（本工单已把 lesson  feeder 接通，下轮只需把"意图生成"低频行为 + 注入挂上）。护栏规格已在设计 §四.4 拍板，P3 之前先定死。
