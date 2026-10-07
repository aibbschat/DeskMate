# ROUND-03 指令（R3 · 召回顺锚走一跳）

> 下发：WorkBuddy（指挥员）→ Trae（一线）。依据：P1 工单 §4.3；R2 QC 移交项。
> 范围：**只改 `recall_items` 的召回逻辑**。纯增益、零破坏、可回退。

## 一、目标
现有 FTS 召回命中 seed 项后，顺 seed 项的锚「走一跳」召回同锚的其他 profile_item，作为邻座补进结果。让 P1「同锚即邻座」真正生效（R2 已把锚写进 memory_anchors，R3 让它被消费）。

## 二、落点
- `deskmate/cognitive/store.py` 的 `recall_items(self, query, ...)` 方法体内（当前位于 upsert_anchors 之后；动手前先 `grep -n "def recall_items"` 确认精确行号，勿凭记忆）。
- 复用 R1 已建表 `memory_anchors` + 索引 `idx_anchor_lookup(anchor_kind, anchor_value, item_kind)`（store.py:285 区域）。

## 三、实现规格（必须照做）
1. **取 seed 锚**：FTS 命中 seed 项后，查 `memory_anchors WHERE item_id IN (seed_ids)` 拿 seed 的 (anchor_kind, anchor_value) 集合。
2. **反查邻座**：`SELECT DISTINCT item_id FROM memory_anchors WHERE (anchor_kind, anchor_value) IN seed_anchors AND item_id NOT IN (seed_ids) AND item_kind='profile_item'`（或走 idx_anchor_lookup）。
3. **召回邻座全文**：用邻座 item_id 反查 `profile_items` 拿内容，按既有返回结构附上（建议带来源标记 anchor-neighbor，便于调试与 fail-open 区分）。
4. **限量**：邻座召回 ≤ 5 条（防爆炸，P1 本就量小）。
5. **整段 fail-open**：顺锚逻辑包 try/except，任何异常 → 仅 warning，**退回今天纯 FTS 口径**（seed 召回不受影响，绝不空结果）。
6. **开关**：`DESKMATE_ANCHOR_RECALL`（env，默认 1；=0 即时关顺锚召回，纯 FTS）。
7. **不改返回结构签名**：既有调用方（memory_injector 等）零改；邻座作为结果集额外项，向后兼容。

## 四、R2 QC 移交的硬前置（必须先做，否则验收不可评估）
1. **修 `test_profile_scope_injection.py` 夹具**：该文件当前 10 failed（存量，非 R2 引入），根因夹具不跑 `sync_store_sqlite.migrate` → persist 炸在 recall 前（`profile_items has no column named sync_uuid`）。R3 动手第一件事：让该 fixture 建库后先 `migrate(conn, 'mac', 'profile_items')` + `migrate(conn, 'mac', 'memory_anchors')`，使 recall 路径可达。
2. **L124 脆弱断言处理**：原断言"无锚数据完整列表=['s2']"。R3 加锚后须新增「有锚数据」测试验证顺边一跳（seed 命中 A、A 与 B 同 topic → 召回含 B）；同时保留无锚数据场景仍 = 纯 FTS。两场景都要绿。

## 五、验收（R3 交回时 WorkBuddy 独立验）
- 写两条 profile_item：s_A{topic:历史真实性}、s_B{topic:历史真实性}（经 extractor 抽锚，或直写 memory_anchors）。
- `recall("历史真实性")` 应召回 s_A 与 s_B（顺锚一跳命中）。
- `recall` 在 `memory_anchors` 表损坏/缺失时仍返回 seed 结果（fail-open）。
- `DESKMATE_ANCHOR_RECALL=0` 时 recall 行为与今天纯 FTS 一致（无邻座）。
- `test_profile_scope_injection.py` 经夹具修复后绿；`test_sync_store_sqlite.py` 仍 40 passed；新增 R3 测试落 `tests/`。

## 六、红线（爆炸半径锁定）
- 不改 `extract_from_text` / `extract_cognitive_items` 签名与返回（R2 已锁）。
- 不引入显式边表（那是 P2+，非 P1）。
- 不碰 `memory/manager.py` 注入链（lessons 注入是 P2+）。
- commit 归 R5，R3 不 commit。

## 七、交付
R3 完成后写 `reports/bridge-trae/ROUND-03-实施记录.md`（含真实落点 + 自测 + 验收证据），交 WorkBuddy QC。
