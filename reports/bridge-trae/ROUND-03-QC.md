# ROUND-03 QC 判词（R3 · 召回顺锚走一跳）

> 判定：**PASS-WITH-NOTES（无阻塞项，1 处已知局限 P1 接受、留 P2）**。
> 验证方式：读 git diff HEAD 真实落点 + 跑测试套件 + 独立功能验证（不沿 Trae 自述）。
> 时间：2026-10-07 18:09。指挥员：WorkBuddy。

## 一、真实改动落点（git diff HEAD 实锤）

R3 真正新增：
- `deskmate/cognitive/store.py`
  - 模块级 L70-81：`_ANCHOR_SEP` / `anchor_row_key` / `_ANCHOR_NEIGHBOR_CAP=5` / `_ANCHOR_KIND_LABELS` / `_anchor_kind_label`（注：R1 已引入 `anchor_row_key`/`upsert_anchors`/建表，R3 补 recall 侧常量）。
  - `recall_items()` L948-962：FTS 命中 `hits` 后 `if DESKMATE_ANCHOR_RECALL!='0' and hits:` 追加 `_recall_anchor_neighbors`，**整段 try/except → warning 退回纯 FTS**；末尾 `return hits[:top_k]`。
  - 新增 `_recall_anchor_neighbors()` L964-1027：seed 锚集合 → 反查同锚邻座（走 `idx_anchor_lookup`）→ 拉 active 正文 → 附 `via_anchor=True` + `anchor_evidence`（如「同主题：历史真实性」）。
- `tests/test_p1_anchor_recall.py`：新增 5 例（R3 专项）。
- `tests/test_profile_scope_injection.py`：夹具补 sync migrate（R2-QC 硬前置，10 failed→12 passed）。

R3 未碰：`extract_from_text`/`extract_cognitive_items` 签名（R2 锁）、`memory/manager.py` 注入链、显式边表（P2+）。

## 二、独立功能验证（不沿用 Trae 自测，/tmp/r3_verify.py 实跑）

| 验证点 | 结果 |
|---|---|
| 1 真·走一跳：seed=sA，sB 仅同锚被带回，`via_anchor(sB)=True` | 通过（断言 `ids[0]=='sA' and 'sB' in ids`） |
| 2 **真·表损坏 fail-open**：DROP `memory_anchors` 后 recall 仍返回 `[sA]` | 通过，日志实打 `顺锚召回失败（退回纯 FTS）: no such table: memory_anchors` —— 是真异常路径，非 monkeypatch 假象 |
| 3 截断局限实证：1 seed + 9 同锚邻座、默认 top_k=8 → 返回 6（1+5），邻座截到上限 5 | 通过（局限真实存在，见 §五） |
| 4 book 锚邻座（多锚类型） | 通过 `['a','b']`，b 带 `via_anchor` |

**副产品发现（反向实锤 R2-QC 移交项）**：我的验证脚本最初未跑 sync migrate 就 `persist`，直接撞 `profile_items has no column named sync_uuid`。证明"persist 路径必须先 migrate"是硬前置——Trae 修 `test_profile_scope_injection` 夹具正是为此，修复正确且必要。

## 三、测试（实跑）

- `tests/test_p1_anchor_recall.py`：**5 passed**（含真·走一跳 + 开关即时回退 + 封顶 + 无锚纯 FTS + monkeypatch 异常 fail-open）
- `tests/test_profile_scope_injection.py`：**12 passed**（夹具修复后 recall 路径可达；L78-80 无锚脆弱断言 `[0]["id"]=="s1"` 仍绿，未弱化）
- `tests/test_deskmate_local_extractor.py`：**24 passed**（R2 仍绿）
- `tests/test_sync_store_sqlite.py`：**40 passed**（R1 仍绿）

## 四、对 R3 指令 §五 验收的闭合

- ✅ seed 命中 A、A/B 同 topic → recall 含 B（带 `via_anchor` + evidence）
- ✅ `memory_anchors` 缺失/异常 → 仍返回 seed（真·表损坏实证，非结构佐证）
- ✅ `DESKMATE_ANCHOR_RECALL=0` → 与纯 FTS 一致
- ✅ 邻座 ≤ 5 封顶
- ✅ 不改返回签名，消费方 `memory_injector.py:158` 按 `r["id"]` 取值，增键兼容
- ✅ 爆炸半径内零新增失败（`recall_items` 调用方仅上面两套测试，全绿；`test_worldview_consumption` 等 36 存量失败属 worldview/权限引擎模块，不调 recall、R3 未碰）

## 五、已知局限（PASS-WITH-NOTES 之 NOTE，非阻塞）

R3 采用「`hits + neighbors → [:top_k]` 追加后截断」（忠实工单 §五 伪码）。**局限**：当 FTS 自身已填满 `top_k` 时，邻座被截掉（结果=纯 FTS，零破坏但不增益）。独立验证 §二.3 已实锤此局限。
- **P1 最小版接受**（量小、锚是增益不是入口）。
- 若 P2 要「邻座必现」，可改为「为邻座预留配额」——但那会挤压 FTS 结果、非纯增益，故本轮不做，留 P2 由指挥员定夺。

## 六、结论与移交 R4

**R3 PASS，无阻塞。** 抽锚（R2 写）→ 顺锚召回（R3 读）闭环成立，"同锚即邻座"在 P1 最小版真正生效。R4 指令见 `ROUND-04-指令.md`。

R4 必须做：① 端到端集成（经 `extractor.extract_from_text` 真抽一条含 topic 的 stance → 自动落锚 → 另一条同 topic → recall 命中第一条时带回第二条，验证 R2+R3 在真实抽取链路闭环）；② 部署前置检查：`deploy_cloud_sync.sh` 的 CHANNEL_MODULES 须含 `deskmate/cognitive/store.py`（R1 踩过 manager.py 漏传，勿重蹈）；③ 验收判据清单确认。
