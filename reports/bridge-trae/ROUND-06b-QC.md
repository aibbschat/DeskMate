# ROUND-06b QC（P2-R1 补丁 · 修中文词边界失效）re-QC

> 验收：WorkBuddy 独立读真实 diff + 跑测试 + 独立实测，非信"完成"二字。依据：ROUND-06-QC 的 BLOCKER。

## 判词：PASS（BLOCKER 已解除）

词边界正则已彻底移除、改中文子串匹配 + 更长名排除逻辑；测试断言已改正；三套件无回归；爆炸半径仅 `extractor` + `test_p2_known_persons`；fail-open 保持；未 commit（符合 R1 收口红线）。

## 一、改动面核对（爆炸半径）
`git status` + `git diff HEAD`：06b 仅改 `deskmate/cognitive/extractor.py`(+86 累积含 R1) 与 untracked `tests/test_p2_known_persons.py`。**未碰** `store.py`/`sync_store_sqlite.py`/`recall_items`(P2-R2)/`manager`(P2-R3)。未 commit（modified + untracked，R1 收口时一并 commit）。

## 二、验证点（逐项实锤）
1. **词边界正则已移除**：Grep `_person_pattern` / `_PERSON_PATTERN_CACHE` → No matches（确认无残留调用）✓。
2. **`_person_in_text` 改子串 + 排除逻辑**（读 diff 实锤）：
   - `if not name or not text or name not in text: return False`（子串初判）
   - 排除：`for ln in longer_names: if ln and ln!=name and name in ln and ln in text: return False`（短名被更长已知名包含→不认）
   - `extract_anchors`(:191-198) 改为 `names=_active_person_names(store); for name in names: if _person_in_text(name, text or "", names): _add("person", name)` ✓。
3. **测试断言改正**（读 `tests/test_p2_known_persons.py`）：
   - `test_full_name_followed_by_cjk_matches`(:87-95)：「张居正推行改革」（后跟汉字）断言**命中**；注释改「中文无词边界，全名后跟汉字应命中」——原 BLOCKER 的「不命中」错误断言已推翻 ✓。
   - `test_no_partial_match_when_longer_name_known`(:77-84)：『张居业的笔记』只产 `张居业`、断言 `张居` not in（排除逻辑）✓。
   - 新增 `test_substring_chinese_full_name`(:98-103)：『朱元璋建立了明朝』命中所 seed 的朱元璋 ✓。
4. **三套件全绿无回归**：`test_p2_known_persons`(12) + `test_deskmate_local_extractor`(24) + `test_sync_store_sqlite`(40) = **76 passed**（比 06b 前 +1 个新用例）✓。
5. **独立实测 `_person_in_text` 8 例 ALL PASS**：
   - `('张居正','张居正推行改革')`→True（全名后跟汉字，原 BLOCKER 必 False）
   - `('张居正','朱元璋与张居正都是名臣')`→True（前后汉字）
   - `('张居正','张居正，明朝首辅。')`→True、`('张居正','张居正')`→True（旧口径已对）
   - `('张居','张居业的笔记',['张居','张居正','张居业'])`→False（被更长名张居业排除）
   - `('张居业','张居业的笔记',[...])`→True、`('张居正','张居业的笔记',[...])`→False（不在文本）
   - `('王','王安石变法',['王','王安石'])`→False（单字王被更长名排除）

## 三、结论
P2-R1 现**真闭环**：person 锚在中文下能正确命中（核心目的恢复）+ 模式 B 自生长 + 跨端 SYNC + 只读 seed，全部按 ROUND-06 落地且 BLOCKER 已修。R1 业务代码可纳入收口 commit。

下一步：提前写 **P2-R2（截断 Option B）** 指令（ROUND-07），修复 R3 已知局限——`recall_items` FTS 填满 top_k 后邻居被 `hits[:top_k]` 截断吞掉。
