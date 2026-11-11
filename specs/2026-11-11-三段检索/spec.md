# 三段「检索」规格（记忆文件层向量检索 + 兜底 + 实体一跳）

- 日期：2026-11-11
- 分支：本地 `fork/base`（不 push、不提交，留到整盘收尾一次性提交）
- 红线：先备份再动数据；不改主稿正文；不碰 `entities.db`；不改 MEMORY.md / USER.md 正文内容
- 前置事实（已只读核实）：
  - 本地 embedding 可用：`fastembed` + `BAAI/bge-small-zh-v1.5`，512 维，离线免费；唯一调用点 `deskmate/memory/local_embed.py::embed()`，外部信息层已用同一通道。
  - 语料规模：vault 534 md/406KB；diary 225 文件/1.19MB（需排除 `.bak*`、`.sync-conflict*`）；`diary_memory.md` 26KB；`MEMORY.md` 13KB；`USER.md` 22KB。估算 900–1,200 chunk，向量约 2MB。
  - 现有关键词实现分散在 `manager._search_recent_memory` / `_search_global_memory` / `_search_vault` / `_search_user_memory` / `user_preferences.recall_preferences` / `user_fact_context`（后者只覆盖最近 7 天、≤80 字的日记行）。
  - `memory_anchors` 现有 192 行（topic 171 / person 21）；`_recall_anchor_neighbors` 已有一跳邻座召回。

## 1. 目标与非目标

### 目标
1. 建立**可复跑**的真实检索评测集：20 个意图 × 3 种说法 = 60 条 query，覆盖偏好、旧事、创作约定、人物设定、文件位置、vault 知识。
2. 在当前关键词实现上跑基线，落盘命中率、漏检样例、单条延迟。
3. 为记忆文件层（USER 硬规矩/偏好、MEMORY、diary 视图、daily diary、vault）建**持久索引 + 增量更新**，复用现有 embedding 通道。
4. 关键词 / 向量 / 锚三通道融合，保留关键词对专有名词与成句引用的优势。
5. 提供有上限的兜底召回（无主动查询时）。
6. 用 `memory_anchors` 做实体链接 + 一跳扩展；不上 OpenIE 构图 / PPR / 两跳。
7. 同一套题复测，给出命中率、延迟、上下文占用、索引新鲜度、回滚方式的数字。

### 非目标
- 不覆盖外部信息层 `external_items`（661/747 已有向量，管线不动）。
- 不做全量 OpenIE 知识图谱、不做 PPR、不做两跳。若实测一跳不足，先出数据再议。
- 不重写 `vault_store` / `semantic_search` 现有 API；新索引只读源文件。
- 不改 `MEMORY.md`/`USER.md` 正文，不写 `entities.db`。

## 2. 评测集（先做，不改代码）

### 文件
`reports/2026-11-11-检索评测集.jsonl`，每行一条 query：
```json
{"id":"pref-01a","intent":"pref-01","category":"偏好","layer":"user_pref",
 "variant":"同义|口语|指代","query":"...","gold":"唯一子串",
 "gold_source":"USER.md#偏好与事实"}
```
- 20 个意图，每意图 3 说法（同义改写 / 口语化 / 指代式），共 60 条。
- 分类与 gold（已逐条核实存在）：
  - 偏好 4：中国妖怪录、御姐音、此岸／对岸、延伸话题（`USER.md`）
  - 旧事 4：影视公司要版权、两本书合同废了、尹逍删聊天记录（`diary_memory.md`）；太原火灾 14 人遇难（`diary/2026-11-10.md`）
  - 创作约定 4：一个也不能死（`MEMORY.md`）、合理不等于有力、只标不改、取不到字节不算（`USER.md`）
  - 人物设定 4：迟正河、韩效武、腿肚子、倭军要李昖活（`MEMORY.md`）
  - 文件位置 2：夜不收：绝命救援.docx、10-31 的旧快照（`USER.md`）
  - vault 知识 2：官制设定须有正史依据（`vault/preferences/2026-09-18-萧盛立下铁律.md`）、韦涵亮（`vault/work/2026-08-10-试卷调包案设定.md`）

### 基线口径
新脚本 `scripts/bench_memory_files.py`，`--impl keyword`：
- 调用**生产现路**，不另写理想实现：
  - `manager._search_recent_memory(q)`
  - `manager._search_global_memory(q)`
  - `manager._search_vault(q)`
  - `manager._search_user_memory(q)`
  - `user_preferences.recall_preferences(base,q)`
  - `user_fact_context.build_user_fact_context(base,q)`
- 检索单元：按各通道返回顺序切条目（bullet/段落/`**[path]**` 文件块），rank 从 1 起。
- 指标：`Hit`（任一通道命中 gold）、`R@5`（任一通道 rank≤5）、`MRR`、逐条耗时、mean/median/p95、按分类/说法分项、漏检清单。
- 额外记录：gold 是否已在**常驻注入**（`strip_auto_review(USER.md)` 去掉「偏好与事实」节 + 硬规矩），用于区分「本来就常驻」与「检索捞回」。
- 产物：`reports/2026-11-11-检索基线.md` + `.json`。

## 3. 索引层

### 新文件 `deskmate/memory/file_index.py`
表建在 `~/.deskmate/deskmate.db`（与认知层同库，不碰 `entities.db`）：

```sql
CREATE TABLE memory_chunks (
  id TEXT PRIMARY KEY,            -- sha1(source_kind|source_path|chunk_no)
  source_kind TEXT NOT NULL,      -- user_hard|user_pref|memory|diary_view|diary_day|vault
  source_path TEXT NOT NULL,
  section TEXT DEFAULT '',        -- 节标题/日期，供展示与锚链接
  chunk_no INTEGER NOT NULL,
  content TEXT NOT NULL,
  tokens TEXT NOT NULL,           -- CJK 2-gram + 整词，空格分隔，供 FTS5
  content_hash TEXT NOT NULL,
  embedding BLOB,
  embedding_model TEXT DEFAULT '',
  embedding_dim INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE(source_path, chunk_no)
);
CREATE INDEX idx_chunks_kind ON memory_chunks(source_kind);
CREATE INDEX idx_chunks_embeddable ON memory_chunks(source_kind) WHERE embedding IS NOT NULL;

CREATE TABLE memory_file_state (
  source_path TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  mtime_ns INTEGER NOT NULL,
  size INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  indexed_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_chunks_fts USING fts5(
  tokens, content='memory_chunks', content_rowid='rowid', tokenize='unicode61'
);
```
- 索引增删与 `memory_chunks` 同步（触发器或手动维护；采用手动事务，减少 schema 复杂度）。

### 切分规则
- `USER.md`：`##` 节内每条 bullet = 1 chunk；>400 字再按句切。
- `MEMORY.md`：`§` 节内每条日期条目 = 1 chunk。
- `diary_memory.md`：每条 `YYYY-MM-DD：🩷 ...` = 1 chunk。
- `diary/YYYY-MM-DD.md`：按 `## HH:MM` 分块，剥离 `- 默契 ...` 元数据行；>600 字按段切。
- `vault/**/*.md`：排除 `archive/`、`.bak*`、`.sync-conflict*`；按段落聚合 ≤600 字。
- 空块、纯前端注释块不入索引。

### 增量与新鲜度
- `sync(force=False, budget=None)`：
  1. 枚举源文件，读 `(mtime_ns,size)`；与 `memory_file_state` 相同且非 dirty → 跳过（零读盘）。
  2. 变化的文件：读内容 → 切块 → 与旧 `content_hash` 比对，只重嵌变化块；删除已消失块。
  3. 新块用 `local_embed.embed(texts)` 批量向量化；写入 `embedding/embedding_model/embedding_dim`。
  4. 更新 `memory_file_state`。
- 新鲜度目标：**下一轮对话即可检索**。检索入口先 `sync()`；单文件变更（一条新记忆）耗时目标 < 1s。首次全量建索引走 CLI，不阻塞对话。
- 新写入无需改各写入方：以文件 mtime/size 为失效信号，天然覆盖 MEMORY/USER/diary/vault 的所有写入路径。另提供 `mark_dirty(path)` 供已知写入点主动置脏（可选，不强制）。

### CLI `scripts/build_memory_index.py`
- `--backup`：复制 `deskmate.db` 到 `backups/2026-11-11-s3/<name>.bak-<ts>`（先备份再建表）。
- `--rebuild` / `--status`：全量重建、查看块数与向量覆盖率、DB 增量大小。
- 若全量向量化 > 5 分钟或 DB 增量 > 50MB，**停止并报告**（不硬上）。

## 4. 检索层

### 新文件 `deskmate/memory/file_search.py`
`search_memory_files(query, k=8, budget_chars=1200) -> list[Hit]`：
1. `file_index.sync()`（增量，最多处理 N 个变更文件，其余留待后台）。
2. 三通道候选：
   - **keyword**：query → `_extract_keywords` → FTS5 `MATCH`（token OR），`bm25` 排序，top 30。
   - **vector**：`local_embed.embed([query])` → 进程内 numpy 矩阵缓存（DB 变则重载），余弦 top 30。
   - **anchor**：`memory_anchors.anchor_value` 在 query 中精确/归一命中 → 含该锚的 chunk；再调用/复用 `_recall_anchor_neighbors` 做**一跳**邻座扩展，最多 6 条，权重 0.5。
3. 融合：**RRF**（`k=60`）+ 通道权重 `vector 1.2 / keyword 1.0 / anchor 0.8`；若 query 原文（≥6 字）是 chunk 子串，直接加精确命中标记并置顶。
   - 理由：向量与关键词分数量纲不同，RRF 只看排名更稳；关键词保留对专名/引文/编号的精确优势；锚通道补「问法里没出现原词但实体相同」的缺口。
4. 去重（同 source_path+section 合并）→ 按预算截断（单条 ≤400 字，总 ≤1200 字）。
5. fail-open：索引不可用/模型失败 → 返回空列表，调用方回落旧关键词路径。

### 接线 `deskmate/memory/manager.py`
- `build_full_context_for_turn` 新增块 `file_recall`：`priority=34, cap=1200, keep_light=False`。
- `preference_hits` 改为优先走 `file_search`（`source_kind=user_pref`，cap 700）；失败回落 `recall_preferences`。
- `prefetch` 在 `DESKMATE_FILE_INDEX=1` 且索引健康时，只保留项目记忆检索，跳过 MEMORY/vault 关键词层（避免与 `file_recall` 重复）；索引不健康则保持原样。
- 开关：`DESKMATE_FILE_INDEX=0` 一键回滚到旧路径（默认 1）。

### 兜底召回（预算）
- 触发：`file_search` 命中为 0，或 query 长度 < 6（闲聊/短指令）。
- 内容：按「用户硬规矩/偏好 + MEMORY 最近条目」取 ≤3 条、≤400 字；不走向量，按重要度/时间。
- 依据：现有总预算 6000；`preference_hits` 700 + `journal_digest` 1400 已占 2100；兜底 400 ≈ 6.7%，加 `file_recall` 1200 后新增总占用 ≤1600，仍留 ≥2000 给画像/情绪/事实块，且去掉 `prefetch` 重复块后净增更小。
- 兜底块名 `memory_backstop`，`priority=60`（预算紧张先丢）。

### 实体链接与一跳
- 复用 `memory_anchors`（192 行）与现有 `_recall_anchor_neighbors`；不新建图结构。
- 链接口径：锚值在 query 中出现（归一后去空格/标点）；`anchor_kind` 不限。
- 一跳：命中锚的 chunk 之外，取共享同一锚值/邻座的 chunk ≤6 条，融合权重 0.5。
- 两跳：本轮**不做**；验收报告给出「一跳召回贡献」数据，若仍需两跳，先报数据。

## 5. 验收（必须有数字）

同一评测集，`--impl keyword` vs `--impl fused` 对比，产物 `reports/2026-11-11-三段检索验收.md`：
1. **Hit / R@5 / MRR**：总体 + 按 6 类 + 按 3 种说法。
2. **延迟**：mean / median / p95；对照基线；冷/热索引分开报。
3. **漏检样例**：逐条列出基线漏检、融合后是否捞回；未捞回的原因归类（无该源/切块失败/语义仍远）。
4. **上下文占用**：`build_full_context_for_turn` 实际注入字符数、`file_recall`/`memory_backstop` 字符数，是否 ≤ 预算。
5. **索引新鲜度**：写入一条测试记忆 → 下一次检索可见的耗时（目标 <1s；给出实测 ms 与轮次）。
6. **回滚**：`DESKMATE_FILE_INDEX=0` 生效验证；索引表可 `DROP` 不影响源文件；备份路径。
7. **索引统计**：chunk 数、向量覆盖率、DB 增量大小、全量建索引耗时。

## 6. 实施顺序（批准后）

1. 写评测集 + `bench_memory_files.py` → 跑基线 → 落盘基线报告。**先不改检索代码。**
2. 备份 `deskmate.db`；建索引表 + 切分 + 增量 + CLI；全量建索引并出统计。
3. 实现 `file_search` 三通道融合；用同一评测集离线复测（不接线先看数）。
4. 接线 `manager`（file_recall / preference_hits / prefetch 去重 / 兜底预算 / 开关）。
5. 实体链接 + 一跳；出贡献数据。
6. 跑验收报告 + 单测；不提交。

## 7. 风险与决策点
- **embedding 成本**：本地模型免费，但首次全量需 CPU 时间；若 >5 分钟或 DB 增量 >50MB → 停止并给「只索引高价值层 / 分批」选项。
- **切块质量**：diary 长文与 vault 段落切分可能影响召回；以评测集数据决定是否调块大小（不在本轮调参超过 1 轮）。
- **重复注入**：`prefetch` 与 `file_recall` 并存会挤预算；以「索引健康则跳过旧关键词层」处理，开关可回滚。
- **数据备份**：建表前备份 `deskmate.db`；不修改任何源记忆文件。
