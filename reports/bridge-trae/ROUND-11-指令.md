# ROUND-11 指令 · P3 自主闭环（意图 → 提案 → 裁决 → 教学）

> 承接 R9（反思 R1/R2 落地）+ R10（教学回流·写）+ R3（lessons 注入）。
> 设计底稿：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md` §四。
> 本工单只做「自主」的**前半程**——把「她会自己想事、并把想法变成提案」打通；
> 自生成意图的**自动执行**刻意不放开（留 R11-b），遵循「自主是最大一步、护栏先定死」。

## 一、背景与目标

现状：小桔是「白名单 + 风险分级」的定时执行器——`route_task` 恒 `self`
（`engine.py` L67-L70），她只能提**注册好的动作**，不能提「她自己想干的事」（上发条）。
R9/R10 已把「反思 + 裁决留教」闭环，但**意图源头仍锁死**。

目标：新增**意图层**，让「目标」成为一等公民；意图 → 生成 `proposed` 提案（带
`intent_id` + 她打算怎么做）；裁决/教学复用 R10 已落地的 `resolve_proposal`→`lessons`。

## 二、落地清单（分文件，fail-open 横贯）

### 1. `deskmate/cognitive/store.py` —— 新增 `intents` 表 + CRUD
- DDL（在 schema 初始化里加）：
  ```
  intents(
    id TEXT PK, ts TEXT, goal TEXT, motivation TEXT,
    horizon TEXT,            -- short | long
    status TEXT,            -- open | active | done | dropped
    origin TEXT,            -- 未满足的关怀 | 未接着的观察 | 长期兴趣 | 你的话
    proposed_task_id TEXT   -- 关联 task_log 的提案
  )
  ```
- `add_intent(goal, motivation, horizon, origin, status='open') -> id`
- `get_open_intents(limit=N)`（status='open'，供提案行为取用）
- `link_intent_to_task(intent_id, task_id)`（提案生成后回填）
- `set_intent_status(intent_id, status)`
- 全部 fail-open：写库异常仅告警返回空/False，绝不抛、绝不拖崩主链路。

### 2. `deskmate/agency/behaviors/intent_generation.py` —— 新行为（低频）
- `run_intent_generation(base_dir)`：读三类素材（**不凭空造**，都从河床长）：
  - **未满足的关怀**：proactive_ledger 里挂着、用户没接的关怀项；
  - **未接着的观察**：观察日记 / world_context 里悬而未决的事；
  - **长期兴趣**：P1 汇流层的关联边聚出来的主题簇（`memory_edges`，若 P1 未上线则降级跳过）。
- 用 `chat`（付费主模型，稳定 JSON）让她以**第一人称**起草
  `goal`（我想做什么）+ `motivation`（为什么，从哪条记忆长出来的）；
  `horizon`/`origin` 由调用方依素材来源填。
- **7 天错峰**（仿 R9 reflection 的 cooldown，防刷屏）；本回看窗口无新意图 → 静默返回。
- fail-open：`chat` 失败 / 解析失败 → 仅告警返回，不写、不崩。
- 写库走 `store.add_intent`，幂等靠「同 origin+goal 近重复去重」可由 `get_open_intents` 后比对（宽松即可，不必强求）。

### 3. `deskmate/agency/engine.py` —— 提案放开到意图
- **新增 `propose_from_intent(intent)`**：取一条 open intent → 调 `self.log.add(...)`
  生成 `status=PROPOSED` 记录，`extra` 里带 `intent_id` 与 `how`（她打算怎么做，从 intent 派生）
  + `source='intent'`；再 `link_intent_to_task` 回填；`set_intent_status('active')`。
  - **关键护栏**：自生成意图的提案**一律保持 PROPOSED，绝不自动执行**（未知/自生成动作走人审）。
    即本回合不改 `_run_low_risk` 的自动执行逻辑，意图提案天然落在「需裁决」分支。
- `tick()` / `_run_low_risk` 增加 `intent_generation` 分支（仿 reflection：not yet-run → return None 不刷屏）。
- `route_task` **保持 `self` 不变**——意图不绕过路由，而是走「提案待人审」。

### 4. `deskmate/agency/registry.py` + `settings.py` —— 登记行为
- `registry` 登记 `intent_generation`（risk=low，与 reflection 同级）。
- `settings` 加配置段：`AGENCY_INTENT_GENERATION_ENABLED`（默认 1）、
  `AGENCY_INTENT_COOLDOWN_DAYS`（默认 7）；全链路引用（仿 reflection 配置写法）。
- 复用现有 `kill switch` / `outbound` / `visibility_gate`，**不动**。

### 5. `deskmate/memory/sync_store_sqlite.py` —— 跨端（可选，谨慎）
- `SYNC_TABLES` 加 `intents`（**只同步明文**，无向量，仿 `teachings`）。
- ⚠️ **双写风险预警**：Mac（24h 常驻）+ 云端（每小时 :02 窗口）双 daemon 都会跑意图生成，
  可能两端各生成相似意图 → 撞 LWW。本回合**先不强制进 SYNC_TABLES**，
  仅在代码注释里标注「待萧哥拍板单写窗口后启用」；若萧哥要求先同步，再加并幂等去重。

## 三、验收判据（对应设计 §4.6）
1. 她**提出过**至少一条「不在原白名单动作里」、且 `extra.source='intent'` 的 `PROPOSED` 提案（=真会想事了）。
2. 该意图提案**始终停在 PROPOSED**，未被自动执行（护栏成立）。
3. 你裁决（rejected/never）该提案后，R10 的 `lessons` 写入生效，且 R3 的注入链能在下轮取到（=教进去了，复用既有测试 `test_lesson_is_injectable` 思路）。
4. 全套测试 **零回归**（基线 144 + 本回合新增）。

## 四、红线（必须遵守）
- 只改后端 Python + 测试，**无新表以外的破坏性 DDL**、无新前端、无 token 改动。
- 所有 LLM 环节 fail-open；新增行为 7 天错峰，绝不刷屏。
- **自生成意图本回合禁止自动执行**（最高优先级护栏）；高风险/未知动作永远走人审。
- 不碰 `route_task` 的 `self` 路由、不碰 `outbound`/`visibility_gate`/`kill switch`。

## 五、明确 scope（给 Trae）
- ✅ 做：intents 表 + CRUD、intent_generation 行为、propose_from_intent、registry/settings 登记、测试。
- ❌ 不做（留 R11-b）：自生成意图的自动执行、意图自动映射到未知 action_type 的真实执行。
- 若实现中发现 R10 的 `lessons` 注入在 manager 里被 `DESKMATE_LESSONS_INJECT=0` 默认关，
  需在工单里说明并默认打开（或至少在 settings 里把默认调成 1），否则「教进去但没注入」等于白教。

---
拟稿：WorkBuddy（异步桥接）
日期：2026-10-08
