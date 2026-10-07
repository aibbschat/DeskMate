# ROUND-06 实施记录（P2-R1 · person 锚 DB 注册表 + 模式 B 自生长）

> 依据：`bridge-trae/ROUND-06-指令.md`（P2-R1）+ `P2_实施工单_2026-10-07.md`。
> 状态：**待 WorkBuddy QC**。未 commit（按工单，commit 归 P2 收口）。
> 环境：`.venv/bin/python`（系统 python3 无 pytest）。

## 1. 改了哪些文件 / 函数 / 行号（实测）

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | 模块级 L75 | 常量 `_PERSON_PROMOTE_THRESHOLD = 3` |
| 同上 | `_init_schema()` L308-321 | `known_persons` DDL（name PK/aliases/source/status/seen_count/created_at）+ `idx_known_persons_status` / `idx_known_persons_name` |
| 同上 | L934-947 | `list_active_persons()`（`status='active'`，fail-open 返回 []） |
| 同上 | L949-971 | `seed_known_persons(names, source='seed')`（`INSERT OR IGNORE`、直接 active、幂等，返回新增条数） |
| 同上 | L973-1004 | `add_known_person(name, source='auto')`（candidate→seen_count+1→阈值转正，原子三步） |
| `deskmate/memory/sync_store_sqlite.py` | `SYNC_TABLES` L242-261 | 末尾追加 `known_persons` spec（pk/sync_key=`name`，string，无向量列） |
| `deskmate/cognitive/extractor.py` | prompt L66 | `EXTRACTION_SYSTEM_PROMPT` 增可选字段 `"persons": [...]` |
| 同上 | L107-152 | `_person_pattern()` / `_person_in_text()` / `_active_person_names(store)`（**删** `_person_names_from_config`） |
| 同上 | `extract_anchors(..., store=None)` L156-200 | person 段改**全词/词边界**匹配，人名源走 env override 或 DB |
| 同上 | `_attach_anchors()` L217-220 | 把 `pm.store` 透传给 `extract_anchors` |
| 同上 | `extract_from_text()` L392-396 + `_grow_persons()` L403-419 | 轮末自生长：LLM 抽到的 `persons` → `add_known_person(name,'auto')` |
| `scripts/seed_known_persons.py` | 新增 | 一次性只读 seed 脚本（LLM 扫已出版书 → 灌 known_persons） |
| `tests/test_p2_known_persons.py` | 新增 | 11 用例（seed/词边界/override/门槛/fail-open/SYNC） |
| `tests/test_sync_store_sqlite.py` | L531-534 | `TABLES_ABSENT_IN_FIXTURE` 补 `"known_persons"`（同 R1 加 memory_anchors 的坑） |

## 2. 关键决策 / 与指令的偏差说明

1. **`.docx` 读取复用仓库既有 reader，未引入 `python-docx`**。
   指令写"用 python-docx"，但仓库约定是**零外部依赖**（`build_scholar_index.py:38` 明写"不装 python-docx"，用 zipfile+XML），且沙箱 `pip` 装包不稳。
   故复用 `scripts/docx_extract.py::extract_docx`——功能等价（抽 `.docx` 正文）、零新依赖、风险更低。`.txt/.md` 做编码探测（utf-8 → gb18030，对齐仓库 GBK 实测坑）。
2. **`_grow_persons` 放在条目落锚之后（轮末），而非解析后立即调**。
   若紧跟 `_extract_json` 调用，第 3 次抽取会在落锚**之前**就把名字转 active → 第 3 次即产 person 锚，与验收"**第 4 次起**产锚"不符。
   放轮末后：本轮锚用本轮开始时的 active 名单，门槛在轮末生效 → 实测"前 2 次不产锚 / 第 3 次转正 / 第 4 次起产锚"。
3. **person 匹配用严格词边界** `(?<![\w])name(?![\w])`（Python `\w` 含 CJK）。
   满足验收"「张居」不吞「张居正 / 张居业」"。**已知局限见 §6**。
4. **seed 走"先 migrate(known_persons)、再灌种子"的顺序**。
   先装触发器，种子行才会落 `_sync_oplog` → 跨端同步；顺序反了则种子行只被"无 op 回填"，云端收不到。migrate 失败 fail-open（本地仍灌，跨端由 daemon 的 migrate 补齐）。
5. **书单** = `build_scholar_index.DEFAULT_BOOKS`（8 本显式书单，防自动扫目录污染）+ 国子监 `.docx` + 国子监**人物小传.md**（后者是显式人名清单，零成本高价值）。

## 3. 自带测试（pytest 真实输出）

- 新套件：`tests/test_p2_known_persons.py` → **11 passed in 0.41s**
- 定向回归：
  - `tests/test_deskmate_local_extractor.py` → **24 passed**（指令点名）
  - `tests/test_sync_store_sqlite.py` → **40 passed**（指令点名）
  - `test_p1_anchor_recall + test_p1_anchor_e2e + test_profile_scope_injection` → 22 passed（+上述 64 = **86 passed in 1.60s**，含 R1/R2/R3 锚链路无回归）
- 全量回归：`pytest tests/ -q` → **37 failed, 3771 passed, 1 skipped**。
  - 对照 ROUND-01 记录的全量基线 **47 failed, 3740 passed** → **失败数下降、无新增失败**。
  - 37 条全属存量类，无一触及本次改动面（21 个文件）：权限引擎 `full access`（auto/bypass 环境类，12 条）、`profile_items has no column sync_uuid`（测试夹具未跑 migrate）、`test_health_producers::test_cron_missed`（已知 flaky）、LLM/网络、worldview 反射、token_usage 等。

## 4. seed 实证（临时库，不碰生产库）

命令（`DESKMATE_DIR=/tmp/seedtest` 隔离，绝不写 `~/Documents/已出版/**`）：

```
DESKMATE_DIR=/tmp/seedtest .venv/bin/python scripts/seed_known_persons.py --limit-chunks 8
```

- `known_persons` 表：`source='seed'`、`status='active'`、`sync_uuid` 非空、`_sync_oplog` 落 10 条。
- 人名抽样（首轮 国子监限块跑）：`丁镇岳、丁闻香、叶子询、沈从安、王安石、范仲淹、董文正、金成泰、陈冲、韩笑文`（含真实历史人物 王安石/范仲淹）。

> 注：本次仅以**限块小样本**验证脚本链路（省时省费）。全量书单 seed 是部署时的一次性动作，QC 可独立跑（幂等、可重复）。

**补充（后台全量跑完回填）**：同一 `DESKMATE_DIR=/tmp/seedtest` 隔离库、全量书单 10 本 × 8 块跑完 → `known_persons` 共 **292 条**（新增 282、幂等跳过 10），全部 `source='seed'`、`status='active'`、`sync_uuid` 非空、`_sync_oplog` 同步落条。抽样含王安石、范仲淹、上官婉儿、万安等真实历史人物（另含小说人物 丁镇岳/叶子询 等）。实证脚本可重复、幂等、只写 `known_persons` 不碰原稿。

## 5. 模式 B 三次转正实证（pytest 真实输出）

`tests/test_p2_known_persons.py::TestModeB`：

- `test_mode_b_promotion_direct`：连续 3 次 `add_known_person("朱元璋")` → 第 1/2 次 `list_active_persons()==[]`（candidate），第 3 次含"朱元璋"；落库 `(source,status,seen_count)==('auto','active',3)`。✅
- `test_mode_b_e2e_extract_promotes_on_third`（真实抽取链路，LLM 打桩返 `persons:["朱元璋"]`）：
  - 第 1、2 次 `extract_from_text` → `memory_anchors` **无** person 锚；
  - 第 3 次 → 本轮**仍无** person 锚（轮末才转正），但 `list_active_persons()` 已含"朱元璋"；
  - 第 4 次 → person 锚出现。✅ 门槛生效实证。

## 6. 红线核对 + 遗留

- **只读 seed**：✅ 脚本只读 `PUBLISHED`，写操作只入 `known_persons`（不碰原稿正文/`.deskmate`，含显式 `.deskmate` 跳过守卫）。
- **不改 topic/book 抽锚、不碰 `recall_items`、不碰 `manager.py` 注入链**：✅ 均未改动。
- **SYNC**：✅ 注册 `known_persons`；`migrate` 后三列齐 + 新插入落 `_sync_oplog`（`test_known_persons_migrate_cols_and_oplog`）。
- **fail-open**：✅ store 查询异常 → `[]`；`add_known_person` 异常 → 忽略；LLM persons 字段缺失/类型错 → 忽略，主抽取照常（`test_fail_open_*`、`test_grow_fail_open_when_store_broken`）。
- **向后兼容**：✅ env `DESKMATE_PERSON_NAMES` 仍生效（强制 override，`test_env_override_beats_db`）。
- **R1 不 commit**：✅ 未 commit。

### 遗留 / 待萧哥拍板
1. ~~**词边界严格口径的召回局限**~~ → **已闭环（ROUND-06b）**：QC 判为 BLOCKER，06b 已把词边界正则改为「中文子串匹配 + 排除被更长已知名包含」，「张居正推行改革」现正常命中。详见 `ROUND-06b-实施记录.md`。
2. ~~全量书单 seed 尚未跑~~ → **已完成**：全量跑出 **292** 条 known_persons（见 §4 补充）。生产库 seed 仍是部署时的一次性动作。