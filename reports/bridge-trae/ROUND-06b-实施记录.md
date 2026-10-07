# ROUND-06b 实施记录（P2-R1 补丁 · 修中文词边界失效）

> 依据：`ROUND-06b-指令.md`；来源：`ROUND-06-QC.md` 判词 PASS-WITH-BLOCKER（person 锚在中文下系统性漏检）。
> 范围：只改 `extractor` 的人名匹配 + 测试断言。零破坏、fail-open、不 commit。
> 状态：**待 WorkBuddy re-QC**。

## 0. 一句话
QC 的 BLOCKER 已修：中文人名从「词边界正则」改为「子串匹配 + 排除被更长已知名包含」，`('张居正','张居正推行改革')` 由 False 变 True；被锁进绿测的错误断言已改正。

## 1. 真实落点（只碰这两处）

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/extractor.py` | L107-125 | 删除 `_person_pattern` / `_PERSON_PATTERN_CACHE`；新增 `_person_in_text(name, text, longer_names=None)`——子串初判 + 排除被文本中更长已知名包含的短名 |
| 同上 | L193-196 | `extract_anchors` person 段：`names = _active_person_names(store)` 取一次，循环 `for name in names: if _person_in_text(name, text or "", names): _add("person", name)`（把名单传入做排除） |
| `tests/test_p2_known_persons.py` | L74-103 | `test_word_boundary_matches_full_name` → `test_full_name_followed_by_cjk_matches`（「张居正推行改革」断言改**命中**）；`test_word_boundary_no_partial_match` → `test_no_partial_match_when_longer_name_known`（seed 张居正+张居业，验证只产张居业不产张居）；新增 `test_substring_chinese_full_name` |

未碰：`store.py` / `sync_store_sqlite.py`（R6 增量）/ `seed_known_persons.py` / `recall` / `manager`——符合 06b §五红线。

## 2. 关键决策

1. **为什么删正则**：`(?<![\w])name(?![\w])` 里 Python `\w` 含 CJK，中文名后紧跟汉字（"改"/"都"）时负向先行断言拒绝 → 全名不命中。中文无人名词边界，此口径系统性失效（QC 估 90%+ 漏检）。
2. **排除逻辑为何必要**：纯子串会让短名误吞长名（seed 里有「张居」时就可能把「张居正」也认成「张居」）。做法：若 `longer_names` 中存在 `ln != name and name in ln and ln in text` → 不认。等价于「同段内优先认最长已知名」。
3. **名单来源不变**：`_active_person_names` 仍是 env `DESKMATE_PERSON_NAMES` 优先（强制 override）→ 否则 `store.list_active_persons()`，fail-open 不变。

## 3. 自带测试（pytest 真实输出）

- 定向+回归：`.venv/bin/python -m pytest tests/test_p2_known_persons.py tests/test_deskmate_local_extractor.py tests/test_sync_store_sqlite.py -q`
  → **`76 passed in 1.36s`**（p2_known_persons 12 + extractor 24 + sync 40）。
- 达 06b §四「11+ 用例全绿；回归 extractor 24 / sync 40 仍绿」。

## 4. 独立实测（06b §四原口径，`.venv/bin/python` 直跑）

```
_person_in_text('张居正', '张居正推行改革')                     → True    （修复前 False，BLOCKER 已消）
_person_in_text('张居', '张居业的笔记', ['张居正','张居业'])      → False   （短名被更长已知名排除）
_person_in_text('张居', '张居业')                              → True    （名单内无更长名时不误杀）
_person_in_text('张居正', '张居正，明朝首辅。')                  → True    （后跟标点，原口径本就 True，未回归）
_person_in_text('张居正', '张居正')                            → True    （整串）
_person_in_text('张居正', '朱元璋与王阳明')                     → False   （不含）
```

外加测试用例实测（走 `extract_anchors` 全链路）：

- `extract_anchors("张居正推行改革", ...)` + active{张居正} → 产 `("person","张居正")` ✓
- `extract_anchors("张居业的笔记", ...)` + active{张居正,张居业} → 产张居业、**不产张居** ✓
- `extract_anchors("朱元璋建立了明朝", ...)` + active{朱元璋} → 产 `("person","朱元璋")` ✓

## 5. 红线核对

- ✅ 只改 `extractor` 人名匹配 + 测试断言；未动 store/seed/sync/recall/manager。
- ✅ 未 commit（R1 收口时一并 commit）。
- ✅ fail-open 保持：`_person_in_text` 纯字符串判断不抛；`extract_anchors` 外层 try 未动；`_active_person_names` 异常退回 []。

## 6. 遗留 / 备注

- QC BLOCKER 项已闭环，无新增遗留。
- R6 主记录 `ROUND-06-实施记录.md` §6 原列「词边界局限待拍板」的遗留项，即本条 BLOCKER，现由 06b 修复。