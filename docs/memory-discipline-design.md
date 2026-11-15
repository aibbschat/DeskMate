# 小桔长期记忆纪律：按书分区、来源分级、正文为事实源

> 2026-11-10 萧哥现场：「记忆乱窜，不是她记性差，是系统把不同时间、不同效力、不同来源的判断全压成同一种确定事实」。
> 本文是审计结论 + 已落地修复 + 长期可执行方案。所有数据结论来自 2026-11-10 12:40 对
> ~/.deskmate/deskmate.db 的只读快照（.backup 到 /tmp 后查询，未写真实库）。
> 代码行号以本轮改动后的工作区为准，改动前以 commit e35d84e1 为基线。

## 0. 一句话结论

今早的 e35d84e1 把「来源/效力」写进了 source 列，把「记忆只是线索、正文才是基准」写进了注入文本，
**方向对、但只修到「写入口 + 注入措辞」这一层**：召回侧不分书、不分存量来源、不报冲突，
旁路写入仍不标 source，MEMORY.md/USER.md 这条更早的注入链完全没纳入纪律，正文索引与记忆之间也没有锚点可回查。
P0（召回侧书隔离 / 来源兜底 / 冲突显式 / 陈旧降级）于 12:51 提交 `9c56f6d7`；
**P1–P3 已于 2026-11-10 14:09 全部落地**（写入侧来源纪律、按书注入链、生命周期与
30 条评测集、正文证据锚点），详见 §11。本文件 §1–§8 保留的是**当时的审计证据**，
不代表当前未做状态。

## 1. 审计结论

### 1.1 已根治（e35d84e1 + 本轮 P0）

| 项 | 证据 | 状态 |
|---|---|---|
| 同一批写入里「设定/推理/大纲/进度/外部」不再冒充用户口述事实 | learned_store.NON_FACT_SOURCES + manager 抽取 prompt 要求 source（manager.py:2485） | 主链路已收口；旁路仍无 source，见 1.2②（P1） |
| 注入块权威口吻「不是她猜的；优先据此作答」已删除，改为「只是线索 + 冲突须明说」 | user_fact_context._FACT_DISCIPLINE | 已根治 |
| 无 query 时取最老 10 条的问题 | 原 SQL 无 ORDER BY，SQLite rowid 升序；现 ORDER BY ts DESC, id DESC（_rows） | 已根治 |
| 注入无字数上限 | _FACT_CONTEXT_MAX_CHARS=4000 | 已根治 |
| 正文条写入时同锚点非正文条让位；非正文不能推翻正文 | learned_store.append 规则(2)(4)，tests/test_memory_canon_discipline.py:84-117 | 已根治（同 entity 场景） |
| 云端同步漏传 learned_store.py | scripts/deploy_cloud_sync.sh（e35d84e1 补入） | 已根治 |
| 召回不分书：书 A 专属记忆注进书 B 会话 | 见 1.2 第 1 条实测；本轮加书维度过滤，tests/test_memory_recall_scope.py | 已根治（召回侧 P0） |
| 本地笔记（local_intake）被当成「萧哥口述」进主块 | 实测 34/633 条；本轮把 local_intake 纳入非事实来源（learned_store.NON_FACT_SOURCES） | 已根治 |
| 存量 live/legacy 里混着外部新闻 | 本轮读侧归一 effective_source + 显式清洗入口 LearnedStore.reclassify_legacy（默认 dry-run） | 已根治（读侧），数据清洗待人工执行 |

### 1.2 真实隐患（每条有证据）

**① 书维度只写不滤（本轮已修召回侧，写入侧仍依赖提示）**
- 数据：learned_history active 1389 条，带书后缀的只有 20 条（全是夜不收，且是人工/旧轮次打的）；
  1335 条无书维度；108 条正文提到《书名》但没有书维度；34 条 local_intake 的书名藏在路径里。
- 代码：book_id 参数只有 learned_store.append 有（learned_store.py:append），全仓没有调用方传它；
  e35d84e1 的 app.py 只按 source 分组落库（coworker/server/app.py:679-703）。
- 实测（修复前）：query「我在写《国子监秘闻录》第六卷」→ 参考层注入 3 条夜不收（大和盟逻辑链、
  龙山仓方向、龙山仓消防）；query「接着写《衙探》的访谈稿」→ 同样注入夜不收 3 条；
  无 query 时主块 10 条里 6 条是夜不收/龙山仓。
- 结论：**不是「可能串」，是实测在串**。本轮已在 user_fact_context 做召回侧书隔离；写入侧书维度
  改为 from fact/session 书名自动补后缀（manager.with_book），仍需 P1 全链路接线。

**② 旁路写入仍不标 source（未动，需跨域同意）**
- deskmate/digest/engine.py:215 append(facts, valid_until=expiry)（默认 live）
- deskmate/auto_tasks/recorder.py:206 LearnedStore().append(facts)（连 base_dir 都不传）
- deskmate/ingest/local_intake.py:290 append(facts, source=src)（src 是文件路径，语义正确，但读侧此前不认；
  本轮把 local_intake 归入参考层后已不再冒充口述）
- ~~deskmate/agency/engine.py:449~~（2026-11-10 复核：此处是 `resolve_subject_conflict` 冲突裁决，不写 learned_history，非旁路；真旁路只有 digest / recorder / app else 三处）
- coworker/server/app.py:701 else 分支 store.append(facts)（sources 缺失时回落 live）
- 影响：这些路径写进来的条目，效力只能靠文本猜；digest 写的是世界观察，却记成 live。

**③ MEMORY.md / USER.md 注入链完全没有纪律（未动）**
- 证据：USER.md 里仍有「## 当前创作（《国子监秘闻录》）」整节，而实际在写《夜不收》——
  每轮 build_full_context_for_turn 都整段 load_user 注入（manager.py:596-612）；
  MEMORY.md「§ 纠正」整节（含夜不收专有裁定）经 _get_core_memory 每轮无条件注入
  （CORE_TAGS=preference/correction，manager.py:130、492-502）。
- 证据：after_action._maybe_review_memory 把 LLM 复盘写成「## 自动复盘 · 日期」块
  （after_action.py:779-845），只做模糊去重，不带 source/效力/书/有效期；同一条规则在
  MEMORY.md 和工作区 projects/ 里各有一份，改一处另一处不变。
- 证据：项目记忆（按书）的注入依赖 active_project，而应用层从未调用 set_active_project
  （manager.py:2092 注释与 2122 注释都记着这个坑）→ 按书的 projects/<书名>/longterm.md
  在正常对话里不可达；书稿内容全挤在全局 MEMORY.md 里被所有会话共享。

**④ 时间效力名存实亡（未动，P1-P2）**
- 数据：valid_until 只有 7/1389 条有值；valid_from 全部 = ts；status 只有 active/superseded/questioned/forgotten。
- 代码：append 只在 canonical 取代时写 valid_until；没有有效期/衰减策略；没有 correction 链
  （superseded_by 只指向「谁取代我」，没有「我纠正了谁」的双向可查语义）。
- 后果：三月前的进度条、旧版设定会和今天的裁定同时 active；只要不撞同 entity 就永远不退休。

**⑤ 正文索引没有和记忆接上（未动，P2-P3）**
- novel_memory 已有按书的 FTS5 正文索引与写作圣经（deskmate/novel_memory/core.py:1396 build_fts5_index、
  1679 novel_search_fts5、1946 novel_recall），但 learned_history 里没有任何行引用
  「哪本书的哪一段」；source=manuscript 至今 0 条。
- 结果：「已写正文是唯一事实基准」目前只是 prompt 里的一句话 + 来源前缀的命名约定，
  没有数据结构能回答「这条记忆对应正文哪一段」，也就无法自动校验/回查。

**⑥ 冲突提示/人工确认队列缺失（部分）**
- 写入侧只有「同 entity + 反极性」「主体颠倒」两种机器判定，且主体颠倒走 L2 提议卡
  （learned_store._detect_subject_conflict / _queue_subject_proposal）。
- 没有「同锚点多条并存时的明确提示」；本轮在召回侧补了冲突提示（同 entity + 反极性 +
  去极性后正文等同），更广的锚点冲突仍缺。

### 1.3 lead 用真实库实测反馈（2026-11-10 13:00）与处理

lead 在只读真实库上直接调 build_user_fact_context（实测 20-23ms/次）报了 5 条，本轮已修 1-4：

1. **known_books 只有 3 本 → 已修**：除了 projects/ 书目录，新增从 local_intake source 路径、
   最近 8 个 session 文件里的「16.夜不收」这类「数字.书名」目录提取书名。
   真实快照实测：3 本 → 14 本（夜不收/兰陵王/国子监秘闻录/袁崇焕/天下苍生/师任堂/大唐惊闻录/
   大秦宣太后/刺明/大明梦华/大清钱王/中国妖怪录/大明水师/衙探）。
2. **冲突提示几乎不触发 → 已修**：新增「同一主题有多条不同时期的记忆」弱提示——同 entity
   活跃 ≥2 条、按**语义日期**（正文开头的 YYYY-MM-DD，而不是入库 ts）跨度 ≥7 天、文本骨架不同
   → 按时间列出旧→新，并明说旧的不等于现在还成立。投名状/稻粱谋两个实测量例已触发。
3. **参考层整段灌本地笔记 → 已修**：参考层每条截断 240 字（超长加「（节选）」），条数上限 3→2。
4. **日记模板/重复行 → 已修**：过滤「今天我留意到世界上的一些新动静」「小桔的日记」等模板行、
   「YYYY-MM-DD 周X · 小桔的日记」标题行；跨天重复行按归一正文去重。
5. **2-gram 噪声 → 部分修**：召回打分改加权——书名/引号专名整词命中权重 6，2-gram 权重 2。
   实测 query「夜不收 王炽 粮仓」主块仍会混入《大清钱王》《国子监》的泛词命中，
   彻底解决需要 P2 的锚点表（人物名 canonical id），本轮只做加权。

## 2. 概念模型（不新增列即可表达）

- **Book（书）**：一本书 = 一个记忆分区。现可用 source 后缀（<prefix>:<书名>）与 entity「书:书名」表达。
- **Anchor（锚点）**：人物/地点/情节/设定/规则。当前只有《书名》能自动抽 entity；
  人物名等尚无锚点（P2 需要锚点表或 entity 规范化）。
- **Claim（判断）**：一条记忆 = (subject, predicate, value, polarity, source, valid_from/until, book)。
- **Source / Authority（来源与效力）**：learned_store._AUTHORITY，正文 100 > 作者裁定 60 > 口述 50 >
  进度 30 > 本地笔记 25 > 设定 20 > 推理 15 > 圆桌 10 > 外部 5。
  只有 manuscript 是「事实基准」，其它都是线索（NON_FACT_SOURCES 进参考层）。
- **Validity（时间效力）**：valid_from/valid_until + status；退休用 status=superseded/forgotten，
  不物理删除（可追溯、可回滚）。
- **Canon Evidence（正文证据）**：P3 引入（书, 文件, 段号/字符区间, 引文 hash, 抽取时间），
  让每条 manuscript 记忆能回查到正文原句。

## 3. 写入纪律（P0 已部分落地，P1 收口）

统一规则（任何写记忆的路径都必须遵守）：

1. **必带 source**：不允许默认 live。调用方按语义传 manuscript / author_ruling / correction /
   setting / reasoning / progress / roundtable / external / local_intake。
   门禁：仓库内 grep 「LearnedStore().append(」 与 「append(facts)」 不允许新增无 source 调用。
2. **能判书就带书**：与书有关的来源补 <prefix>:<书名> 后缀（本轮 manager.with_book；
   判定顺序 = 本条事实唯一书名 → 整段会话唯一书名 → 不猜）。
3. **写前查重**：fp 完全重复 + 归一近似重复（本轮召回侧也做了一遍兜底）。
4. **冲突即取代或进队列**：
   - 正文写入 → 同锚点非正文旧条 superseded；
   - correction 写入 → 同锚点最近一条非正文旧条 superseded（现有规则 3，但需要 supersedes/anchor
     能从 app 传进来，见 P1）；
   - 反极性 / 主体颠倒 → 机器不判谁对，进人工确认队列（现有 L2 卡）。
5. **时间与效力**：进度类默认 30 天有效期（写入侧补 valid_until，P1）；设定/推理不自动过期但
   在召回侧降权；正文/裁定永久。

## 4. 召回纪律（本轮 P0 已落地）

入口：user_fact_context.build_user_fact_context（聊天每轮经 manager.build_full_context_for_turn）。

1. **取数**：status=active 且未过期，ORDER BY ts DESC。
2. **读侧来源归一**：source 为空 → legacy；live/legacy 但带外部信源标记 → external
   （effective_source）；local_intake → 参考层。
3. **书隔离**：当前书识别顺序 query 书名 → 最近 5 个 session 文件的唯一书名；
   query 同时出现≥2 本可识别书 → 不猜、不过滤。当前书确定后：
   - 属于其它书的条目剔除；
   - 无书维度的条目（偏好/习惯/跨书经验）保留；
   - 认不出的书名标题只用来排除「其它已知书的专属条目」，不反过来过滤通用事实。
4. **分层**：正文/口述/裁定进主块；推理/设定/大纲/简报/进度/本地笔记/外部进参考层（最多 2 条，见 1.3 第 3 条：本轮由 3 收到 2）。
5. **排序**：相关度分 → 效力 → 时间；无 query 时 效力 → 时间（稳定排序）。
6. **冲突显式**：两层提示——
   a) 强冲突：同锚点、极性相反、去否定词后正文等同 → 「冲突提示」，要求先回正文核；
   b) 弱冲突/多版本：同锚点活跃 ≥2、语义日期跨度 ≥7 天、文本骨架不同 → 「同一主题有多条
   不同时期的记忆」，按旧→新列出，明确旧条可能已被推翻。
7. **陈旧与重复**：progress 超过 30 天不注入（仍在库）；归一近似重复只留一条；主块出现过的句子
   不再出现在参考层；参考层每条 ≤240 字、最多 2 条；整块 ≤4000 字符。
8. **打分加权**：书名/引号专名（整词）权重 6，中文 2-gram 权重 2；同分再按效力、时间。
9. **失败**：任何异常返回空串（fail-open），不阻断聊天。

## 5. 正文索引 = 事实源（P2-P3）

目标：问「书里到底写没写」时，不靠记忆，靠正文逐字直引，且记忆条目能回查到原句。

- 已有能力：novel_memory 按书的 FTS5 索引（build_fts5_index / novel_search_fts5 / novel_recall），
  写作圣经（build_bible / upsert_bible_from_text），修订记录（list_revisions / what_changed）。
- 缺口：记忆与正文之间没有 key。建议 P3 加「canon_evidence」表（或独立 JSON 索引，不改同步表）：
  (book, doc_rel_path, paragraph_no, char_start, char_end, quote_hash, extracted_at, learned_id)。
- 写入：只有带 canon_evidence 的条目才允许 source=manuscript（服务端校验 quote 能在 docx 文本里
  命中 hash 才落「正文」效力，否则降为 author_ruling/reasoning）。
- 召回：主块里的正文条目附「出处：书名 第N段」；参考层冲突时提示「回正文核」并给出可执行工具
  （novel_recall / novel_search_fts5）。
- 迁移（2026-11-10 校正，本节与 §9 P3 风险栏原先互斥，现统一为两条待拍板路线）：
  - **方案 A · 建 canon_evidence 表并纳入双端同步**：要动的是 `SYNC_TABLES` **表注册本身**
    + 两端 DDL/触发器/migrate，而不只是 `business_cols`（那是**列**白名单，加表不会自动带上）；
    同时必须把新增/改动的 Python 文件补进 `deploy_cloud_sync.sh`。两端必须同版本上线，
    否则集合级对账会漂移、且 fail-open 静默。
  - **方案 B · 零 schema（推荐）**：复用 `novel_memory/entities.py` 已有的
    `tier=manuscript` + `source_file` + `evidence` 闸门与 `core.py` 的按书 FTS5 索引，
    「记忆 → 正文段」指针落在书稿目录 `.deskmate/` 内的独立索引，不进统一库、不进同步表。
    边界：云端没有书稿正文，只能回查到指针、不能本地校验引文；需明确「正文校验只在本地发生」。
  - 两条路线都要求「正文索引本身按书本地重建」，不随记忆同步。

## 6. 生命周期（P1-P2）

- **consolidation**：同书同锚点的多条近重复/同义条目，定期合并为一条并保留 superseded_by 链
  （复用 after_action._compact_recap 的思路，但必须写回 learned_history 而不是只改 MEMORY.md）。
- **decay**：按来源分级 TTL——progress 30 天、external 随 valid_until、reasoning/roundtable 90 天降权
  （不删，只不进主块/参考层）；preference/correction/manuscript/author_ruling 不衰减。
- **archive**：超期条目 status=archived（status 值是自由文本，不需要加列、不影响同步），
  UI 默认不展示，检索可显式包含。
- **人工确认队列**：现有 L2 TaskLog 提议卡已覆盖「主体冲突」；P2 扩展到「同锚点多条互斥」
  「批量纠正」「批量作废旧计划」，用户在卡片上选「以新为准/保留旧/都要（分层）」，机器不替人裁决。

## 7. 多书隔离的完整落地

- P0（本轮）：召回侧过滤（书 A 条目不进书 B 会话）+ 写入侧补书后缀（与书有关的来源）。
- P1：把当前书接线到注入链：manager.prefetch / build_system_prompt / _get_core_memory 都按当前书过滤；
  MEMORY.md 里按作品名建的「§ <书名> #stable」分节只在当前书命中时注入；
  USER.md「当前创作」段改为从当前项目/最近进度动态生成（或至少加「可能过时，回项目笔记核」标注）。
- P1：active_project 真正接线（谁在写哪本书由宿主每轮挂载，参考 44b5b3e0 的
  deskmate/bots/notebooks.py mount_current_book 做法），项目记忆 projects/<书名>/ 成为按书注入的正路。
  （2026-11-10 校正：本节原标 P2，与 §9 表格 P1 矛盾；统一按 §9 定为 P1。）
- P3：锚点表（人物/地点/事件 canonical id），跨书同名人物（如「韩效武」属国子监）靠 book_id 隔离。

## 8. 评测集与回归门禁

已落地：tests/test_memory_recall_scope.py（13 例）锁住书隔离/来源分层/冲突提示/陈旧/近重复；
tests/test_memory_canon_discipline.py（11 例）锁住写入纪律。均为 tmp_path，不碰真实库。

评测集（建议扩到 30 条，P2 做成脚本跑真实快照）：
| # | query | 期望 |
|---|---|---|
| 1 | 我在写《国子监秘闻录》第六卷 | 不含夜不收专属条目；含国子监/通用偏好 |
| 2 | 《夜不收》第十一章粮仓怎么改 | 含夜不收进度/设定；不含国子监党争 |
| 3 | 《衙探》访谈稿 | 不含夜不收/国子监专属条目 |
| 4 | 无 query（真实会话首轮） | 只注入通用事实 + 当前书（当前书未知时不误杀） |
| 5 | 同锚点「在/不在史料库」历史双 active | 出现冲突提示 |
| 6 | 20 条外部新闻 + 5 条口述 | 主块 0 条新闻；参考层可选 |
| 7 | 34 条 local_intake | 全部参考层 |
| 8 | 旧 progress（>30 天） | 不注入 |
| 9 | 近重复「这一卷」/「这一卷/篇」 | 只出现一条 |
| 10 | 正文条 + 同锚点旧设定 | 旧设定不出现（写入侧已取代） |

门禁：
- memory 相关改动必须跑 pytest tests/test_memory_recall_scope.py tests/test_memory_canon_discipline.py
  tests/test_user_fact_context.py deskmate/memory/test_learned_store.py。
- 新增任何写 learned_history 的调用点，必须带 source；review checklist 里加一条。
- 召回评测通过率 ≥95%，书隔离 case 必须 100%。

## 9. 分阶段落地（含改动量与风险）

| 阶段 | 内容 | 文件 | 预估 | 风险/回滚 |
|---|---|---|---|---|
| P0 本轮 | 召回书隔离/来源兜底/冲突提示/陈旧降级/存量清洗入口 | deskmate/memory/user_fact_context.py、learned_store.py、manager.py（书后缀）、ingestion_pipeline.py、tests | 已完成 | 低；只读侧行为，异常 fail-open |
| P1（1-2 天） | 旁路 source：digest/recorder/agency/app else 分支；active_project 接线；MEMORY.md 分节按书过滤；USER.md 当前创作动态化 | deskmate/digest/engine.py、auto_tasks/recorder.py、agency/engine.py、coworker/server/app.py、memory/manager.py、after_action.py | 80-150 行 | 中；需 lead 同意改 app.py；行为变更用开关（env）灰度 |
| P2（3-5 天） | progress 写入侧 valid_until；consolidation/decay/archive；冲突人工确认队列扩展；评测脚本 | memory/manager.py、after_action.py、deskmate/agency/task_log.py（跨域） | 200-300 行 | 中；consolidation 会改数据，先 dry-run + 备份 |
| P3（1-2 周） | canon_evidence（正文锚点可回查）、锚点规范化、按书项目记忆成为正路 | novel_memory/core.py、learned_store.py、需要 schema/同步方案 | 300-500 行 | 高；若走方案 A（建表并同步），须改 `SYNC_TABLES` **表注册** + 两端 DDL/触发器/migrate（不是 `business_cols`，那是列白名单）与 `deploy_cloud_sync.sh` 清单，两端同版本上线，否则双端静默不一致；方案 B（零 schema、书稿目录内索引）无此风险，但云端不能本地校验引文 |

> **阶段状态（2026-11-10 14:09 更新）**：上表 P1/P2/P3 的「预估 / 风险」列是 12:44 起草时的
> 排期口径，三阶段现已全部落地；实际改动文件、路线取舍与刻意未做项见 **§11**。

## 10. 迁移与回滚

1. 存量清洗：LearnedStore.reclassify_legacy(dry_run=True) → 看 samples/candidates → 备份
   deskmate.db → 再 dry_run=False。只改 source 列，可逆（记录 id→原 source 即可回滚）。
2. 不新增列：P0/P1 全部复用 source/entity/status/valid_*，sync_store_sqlite.SYNC_TABLES 白名单不变
   （learned_history business_cols 已含 source/entity/polarity/status/superseded_by/valid_from/valid_until）。
3. 新增文件必须同步 deploy_cloud_sync.sh。⚠️ 2026-11-10 13:45 实测纠错：本句原写
   「均在清单或已有依赖，无需新增文件」是错的——`deskmate/memory/ingestion_pipeline.py`
   **从未在册**，云端 `/opt/openworker/deskmate/memory/ingestion_pipeline.py` 仍是 2026-09-06
   旧版，P0 加的 `source=external` 不会上云且被 fail-open 静默吞掉。已补进 CHANNEL_MODULES。
   本次其余改动文件（learned_store.py / user_fact_context.py / manager.py / app.py / persona md）均在册。
4. 回滚：代码 git revert 即可；数据侧只动了 source（可逆）。不要回滚 MEMORY.md/USER.md（人工标注是证据）。

## 11. 本轮已做 / 建议做（明确边界）

已做（**已提交 `9c56f6d7`，2026-11-10 12:51**；本节原写「本工作区未提交」为起草时状态，已校正）：
- deskmate/memory/learned_store.py：local_intake/legacy 来源常量、BOOKISH_SOURCES、
  effective_source/looks_external/fact_titles/fact_book/infer_book_hint/with_book、
  reclassify_legacy（默认 dry-run）。
- deskmate/memory/user_fact_context.py：召回书隔离、来源读侧归一、参考层扩容、
  冲突提示、progress TTL、近重复去重、4000 字上限保留。
- deskmate/memory/manager.py：抽取写入补书后缀（with_book）、注入时传 active_project。
- deskmate/memory/ingestion_pipeline.py：外部摄入显式 source=external。
- coworker/personas/builtin/deskmate.md：加「按书隔离」规则。
- tests/test_memory_recall_scope.py：13 例回归。

### P1–P3 已落地（2026-11-10 14:09，萧哥拍板「全做」）

原「建议做」清单已全部实现，逐项对照：

| 原建议 | 落地 | 状态 |
|---|---|---|
| app else 分支不回落 live | `_append_learned_history` 显式 `source=None` 分组；`LearnedStore.append` 默认 source 从 `"live"` 改为 `None`，逐条 `infer_source` + `with_book` | ✅ |
| digest / recorder 按语义传 source | digest → `external`；recorder → `reasoning` 且补 `base_dir`；ingestion_pipeline → `external`（P0 已做但**漏传清单**，本轮补进 `deploy_cloud_sync.sh`） | ✅ |
| ~~agency/engine.py:449~~ | 复核为 `resolve_subject_conflict` 冲突裁决，不写记忆，非旁路，不需改 | 误列，已删 |
| MEMORY/USER 读侧按书过滤 | 新增 `deskmate/memory/book_scope.py`：`resolve_turn_book` + 整节/条目两级过滤。实测真实 MEMORY.md 只有 `§ 纠正/§ 工作/§ 创作`、无按书名分节，故补**条目级**过滤；USER.md「当前创作」段按书剔除并标注「可能过时」，**文件本体一字未改** | ✅ |
| active_project 应用层接线 | 改为**按轮解析**（`build_full_context_for_turn` / `build_system_prompt` / `prefetch` 共用 `_book`），不写进程级 `active_project`——云端多会话共享单例 manager，写全局状态会串味。顺带修 P0 缺口：显式 book 是工作区 UUID 被判空后不再回退 query 推断，会让整轮书隔离静默失效 | ✅ |
| P2 生命周期 | progress 写侧默认 30 天 `valid_until`；reasoning/roundtable 90 天降权（不删行）；`recent()` 补过期过滤；`retire_by_source(superseded_by=)` 补链；`scripts/memory_lifecycle.py` **只读 dry-run**（decay/consolidation/conflict_review 三组候选，不自动发卡、不改库） | ✅ |
| 30 条评测集 | `scripts/bench_memory_recall_discipline.py`：18 条真实库只读 `.backup` 快照不变量 + 12 条合成库结构用例；实测 **30/30 PASS，书隔离 6/6** | ✅ |
| canon_evidence + 正文锚点回查（P3） | **按方案 B（零 schema）落地**，未建表、未改 `SYNC_TABLES`：新增 `deskmate/novel_memory/canon.py`（复用按书 FTS5 索引，F 段号 = content 按 `\n` 的 1-based 行号）；`LearnedStore.append(canon_quote=, canon_folder=)` 校验引文命中本地正文才允许 `source=manuscript`，否则降级 `author_ruling`；证据指针写 `~/.deskmate/canon_evidence.json`（原子写、键用 fp、不进同步表）；召回附「出处：书名 第N段」，云端无索引时明说「仅本地可回查」 | ✅ |

**P3 路线拍板说明**：选方案 B 而非文档 §5 原案 A（建 canon_evidence 表并纳入双端同步）。理由：A 要改 `SYNC_TABLES` 表注册 + 两端 DDL/触发器/migrate，而云端**没有书稿正文**，建了表也无法校验引文，只换来双端不一致风险；B 把校验明确限定在本地、云端自动降级，零 schema、零同步白名单改动。若要切回 A，需单独排期做两端同版本迁移。

**刻意未做（诚实边界）**：L2 提议卡新增 `memory_multi_version` 动作类型，需 `agency/engine.resolve_proposal` 与前端 `AgencyPanel` 动作枚举同版本上线，且自动发卡会打扰萧哥。本轮只做**候选发现**（`memory_lifecycle.py` 的 `conflict_review` 组），机器不替人裁决。

**新增文件均已补云同步清单**：`book_scope.py`、`canon.py`。运维脚本 `scripts/memory_lifecycle.py` / `scripts/bench_memory_recall_discipline.py` 是本地工具、不被云端 import，不需在册。

---

## 12. 上下文 token 预算与闲聊跳块（2026-11-10 已落地，性能收口）

**为什么**：召回纪律解决「注入什么」，这一节解决「注入多少」。实测 `build_full_context_for_turn`
每轮注入 **约 40KB（约 3 万 token）**：USER 画像 12.4KB / cognitive 8KB / world 7.5KB /
prefetch 5.3KB 是 prefill 大头，闲聊一句「早」也照灌，直接拉长模型首包时间。

**机制**（`deskmate/memory/context_budget.py`）：
1. 每个注入块登记 `(name, priority, cap, cap_light, keep_light)`；总预算按估算 token
   （中文 0.75/字、其它 0.25/字符）控制，默认 **13000 token**（quality-first：
   兼顾 prefill 与人格/认知/世界观完整度；调小更省，调大更保）。
2. 超预算：先整块丢 `priority > 50` 的明显可选长块（溯源标注/信箱/读后感/课时），
   **功能块**（画像、prefetch 记忆检索、认知/分身、世界观、用户事实、情绪、时钟、意图）
   只截断不整块丢——开了的功能不会因为预算静默失效。各块上限：画像 6000（闲聊 2600）、
   prefetch 4500、cognitive 4500、world 4500、用户事实 2000（闲聊 900）、
   emotion_state 1800、emotional 1200、intent 800。
3. 注入前先剥 USER.md 的「## 自动复盘 · 日期」流水段（稳定画像只有 ~2.6KB，
   其余 ~9.7KB 是每日复盘日志），把画像块从 12.4KB 降到 ~2.6KB 而不丢画像内容。
4. 闲聊轮：短（≤24 字）、含中文、无《书名》、无写作/书籍/检索/记忆指代类词、非寒暄例外的
   消息，跳过 cognitive / prefetch / lessons / reflection / intent 等重块，只留画像（短）、
   用户事实（短）、情绪与时钟。**「记得/上次/之前/后来/结局/怎样」等记忆指代词不算闲聊**。

**2026-11-15 修订（萧哥提问「她真的记着吗」）**：原第 4 条把 `world` 也整块跳过，
结果越像「跟人说话」的场合（早／在吗／晚安／好累）越没有她的理解、立场与防虚构硬锚，
也没有「他前几天亲口说过的未了结之事」。现改为：

- `world` 闲聊轮走 `compact=True` 精简配额（立场 700 / 理解 300 / 事实 0，约 1000 字），
  `keep_light=True` + `cap_light=1200`；不灌新闻剪报、不跑语义检索，`_clip_beliefs`
  保证「萧盛真实作品事实」硬锚永不裁且落在存活区内。
- 新增 `user_episodes` 块（`priority=28`、`cap_light=900`、`keep_light=True`）：把用户
  亲口说的近况（逐字引证、带 open/resolved 状态与闭合证据）作为**真实状态**注入。
  只给状态，不给话术、不做时间/关键词触发、不弹卡、不占 proactive_ledger 送达额度。
  详见 `deskmate/memory/user_episodes.py` 与 `tests/test_user_episodes.py`。

**实测**（完整真实 ~/.deskmate COW 克隆，同一句分别按工作轮/闲聊轮）：

| 场景 | 改前 | 改后 | 降幅 |
|---|---|---|---|
| 写作轮（"夜不收 粮仓 方向 怎么改"） | 40070 chars / ≈3 万 token | 22000–22800 chars / ≈12.7–12.9k token | −57% |
| 闲聊轮（"早"） | 40070 chars | 5450 chars / ≈3.2k token | −89% |

写作轮里只有各块的**尾部**被截断（prefetch/cognitive/world 的头部=核心/最近/最相关，
保留；画像的稳定段全保留，只丢每日复盘流水）；闲聊轮才整块跳重块。

**env 旋钮**：
- `DESKMATE_CONTEXT_TOKEN_BUDGET`（默认 13000；24000≈完全旧保真，0=不限制）
- `DESKMATE_CONTEXT_LIGHT=0` 关闲聊跳块（怀疑误判时用）
- `DESKMATE_CONTEXT_BUDGET=0` 整体关预算（恢复旧行为，排障用）
- `DESKMATE_CONTEXT_LIGHT_MAX_CHARS`（默认 24）闲聊长度阈值

**边界与调优**：轻判是启发式，保守优先——纯英文短词、带《书名》、带写作/检索/记忆指代词都不算闲聊；
如果真实使用中发现「该带的记忆没带上」，先 `DESKMATE_CONTEXT_LIGHT=0` 对照，再调长度阈值或补关键词。
预算只裁「每轮注入的记忆上下文」，不动 system prompt。回归：`tests/test_context_budget.py`（31 例）。
