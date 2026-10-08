# ROUND-10 实施记录（反思 R2 · 裁决落地 + 教学回流）

> 依据：`ROUND-10-指令.md`；上游：`ROUND-09-QC.md` PASS（R9 已收口 `86c2dd4`）。
> 范围：反思裁决**真正落地**（`apply_reflection_verdict`）+ 提议卡裁决**自动留教**（`lessons`）+ 还清 sync_uuid 存量债。
> 状态：**待 WorkBuddy re-QC**。未 commit（工作树改动，符合收口红线）。

## 0. 一句话

R9 让反思「留了痕」，但裁决对条目本身是**惰性的**（`revise`/`overturn` 改了主意却一个字没动）；
R10 把这条闭环补上——`revise` 真的下调该条可信度、`overturn` 真的让它退出召回，
每次提议卡裁决（放行/拦下/以后别提）也自动沉淀成一条 `lesson`（「拦必留教」）。
顺带还清了从 R1 起反复撞的 `sync_uuid` 测试夹具债。

## 1. 真实落点

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | 新增 `apply_reflection_verdict()`（L1168-1224） | `maintain`→不改；`revise`→`confidence×0.6`；`overturn`→`status='superseded'`；均追写 `item_history` `action='reflected_apply'`（reason 透传）。全 fail-open |
| 同上 | `persist()` 孤儿清理（L575-583） | **必要修复（见 §2.1）**：孤儿清理排除反思账本行（`reflected`/`reflected_apply`），否则整层覆盖会吞掉反思痕 |
| `deskmate/agency/behaviors/reflection.py` | `run_reflection` 落痕循环（L316-330） | `record_reflection` 成功后，对 `revise`/`overturn` 追加 `apply_reflection_verdict`；`maintain` 不调 |
| `deskmate/agency/engine.py` | `resolve_proposal`（L380-382）+ 新增 `_record_adjudication_lesson()`（L434-479） | 裁决成功后自动写 lesson；`never`→`permanent`，`executed`/`rejected`→`recent`，`source='adjudication'`，`user_note` 并入正文。fail-open |
| `tests/test_worldview_reflection.py` | 新增 autouse fixture `_sync_ready`（L27-49） | 建表后跑 `sss.migrate()` 补 sync 三列，还 `sync_uuid` 存量债（2 failed → pass） |
| `tests/test_p3_reflection.py` | `env` fixture 补 migrate + 新增 7 例（R2 落地） | 见 §3 |
| `tests/test_teaching_feedback.py` | **新增**，7 例 | 教学回流 + fail-open |

**指令外必要动作**：`store.persist()` 的孤儿清理改动（§2.1）——指令 §3.1 要求「覆盖后反思痕不被吞」并已假设 R1 规避到位，实测不成立，必须在 persist 层修（详见偏差）。

未碰：judge 核心 / `manager` 其他注入块 / 前端 / `recall_items` / `extractor` / person 注册表；未新增表、未新增跨端字段、未新增向量。

## 2. 关键决策 / 与指令的偏差

1. **🟥 指令 §3.1「R1 已用独立行规避覆盖」不成立（实测推翻）**：
   `persist()` 的清理顺序是「先 `DELETE FROM profile_items WHERE layer=?`，再 `DELETE FROM item_history WHERE item_id NOT IN (profile_items)`」——
   本层条目刚被删，随即被当成孤儿，其**整段历史（含 `reflected` 反思痕）被一并清空**。
   而 `worldview_reflection._persist_worldview` 是**新建 Stance**（`history` 为空），
   无法靠「in-memory history 回写」把痕带回来 → worldview 层**每次回望都会吞掉自己的反思痕**。
   我加的 `test_reflection_survives_layer_overwrite` 先红后绿证实了这一点。
   **修复**：persist 的孤儿清理加白名单排除 `reflected`/`reflected_apply`（反思账本行不受条目重写影响）。
   这是指令 §3.1 的**验收要求本身**，落在 store 层而非 worldview 主逻辑（符合「不改回望主逻辑」红线）。
2. **`overturn` 的「非活跃」用 `status='superseded'`**：表无独立 `active` 列，只有 `status`（默认 `'active'`）；
   `superseded` 属终态集合 `_TERMINAL_STATUSES`，且 `recall_items` 只召回 `status='active'` → 推翻即退出召回，语义正确。
3. **`revise` 用 `COALESCE(confidence,0.5)*0.6`**：防 NULL 置信度算出 NULL。
4. **`reflected_apply` 的 `source` 写 `'reflection'`**：该函数拿不到 `subject_kind`，用来源标代替（`record_reflection` 用 `source` 存 subject_kind，此处存来源，语义清晰不冲突）。
5. **lesson 标题优先取 registry label**：`get_spec(action_type).label`（如「写文件」），未注册动作回退 `action_type` 原文，不崩。
6. **`user_note` 来源**：`rec.extra['user_note']`（无则留空，规则仍从 decision 派生）。
7. **存量债修复面**：`test_worldview_reflection.py`（autouse fixture）+ `test_p3_reflection.py`（`env` fixture）都补 `migrate`——R2 的 persist 生存性用例要真跑 persist，同样需要 sync 列。

## 3. 自带测试

- 定向（R10 相关 8 文件）：
  `.venv/bin/python -m pytest tests/test_p3_reflection.py tests/test_worldview_reflection.py tests/test_teaching_feedback.py tests/test_agency.py tests/test_subject_guard.py tests/test_sync_store_sqlite.py tests/test_p2_lessons.py tests/test_settings.py -q`
  → **137 passed**（含新增 `test_p3_reflection` 7 例 + `test_teaching_feedback` 7 例，`test_worldview_reflection` 19 例全绿）。
- 新增用例覆盖 §五 判据 1/2/3：
  - ① `revise` 降 confidence、`overturn` 置 superseded、`reflected_apply` 痕 reason 透传、`maintain` 零写；
  - ② 裁决回流三态 + scope 映射 + `source='adjudication'` + `user_note` + 可注入；
  - ③ 防自循环：`_recent_changes` 不含 `reflected_apply`/`reflected`；
  - 覆盖钉死：整层 `persist` 后 `get_reflections_for_injection()` 仍返回原反思。

## 4. 独立实测（脱离 pytest 夹具，`.tmp_r10_live_check.py`，跑后已删）

```
[PASS] revise 下调 confidence 0.8→0.48
[PASS] overturn 置 superseded
[PASS] maintain 不改条目
[PASS] reflected_apply 痕 + reason 透传
[PASS] run_reflection 落地 revise
[PASS] 裁决留教 1 条 recent
[PASS] user_note 并入正文
[PASS] never → permanent
[PASS] lesson 可注入
ALL LIVE CHECKS PASSED
```

## 5. 全量回归（受控对照，证明零新增失败）

**运行口径**（离线态 + 每测 90s 墙上时钟护栏，见 ROUND-11 记录 §5 的环境说明）：
```
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m pytest -q --tb=line -rfE -p pytest_timeout_guard --ignore=tests/test_intent_generation.py
```
（该次运行的工作树 = **R10 态**：`a726785` 叠加「R11 tracked 改动已 stash、R11 新测试已 ignore」。）

- **R10 态结果**：`44 failed, 3799 passed, 1 skipped`。
- **与 R11 态（43 failed / 3822 passed）逐条比对**：失败集合**完全一致**，
  唯一集合差是 `tests/test_health_producers.py::test_cron_missed`（时间窗口 flaky，R10 态失败 / R11 态通过）。
  ⇒ 这 44 条在 R10 态即已存在，**无一条落在 R9/R10 改动面**（`reflection` / `teachings` /
  `store.apply_reflection_verdict` / `persist` 孤儿清理）；分布为既有环境债：
  `test_emotes`（表情包 v2 改名）、`test_forgetting_and_asof` / `test_interview_dismiss` /
  `test_p1_temporal`（`sync_uuid` 夹具债）、`test_self_protection` / `test_permissions_risk` /
  `test_tools_permissions`（权限决策）、`test_token_usage`、`test_seed_sampling`、`test_llm_circuit_breaker` 等。
- R10 相关**定向 8 文件 137 passed**（§3），R9/R10 主链路全绿 → 判定 **R10 零新增失败**。

> ⚠️ 环境说明：本仓全量套件含一个真实联网用例
> `tests/test_proactive_ledger.py::test_relevance_tiers`（触发 fastembed 下载 BGE 模型），
> 无网络时会把整轮永久挂死——故加 `HF_HUB_OFFLINE=1` 走离线（0.77s 通过）并挂临时超时护栏插件。
> 该问题与 R9/R10 无关，属既有环境债。

## 6. 红线核对

- ✅ 未 commit（改动全在工作树），不推 origin、不 `--force`。
- ✅ 未新增表 / 未新增跨端字段 / 未新增向量；`item_history` 复用、零 DDL。
- ✅ 未改 judge 核心 / manager 其他注入块 / 前端 / `recall_items` / `extractor` / person 注册表。
- ✅ 未改 worldview 回望主逻辑（仅 persist 的孤儿清理排除反思账本行，R9 的 `_persist_worldview` 留痕逻辑原样保留）。
- ✅ 全链路 fail-open：`apply_reflection_verdict` / `_record_adjudication_lesson` 异常都仅告警返回，不抛、不断 tick。

## 7. 遗留 / 备注

- **R9 QC 观察点**（反思裁决走 `chat_memory` 免费组）：延续 R9 结论，非本工单必改；若生产日志 `reflection: ... llm_error` 频发再改 `chat`，不动架构。
- **stretch（指令 §3.2 可选、非验收必需）**：聊天流里检测用户显式纠错自动写 lesson —— 未做，留待后续轮次（本工单只做 `resolve_proposal` 主链路）。
- **ROUND-11 预告**：`intents` 表（意图成一等公民）+ 提案来源放开到意图 + `lessons` 注入链接 `<memory-context>` 同层；本工单已把 lesson feeder 接通。