# ROUND-07 指令（P2-R2 · recall_items 邻居截断配额 = Option B）

> 下发：WorkBuddy（指挥员）→ Trae（一线）。依据：ROUND-06-QC 已知局限（R3 留的"截断局限"）+ ROUND-06b-QC 已 PASS。范围：只改 `recall_items` 末段截取逻辑。零破坏、fail-open。

## 一、问题（实测背景）
`store.recall_items`（store.py:1006）现状：
- L1036 FTS 已 `LIMIT ?`（=top_k，上限 50）→ `hits` 最多 top_k 条 FTS 命中。
- L1053 `hits.extend(self._recall_anchor_neighbors(hits, excl))` 追加邻居（该函数内部 cap 到 `_ANCHOR_NEIGHBOR_CAP`，默认 5）。
- L1056 `return hits[:top_k]` 截断。**Bug**：当 FTS 命中填满 top_k 时，邻居被 `[:top_k]` 截掉、永远进不了结果 → 顺锚带回的"同一人/同一主题"记忆在相关度高的查询下完全失效（=纯 FTS，零增益）。R3 已实锤（1 seed + 9 邻座 → 返回 6，邻座截到 5）。

## 二、修复（Option B：有邻居才为邻居让位）
**只改 L1051-1056 这一段**，把"先 extend 再 [:top_k]"换成"先算邻居、再为邻居预留 FTS 配额"：

```python
        # P1 汇流（R3）+ P2-R2 截断修复（Option B）
        neighbors = []
        if os.getenv("DESKMATE_ANCHOR_RECALL", "1") != "0" and hits:
            try:
                neighbors = self._recall_anchor_neighbors(hits, excl)
            except Exception as e:  # noqa: BLE001 —— fail-open：顺锚失败退回纯 FTS
                logger.warning("顺锚召回失败（退回纯 FTS）: %s", e)
                neighbors = []
        # Option B（2026-10-07）：邻居至多 _ANCHOR_NEIGHBOR_CAP 条；
        # 有邻居时从 FTS 取数里预留等额配额，让邻居必进结果、FTS 不超额、
        # 无邻居时 FTS 不退化（= 历史纯 FTS 行为）。
        if neighbors:
            keep = max(0, top_k - len(neighbors))
            hits = hits[:keep] + neighbors
        return hits[:top_k]
```

要点：
- `_recall_anchor_neighbors` **不改动**（其内部 `skip` 已排除 seed + excl，且 cap 到 `_ANCHOR_NEIGHBOR_CAP`）。
- 终截 `return hits[:top_k]` 保留（邻居 cap > top_k 的极端情形仍不超额）。
- `DESKMATE_ANCHOR_RECALL=0` 时 `neighbors=[]` → 整段退回纯 FTS（开关语义不变）。

## 三、测试（`tests/test_p1_anchor_recall.py` 追加）
新增 `test_recall_reserves_quota_when_fts_full`（关键区分用例，用 `top_k=3` 揭露旧 bug）：
- 构造：`profile_items` 里 3 条都含 query 词（FTS 命中 3 = 满 top_k）+ 这 3 条同锚 → 邻座 2 条（不同 FTS 词、不在 query 命中集）。
- `recall_items(query, top_k=3)`：
  - 修复后：`neighbors=2` → `keep=max(0,3-2)=1` → `hits[:1] + 2邻居` = 3 条，且**邻居 id 在结果中**。
  - 旧口径：3 FTS → `[:3]` 截掉邻居（邻居不在结果）。
- 断言：邻居 id ∈ 召回结果 id 集合；结果长度 ≤ top_k。
- 另加 `test_recall_no_neighbors_no_degrade`：`top_k=8`、查询只命中 2 条 FTS 且无可顺锚邻座 → 返回 2 条（= 旧行为，无邻居不退化）。

## 四、验收（WorkBuddy 独立验）
- `tests/test_p1_anchor_recall.py` 新用例 2 passed + 存量 5 passed；回归 `test_p2_known_persons`(12)/`extractor`(24)/`sync`(40) 仍绿。
- 独立实测：FTS 满 top_k + 同锚邻座 → 邻居进结果且总数不超 top_k；`DESKMATE_ANCHOR_RECALL=0` → 纯 FTS（旧行为）。

## 五、红线
- 只改 `recall_items` 的 L1051-1056 截取段；**不改** `_recall_anchor_neighbors` / `extractor` / `store` 其他函数 / `manager` 注入链 / `seed` / `sync`。
- fail-open 保持（顺锚异常 `neighbors=[]` 退回纯 FTS，绝不空结果）。
- 不 commit（R1 收口时一并 commit）。

## 六、交付
写 `reports/bridge-trae/ROUND-07-实施记录.md`（真实落点 + 自测 + 独立实测），交 WorkBuddy re-QC。
