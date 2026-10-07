# ROUND-04 实施记录（R4 · 端到端集成验证 + 部署前置检查）

> 依据：`ROUND-04-指令.md`；R3-QC §六 移交项；README §一 R4；工单 §七。
> 状态：**待 WorkBuddy QC**。**未 commit、未 deploy**（按指令 §四，commit 归 R5、部署由萧哥走红线）。

## 1. 端到端集成（指令 §3.1 核心）

新增 `tests/test_p1_anchor_e2e.py`（2 例）——**不手工 upsert 锚**，全程走真实抽取链路（LLM 打桩）：

- `test_p1_extract_then_recall_closed_loop`：
  1. `extract_from_text("万历十五年中，史料必须交叉比对", "万历十五年", "book")` → 抽出一条 topic=历史真实性 的 stance；
  2. 再抽一条 domain=历史真实性 的 thinking_framework（**不同维度、同 topic 锚**，避开同 topic 立场会走 update/矛盾分支）；
  3. **断言 ①「抽取即落锚」**：`memory_anchors` 里有 `('topic','历史真实性')` + `('book','万历十五年')`（book 为来源级锚）；
  4. **断言 ②「顺锚召回」**：`recall_items("交叉比对")` → seed 是 FTS 直命中的 stance；thinking_framework 以 `via_anchor=True` + `anchor_evidence="同主题：历史真实性"` **被顺锚带回**。
- `test_p1_extract_survives_anchor_failure`：打桩让 `CognitiveStore.upsert_anchors` 抛 `RuntimeError` → `extract_from_text` 仍返回 `stances==1`、无 `error`（抽取链路不因挂锚失败中断）。

> 说明：R2 的挂线是**逐条目**抽锚（单条目 `[item_dict]`），故两条不同维度的条目各挂到自己的 topic 锚——同 topic 值 → 同一 `(topic, value)` 锚 → 互为邻座，闭环成立。

## 2. 部署清单核对（指令 §3.2 / R5 前置）

- 读 `scripts/deploy_cloud_sync.sh`：**`CHANNEL_MODULES` 原缺** `deskmate/cognitive/store.py` 与 `extractor.py`（只有 LLM 通道组/熔断 + sync 三件套 + `manager.py`）。
- **已编辑补入**（L95-100，含漏传风险注释）：
  ```
  "deskmate/cognitive/store.py"      # memory_anchors 表 + recall_items 顺锚走一跳
  "deskmate/cognitive/extractor.py"  # 抽取时挂锚
  ```
- 验证：`bash -n scripts/deploy_cloud_sync.sh` → **SYNTAX OK**；推送分支 `deskmate/*` → 目标 `/opt/openworker/deskmate/cognitive/`，脚本内含 `mkdir -p` + 清 `__pycache__`，路径可落地。
- **未执行部署**（按指令 §四）。

## 3. 测试（pytest 真实输出）

- 新增/补充后 P1 全相关套件：
  `tests/test_p1_anchor_recall.py tests/test_p1_anchor_e2e.py tests/test_profile_scope_injection.py tests/test_sync_store_sqlite.py tests/test_deskmate_local_extractor.py -q`
  → **`86 passed in 0.72s`**
- 工单 §七 7 例对照（名称按 R3 指令微调，语义一一对应）：

  | 工单 §七 | 落成用例 | 结果 |
  |---|---|---|
  | `test_anchor_extract_topic_book` | `test_anchor_extract_topic_book` | ✅ |
  | `test_anchor_upsert_and_recall` | `test_anchor_recall_hops_to_neighbor` | ✅ |
  | `test_anchor_per_cap` | `test_anchor_recall_cap`（上限=5，按 R3 指令） | ✅ |
  | `test_anchor_recall_off` | `test_anchor_recall_off_returns_pure_fts` | ✅ |
  | `test_anchor_dedup` | `test_anchor_dedup`（新增） | ✅ |
  | `test_anchor_sync_plaintext` | `test_anchor_sync_plaintext`（新增） | ✅ |
  | `test_anchor_fail_open` | `test_anchor_recall_fail_open` | ✅ |

## 4. 验收判据确认清单（指令 §3.3）

- [x] 抽取即落锚（R2 真实链路）——e2e 断言 ①
- [x] 顺锚召回在真实链路命中邻座（R3 真实链路）——e2e 断言 ②（`via_anchor=True`）
- [x] `DESKMATE_ANCHOR_RECALL=0` 即时回退纯 FTS——`test_anchor_recall_off_returns_pure_fts`
- [x] 真·表损坏 fail-open——R3-QC §二.2 已实锤（DROP 表→仍返回 seed）
- [x] `deploy_cloud_sync.sh` CHANNEL_MODULES 含 `store.py`（R5 不漏传）——§2 已补

## 5. 红线 / 遗留

- 未碰 `extract_from_text`/`recall_items` 签名与返回（R2/R3 已锁）；未引显式边表；未碰 `memory/manager.py` 注入链。
- **R4 未 commit、未 deploy**（归 R5；部署由萧哥走红线）。
- R4 仅新增测试 + 改部署脚本清单，**未改生产代码** → 无回归风险（R3 全量 `36 failed/3756 passed` 结论不变）。