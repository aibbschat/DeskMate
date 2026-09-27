# MemPalace 记忆借鉴落地方案（2026-09-26）

> **进度（2026-09-26 更新）**：✅ P0-1 记忆体检已落地（`deskmate/memory/audit.py` + `python -m deskmate.memory.audit`，19 测试全绿；真实库首跑：向量健康 ✓ / FTS 零滞后 / 日记红线守住，查出 MEMORY.md 16 行无日期不衰减条目）。
> ✅ P0-2 recall 基准已落地（`scripts/bench_memory_recall.py` + 种子集 `~/.deskmate/bench/recall_cases.jsonl` 20 条，6 测试全绿）。种子集 19 条已代笔改写为自然问法。
> ✅ 后续：史料 FTS 改述召回 40%→50%@5（合并层四修复：实义覆盖度/cov<2 门槛/覆盖度优先排序/每档独立预算；基准判法升级「锚子串 OR 同文章」；+9 测试）。残余 4 miss 需语义通道根治，**待拍板**。MEMORY.md 16 行无日期条目已补前缀，体检 5/5 全绿。
> ⬜ ~~P1-1 主体错记检测 / P1-2 范围化注入待开工~~
> ✅ P1-1 已落地（`learned_store._detect_subject_conflict` + `_SUBJECT_RULES` 主体消解表；新条标 `questioned` 不内化 + L2 提议卡 `memory_subject_conflict` 由用户裁决，`engine.resolve_proposal` 已接 executed/rejected/never 三态；kill switch `DESKMATE_SUBJECT_GUARD=0`；真实库 577 条回放 0 误报，13 测试）。
> ✅ P1-2 已落地（`profile_items_fts` FTS5 unicode61+字间空格与 history 通道同约定，persist/delete_item 同事务同步 + 老库自愈回填；注入改两级：核心层置信度 top-N 字符预算自适应 + 长尾按 user_message 召回 top-8，worldview 恒全量；fail-open 退全量，kill switch `DESKMATE_PROFILE_SCOPE=0`；真实库 90 条回填 + 召回 9.5ms，11 测试；manager 已传 user_message）。
> 🔍 **质检（2026-09-26，萧哥要求复核）**：逐行复查四类改动，修 **2 真缺陷 + 1 设计不一致 + 1 中隐患**：
> ① 前端 `AgencyPanel.tsx` 缺 `memory_subject_conflict` 中文标签 → 面板漏英文内部键（违反「内部键≠呈现文案」红线），已补 + 前端用例锁定。**前端改动须重打包生效**。
> ② `fts5.py` 每档预算上限 `4 × fetch_limit` 与 5 个档位不符 → synonym 档被 `break` 跳过（同义词兜底静默失效），改 `len(_TIERS) × fetch_limit` 自适应。
> ③ `search_one_db` 逐篇择优仍是「档位优先」（与已改「覆盖度优先」的合并层不一致）→ trigram 泛匹配吃满 `per_db_limit` 名额；**这才是韩愈 case 长期 miss 的真因**（实测：per_db_limit=15 时单库 15 条全为 trigram，正确答案在 bigram 档排第 11 却被截断）。改 cov 优先后正确文章升入库内第 4；新增测试并实证能区分新旧实现。
> ④ FTS 自愈回填判据 `n_fts == 0` 过弱 → 绕过 persist 的直插（restore 脚本）/ persist 中途抛错时不自愈、新条目永久搜不到。改「数量不符即重建」+ restore 脚本补同事务 rebuild + 测试。
> ⑤ 清理死代码 `_SUBJECT_ANY_RE`。⚠ 诚实记录：为 ② 写的测试在旧代码下也通过（FTS 档间命中包含关系使触发场景无法用合成数据构造），按「绿灯≠锁住目标行为」已删除并在日志说明。
> 复跑：**310 passed**；基准 deskmate 三通道 100% / history fts5 50%@5；体检 5/5 全绿；前端 5 tests。
> ~~⚠ 待萧哥决策：MEMORY.md 那 24 行在 `§ 工作 #work`（TTL 30 天）下，补日期前缀后由「永久保留」变为「30 天后归档 vault」；若要长期留存「国子监进度」应移入 `§ 连载项目`。~~
> **已解决（见下方「追加轮」）**：改落 `§ 创作 #stable`（永不衰减），而非 `§ 连载项目`；并修掉「换本新书又复发」的结构性根因。

> ✅ **追加轮（2026-09-26 下午）——根治「书稿记忆会过期」**。萧哥指出：把「国子监秘闻录进度」搬进 `§ 连载项目` 只是修一本书的数据，**换本新书还会复发**。查证属实，且是结构性缺陷：
> ① 决定记不记得住的是**分节标签**（`MEMORY_DECAY_TTL`），而自动路由能产出的标签只有 `preference/correction/work/note`——**`stable` 是自动路径到不了的孤岛**（只有手写标签或显式 `target_section` 才进得去）。09-21 那支硬编码 27 条 `MOVE_MARKERS` 的搬运脚本就是被这个缺口逼出来的。
> ② 更关键：§工作 里那 39 条书稿记忆是**小桔自己写的 `#work`**（「#work 国子监秘闻录进度 09-25…」）——她照工具说明办事。所以只加内容规则没用，**显式 `#work`/`#note` 必须对已识别的创作内容让位**。
> ③ 兜底方向是错的：旧兜底落 `#note` = **7 天后直接删除且不落 vault**，而这条兜底只在 LLM 不可用/超时/返回跑偏时生效 → 「最不确定的环境做最不可逆的动作」。
> 落法：新增 `§ 创作 #stable`（永不衰减）+ 确定性创作规则 `_is_creation_note`（读书闸 + 运营闸 + 创作信号，**不需要书名**）；分类器白名单补 `stable`；兜底改 `长期事实`；`clean_global_memory` 改按 `#stable` 标签保留（否则新建的创作分节会被迁进 `projects/` → 迁出即不可达）；persona + `memory_append` 工具说明补写入端约定。
> 存量：`scripts/migrate_memory_work_to_creation.py`（dry-run 默认 + 备份 + 锁内读写 + 写后自校验）把 **31 条**书稿记忆迁入 `§ 创作 #stable`，**8 条**运营流水账（敲钟/擂台/技能包/写作营/日报）留在 `§工作` 走 30 天 TTL。零删除。
> ⚠ **原方案「按书名一册一节」被实测否掉**：真实语料里 39 条有 **33 条压根没写书名**（「章太聪革职被捕」这类纯散文），按《》出现次数取书名还会取到章节名（屯田策/忘川帖）→ 整批归错书。猜错书比不分组危险（串书禁令），故退成单节；精确到哪本书留给丙。
> ⚠ **丙的必要性拿到硬证据**：全仓库无任何生产调用点给 `DeskMateMemoryManager` 传 `active_project`（`server.py`/`agent.py`/`app.py` 全部只传 `base_dir`）→ `projects/<书>/` **当前不可达**，而 `extract_session_facts` 的书稿分支正往里写（`_KEEP_GLOBAL_SECTIONS` 与 09-21 脚本注释都记着这条）。丙 = 接 `active_project`，之后才能按书精确分节。
> 复跑：**3432 passed / 20 failed**，失败清单与既有基线逐条一致（权限/自理保护类），**零回归**；新增 22 条测试（路由 12 + 迁移脚本 6 + 参数化 6，含 1 条旧断言改锁新价值）。

> 对标仓库：https://github.com/MemPalace/mempalace
> 方法：先读小桔真实代码圈空白，再定方案。**结论：矛盾检测、双时态、fp 去重、衰减、溯源小桔已有，不重复造；真空白收敛为 4 项，分 P0/P1 落地；3 项明确不抄。**

---

## 一、现状盘点（先承认已有的，别重复造轮子）

逐项核对过源码，MemPalace 的核心能力在小桔的覆盖情况：

| MemPalace 能力 | 小桔现状 | 判定 |
|---|---|---|
| 时间知识图谱（validity windows） | `learned_store.py` 已有 `valid_from/valid_until/superseded_by/status` 全套双时态列 + 索引 | ✅ 已有 |
| 矛盾检测（同实体反极性） | `learned_store.append()` 写入侧已做：`_extract_entity`（书名高置信）+ `_extract_polarity`（deny/assert）→ 旧条标 superseded | ✅ 已有（覆盖面窄，见 P1-1） |
| 情感/偏好冲突 | `cognitive/conflict_gap.py`：极性词表 + 三道闸 + tick 调度 + 幂等，很成熟 | ✅ 已有 |
| fp 去重 / 逐字存档 | learned_history fp 唯一索引；observation jsonl / diary / expressions 原始流即逐字层 | ✅ 已有 |
| 记忆衰减 | `MEMORY_DECAY_TTL` 白名单式衰减 + forgot.jsonl 遗忘可见 | ✅ 已有 |
| 溯源 | traces/inject.jsonl + item_sources + item_history 留痕 | ✅ 已有 |
| 范围化检索（Wing/Room） | 史料/私人库：FTS5 四级降级 + 分类库隔离，很成熟；**但认知画像 `profile_items` 是全量注入，无按 query 召回** | ⚠️ 一半（→ P1-2） |
| 记忆审计 + 自愈 | 只有人工 skill（deskmate-memory-audit），**无自动化体检** | ❌ 空白（→ P0-1） |
| 检索基准（LongMemEval 式） | **完全没有**。「感觉变笨了」无法量化 | ❌ 空白（→ P0-2） |
| 实体覆盖面（人名/代词/年龄） | 只抽书名实体；「把她的活儿写成你」这类**主体错记**抓不到 | ⚠️ 窄（→ P1-1） |

## 二、明确不抄的（以及为什么）

| 不抄项 | 理由 |
|---|---|
| AAAK 30× 压缩方言 | 小桔上下文预算 6K + 长度路由（>200 字进 vault）+ LLM 压缩合并已在做；引入自定义方言是高复杂度低收益 |
| 可插拔多后端（Milvus/Qdrant/PgVector） | 单机单用户，SQLite + BGE 本地语义已闭环；多后端契约是给多租户产品做的，过度工程 |
| 宫殿层级重构（Wing/Room/Closet/Drawer） | 小桔的「分类库 + layer×dimension 双轴 + tags」在结构上已覆盖 scoped index 的意图；换成宫殿隐喻 = 推翻重来。**抄 scoping 精神，不抄结构**（即 P1-2） |

## 三、落地项

### P0-1 记忆自动化体检（对应 mempalace audit）

**目标**：把 deskmate-memory-audit skill 的人工巡检变成一条命令可跑、可定时触发的只读体检，输出打分 + 待修复清单。

**落点**：新模块 `deskmate/memory/audit.py`，入口 `run_audit(base_dir=None) -> dict`；CLI `python -m deskmate.memory.audit`；后续可挂 `/v1/deskmate/memory/audit` 诊断端点。

**检查项（五层，对齐 MemPalace 的五层 audit，全部只读）**：

| 层 | 检查内容 | 判据来源（都是已踩过的坑） |
|---|---|---|
| 向量健康 | external_items 中 digested 数 > 0 而 vector 数 = 0（「既搜不到也补不上」指纹）；embedding_model 混用；维度不一致计数 | 09-22 语义检索坑 |
| 画像健康 | profile_items 各层 status 分布、置信度分布、僵尸条目（superseded 但未指向 superseded_by）、gap 全 0（好奇心断粮信号） | conflict_gap.py 顶部说明的静默失败 |
| 全局记忆 | MEMORY.md 体积、日记沉淀块是否回流（应恒为 0）、衰减白名单执行情况、forgot.jsonl 是否在长 | 09-21 日记固化坑 |
| 检索可达 | 私人库/史料库 FTS 可打开、chunks_fts 行数与 chunks 一致 | 「记了查不到」 |
| 溯源完整 | inject.jsonl / forgot.jsonl 是否在写、行数 | P0-4/8.3 |

**设计约束**：纯只读（`mode=ro` 打开各库）；fail-open（单项失败不阻塞整体，报告中记 error）；不写任何库；输出同时给机器（JSON）和人（终端摘要）。**不引入 LLM**。

**验收**：对真实 `~/.deskmate` 跑一次能复现已知问题（如语义检索历史坑），对 /tmp 假库跑全绿不误报；pytest 用例覆盖每层判据（临时库，`--basetemp` 规矩照旧）。

**工作量**：1 天。

### P0-2 检索 recall 基准（对应 benchmarks/）

**目标**：给小桔各检索通道建一套可复现的 R@5/R@10 评测，让「检索变笨/变好」从体感变成数字。

**落点**：`scripts/bench_memory_recall.py` + 评测集 `~/.deskmate/bench/recall_cases.jsonl`（每行：`{"query": "...", "expect_db": "deskmate|private-xxx|history", "expect_ids": [...]}`）。

**评的通道**（同一批 query 跑四路，出对比表）：
1. learned_history 关键词 + 2-gram（`manager._extract_keywords` 路径）
2. 私人库/史料库 FTS5 四级降级（`fts5.search_across_dbs`）
3. external_items 语义通道（BGE 本地）
4. 混合（语义+关键词，alpha=0.7）

**评测集怎么来**：首轮从真实库抽 30–50 条（每库 ≥10 条）：取既有条目，人肉把它的内容改写成你会怎么问的自然 query（这步只有你能做，我起草你改）。此后新坑复盘时顺手加 case。

**设计约束**：全程只读；评测集进 `~/.deskmate/bench/`（不进仓库，避免把私人资料带出）；输出 markdown 对比表，直接可贴进对话。

**验收**：同一评测集连跑两次结果一致（确定性）；跑完不产生任何写入（前后快照核对）。

**工作量**：1 天（写 harness 半天，攒评测集半天）。

### P1-1 实体一致性扩面：主体错记检测（对应 contradiction detection）

**目标**：抓「人称/主体记错」——你实际踩过的坑（把小桔的活儿写成你、日记里人称/主体颠倒），现在的矛盾检测只认书名实体，抓不到这类。

**落点**：`deskmate/memory/learned_store.py` 的 `_extract_entity` 扩展 + 新增 `_extract_subject`；挂载点就在现有 `append()` 矛盾检测旁。

**方案（纯规则、零 LLM，延续「宁可漏判不误判」原则）**：
- 主体白名单起步：只认**高置信主体**——「小桔/我（小桔视角）」「萧盛/你（对萧哥说话视角）」、项目/书籍专名。这条对应你定过的红线：**内部键不猜，呈现必人话；≥2 字才算证据**。
- 检测模式：同主体 + 相反极性（沿用现有 deny/assert）或**主体颠倒对**（「小桔完成了 X」 vs 「你完成了 X」同 X）→ 不自动改写，**标 questioned + 走 L2 提议卡**让你裁决（符合「无据不自动内化」红线，与现有 supersede 的差别：涉及「谁做的」这种归因错误，机器不该自行判谁对）。
- 人称消解规则集中一个表（`_SUBJECT_RULES`），别散落。

**验收**：单测覆盖你真实踩过的 3 个案例形态（人称颠倒、主体记错、同实体反极性既有用例不回归）；对既有真实库全量回放一遍，误报率人工抽查 ≤10% 才开默认开关（可用 env 开关灰度）。

**工作量**：1.5 天（规则 0.5 + 回放验证 1）。

### P1-2 认知画像按 query 范围化注入（对应 scoped search / rooms）

**目标**：`profile_items` 目前由 `memory_injector.py` 全量注入。条目还少时不痛，但它是只增不减的（衰减不覆盖认知层），涨到几百条后会撑爆 6K 预算、稀释关键条目。趁早把「核心全量 + 长尾按需」立起来。

**落点**：`deskmate/cognitive/store.py`（加 FTS 索引与同步）+ `deskmate/cognitive/memory_injector.py`（注入策略）。

**方案**：
- `deskmate.db` 加虚拟表 `profile_items_fts`（FTS5，unicode61 + 字间空格，与 `coworker/history/fts5.py` 同一套预处理约定），在 profile_items 的 upsert/状态变更**同一事务**里同步（防「记了查不到」）。
- 注入策略改两级：
  1. **核心层**：cognitive/emotional 层置信度 top-N（N 按字符预算自适应，沿用 MEMORY_CONTEXT_MAX_CHARS=6000 框架）+ worldview 层全量（她的判断条目少）——保证「她是谁」永远在场；
  2. **长尾召回**：其余条目按当轮 `user_message` 走 FTS 召回 top-K（K=8），命中才注入。prefetch 里已有 user_message，接线点现成（`manager.prefetch` → injector 传参）。
- 兜底：FTS 不可用/空 query → 退回现全量行为（fail-open，行为与今天完全一致）。

**验收**：注入条数随画像增长有硬上限（预算自适应断言）；随机 50 条真实 query 下，top-8 召回的人工相关性抽查 ≥70%；既有 memory_injector 测试全绿（默认路径不变）。

**工作量**：2 天。

## 四、实施顺序与依赖

```
P0-1 体检 ──┐（体检先行：先看清现状再动刀）
P0-2 基准 ──┤
            ├──> P1-1 实体扩面（用基准的评测集验证误报率）
            └──> P1-2 范围化注入（用体检确认 profile_items 规模后动）
```

- P0-1 / P0-2 相互独立，可并行，都不碰写路径 → **风险最低、先落**。
- P1-1 依赖 P0-2 的评测集来量化误报率。
- P1-2 是唯一动注入行为的，放最后，且有 fail-open 兜底。

**通用红线**（所有项共用）：`DESKMATE_DIR` 调用时解析、禁模块级常量；原子写走 `_atomic.py`；fail-open；pytest 用 `--basetemp=/tmp/新目录`；不加新第三方依赖。

## 五、总账

| 项 | 改动面 | 新依赖 | 工作量 | 风险 |
|---|---|---|---|---|
| P0-1 记忆体检 | 新增 1 模块 + 1 CLI | 无 | 1 天 | 低（只读） |
| P0-2 recall 基准 | 新增 1 脚本 + 评测集 | 无 | 1 天 | 低（只读） |
| P1-1 主体错记检测 | learned_store 扩展 | 无 | 1.5 天 | 中（有 env 开关灰度） |
| P1-2 范围化注入 | store + injector | 无 | 2 天 | 中（fail-open 兜底） |

合计约 5.5 天，零新依赖，不动存储 schema 大结构，不碰任何红线（guarded_write / 史料合规 / mood 派生值均无关）。

---

*现状核对于 2026-09-26，基于以下源码：`deskmate/memory/learned_store.py`（双时态+矛盾检测已存在）、`deskmate/cognitive/conflict_gap.py`（情感冲突已存在）、`deskmate/cognitive/memory_injector.py`（全量注入现状）、`deskmate/memory/manager.py`（prefetch/sync/衰减/路由）、`coworker/history/fts5.py`（四级降级检索）、`deskmate/memory/semantic_search.py`（BGE 语义通道）。*
