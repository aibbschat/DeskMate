# ROUND-06b 指令（P2-R1 补丁 · 修中文词边界失效）

> 下发：WorkBuddy（指挥员）→ Trae（一线）。依据：ROUND-06-QC 的 BLOCKER（person 锚在中文下系统性漏检）。范围：只改 `_person_in_text` + `extract_anchors` 调用 + 测试断言。零破坏、fail-open。

## 一、问题（实测）
`_person_pattern` 用 `(?<![\w])name(?![\w])`，Python `\w` 含 CJK → 中文名后跟汉字时 `(?![\w])` 拒绝 → 全名不命中。实测 `_person_in_text('张居正','张居正推行改革')`=False。中文无人名词边界，此口径 90%+ 漏检——person 锚在真实中文下基本沉默。

## 二、修复（方案 2：子串 + 排除被更长名包含）
1. **`extractor.py` 改 `_person_in_text(name, text, longer_names=None)`**：
   - 初判：`if name not in text: return False`。
   - 排除：若 `longer_names` 中存在 `ln != name and name in ln and ln in text`（短名被文本中更长已知人名包含）→ `return False`（防「张居」误吞「张居正」）。
   - 否则 `return True`。
   - 移除 `_person_pattern` / `_PERSON_PATTERN_CACHE`（不再用词边界正则）。
2. **`extract_anchors` 调用处（:196-198）**：先取 `names = _active_person_names(store)`，循环改为 `for name in names: if _person_in_text(name, text or "", names): _add("person", name)`（把名单传给排除逻辑）。
3. **`_active_person_names` 不变**（env override 优先 + DB，已 fail-open）。

## 三、测试修正（`tests/test_p2_known_persons.py`）
- `test_word_boundary_matches_full_name`（:84-92）：`anchors2 = extract_anchors("张居正推行改革", ...)` 断言改为 **命中**（`("person","张居正") in anchors2`）；注释改为「全名后跟汉字应命中（中文无词边界）」。
- `test_word_boundary_no_partial_match`（:77-81）：改为「active 含 张居正、张居业 时，文本『张居业的笔记』只产 张居业、不产 张居」（验证排除逻辑）。
- 新增 `test_substring_chinese_full_name`：`extract_anchors("朱元璋建立了明朝","文","article",[],store=seed(["朱元璋"]))` → 产 person 锚。

## 四、验收（WorkBuddy 独立验）
- 11+ 用例全绿（修复后）；回归 extractor 24 / sync 40 仍绿。
- 独立实测：`_person_in_text('张居正','张居正推行改革')`=True；`('张居','张居业的笔记')` 在 active 含 张居正、张居业时 =False（被更长名排除）。

## 五、红线
- 只改 `extractor` 的 `_person_in_text`/`_person_pattern`/`_person_pattern_cache`/`extract_anchors` 调用 + 测试；不改 store/seed/sync/recall/manager。
- 不 commit（R1 收口时一并 commit）。
- fail-open 保持（异常不抛）。

## 六、交付
写 `reports/bridge-trae/ROUND-06b-实施记录.md`（真实落点 + 自测 + 独立实测），交 WorkBuddy re-QC。
