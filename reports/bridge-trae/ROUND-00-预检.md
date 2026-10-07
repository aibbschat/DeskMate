# ROUND-00 · 施工前预检（WorkBuddy 执行，纯读码，未动笔）

> 对应工单 §九「施工前确认项」。目的：给 Trae 一个干净、无坑的实施基线。
> 方法：逐条读真实代码，结论均有 `文件:行` 实证，非推断。

## 确认项 1：`add_*` 返回值 —— ⚠️ 工单 §4.2 有笔误，须改

- 实测 `profile_manager.add_stance`（`deskmate/cognitive/profile.py:101`）返回 `(Stance, op)`，**二元组第一元素是 Stance 对象，不是 id 字符串**（L144 `return stance, "created"`，其余 add_argument_pattern/add_value_ranking/add_thinking_framework 同构：L317/373/447）。
- `Stance` 继承自 `CognitiveItem`，有 `id` 字段（`models.py:56 id: str`，L119-120 `_new_id("stance")` 赋值）。
- `extractor.extract_from_text` L182 当前是 `_, op = self.profile_manager.add_stance(...)`——**丢弃了 id**。
- **工单 §4.2 写「`item_id, op = ...`」是笔误**：那样会把 Stance 对象赋给 `item_id`。正确写法：
  ```python
  stance, op = self.profile_manager.add_stance(...)
  item_id = stance.id
  ```
- 四项 `add_*`（stance/argument_pattern/value_ranking/thinking_framework）返回同构 `(obj, op)`，挂线循环可统一处理。

## 确认项 2：`recall_items` 插入点 + 调用方兼容 —— ⚠️ 有一处既有测试炸弹

- 插入点确认：`store.recall_items`（`store.py:828-870`）在 `return [...][:top_k]`（L865-870）之前插入「邻座召回」段，与工单 §五 一致。
- 返回体为 dict：`{id, layer, dimension, topic, content, confidence}`（L866-867）。新增 `via_anchor`/`anchor_evidence` 是**纯增键**。
- 调用方 `memory_injector.py:155-161` 只用 `r["id"]`（L158），**兼容**新键。
- **⚠️ 炸弹**：`tests/test_profile_scope_injection.py:124` 断言 `recall_items("深夜写东西思路")` 返回**完整列表**恰为 `["s2"]`。一旦顺锚召回给该 query 加邻居，这条会红。
  - 缓解：现有测试 DB 不经由 extractor 写锚 → 无 `memory_anchors` 行 → 邻座段取不到 sibling → 列表不变 → 仍通过。**但属脆弱断言**，Trae 在 R3/R4 若新建"经 extractor 写锚 + 断言完整召回列表"的测试，必红。建议：R4 新增测试用 `exclude_ids` 或只断言 `[0]`/含某 id，不断言完整列表；既有 L124 如受影响须同步更新并说明。

## 确认项 3：`SYNC_TABLES` 注册 —— ⚠️ 工单 §三 复合主键 DDL 不可直接注册，须改

- `SYNC_TABLES` 在 `sync_store_sqlite.py:61`，每条 spec 须含 `pk`/`pk_type`/`sync_key`/`sync_uuid_kind`/`business_cols`/`conflict_col`。
- 机制约束：`sync_key` 被触发器生成直接用作 `NEW.<sync_key>`（`sync_store_sqlite.py:336 COALESCE({sync_key}, ...)`），回填用 `WHERE {spec['pk']}=?`（L469/487）。即 **`pk` 与 `sync_key` 都必须是单列名**。
- 工单 §三 的 `memory_anchors` 是**四列复合主键** `(item_kind, item_id, anchor_kind, anchor_value)`——**无法直接注册**（触发器只会 `NEW.item_kind`，丢三列）。
- **修正方案（推荐）**：表加单列 `row_key TEXT PRIMARY KEY`，确定性 = `item_kind + '\x1f' + item_id + '\x1f' + anchor_kind + '\x1f' + anchor_value`；保留四列 + `UNIQUE(item_kind, item_id, anchor_kind, anchor_value)` 作业务唯一约束。注册：
  ```python
  "memory_anchors": {
      "pk": "row_key", "pk_type": "text", "sync_key": "row_key",
      "sync_uuid_kind": "string",
      "business_cols": ("row_key","item_kind","item_id","anchor_kind","anchor_value","created_at"),
      "conflict_col": None,
  }
  ```
  这样零特殊 casing 接入现有同步/触发器/向量守卫（`_validate_sync_tables_no_vectors` L233 只查向量列，锚表纯文本天然过）。

## 确认项 4：`recall_items` 消费者新增字段兼容 —— ✅ 通过（除确认项 2 的 L124 炸弹外）

- 唯一消费者 `memory_injector.py` 用 `r["id"]`（dict 键访问），不受新键影响。
- 其余调用方均为测试（`test_profile_scope_injection.py`），已见确认项 2。

## 额外落点（供 Trae 直接定位）

- 建表位置：`store.py:246` 建 `profile_items` 同区追加 `memory_anchors` DDL（含上方 row_key 修正）。
- 抽锚挂线入口：`extractor.extract_from_text` L182/L194/L... 各 `add_*` 调用后补 `upsert_anchors`。
- `extractor` 调 `CognitiveStore.upsert_anchors` 的引用方式（self.store / self.profile_manager.store）须 Trae 在 R1/R2 落地时确认——R1 先实现 `upsert_anchors`，R2 再确认 extractor 持有 store 句柄。

## 预检结论

工单 §九 四项中 **3 项需修订工单后才可施工**（§4.2 笔误 / §三 复合主键 / §七 L124 脆弱断言）。本预检已给出具体改法。Trae 进 R1 前请先按本文件修正工单三处，再开写码。
