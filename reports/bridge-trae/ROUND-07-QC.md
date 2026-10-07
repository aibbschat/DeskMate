# ROUND-07-QC（P2-R2 截断 Option B）· 验收判词

> 依据：`reports/bridge-trae/ROUND-07-指令.md`（Trae 实施 P2-R2）
> 方法：读真实 diff + 跑测试 + 独立实测（trust-but-verify，非信"完成"二字）

## 结论：✅ PASS（P2-R2 截断局限已根治，无回归）

## 一、改动面（符合 R2 范围，零越界）
- **仅 `deskmate/cognitive/store.py` 的 `recall_items`** 改了截断段（L1046–1062）。
- 累积未提交里含 P2-R1 的 `known_persons` 表 + 三方法（R1 遗留，非 R2 新碰）。
- **未碰**：extractor / sync / manager / test_sync / 前端。
- `git diff HEAD --name-only` 确认：store.py、extractor.py、sync_store_sqlite.py、tests/*、前端 emotes 六个文件（豆包，零交集）。
- **未 commit**：HEAD 仍为 `28a2507`（P1 提交），符合"R1 收口时一并 commit"红线。

## 二、真实 diff 核对（store.py:1046–1062）
**旧逻辑（R3 已知局限）**：
```python
hits.extend(self._recall_anchor_neighbors(hits, excl))   # 直接 append
return hits[:top_k]                                       # FTS 满时邻居被截掉
```
**新逻辑（Option B）**：
```python
neighbors: list[dict] = []
if os.getenv("DESKMATE_ANCHOR_RECALL", "1") != "0" and hits:
    try:
        neighbors = self._recall_anchor_neighbors(hits, excl)   # 不再 extend
    except Exception as e:
        logger.warning("顺锚召回失败（退回纯 FTS）: %s", e); neighbors = []
if neighbors:                                             # 有邻居才让位
    keep = max(0, top_k - len(neighbors))
    hits = hits[:keep] + neighbors
return hits[:top_k]
```
语义正确：无邻居 → `if` 跳过 → 纯 FTS 不退化；有邻居 → FTS 让出等额配额，邻居必现、总数不超 top_k。`_recall_anchor_neighbors` 不动（仍受 `_ANCHOR_NEIGHBOR_CAP=5` 限），fail-open 保持，开关 `DESKMATE_ANCHOR_RECALL=0` 仍退回纯 FTS。

## 三、测试（四套件 86 passed，无回归）
```
tests/test_p1_anchor_recall.py        # 含 R2 两新测试
tests/test_p2_known_persons.py        # R1 12 例
tests/test_deskmate_local_extractor.py# R2 抽取回归 24
tests/test_sync_store_sqlite.py       # 同步回归 40
→ 86 passed
```
R2 新增两测试锁定**正确行为**（非锁缺陷）：
- `test_recall_reserves_quota_when_fts_full`（:126）：top_k=3，FTS 满 3 + 同锚邻居 2 → 邻居必进、总数≤3。
- `test_recall_no_neighbors_no_degrade`（:151）：无锚 → 纯 FTS 2 条、无 via_anchor、不退化。

## 四、独立实测（脱离 pytest 夹具，真实 DB 构造最关键场景）
脚本：FTS 命中 8 条（满 top_k）+ 同锚「历史真实性」邻居 5 条 →
```
top_k = 8
结果总数 = 8            (<= top_k ✓)
邻居进结果数 = 5        (应=5；旧逻辑 hits[:8] 会把 5 邻居全截为 0 → 旧 bug 根治)
邻居全部进来了? True
总数不超 top_k? True
via_anchor 标记存在? True
```
无邻居场景：结果数 = 2（纯 FTS），无 via_anchor → 不退化 ✓

## 五、红线复核
- ✅ 未 commit（HEAD 28a2507 未变）
- ✅ 前端 emotes（豆包）零交集，并行安全
- ✅ 爆炸半径仅 store.py recall_items，未波及其他模块
- ✅ fail-open 保持（邻居环节异常 → 退回纯 FTS）

## 六、下一步
P2-R1（person 注册表）+ P2-R2（截断）业务代码现已真闭环，可纳入收口 commit。
推进 **P2-R3（lessons 注入 manager.py）**——指令见 `ROUND-08-指令.md`，待萧哥转 Trae。
