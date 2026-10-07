# ROUND-02 实施记录（R2 · 抽锚）

> 依据：`README.md` §一 R2；工单 §4.2；ROUND-01-QC 四条硬输入。
> 状态：**待 WorkBuddy QC**。未 commit（按 README，commit 归 R5）。

## 0. QC 硬输入逐条落实

| QC §四 硬输入 | 落实 |
|---|---|
| 1. 开口问题：extractor 怎么拿 store 句柄 | **已证：`self.profile_manager.store` 存在**——`CognitiveProfileManager.__init__` 在 `profile.py:58` 直接赋值 `self.store = CognitiveStore(...)`，**无需注入**。`extract_cognitive_items` 是模块级函数，改用它自己的 `pm` 形参取 `pm.store`。 |
| 2. 挂线用 `obj, op = add_*(...); item_id = obj.id` | ✅ 八处全改（extract_from_text 四处 + extract_cognitive_items 四处）。 |
| 3. 抽锚失败 fail-open | ✅ 双保险：`extract_anchors` 内 try/except，`_attach_anchors` 外层再 try/except（与 R1 `upsert_anchors` 同构，绝不抛）。 |
| 4. 抽锚范围仅 profile_item；topic/book/person | ✅ 见下；person 因仓库无 known-names 表，改为「可配置名单」兜底（见关键决策 3）。 |

## 1. 改了哪些文件 / 函数 / 行号

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/extractor.py` | 模块头 L18 | 新增 `import os` |
| 同上 | 模块级 L101-158 | 新增 `_ANCHOR_TOPIC_FIELDS`、`_person_names_from_config()`、**`extract_anchors(text, source_title, source_type, extracted_items)`** |
| 同上 | 模块级 L161-182 | 新增 **`_attach_anchors(pm, item_id, text, source_title, source_type, item_dict)`**（取 `pm.store` 调 `upsert_anchors`，fail-open） |
| 同上 | `extract_from_text` L267/289/312/336 | 四处 `_, op = add_*(...)` → `obj, op = add_*(...)`，随后 `_attach_anchors(...)` |
| 同上 | `extract_cognitive_items` L491/506/524/541 | 四处 `pm.add_*(...)`（丢弃返回值）→ `obj, _op = ...`，随后 `_attach_anchors(pm, obj.id, ...)` |

## 2. 关键决策（与工单/QC 的偏差说明）

1. **挂线传单条目，不传整批**：工单 §4.2 的 `extract_anchors(..., extracted_items)` 签名保留，但挂线时按「单条目 `[item_dict]`」调用。理由：若传整批，则该批**所有 topic** 会被挂到**每一条**条目上，8 条不同主题的立场会两两互连成一团噪声（违背 P1「同锚才邻座」的本意）。**每条目只挂它自己的 topic**，book/person 是来源级锚、天然全批共享。
2. **`topic` 字段兼容**：立场取 `topic`，价值排序 / 思维框架取 `domain`（`_ANCHOR_TOPIC_FIELDS = ("topic","domain")`，取到第一个非空即止）。论证模式无议题字段 → 只拿 book/person（实证见下）。
3. **person 用可配置名单兜底**：仓库**不存在** known-names 表（已 grep 全 `deskmate/` 无 `KNOWN_NAMES`/`known_names`）。按工单「best-effort，识别不出则跳过」，实现为读环境变量 `DESKMATE_PERSON_NAMES`（逗号分隔已知人名），命中才产 person 锚，**未配置 = 不产**（零噪声）。P2 可把真实名单表接到同一函数。工单原文的「专名启发」若照 `creation/bible_feedback.py` 的高频子串法搬来，会产生大量伪专名锚 → 污染邻座召回，故未采用。
4. **不改任何签名/返回值**：`extract_from_text` 与 `extract_cognitive_items` 的返回结构不变，所有既有调用方零影响；挂锚是纯副作用。

## 3. 自带测试（pytest 真实输出）

- `.venv/bin/python -m pytest tests/test_sync_store_sqlite.py tests/test_sync_store_sqlite_diary.py -q` → **`64 passed in 0.64s`**
- `.venv/bin/python -m pytest tests/test_deskmate_local_extractor.py -q` → **`24 passed in 0.13s`**
- 冒烟 e2e（临时库，先跑 `sync_store_sqlite.migrate`）：
  - `extract_anchors("文本","万历十五年","book",[{"topic":"历史真实性"}])` → `[('topic','历史真实性'), ('book','万历十五年')]`
  - 不配名单 → 无 person；配 `DESKMATE_PERSON_NAMES=萧盛,张三` → 命中 `萧盛`
  - 空值/重复 → 滤空 + 去重（`[{"topic":""},{"topic":"创作"},{"topic":"创作"}]` → `[('topic','创作')]`）
  - `_attach_anchors`（`SimpleNamespace(store=store)`）→ `memory_anchors` 落 `book`+`topic` 两行
  - **e2e（patch `llm.chat`）**：`extract_from_text("萧盛说史料要交叉比对","万历十五年","book")` → 立场得 `book/topic/person` 三锚，论证模式得 `book/person`（无 topic，符合预期）；锚的 `item_id` JOIN `profile_items` 全部命中
  - `extract_cognitive_items(...)` → `{'deposited':1,'skipped':0}`，落 `topic=创作观` 一锚
  - fail-open：`_attach_anchors(SimpleNamespace(), ...)`（无 store）静默不抛

## 4. 自测覆盖（对工单验收/红线）

- §4.2 三项（topic/book/person）全部实证产出。
- 红线「fail-open」：外层 `_attach_anchors` 吞异常 + `upsert_anchors` 自身 fail-open，双保险。
- 红线「零破坏」：返回值不变、调用方零改动；未挂锚的库行为 = 今天口径。
- §七 `test_anchor_extract_topic_book` 的抽取部分：✅ 已自证（R4 落成正式用例）。

## 5. 遗留 / 不确定项

1. **⚠️ QC §四.5 的预期与环境实况不符（须 QC 知悉）**：QC 提示「`test_profile_scope_injection.py` 无锚数据下应仍绿」。但本环境实跑该文件为 **`10 failed, 2 passed`**，**全部**红在 `table profile_items has no column named sync_uuid`（存量根因①：夹具不跑 sync migrate，persist 在到达 recall 之前就炸）。即：**在能观察到 L124 之前就失败了**，L124 脆弱断言在本环境**不可评估**。R4 会自带「先 migrate」的夹具来真正覆盖召回路径；此处不改既有文件（存量失败，非本任务引入）。
2. person 锚默认不产（无名单）—— 若 QC 认为 P1 必须产出真实 person 锚，请指定名单来源（我未在仓库找到现成的）。
3. 未 commit（按 README R5 收口）。