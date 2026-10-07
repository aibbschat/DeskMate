# ROUND-01 实施记录（R1 · 数据层）

> 依据：`README.md` §一 R1；工单 §三/§4.1/§4.3；ROUND-00 确认项 3。
> 状态：**待 WorkBuddy QC**。未 commit（按 README，commit 归 R5）。

## 0. R0 前置：已按 ROUND-00 修正施工单（干净基线）

工单 `reports/P1_汇流最小版_施工单_2026-10-07.md` 已改三处 + 一处补丁：

- **§三 DDL**：四列复合主键 → **单列 `row_key TEXT PRIMARY KEY`** + `UNIQUE(item_kind,item_id,anchor_kind,anchor_value)` 兜底（触发器只认单列 `sync_key`，见 ROUND-00 §3）。
- **§4.2 id 取法**：`item_id, op = ...`（笔误）→ `obj, op = ...; item_id = obj.id`（`add_*` 返回 `(对象, op)`，ROUND-00 §1）。
- **§七**：补 `test_profile_scope_injection.py:124` 脆弱断言提示（R3 注意）。
- **§4.3 / §九**：补 `SYNC_TABLES` 注册 spec、四项确认项勾核。
- ⚠️ **新增发现（ROUND-00 未列）**：`tests/test_sync_store_sqlite.py:529` 的 `TABLES_ABSENT_IN_FIXTURE` 须补 `"memory_anchors"`——该夹具不建此表，`migrate` 会把它一并报 `skipped`，L613 断言 `skipped == list(TABLES_ABSENT_IN_FIXTURE)` 会红。已改。

## 1. 改了哪些文件 / 函数 / 行号

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | `_init_schema()` L264-280 | 追加 `memory_anchors` DDL + `idx_anchor_lookup` 反查索引（建 `profile_items` 同区） |
| 同上 | 模块级 L60-67 | `_ANCHOR_SEP="\x1f"` + `anchor_row_key()` 确定性主键派生 |
| 同上 | `CognitiveStore.upsert_anchors()` L861-896 | `INSERT OR IGNORE` 批量写锚，幂等 + fail-open |
| `deskmate/memory/sync_store_sqlite.py` | `SYNC_TABLES` L222-241 | 字典**末尾**追加 `memory_anchors` spec（pk/sync_key=row_key，string） |
| `tests/test_sync_store_sqlite.py` | L530-533 | `TABLES_ABSENT_IN_FIXTURE` 补 `"memory_anchors"` |

## 2. 关键决策（与工单/ROUND-00 的偏差说明）

1. **`row_key` = US(`\x1f`) 拼接的确定性字符串**（照 ROUND-00 §3 方案），未用随机 uuid——双端同锚 → 同 `row_key` → 同 `sync_uuid` → 自动合并。
2. **`upsert_anchors` 幂等**：`INSERT OR IGNORE`；重复锚由主键 + UNIQUE 双重跳过。**fail-open**：异常仅 `logger.warning` 并返回 0，绝不抛（不影响主抽取主链路）。
3. **不写迁移代码**：`migrate()` 遍历 `SYNC_TABLES` 自动补 `sync_uuid/sync_hlc/sync_tombstone` 三列 + 三触发器，注册即可（已实证）。
4. **锚表落 `deskmate.db`**（与 `profile_items` 同库），故可被 `migrate` 覆盖。

## 3. 自带测试（pytest 真实输出）

- 定向套件：`.venv/bin/python -m pytest tests/test_sync_store_sqlite.py tests/test_sync_store_sqlite_diary.py -q`
  → **`64 passed in 1.23s`**
- 烟雾自测（临时库，非落库）：
  - `upsert_anchors("profile_item","s1",[("topic","历史真实性"),("book","万历十五年"),("","")])` → 写 2 条（空锚被过滤）；再次调用不增行（幂等）。
  - `migrate(conn,"mac","memory_anchors")` → `{'backfilled': {'memory_anchors': 2}, 'tables': ['memory_anchors'], 'skipped': []}`；三列齐。
  - 回填 `sync_uuid` = `deskmate:memory_anchors:profile_item\x1fs1\x1ftopic\x1f历史真实性`（确定性字符串，非 uuid5）。
  - 新 `INSERT` → 落 `_sync_oplog` 1 条 upsert，payload 含全部 6 业务列。
- **全量回归（零新增失败的硬证据）**：对同一组 20 个存量失败测试文件，`git stash`（去掉 R1 改动）前后均为 **`45 failed, 344 passed`**——**R1 未引入任何新失败**。
- 全量：`.venv/bin/python -m pytest -q` → `47 failed, 3740 passed, 1 skipped`。47 全部为**存量失败**，两类根因均与 R1 无关：
  - `sqlite3.OperationalError: table profile_items has no column named sync_uuid`（测试夹具未跑 sync migrate）；
  - 权限引擎 `Decision(allowed=True, reason='full access')` 环境类（`bypass/auto` 模式）。

## 4. 自测覆盖（对工单验收/红线）

- §九 确认项 3（单列 pk 可注册）：✅ 实证 `migrate` 成功 + 触发器落 oplog。
- §七 `test_anchor_sync_plaintext` 的一半（注册 + 无向量列）：✅ 模块加载守卫 `_validate_sync_tables_no_vectors` 通过（import 成功）。
- 红线「绝不搬向量」：✅ `business_cols` 无向量列，启动守卫通过。
- 红线「零破坏/可回退」：✅ 建表为 `IF NOT EXISTS` 纯增益；表空/未建不影响任何既有查询。

## 5. 遗留 / 不确定项

1. 触发器需 `migrate` 跑一次才在**真实库**生效——部署时 `SyncNode.migrate` 覆盖（归 R5）。
2. 部署脚本 `CHANNEL_MODULES` 是否含 `store.py`/`extractor.py`，待 R5 核。
3. **R2 开口**：需确认 `extractor` 拿 `store` 句柄的路径（`self.profile_manager` 是否暴露 `.store`，或需注入）——R2 落地时先证。
4. 未 commit（按 README R5 收口）。