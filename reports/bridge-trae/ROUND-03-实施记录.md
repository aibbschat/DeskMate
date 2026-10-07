# ROUND-03 实施记录（R3 · 召回顺锚走一跳）

> 依据：`ROUND-03-指令.md`；工单 §4.3/§五；R2-QC §五 移交项。
> 状态：**待 WorkBuddy QC**。未 commit（按 README 六，commit 归 R5）。

## 0. R2-QC 移交硬前置（已先做）

1. **修 `tests/test_profile_scope_injection.py` 夹具**：`_store()` 建库后先跑
   `sync_store_sqlite.migrate(conn,'mac','profile_items')` + `migrate(...,'memory_anchors')`。
   → 该文件 **10 failed → 12 passed**（recall 路径至此可达；L122 脆弱断言在无锚数据下仍 `==["s2"]`，保持原样未弱化）。
2. **person 邻座**：按 QC 建议，P1 **不强制** person 邻座；person 锚作可选增强（默认沉睡）。R3 未改此口径。

## 1. 改了哪些文件 / 函数 / 行号

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | 模块级 L70-81 | 新增 `_ANCHOR_NEIGHBOR_CAP=5`、`_ANCHOR_KIND_LABELS`、`_anchor_kind_label()` |
| 同上 | `recall_items()` L948-962 | FTS 命中后（`hits`）追加顺锚一跳，整段 `try/except → warning`，返回 `hits[:top_k]` |
| 同上 | 新增 `_recall_anchor_neighbors()` L964-1027 | seed 锚集合 → 反查同锚邻座（走 `idx_anchor_lookup`）→ 拉正文（只取 active）→ 附 `via_anchor`/`anchor_evidence` |
| `tests/test_profile_scope_injection.py` | `_store()` L28-41 | 夹具补 sync migrate（QC 硬前置） |
| `tests/test_p1_anchor_recall.py` | 新增（5 例） | R3 召回专项测试 |

**未改**：`extract_from_text`/`extract_cognitive_items` 签名与返回（R2 已锁）；`memory/manager.py` 注入链；未建显式边表。

## 2. 关键决策（与工单/指令的偏差说明）

1. **邻座上限 = 5**：照 `ROUND-03-指令.md` §三.4（≤5），**不采用**工单 §五 的 `per_anchor_cap=8`。指令是 R3 的专项细化，更具体、更晚出，从之。
2. **追加后截断（`hits + neighbors` → `[:top_k]`）**：忠实于工单 §五伪码。**已知局限**：当 FTS 自身已填满 `top_k` 时，邻座会被截掉（此时结果 = 纯 FTS，**零破坏但也不增益**）。P1 最小版接受此局限；P2 若要「邻座必现」，可改为「为邻座预留配额」——但那会挤压 FTS 结果，非纯增益，故本轮不做，留 QC/指挥员定夺。
3. **来源标记**：用纯增键 `via_anchor=True` + `anchor_evidence`（如「同主题：历史真实性」），对齐工单 §五；未再加 `source="anchor-neighbor"` 以免键泛滥（指令 §三.3 属"建议"）。
4. **`item_kind` 收敛**：邻座查询硬编码 `item_kind='profile_item'`，与 `recall_items` 查询的表一致（P1 锚范围）。
5. **fail-open**：`_recall_anchor_neighbors` 内不吞异常（由调用方整段包），调用侧 `except Exception → logger.warning`，**不空结果、不抛**；开关 `DESKMATE_ANCHOR_RECALL=0` 即时退回纯 FTS。
6. **不改签名**：`recall_items` 返回结构仅**增键**，唯一消费方 `memory_injector.py:157` 按 `r["id"]` 取值，兼容。

## 3. 自带测试（pytest 真实输出）

- `tests/test_p1_anchor_recall.py`（新增，R3 专项）→ **`5 passed`**
  - `test_anchor_recall_hops_to_neighbor`：A/B 同锚、topic 留空 → 只有 A 被 FTS 命中，**B 由顺锚带回**，带 `via_anchor=True` + evidence 含主题值（证明是真·走一跳，非 FTS 直命中）。
  - `test_anchor_recall_off_returns_pure_fts`：`DESKMATE_ANCHOR_RECALL=0` → `["s_A"]`，无邻座。
  - `test_anchor_recall_no_anchors_keeps_pure_fts`：表空 → `["s_A"]`，结果无 `via_anchor` 键。
  - `test_anchor_recall_cap`：9 条同锚 → 邻座数 `≤ _ANCHOR_NEIGHBOR_CAP`(5)。
  - `test_anchor_recall_fail_open`：monkeypatch `_recall_anchor_neighbors` 抛 `RuntimeError` → 仍返回 `["s_A"]`，不抛（真·异常路径实证，非结构佐证）。
- `tests/test_profile_scope_injection.py tests/test_sync_store_sqlite.py` → **`52 passed`**（夹具修复后原 10 failed 全转绿；R1 同步套件仍绿）。
- **全量 `pytest -q`** → **`36 failed, 3756 passed, 1 skipped in 891.78s`**。
  - 基线（R1 自测记录）：`47 failed, 3740 passed, 1 skipped`。
  - 差：**失败 47→36（少 11）**、通过 3740→3756（多 16 = 修回 11 + 新增 5）。**零新增失败**；剩余 36 全为存量环境类（权限引擎 `Decision(allowed=True, reason='full access')`、`worldview_reflection`、`token_usage` 等），与本任务无关。

## 4. 自测覆盖（对 R3 指令 §五 验收）

- ✅ 写 s_A/s_B 同 topic 锚 → `recall("史料")` 召回 s_A（seed）+ s_B（顺锚）。
- ✅ `memory_anchors` 缺失/异常 → 仍返回 seed（fail-open 实证）。
- ✅ `DESKMATE_ANCHOR_RECALL=0` → 与纯 FTS 一致。
- ✅ `test_profile_scope_injection.py` 夹具修复后绿；`test_sync_store_sqlite.py` 40 passed。

## 5. 遗留 / 不确定项

1. §2 决策 2 的「追加后截断」局限，需 QC/指挥员确认 P1 是否接受。
2. 未 commit（按 README R5 收口）。