# 三段「检索」任务清单

> 执行前置：Spec 已批准；分支 `fork/base`；本轮不提交、不 push。

## T1 评测集与基线（先做，不改检索代码）
- [x] 落盘 `reports/2026-11-11-检索评测集.jsonl`：20 意图 × 3 说法 = 60 条，字段 `id/intent/category/layer/variant/query/gold/gold_source`。
  - 校验：每条 gold 在 `gold_source` 中存在（脚本内自检，缺失即失败）。
- [x] 新建 `scripts/bench_memory_files.py`：
  - `--impl keyword`：调用 `_search_recent_memory` / `_search_global_memory` / `_search_vault` / `_search_user_memory` / `recall_preferences` / `build_user_fact_context`。
  - 指标：Hit、R@5、MRR、逐条 ms、mean/median/p95、分类/说法分项、漏检清单、常驻覆盖标记。
  - 产物：`reports/2026-11-11-检索基线.md` + `.json`。
- [x] 跑基线并把数字贴进报告（不改任何检索实现）。
- 验收：60 条全部跑完；报告含命中率、漏检、延迟；同集重复跑两次结果一致。

## T2 索引层
- [x] 备份 `~/.deskmate/deskmate.db` 到 `~/.deskmate/backups/2026-11-11-s3/`。
- [x] 新建 `deskmate/memory/file_index.py`：
  - 表 `memory_chunks` / `memory_file_state` / `memory_chunks_fts`。
  - 切分：USER bullet、MEMORY `§` 条目、diary_memory 条目、daily diary `## HH:MM`、vault 段落；排除 `.bak*`、`.sync-conflict*`、`archive/`。
  - `sync()`：mtime/size 快路径 → hash 比对 → 只重嵌变化块 → 删失块。
  - 向量复用 `local_embed.embed()`；写入 model/dim。
- [x] 新建 `scripts/build_memory_index.py`：`--backup/--rebuild/--status`。
- [x] 全量建索引，记录：chunk 数、向量覆盖率、耗时、DB 增量。
- 验收：重复 `sync()` 零重嵌；改一个文件只重嵌该文件块；`--status` 数字正确。
- 停止条件：全量 >5 分钟或 DB 增量 >50MB → 停止并给可选方案。

## T3 检索融合
- [x] 新建 `deskmate/memory/file_search.py`：
  - keyword：FTS5 MATCH + bm25，top 30。
  - vector：numpy 矩阵缓存 + 余弦，top 30。
  - anchor：`memory_anchors` 链接 + `_recall_anchor_neighbors` 一跳，≤6 条，权重 0.5。
  - RRF（k=60，权重 vector 1.2 / keyword 1.0 / anchor 0.8）+ 原文精确子串置顶。
  - 去重 + 单条 400 字 / 总 1200 字预算。
- [x] `bench_memory_files.py --impl fused` 离线复测（先不接线）。
- 验收：与基线同集对比，数字写入临时结果；确认关键词专名类不退化。

## T4 接线与兜底
- [x] `manager.build_full_context_for_turn` 增加 `file_recall`（priority 34，cap 1200）。
- [x] `preference_hits` 优先走 `file_search(user_pref)`，失败回落 `recall_preferences`。
- [x] 索引健康时 `prefetch` 跳过 MEMORY/vault 关键词层，保留项目记忆。
- [x] 兜底 `memory_backstop`：0 命中或 query<6 字时 ≤3 条/≤400 字，priority 60。
- [x] 开关 `DESKMATE_FILE_INDEX=0` 回滚旧路径。
- 验收：注入字符数 ≤ 预算；开关两侧行为可复现；无异常日志。

## T5 实体链接与一跳
- [x] 复用 `memory_anchors` 做 query 实体链接；一跳扩展 ≤6 条。
- [x] 输出「锚通道贡献」：贡献召回条数、独有命中数、误召回样例。
- 验收：给出数据说明一跳是否够；不做两跳。

## T6 验收与测试
- [x] `tests/test_file_index.py`：切分、增量、融合排序、预算、新鲜度、开关。
- [x] `reports/2026-11-11-三段检索验收.md`：命中率/延迟/漏检变化/上下文占用/新鲜度/回滚/索引统计。
- [x] 跑相关 pytest + 全量评测两次。
- 验收：所有数字齐全；回滚步骤实测；未触碰 `entities.db` 与源记忆正文。

## 收尾（不在本轮提交）
- [ ] 待四段与整盘收尾后，一次性提交到本地 `fork/base`。
