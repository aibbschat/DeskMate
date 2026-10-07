# ROUND-01 QC（WorkBuddy · R1 数据层）

> 方法：读真实 diff（`git diff HEAD`，3 文件 +92 行）+ 读真文件落点 + 独立功能验证（不沿用 Trae 自测）。
> 判词：**PASS**（无阻塞项）。R1 可收口，Trae 进 R2。

## 一、判词：PASS

## 二、证据（逐条对照工单/ROUND-00）

### 1. 表 DDL（store.py:281-292）—— ✅
- 单列 `row_key TEXT PRIMARY KEY` + `UNIQUE(item_kind,item_id,anchor_kind,anchor_value)` 双锁（落实 ROUND-00 §3 修正，复合主键不可注册问题根除）。
- `idx_anchor_lookup` 反查索引 `(anchor_kind, anchor_value, item_kind)` 对齐 R3 邻座召回查询形态。
- 建在 `profile_items` 同区、`IF NOT EXISTS` 纯增益。

### 2. `anchor_row_key`（store.py:59-67）—— ✅
- `_ANCHOR_SEP="\x1f"`（US，正文不会含）+ `.join(四列)` 确定性派生。独立验证：`anchor_row_key(...)` 两次调用相等；`sync_uuid` 派生为 `deskmate:memory_anchors:profile_item\x1fs1\x1ftopic\x1f历史真实性`（双端同锚→同 uuid→自动合并）。

### 3. `upsert_anchors`（store.py:861-896）—— ✅（含 commit 实锤）
- **commit 生效（关键风险点）**：`_connect()` 是 contextmanager，`yield` 后 `conn.commit()`（store.py:235）。独立验证：写后另开连接读回 → **2 行真实落盘**，证明非悬空。
- 幂等：`INSERT OR IGNORE` + UNIQUE 双重跳过。独立验证：重复写 → 行数稳定 2。
- 空锚过滤：`(k,v)` 中 `k or v` 空则跳过（独立验证：含 `('','')` 输入被滤）。
- fail-open：整段 `try/except → logger.warning + return 0`（结构上正确；注：本 QC 的 fault-inject 测试因 `__init__` 自动建表未触发异常路径，故 fail-open 行为靠代码结构佐证，非故障注入实证——非阻塞）。

### 4. SYNC_TABLES 注册（sync_store_sqlite.py:227-241）—— ✅
- `pk/sync_key = row_key`、`pk_type=text`、`sync_uuid_kind=string`、无向量列。
- 模块加载守卫 `_validate_sync_tables_no_vectors` 经 import 通过（40 测试绿即证）。
- 注：diff 中 `sync_uuid_kind` 行前缀的诡异 `m` 是**终端渲染伪影**——读真文件 L231 为干净 `"sync_uuid_kind": "string",`，非真实字符，无语法问题。

### 5. migrate 集成（Trae 宣称，本 QC 独立验证）—— ✅
- `migrate(conn,'mac','memory_anchors')` → `{'backfilled':{'memory_anchors':3},'tables':['memory_anchors'],'skipped':[]}`。
- 同步三列（`sync_uuid/sync_hlc/sync_tombstone`）已补；3 行 `sync_uuid` 全回填。
- 触发器生效：新 INSERT → `_sync_oplog` 落 1 条（`table_name='memory_anchors'`）→ 跨端收敛链路接通。

### 6. 测试（test_sync_store_sqlite.py:529-533）—— ✅
- `TABLES_ABSENT_IN_FIXTURE` 补 `"memory_anchors"`（Trae 发现的真实修复：否则 L613 `skipped==` 断言红）。
- 定向套件 `tests/test_sync_store_sqlite.py` 本 QC 实跑 **40 passed**（Trae 报 64 含 diary 套件，口径一致）。

### 7. R0 工单修正已落实 —— ✅
- 工单 §三（row_key）、§4.2（id 取法 `obj, op=...; item_id=obj.id`）、§4.3/§九（SYNC_TABLES spec）均按 ROUND-00 改正（grep 工单确认）。

## 三、阻塞项
**无。**

## 四、对下一轮（R2 抽锚）的输入
1. **开口问题**（Trae 实施记录 §5.3）：`extractor` 如何拿 `CognitiveStore` 句柄调 `upsert_anchors`？须先证 `self.profile_manager` 是否暴露 `.store`，或需注入——R2 第一步落地。
2. R2 挂线须用 `obj, op = add_*(...); item_id = obj.id`（**非** `item_id, op=...`，ROUND-00 §1 已钉死）。
3. 抽锚范围 P1 仅 `profile_item`；`topic`（取 extracted_items 的 topic 字段）/ `book`（source_type=='book'→source_title）/ `person`（best-effort）按工单 §4.2。
4. 抽锚失败须 fail-open（异常不阻断主抽取），与 R1 `upsert_anchors` 的 fail-open 同构。
5. R2 写完先跑 `tests/test_sync_store_sqlite.py` + 既有 `test_profile_scope_injection.py`（留意 §七 L124 脆弱断言，无锚数据下应仍绿）。
