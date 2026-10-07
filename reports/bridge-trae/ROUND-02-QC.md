# ROUND-02 QC 判词（R2 · 抽锚）

> 判定：**PASS（无阻塞）**。独立读 git diff HEAD + 跑测试 + 功能验证，非凭实施记录自述。
> 时间：2026-10-07 17:45。指挥员：WorkBuddy。

## 一、真实改动落点（读 git diff HEAD 实锤）
R2 仅新增 `deskmate/cognitive/extractor.py` 改动（+123 行）。store.py / sync_store_sqlite.py / test_sync_store_sqlite.py 的 M 是 **R1 遗留未提交**（R1 即写 upsert_anchors + SYNC_TABLES 注册），R2 未再改这三文件。

extractor.py 实际落点：
- 新增 `import os`
- `_ANCHOR_TOPIC_FIELDS = ("topic","domain")`
- `_person_names_from_config()`：读 `DESKMATE_PERSON_NAMES` 环境变量，空=不产 person 锚
- `extract_anchors(text, source_title, source_type, extracted_items)`：抽 topic/book/person，滤空去重，try/except fail-open
- `_attach_anchors(pm, item_id, text, ...)`：取 `pm.store`（getattr 安全）调 `store.upsert_anchors("profile_item", item_id, anchors)`，外层再 try/except 双保险
- 八处挂线：4 处 `extract_from_text` + 4 处 `extract_cognitive_items`，均 `obj, op = add_*(...); _attach_anchors(pm, obj.id, ...)`

## 二、独立功能验证（不沿用 Trae 自测）
| 验证点 | 结果 |
|---|---|
| A 默认无人名名单 → 仅 topic/book，无 person | 通过 `[('book','万历十五年'),('topic','历史真实性')]` |
| B 配 `DESKMATE_PERSON_NAMES=萧盛,张三` → person 命中 | 通过 含 `('person','萧盛')` |
| C 滤空 + 去重 | 通过 `[{'topic':''},{'topic':'创作'},{'topic':'创作'}]` → `[('book','y'),('topic','创作')]` |
| D `_attach_anchors` 真落 `memory_anchors`（含 person） | 通过 3 行：book+person+topic，JOIN 语义成立 |
| E 幂等：重挂同锚行数不变 | 通过 count=3 稳定 |
| F fail-open：pm 无 store 不抛 | 通过 |
| G fail-open：extracted_items=None 不抛 | 通过 返回含 book 锚（book 来自 source，与 items 无关，设计正确；脚本断言写反，非代码缺陷） |
| 锚 item_kind 恒 profile_item | 通过 仅写 recall_items 查询的表，范围收敛 |

## 三、测试（实跑）
- `test_deskmate_local_extractor.py`：24 passed（R2 抽锚有覆盖）
- `test_sync_store_sqlite.py`：40 passed（R1 仍绿，R2 未破坏）

## 四、对 R1 QC 四条硬输入的闭合
1. 开口：`self.profile_manager.store` 确有（profile.py:58 暴露），挂线 `pm.store` 取句柄 通过
2. 挂线 `obj, op = add_*(...); item_id=obj.id` 通过 八处全对（无再犯 `item_id, op=...`）
3. fail-open 双保险 通过
4. 抽锚仅 profile_item、topic/book 为主 person 轻量 通过

## 五、遗留 / 移交 R3（非阻塞，但 R3 必须处理）
1. **`test_profile_scope_injection.py` 存量 10 failed（非 R2 引入）**：根因 = 夹具不跑 sync migrate，`persist` 在到达 recall 前就因 `profile_items has no column named sync_uuid` 炸。**R3 改 `recall_items` 前，必须先修该 fixture 让其 migrate**，否则 R3 无法观察到 L124 的 recall 断言（且 L124 脆弱断言"无锚数据完整列表=['s2']"在有锚数据下会红——R3 新测试须在有锚数据下验证顺边一跳，同时保留无锚数据的纯 FTS 回退）。
2. **person 锚默认沉睡**：未配 `DESKMATE_PERSON_NAMES` 不产 person 锚 → R3 召回的 person 邻座能力默认未激活。若 P1 需要 person 邻座，R3 须决定名单来源（仓库无 known-names 表，Trae 已确认）。建议 R3 定为"P1 不强制 person 邻座，person 锚作可选增强"。
3. R2 抽锚是纯副作用写，尚未被召回消费——这是 P1 分阶段的预期（R2 写、R3 读）。

## 六、结论
**R2 PASS，无阻塞。** Trae 可进 R3（recall_items 顺锚走一跳）。R3 细指令见 `ROUND-03-指令.md`。
