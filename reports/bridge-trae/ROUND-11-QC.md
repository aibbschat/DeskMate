# ROUND-11 QC 判词 · P3 自主闭环前半程（意图层）

**交回**：Trae（R11 完工）
**核验方式**：WorkBuddy 读真实 diff + 定向跑测试 + 独立路径一致性核验
**结论**：✅ **PASS** —— 166 passed 零回归（基线 144 + R11 新增 22），意图层两条核心（意图产生 + 提案不越权）实测真落地。

---

## 1. 对照 ROUND-11 工单：逐条验收

| 工单要求 | 实现落点 | 核验 |
|---|---|---|
| 意图层 `intents` 表（open/active/done/dropped + origin + proposed_task_id 回填） | `store.py` DDL + `add_intent`/`get_open_intents`/`link_intent_to_task`/`set_intent_status` | ✅ 全 fail-open；`test_add_and_get_open_intents`/`link_and_set_status`/`crud_fail_open` 覆盖 |
| 低频 `intent_generation` 行为（7 天错峰，从关怀/观察/兴趣长意图） | `behaviors/intent_generation.py` + `registry` 登记 `risk=low label=起心动念` + `settings` 配置段 + `engine` 分支 | ✅ 三素材源各 fail-open；冷却/开关/素材/provider 四闸门；`test_disabled_by_cfg/env`/`no_material`/`no_provider`/`llm_error`/`cooldown_blocks` 钉死 |
| 意图 → 提案 `_propose_from_intent`（带 `intent_id` + how，一律 PROPOSED 不自动执行） | `engine.propose_from_intent`：action_type=`"intent"`（**不在白名单**）/ risk=high / status=PROPOSED；`link_intent_to_task` 回填 + `set_intent_status active` | ✅ `test_propose_from_intent_stays_proposed` + `test_intent_action_not_in_whitelist_so_never_auto_runs`（tick 复跑也碰不到 `intent`） |
| 护栏：自生成意图本回合禁自动执行、`route_task` 保持 self、kill switch/outbound/visibility_gate 不动 | 未触路由/出网/可见性；动作 slug 故意绕开白名单 | ✅ diff 面零越界 |
| `intents` 跨端暂不强制进 SYNC（双写 LWW 风险留拍板） | `sync_store_sqlite.py` **只加注释**，明确不登记 `intents` | ✅ 无越界；符合工单约定 |
| 闭环：意图提案裁决 → 教学回流（R10 链路）| 复用 `resolve_proposal` → `_record_adjudication_lesson` | ✅ `test_adjudicating_intent_proposal_feeds_lessons`（拒绝→recent lesson，注入链可取） |

---

## 2. 指令外必要核验（防隐患）

- **写库路径一致性（红线）**：`propose_from_intent` 用 `os.path.join(base_dir, "deskmate.db")` 重建 `CognitiveStore`。核实 `engine.base_dir` 默认 = `DESKMATE_DIR or ~/.deskmate`，而 `deskmate_db_path()` 同源 `deskmate_base_dir() / "deskmate.db"` → **与生产库路径完全等价，无写错库风险**。✅
- **解析健壮性**：`parse_intents` 容忍代码围栏/前后杂文本/单对象/坏项丢弃/origin 口径外回落默认/horizon 归一。✅
- **幂等去重**：落账前按 `get_open_intents` 近重复（去标点）比对，避免反复追问同一念头。✅ `test_dedup_against_existing_open_intent` 钉死。
- **降级诚实**：设计底稿「长期兴趣」源自 `memory_edges`（未上线），本回降级到已上线的 `memory_anchors` 同锚共现簇（≥2）。已在文档与 prompt 注释里写明，不假装未上线能力。✅

---

## 3. 测试与回归

- **R11 定向套件**：`test_intent_generation.py` 22 例（CRUD / parse / 四闸门 / 护栏 / 闭环）全 PASS。
- **基线回归**：叠加 R9(7)+R10(7 含教学反馈+worldview 夹具修复)+P1/P2/sync/settings/extractor，合计 **166 passed，0 failed，0 regression**。
- 排除 3 个真出网测试（基线惯例），其余全绿。
- 注：`test_intent_action_not_in_whitelist_so_never_auto_runs` 会触发一次 `eng.tick()`，已确认不污染、不执行意图提案（白名单不含 `intent`），符合护栏设计。

---

## 4. 红线核对

- 仅改后端 Python + 新增 1 行为模块 + 1 测试 + 文档；**无新表 DDL 之外的迁移风险**（`intents` 表 `IF NOT EXISTS` 自愈）。
- **未动** 出网/可见性/kill switch/路由 `route_task`。
- 垃圾文件 `:memory:.ses` / `design/` / `surfaces/gui/dist.tar.gz` 已排除，不进库。
- `ROUND-11-指令.md` 已随 a726785 入库（macOS 文件名编码伪影致 status 显 untracked，内容未变，本轮不重复提交）。

---

## 5. 收口与下一步

- 收口 commit（见 git log）：R11 八处改动 + ROUND-11-QC + ROUND-10 实施记录补 §5。
- 部署：仅后端 Python，无新表、无新前端 → 可走 `deploy_cloud_sync.sh` 对齐 + 重启（待萧哥一声）。
- **P3 自主闭环还剩后半程**（本回合按工单**故意留白**）：意图被裁决 executed 后、由 `propose_from_intent` 之外的新执行行为真正落地（仍须经人审/护栏），留 ROUND-12。
- 江河流域：**反思（R9/R10）+ 教学（R3/R10）+ 意图产生（R11）** 已闭环；**自主执行** 仍是「只提不干」状态（设计有意：先让目标成型、人审习惯养成后再放开执行）。
