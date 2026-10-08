# ROUND-12 指令 · 意图执行闭环（P3 自主「出海口」后半程）

**承接**：R11 意图层（意图产生 + 提案 PROPOSED 落地）；R10 裁决写 lesson；R3 lesson 注入。
**设计依据**：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md` §四·第三段（自主）、§4.4 护栏、§八.4 已拍板决策。
**状态**：⚠️ **本工单超出原 P3 四段闭环范围**（设计把"自主"定义为"有意图、能提案"，executed 裁决=记录+写教，未要求自动执行）。本工单是"出海口"终局的第一步——让"你同意了，然后呢"真正发生。开工前请萧哥确认 §零 的待拍板点。

---

## 零、待拍板点（先定死再施工）

1. **执行语义**：R11 之后，用户裁决一条意图提案 `executed`，**当前只写 lesson、小桔什么也不做**。ROUND-12 要让它真正"做"——但是"做"指什么？
   - **建议（default）**：执行 = 一次**受护栏的自主动作**，产出一份**可复核的产物**（她自己的手记/综述草稿/查证笔记），**绝不自动外发、绝不碰用户原稿**。
   - 若萧哥想更激进（如允许意图驱动自动发 AI 圈），须在 §二护栏里再加一道"二次人审"闸，本工单默认不包含自动外发。
2. **intents 跨端同步**：设计 §六 line 267 说 intents 进 `SYNC_TABLES`，R11 因双写 LWW 风险**故意未登记**。ROUND-12 先**本端（云端/本地各自）生效**，不强行进 SYNC；待萧哥拍板"单写窗口"后再补（仿 teachings 业务列 + 幂等去重）。本工单**不阻塞**在 SYNC 上。
3. **memory_edges 显式边**：设计 §六 line 265 的显式边表仍延后（P1 已用共享锚轻量试水）；ROUND-12 不碰汇流层，意图素材继续走 R11 的 `memory_anchors` 共现簇降级。

---

## 一、目标

把"意图 → 提案 → 裁决 → 教学"四段闭环补上**最后一跳**——意图被裁决 `executed` 后，真正落地为一个受护栏的自主动作，并把"做成了什么"回流到记忆/上下文，让自主有底气（设计 §五：自主是出海口）。

落地后判定（对齐设计 §4.6 的延伸）：
- 用户批准一条意图提案后，小桔**实际产出一份可复核产物**（非空跑、不只写 lesson）；
- 该产物**可被小桔自己在后续轮次召回/引用**（闭环回流，非写完即弃）；
- 全程**不碰用户原稿、不自动外发**（护栏钉死）。

---

## 二、护栏（最高优先级，钉死在代码里）

沿用设计 §4.4 + §八.4，逐条落成硬规则：

1. **替你写作 = 永久红线，不可能发生**。意图 goal 若命中"你的稿/原稿/ manuscript"语义（复用 `deskmate/sacred_paths.py` 的 `guarded_write` 信号判断），执行器**直接 no-op 返回**，绝不写用户目录下的原稿正文。
2. **不自动外发**：执行器**绝不**调用 ai-circle 的 post/reply/column/share。若意图 goal 明显指向"对外表达"（发帖/回复/投专栏），执行器只产出**草稿**（落本地可复核区），并生成一条 `risk=high` 的**二次人审提案**让用户决定要不要发；不替用户发出去。
3. **自主写作照常**：若意图是"她写她的"（手记/花园日记/广场内容草稿——谁也不替谁），属设计 §八.4 已授权自主范畴，可执行，但产物仍落**可复核区**（先给萧哥看，再决定要不要真发）。
4. **查资料/查史料佐证无闸**：若意图 goal 是"查 X 史料/佐证"，执行器可主动查——**必须用本地自有史料库**（`~/.deskmate/history/` 或 P1 汇流史料通道），不瞎联网；产出查证笔记。
5. **全链路 fail-open**：LLM 调用失败 / 产物写库失败 / 任何异常 → 仅告警、意图回退 `active`、不抛、不阻断 L2 tick。
6. **executed 才执行**：`rejected`/`never` 裁决**不触发任何执行**（仍只写 lesson，沿用 R10）；只有 `executed` 才走执行路径。

---

## 三、落点（建议实现，Trae 可调）

### 3.1 新增执行器 `deskmate/agency/behaviors/intent_executor.py`
- `run_intent_execution(base_dir, intent: dict, decision) -> dict`：
  - 读 intent.goal / motivation / origin / how（extra）；
  - **护栏闸门 0**：goal 命中 sacred_paths 原稿语义 → 返回 `{"executed": False, "reason": "sacred_manuscript"}`；
  - **闸门 1**：goal 命中"对外表达"语义 → 走"草稿 + 二次人审提案"分支（见 3.4），不执行外发；
  - **闸门 2**：LLM 可用性 + provider（沿用付费 `chat`，不稳降级就 fail-open）；
  - 主路径：以第一人称 + goal + motivation 起草一份**产物**（综述/手记/查证笔记），长度受控；
  - 落库：产物存 `intent_outputs` 表（新，见 3.2），`intent.status=done`，回填 `intent.proposed_task_id` 关联的执行 task_log；
  - 返回 `{"executed": True, "output_id": ..., "summary": ...}`。
- `parse_intent_output`：容忍 JSON / 代码围栏 / 杂文本（仿 `intent_generation.parse_intents` 的健壮性）。

### 3.2 新增 `intent_outputs` 表（`store.py`，`IF NOT EXISTS` 自愈，明文、可后续进 SYNC）
```
intent_outputs(
  id          TEXT PK
  ts          TEXT
  intent_id   TEXT        -- 关联 intents.id
  goal        TEXT        -- 当初想做什么
  artifact    TEXT        -- 产出的可复核内容（她写她的）
  kind        TEXT        -- synthesis | diary | research | draft_external
  status      TEXT        -- done | pending_review
)
```
- `add_intent_output` / `get_recent_intent_outputs(limit)`（fail-open，供注入）。

### 3.3 `engine.resolve_proposal` 扩 intent 分支
- 现有 `_record_adjudication_lesson`（R10）保留；在 `decision == EXECUTED` 且 `extra.source == "intent"` 时，**额外**调 `run_intent_execution`；
- 执行失败（fail-open）→ 仍写 lesson（裁决闭环不破），意图回退 `active`；
- 执行成功 → 意图 `done` + 关联 output。

### 3.4 二次人审分支（对外表达类意图）
- 不执行外发；改为：产出草稿存 `intent_outputs(kind='draft_external', status='pending_review')` + 生成一条 `risk=high` 新提案（action_type 复用现有 ai_circle_* 白名单动作或 `intent` slug），等用户二次裁决才真发。

### 3.5 回流注入（闭环关键）
- `memory_injector.build_intent_context`（fail-open）：取最近 N 条 `intent_outputs(最近 3 条 + 永久?)` 注入小桔每轮上下文（与 lessons 同层、同总开关 `DESKMATE_AUTO_INJECT`）；
- `manager.py` 调用处挂上（仿 R3 lessons 注入的接线方式）。

### 3.6 配置 + 注册
- `settings.DEFAULTS` 加 `intent_execution: {enabled, max_outputs, ...}` + dataclass field + from/to dict + load；
- `registry` 无需新动作（执行是 proposal 裁决后的内部路径，不进白名单 tick）；执行器经 `resolve_proposal` 触发，不新增自主 tick。

---

## 四、验收判据（test_p3_intent_execution.py 覆盖）

1. **主路径**：裁决 `executed` 一条 intent → `run_intent_execution` 产出 `intent_outputs` 一条、intent.status=done、关联 task_log；`get_recent_intent_outputs` 可取（回流链通）。
2. **护栏-原稿**：goal 命中用户原稿语义 → 执行器 no-op（不写 intent_outputs、intent 不 done），不抛。
3. **护栏-外发**：goal 命中"发帖/回复" → 不产生外发调用（断言 ai-circle 不被调）、只落 `draft_external` 草稿 + 二次人审提案。
4. **裁决分支**：`rejected`/`never` 裁决**不触发执行**（断言 `intent_outputs` 无新增）。
5. **fail-open**：LLM 异常 → 意图回退 `active`、不抛、lesson 仍写。
6. **注入**：`build_intent_context` fail-open 可取最近产物（与 lessons 同层）。
7. **回归**：R9/R10/R11 全套（`test_p3_reflection`/`test_teaching_feedback`/`test_intent_generation` 等）零破坏。

---

## 五、红线与排除

- 仅后端 Python + 1 新表（`intent_outputs`，`IF NOT EXISTS` 自愈）+ 1 新行为模块 + 注入块；**无新前端、无新 SYNC 表（intents 暂不进 SYNC）**。
- 绝不碰 `sacred_paths` 守卫的原稿目录；绝不调 ai-circle 外发。
- 不改 `route_task`（仍 self）、不改 kill switch / visibility_gate / 出网审批闸。
- 垃圾文件（`:memory:.ses` / `design/` / `dist.tar.gz`）不进库。
- 部署同 R9-R11：`scripts/deploy_cloud_sync.sh` 已含 agency 模块，**本工单新增文件须补进该脚本清单** + 重启 `openworker-server` + 触发同步窗口。

---

## 六、与 ROUND-11 的关系

R11 让"她会想事、会提案"；ROUND-12 让"你同意了，她真去做、且做得可被你看到和引用"。至此 **P3 自主闭环真正闭合**（意图→提案→裁决→执行→教学回流），设计 §五"自主是出海口"从口号变机制。
