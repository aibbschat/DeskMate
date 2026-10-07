# ROUND-06 指令（P2-R1 · person 锚 DB 注册表 + 自生长）

> 下发：WorkBuddy（指挥员）→ Trae（一线）。
> 依据：R2-QC 移交「person 锚默认沉睡」；萧哥 10-07 口述「得配好，不然白做了」；
>   **萧哥 10-07 拍板两条**：① person 锚源从 env 列表升级为 **DB 注册表**；
>   ② 生长模式选 **B 自生长 + 频次门槛（≥3 次转正）**——"小桔的，让它自己长更符合自由的观点"；
>   ③ 种子源从「史料库」改为 **「已出版目录 ~/Documents/已出版」，由 LLM 一次性扫描抽取**（非萧哥手抄 starter list）。
> 范围：**只做 person 锚源升级为 DB 注册表（含 seed 扫描脚本 + 运行时自生长）**。零破坏、fail-open、可回退。

## 一、为什么（实测事实 + 拍板）
- R2 已实现 person 锚抽取：`extractor.py:153-155` `for name in _person_names_from_config(): if name in (text or ""): _add("person", name)`。
- 但激活依赖 `extractor.py:111` `os.getenv("DESKMATE_PERSON_NAMES","")`——**手工逗号列表 + 子串匹配**，对萧哥历史写作（人物浩如烟海、持续增长）= 默认沉睡 = 白做。
- 升级为 **DB 注册表 `known_persons`**：可 seed（LLM 扫已出版书）、可运行时自生长、可跨端同步、可审计。
- **模式 B（已拍板）**：注册表不是死名单。seed 进来的直接 `active`；运行中 LLM 识别到的**新人名**先进 `candidate`，累计出现 **≥3 次**跨多次抽取才转正为 `active`（开始生产 person 锚）。门槛防「王/李」高频字与路人甲噪声污染。
- **种子源（已拍板）**：`~/Documents/已出版`（书根目录，参照 `scripts/build_scholar_index.py:77` `PUBLISHED`）。**复用其显式书单模式**（不能自动扫整目录——会混入合同/样张/目录页，见该脚本 74-75 行注释），LLM 逐本抽人名灌库。

## 二、落点（均实测行号）

### 1. 新表 `known_persons`（DDL 扩展，含自生长字段）
```sql
CREATE TABLE IF NOT EXISTS known_persons (
  name       TEXT PRIMARY KEY,
  aliases    TEXT DEFAULT '',
  source     TEXT DEFAULT 'seed',   -- 'seed'(LLM扫书) | 'auto'(运行时发现)
  status     TEXT DEFAULT 'active', -- 'candidate' | 'active'
  seen_count INTEGER DEFAULT 0,     -- 运行时发现的累计出现次数
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_known_persons_status ON known_persons(status);
CREATE INDEX IF NOT EXISTS idx_known_persons_name   ON known_persons(name);
```

### 2. 注册进 SYNC_TABLES
`sync_store_sqlite.py` 的 SYNC_TABLES dict，参照 R1 `memory_anchors` 写法：单列 `sync_key=name` 主键、string 类型、无向量；加 `sync_spec` 让 `migrate` 补三列（`sync_uuid`/`sync_hlc`/`sync_tombstone`）+ 触发器（对齐 R1 验证脚本）。`known_persons` 跨端收敛（Mac/云双副本一致）。

### 3. `extractor.py` 改造（模式 B 核心）
- 改 `_person_names_from_config()`（L108-112）→ `_active_person_names(store)`：
  - 优先读 env `DESKMATE_PERSON_NAMES`（保留作**强制 override**，绕过表、直接 active）；
  - 否则 `store.list_active_persons()`（`SELECT name FROM known_persons WHERE status='active'`）。
- `extract_anchors`（L115-158）：person 锚生产仍用 active 名单做词边界匹配（L153-155 的 `if name in text` 改为**词边界/全词匹配**，正则 `(?<![\w]){re.escape(name)}(?![\w])`；中文按「前后非汉字/字母数字」判定），避免「张居」误命中「张居正」。
- **运行时自生长**（在 `extract_from_text` 的 LLM 抽取链，L223-249 prompt + `_extract_json` 解析后）：
  - 在 `EXTRACTION_SYSTEM_PROMPT` 增可选字段 `"persons": ["文中出现的专有历史人物/人名，不含泛指代词"]`；
  - 解析 `data.get("persons", [])`，对每个名调 `store.add_known_person(name, 'auto')`（见 4）——候选累积 / 频次转正。
  - fail-open：persons 字段缺失或 LLM 抽人名失败 → 忽略，不影响主认知抽取。

### 4. `store.py` 新增 / 改
- `list_active_persons() -> list[str]`（`SELECT name FROM known_persors WHERE status='active'`）
- `seed_known_persons(names: list[str], source="seed")`（`INSERT OR IGNORE`，`status='active'`、`source=source`，幂等——seed 来的直接可信）
- `add_known_person(name, source="auto")`：**自生长 + 频次门槛原子操作**：
  ```python
  INSERT OR IGNORE INTO known_persons(name, source, status, seen_count)
       VALUES(?, 'auto', 'candidate', 0);
  UPDATE known_persons SET seen_count = seen_count + 1 WHERE name = ?;
  UPDATE known_persons SET status = 'active'
       WHERE name = ? AND seen_count >= 3 AND status != 'active';
  ```
  （阈值常量 `_PERSON_PROMOTE_THRESHOLD = 3`，集中定义便于调。）

### 5. 种子扫描脚本 `scripts/seed_known_persons.py`（一次性，LLM 扫书）
- **路径**：`PUBLISHED = Path.home() / "Documents" / "已出版"`，复用 `build_scholar_index.py:79-92` 的 `DEFAULT_BOOKS` 显式书单（避免自动扫整目录污染；书均为 `.docx`）。
- **覆盖书单**：默认含 `DEFAULT_BOOKS` 全部历史专著；**另纳入 `20.国子监秘闻录`**（萧哥小说，人物也是人名，小桔读稿本就在用）——seed 仅**只读抽人名**，不碰原稿正文（过 `guarded_write` 红线；只读放行）。
- **读取**：`.docx` 用 `python-docx` 读正文（utf-8/xml，无编码问题）；若扩展扫 `.txt/.md`，做**编码探测**（try utf-8 → except `gb18030`，参照 `deskmate/ingest/local_intake.py:137` 的 GBK 实测坑，21/35 文件是 GBK 系）。
- **切块**：参照 `build_scholar_index.py:159` `split_chunks`（按 `## ` 章节切，CHUNK_SIZE≈900），逐块调 LLM 抽 persons。
- **汇总**：去重全部书名 → `store.seed_known_persons(names, 'seed')`（直接 active）。
- **跳过**：任何 `.deskmate` 目录、合同/样张/目录页等非正文（显式书单已规避大部分；脚本再 `continue` 掉路径含 `.deskmate` 的）。
- fail-open：单本读失败 / LLM 抽失败 → 告警跳过该书，不中断整批。

## 三、实现规格
1. **fail-open**：注册表缺失/查询异常 → 退回 env 列表（再无则无 person 锚），**不抛**；LLM 抽人名失败不阻断主抽取。
2. **向后兼容**：env `DESKMATE_PERSON_NAMES` 仍生效（强制 active override）；未配则走 DB。
3. **不改** `_attach_anchors` 的 `upsert_anchors` 调用（R2 已落 memory_anchors），不改 topic/book 抽锚逻辑。
4. **SYNC**：known_persons 进 SYNC_TABLES，跨端收敛（与 memory_anchors 同机制）。
5. **seed 与运行时分流**：seed 来的 `source='seed'` 直接 active；运行时 `source='auto'` 走 candidate→阈值转正。

## 四、验收（R2-QC 同风格，WorkBuddy 独立验）
- **seed**：跑 `scripts/seed_known_persons.py` 后 `known_persons` 非空（应含 朱元璋/张居正 等真实历史人物，及国子监小说人物）；`source='seed'`、`status='active'`。
- **词边界修复**：`extract_anchors('张居业的笔记', ...)`（含「张居」不含「张居正」）**不应**产出 person 锚（除非「张居业」本身在表里）。
- **模式 B 自生长**：连续 3 次 `extract_from_text` 同一未在表内的人名 → 第 3 次后 `status` 转 `active`，第 4 次起能生产 person 锚；前 2 次 `candidate` 状态**不生产**锚（门槛生效实证）。
- **fail-open**：无 env、DB 空 → 无 person 锚、不抛；LLM 抽人名异常 → 主抽取照常。
- **SYNC**：`migrate` 后 known_persons 有 `sync_uuid`/`sync_hlc`/`sync_tombstone` + 新插入落 `_sync_oplog`（参照 R1 验证脚本）。
- **回归**：`test_deskmate_local_extractor` 24 passed 仍绿；`test_sync_store_sqlite` 40 passed 仍绿。

## 五、红线（爆炸半径锁定）
- 不改 extractor 的 topic/book 逻辑（R2 已锁）。
- 不碰 `recall_items`（那是 P2-R2 的事）。
- 不碰 `manager.py` 注入链（P2-R3 的事）。
- **seed 脚本只读**：绝不写 `~/Documents/已出版/**` 下任何原稿正文（含 `.deskmate/` 圣经）；只产出人名灌 `known_persons`。
- commit 归 P2 收口，R1 不 commit。

## 六、交付
完成后写 `reports/bridge-trae/ROUND-06-实施记录.md`（真实落点 + 自测 + 证据，含 seed 后 known_persons 抽样 + 模式 B 三次转正实证），交 WorkBuddy QC。
