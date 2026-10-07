# ROUND-08 实施记录（P2-R3 · lessons 注入）

> 依据：`ROUND-08-指令.md`；上游：`ROUND-07-QC.md` PASS。对齐 `reports/P2_实施工单_2026-10-07.md` §四。
> 范围：新增 `teachings` 表 + 读写方法 / SYNC 注册 / `build_lessons_context` / manager 注入块 / 测试。
> 状态：**待 WorkBuddy re-QC**。未 commit（R1+R2+R3 统一收口）。

## 0. 一句话
小桔从萧哥纠正/反馈里学到的条目（lessons）现在每轮随记忆一起注入对话，让她回话带得上、不重复踩同一个坑。限额=「全部 permanent + 最近 3 条 recent，总字数 ≤1200」，防挤爆上下文。

## 1. 真实落点（五个文件）

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | L29 | 新增 `import hashlib` |
| 同上 | `_init_schema()` L328-340 | 新增 `teachings` 表 DDL + `idx_teachings_scope` / `idx_teachings_created` |
| 同上 | L1021-1084 | 新增 `add_lesson()`（内容 md5 去重幂等、fail-open）+ `get_lessons_for_injection()`（permanent 全量 + recent 限 N + 字数 cap，fail-open） |
| `deskmate/memory/sync_store_sqlite.py` | `SYNC_TABLES` L262-279 | 注册 `teachings` spec（pk=sync_key=`lesson_id`、string、business_cols 全、无向量） |
| `deskmate/cognitive/memory_injector.py` | L377-402 | 新增 `build_lessons_context(base_dir, query)`（与 `build_emotional_context` 同风，fail-open 返回 ""） |
| `deskmate/memory/manager.py` | L656-668 | 在 delivered 块之后、`if not parts` 之前插入 lessons 注入块（`DESKMATE_LESSONS_INJECT=0` 单独开关） |
| `tests/test_sync_store_sqlite.py` | L531-535 | `TABLES_ABSENT_IN_FIXTURE` 补 `"teachings"`（否则 `skipped == list(...)` 断言红） |
| `tests/test_p2_lessons.py` | 新增 | 12 用例 |

未碰：`recall_items` 逻辑 / `extractor` / person 注册表 / 前端——符合 08 §7.4 红线。

## 2. 关键决策（与指令的偏差说明）

1. **SYNC 注册用**「plain dict」**而非指令里的 `SyncTableSpec(...)`**：`sync_store_sqlite.py` 的 `SYNC_TABLES` 真实格式是手写 dict（`pk`/`pk_type`/`sync_key`/`sync_uuid_kind`/`business_cols`/`conflict_col`），仓库内**不存在** `SyncTableSpec` 类。按内存里 R1/R6 同款写法落 dict（与 `memory_anchors`/`known_persons` 一致）。
2. **不改 `migrate()`**：`migrate()` 自动遍历 `SYNC_TABLES` 为每表补 `sync_uuid/sync_hlc/sync_tombstone` 三列 + 三触发器。指令 §3「并在 migrate 里为 teachings 补 sync 三列」是多余动作——注册即生效（R1 已验证此机制）。
3. **`build_lessons_context` 的库解析**：指令假设 `CognitiveStore(base_dir=...)` 与 `deskmate.paths.deskmate_base_dir`；实际签名是 `CognitiveStore(db_path=...)`。故按仓库既有 `load_cognitive_profile` 同款解析：`db = Path(base_dir)/"deskmate.db"`，否则 `_db_path()`（尊重 `DESKMATE_DIR`）。
4. **`lesson_id` = 内容 md5**：同内容幂等去重（不重插）；双端同内容 → 同 id → 同 sync_uuid → 自动合并（禁随机 uuid，与 P2 工单 §二一致）。
5. **permanent 排前 + 字数 cap**：`permanent` 常驻规则优先保，`recent` 按 `created_at DESC` 限 3；逐条累加文字，超 `char_cap=1200` 即停。

## 3. 自带测试（pytest 真实输出）

- 定向 + 回归：`.venv/bin/python -m pytest tests/test_p2_lessons.py tests/test_p1_anchor_recall.py tests/test_p2_known_persons.py tests/test_deskmate_local_extractor.py tests/test_sync_store_sqlite.py tests/test_proactive_handoff_context.py -q`
  → **`114 passed in 4.09s`**（含新 `test_p2_lessons` 12 例全绿）。
- 全量回归：见 §5。

新增用例覆盖 08 §6 全部要求（recent/permanent 限额、char_cap、空表、AUTO_INJECT 关、读库异常 fail-open、幂等），并加端到端挂线（`build_full_context_for_turn` 含「小桔学到的」块）+ SYNC 注册/触发器落 oplog。

## 4. 独立实测（08 §7.2）

```
add_lesson("反馈：萧哥不要她替用户做主", scope="permanent") → True
get_lessons_for_injection() 含该条 → True（条数 1）
build_lessons_context(base_dir=tmp) 非空 → True
recall_items("史料") 空库 → []（R2 遗产行为不受影响）
```

08 §7.3：`grep -n "teachings" deskmate/memory/sync_store_sqlite.py` → `265:    "teachings": {`（SYNC 注册存在）。

## 5. 全量回归（受控对照，证明零新增失败）

- 全量：`.venv/bin/python -m pytest -q` → **`46 failed, 3777 passed, 1 skipped`**（含豆包并行改的前端 `test_emotes` 9 条）。
- **受控基线**：把 R1/R2/R3 全部改动 `git stash`（回到 HEAD `28a2507`），对**同一组 22 个失败文件**重跑 → **`45 failed, 399 passed`**。
- **集合 diff**：`comm` 比对两边失败清单 → 仅 1 处差异：
  - 新增：`tests/test_llm_circuit_breaker.py::test_attempt_group_walks_to_next_channel_on_failure`（**LLM 通道/网络类，已知 flaky**）。
  - 该用例在我的改动下**单独串跑 3 次 = 23 passed ×3**（整文件全绿），全量里那 1 次失败属抖动，与本改动面无交集（不 import store/teachings）。
  - 除此之外**零新增、零修复**（其余 45 条两侧一致）。
- 失败均为存量类：`profile_items has no column sync_uuid` 夹具（10）、权限引擎 full-access（12+）、`test_emotes`（前端并行）、`test_cron_missed` flaky、LLM/网络等。

> 说明：与 R6 记录的 37 failed 相比有差异，系全量套件存在 flaky（如 `test_llm_circuit_breaker` 单跑 60–70s、`test_cron_missed`）+ 并行前端改动叠加所致；受控对照已证明**本改动零新增失败**（这才是判据）。

## 6. 红线核对

- ✅ 只改 store / sync / memory_injector / manager / tests；未动 `recall_items` 逻辑、extractor、person 注册表、前端。
- ✅ fail-open 全保持：`add_lesson`/`get_lessons_for_injection`/`build_lessons_context`/manager 注入块异常都不抛、不阻断主记忆注入。
- ✅ 未 commit（HEAD 未动）。

## 7. 遗留 / 备注

- feedback **自动写入**机制（把用户纠正自动落 `add_lesson`）按指令留作 **P3 子任务**；本期只提供读 + 手动写入入口（`add_lesson`），写入端架构不新开。
- P2 三件（R1 person 注册表 / R2 截断 Option B / R3 lessons 注入）业务代码至此闭环，待 re-QC 后统一收口 commit。