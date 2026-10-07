# ROUND-06 QC（P2-R1 · person 锚 DB 注册表 + 自生长）

> 验收：WorkBuddy 独立读真实 diff + 跑测试 + 独立实测，非信"完成"二字。

## 判词：PASS-WITH-BLOCKER
代码按设计实现、测试全绿、无回归、爆炸半径正确；但有一处**中文致命缺陷**须修——否则 person 锚在真实中文下基本失效，用户最初担心的"得配好不然白做了"重现。

## 一、改动面核对（爆炸半径）
`git diff HEAD` 仅 4 文件 + 2 新文件，未碰 `recall_items`（P2-R2）/ `manager` 注入链（P2-R3）；未 commit（符合"R1 不 commit"红线）。
- M `deskmate/cognitive/store.py` (+94)
- M `deskmate/cognitive/extractor.py` (+87)
- M `deskmate/memory/sync_store_sqlite.py` (+20)
- M `tests/test_sync_store_sqlite.py` (+3)
- ?? `scripts/seed_known_persons.py`
- ?? `tests/test_p2_known_persons.py`

## 二、验证点（逐项实锤）
1. **store known_persons**：DDL 含 `status`/`seen_count`/index（store.py:312-327）✓；`list_active_persons` fail-open（:936-949）✓；`seed_known_persons` 直接 active、幂等（rowcount）、fail-open（:951-973）✓；`add_known_person` `INSERT OR IGNORE`→`seen_count+1`→阈值 3 转正、fail-open（:975-1004）✓；阈值常量 `_PERSON_PROMOTE_THRESHOLD=3`（:73）✓。
2. **extractor**：`EXTRACTION_SYSTEM_PROMPT` 加 `persons` 字段（:65）✓；`_active_person_names`（env override 优先 + DB，fail-open，:121-138）✓；`_person_in_text` 词边界（:113-119）✓；`extract_anchors` 传 `store` + 词边界匹配（:196-198）✓；`_grow_persons` 运行时自生长挂线于 `extract_from_text` 末（:396-422）✓。
3. **SYNC**：`known_persons` 进 `SYNC_TABLES`（`pk='name'` text、`sync_uuid_kind='string'`、business_cols 全、无向量，sync_store_sqlite.py:242-261）✓。
4. **seed 脚本**：复用 `build_scholar_index` `PUBLISHED`+`DEFAULT_BOOKS`（:42-47），`EXTRA_BOOKS` 含国子监 docx+人物小传（:51-55），`_read_text` GBK 探测（:70-81），`.deskmate` 守卫 + 只读红线（:159-161），fail-open 逐本/逐块（:164-182），migrate 后再 seed（:112-130）✓。
5. **测试**：`test_p2_known_persons` 11 passed；回归 `test_deskmate_local_extractor` 24 passed（R2 无退化）；`test_sync_store_sqlite` 40 passed（同步无退化）。

## 三、BLOCKER（须修，否则 person 锚在中文下失效）
- **实测**（`_person_in_text` 独立脚本）：
  - `('张居正','张居正推行改革')` → **False**
  - `('张居正','朱元璋与张居正都是名臣')` → **False**
  - `('张居正','张居正，明朝首辅。')` → True（后跟标点）
  - `('张居正','张居正')` → True（整串）
- **根因**：`_person_pattern` 用 `(?<![\w])name(?![\w])`，Python `\w` 含 CJK，中文名后紧跟汉字（"改"/"都"）时负向先行断言拒绝 → 全名不命中。**中文无人名词边界**，此口径系统性漏检（真实中文文本人名几乎总被汉字包围，估计 90%+ 漏检）。
- **测试固化缺陷**：`test_word_boundary_matches_full_name` 第 91-92 行把"张居正推行改革不命中"写成预期断言，缺陷被锁进绿测（呼应红线：绿灯≠锁住目标行为）。
- **影响**：person 锚核心目的（把同一人认知串起来）在中文下基本失效 → 用户最初"得配好不然白做了"的担忧重现。
- **修复方案**（推荐方案 2，精确）：中文走子串匹配 + 排除"短名是文本中更长已知人名子串"；ASCII 专有名词仍可词边界。具体见 `ROUND-06b-指令.md`。
- **修复后须 re-QC**：补"全名后跟汉字应命中"用例 + 修正 L91-92 断言 + 保留"张居不误吞张居正"用例。

## 四、结论
P2-R1 架构落地正确（注册表 + 自生长 + 跨端同步 + 只读 seed），已具备"自己长"的能力；但词边界正则对中文是方向性错误，不修则 person 锚在中文下等于没配。建议：Trae 按 `ROUND-06b` 修 BLOCKER → 我 re-QC → 再提前写 P2-R2（截断 Option B）。
