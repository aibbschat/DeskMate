# ROUND-07 实施记录（P2-R2 · recall_items 邻居截断配额 = Option B）

> 依据：`ROUND-07-指令.md`；来源：`ROUND-06-QC.md` 已知局限（R3 留的「截断局限」）。
> 范围：只改 `recall_items` 末段截取逻辑 + `tests/test_p1_anchor_recall.py` 追加用例。
> 状态：**待 WorkBuddy re-QC**。未 commit（R1 收口时一并 commit）。

## 0. 一句话
修好了「FTS 命中填满 top_k 时，顺锚带回来的邻居被 `[:top_k]` 一刀截掉」的 bug：现在有邻居就从 FTS 取数里让出等额配额，邻居必进结果；无邻居时行为与历史纯 FTS 完全一致。

## 1. 真实落点（只碰这两处）

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | `recall_items` L1049-1064 | 把「先 `hits.extend(邻居)` 再 `hits[:top_k]`」改为「先算邻居 → 有邻居则 `keep = max(0, top_k - len(neighbors))`、`hits = hits[:keep] + neighbors` → 终截 `hits[:top_k]`」 |
| `tests/test_p1_anchor_recall.py` | L123-161 | 新增 `test_recall_reserves_quota_when_fts_full`（top_k=3 揭露旧 bug）+ `test_recall_no_neighbors_no_degrade`（无邻居不退化） |

未碰：`_recall_anchor_neighbors` / `extractor` / `store` 其他函数 / `manager` 注入链 / `seed` / `sync`——符合 07 §五红线。

## 2. 关键决策

1. **为什么不直接调大 LIMIT**：FTS 的 `LIMIT` 已被 clamp 到 `min(top_k,50)`，且「多取 FTS 再截」会让邻居仍可能被后面更靠前的 FTS 挤掉，且相关度排序被污染。Option B 只在「确实有邻居」时让位，最小侵入。
2. **终截 `return hits[:top_k]` 保留**：邻居 cap（`_ANCHOR_NEIGHBOR_CAP=5`）理论上可能 > top_k 的极端情形（top_k 传小值），保留终截保证永不超额。
3. **开关语义不变**：`DESKMATE_ANCHOR_RECALL=0` → `neighbors=[]` → 不进入配额分支 → 整段退回纯 FTS。
4. **`_recall_anchor_neighbors` 不动**：其内部已 `skip = seed|excl` 去重 + cap 到 5，调用方无需再处理。

## 3. 自带测试（pytest 真实输出）

- 定向+回归：`.venv/bin/python -m pytest tests/test_p1_anchor_recall.py tests/test_p1_anchor_e2e.py tests/test_p2_known_persons.py tests/test_deskmate_local_extractor.py tests/test_sync_store_sqlite.py -q`
  → **`88 passed in 4.07s`**。
- 达 07 §四：`test_p1_anchor_recall` 新用例 2 passed + 存量全绿；回归 p2_known_persons(12)/extractor(24)/sync(40) 仍绿。

## 4. 独立实测（07 §四口径，`.venv/bin/python` 直跑，临时库）

构造：`top_k=3`；3 条含 query 词「史料」（FTS 满额）+ 2 条不含 query 词但同锚的邻居，5 条共享 `topic=历史真实性`。

```
ON （DESKMATE_ANCHOR_RECALL 默认） 结果: ['s_A', 'n_1', 'n_2']  len=3  邻居进结果=['n_1','n_2']
OFF（DESKMATE_ANCHOR_RECALL=0）   结果: ['s_A', 's_B', 's_C']  len=3  邻居进结果=[]  via_anchor=False
```

- **ON**：邻居 `n_1/n_2` 进了结果，总数 = 3 = top_k（`keep = 3-2 = 1` → `[s_A] + [n_1,n_2]`）。旧口径此处会得 `[s_A,s_B,s_C]`（邻居被截掉）。
- **OFF**：纯 FTS，无邻居、无 `via_anchor`，与历史行为一致。

## 5. 红线核对

- ✅ 只改 `recall_items` 末段截取 + 测试；未动 `_recall_anchor_neighbors` / extractor / manager / seed / sync。
- ✅ fail-open 保持：顺锚异常 `neighbors=[]` → 退回纯 FTS，绝不空结果（存量 `test_anchor_recall_fail_open` 仍绿）。
- ✅ 未 commit。

## 6. 遗留 / 备注

- QC（ROUND-06）列的已知局限「FTS 满 top_k 邻居被截」已闭环。
- 下一步：P2-R3（`manager` 注入链，把 via_anchor 邻座信息带进对话上下文）按后续 ROUND 指令推进。