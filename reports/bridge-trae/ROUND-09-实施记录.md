# ROUND-09 实施记录（反思层 R1 · 回看判断/看法 → 裁决 + 留 why）

> 依据：`ROUND-09-指令.md`；上游：`ROUND-08-QC.md` PASS。对齐《小桔·江河设计》第二段「反思」。
> 范围：新增反思行为 `reflection.py` + `item_history` 裁决留痕读写 + 注入块 + 挂点 + 测试。
> 状态：**待 WorkBuddy re-QC**。未 commit（反思层 R1 收口时与后续轮次统一走 fork/base）。

## 0. 一句话
世界观回望是「到点重新生成、覆盖旧的」；本轮补上**「回看已有看法、做裁决（保持/修正/推翻）、并把为什么留在账本里」**——把沉淀变成**有记忆的沉淀**。裁决 + 理由落 `item_history`（`action='reflected'`），并每轮随记忆注入，让「为什么变」不被覆盖吞掉。

## 1. 真实落点（A–F + 必要接线）

| # | 文件 | 落点 | 内容 |
|---|---|---|---|
| A | `deskmate/agency/behaviors/reflection.py` | 新增文件 | `run_reflection(base_dir, config)->dict`：读 `item_history` 最近变化 → LLM 裁 maintain/revise/overturn + reason → 落痕；`parse_verdicts()`；fail-open |
| B | `deskmate/agency/engine.py` | `_run_low_risk` L203-221 | `worldview_reflection` 分支后加 `if action_type == "reflection":` 分支（`settings.reflection`） |
| C | `deskmate/agency/settings.py` | L96 / L121-129 / L201 / L222 / L259-265 / L339 | 白名单 actions 加 `"reflection"`；新增 `reflection` 配置段 `{cooldown_hours:168, max_recent:20, min_changes:3}`；dataclass 字段 + to_dict + from_dict |
| D | `deskmate/cognitive/store.py` | L1086-1166 | 新增 `record_reflection(item_id, verdict, reason, subject_kind)` + `get_reflections_for_injection(limit=3, char_cap=800)` |
| E | `deskmate/cognitive/memory_injector.py` | L405-429 | 新增 `build_reflection_context(base_dir)`（仿 `build_lessons_context`，无数据空串 + fail-open） |
| F | `deskmate/memory/manager.py` | L669-681 | lessons 块之后、`if not parts` 之前加反思块（`DESKMATE_REFLECTION_INJECT=1` 开关；`DESKMATE_AUTO_INJECT=0` 总开关已覆盖） |
| 接线 | `deskmate/agency/registry.py` | L56-61 | 登记 `reflection`（risk=low）——**指令外必要动作**，见 §2.1 |
| 留痕 | `deskmate/agency/behaviors/worldview_reflection.py` | `_persist_worldview` L493-521 | 写 stance 后同步 `record_reflection(..., verdict="revise", reason="看世界回望（日期）：<believes>", subject_kind="worldview")`，fail-open |
| 测试 | `tests/test_p3_reflection.py` | 新增 | 6 例（§八清单） |

未碰：`worldview_reflection` 主逻辑（只在 `_persist_worldview` 加留痕）、judge 核心、manager 其他注入块、前端——符合 09 §六 红线。

## 2. 关键决策（与指令的偏差说明）

1. **必须登记 `registry.py`（指令外）**：`engine.tick()` 先 `get_spec(action_type)`，返回 `None` 即 `skipped_unknown` 直接跳过，**根本走不到** `_run_low_risk`。故 `reflection` 必须进 `REGISTRY`（risk=low），否则白名单加了也不生效。（与 R8「`SyncTableSpec` 不存在」同类：以真实代码为准 + 记录偏差。）
2. **`item_history` 没有 `subject_kind` 列**：真实表列为 `item_id/seq/action/old_value/new_value/reason/source/ts`。故 `subject_kind` **落到 `source` 列**，并由 `profile_items.layer` 派生（worldview → `worldview`，其余 → `profile_item`）。**真实系统里 `item_history` 只有认知条目（profile_items）的变更，没有独立的 `judgment` 账本**——指令里的 `subject_kind=judgment` 在本仓无对应源，按真实结构落两类。
3. **落痕字段约定（复用既有列，零 DDL）**：`item_id`=被反思条目；`old_value`=裁决字面量；`reason`=为什么；`source`=subject_kind。`action='reflected'` **不在** `store._CONTENT_ACTIONS` 白名单内 → 不会被 `profile_as_of` 双时态回放误当正文（已验证 `_replay_value` 返回 None）。
4. **不自循环**：`run_reflection` 读变化时按 `_CHANGE_ACTIONS`（created/updated/deepened/restored/calibrated/superseded/questioned）过滤，**故意不含 `reflected` 本身**，否则反思会把自己上一轮的痕当成新变化。
5. **引擎返回值对齐**：`result["verdicts"]` 取**本次成功落痕的条数**（int），供 `log.add(count=...)`；`reason` 为空即不落痕（硬断言在 `record_reflection` 内，落代码拦，不靠自觉）。
6. **LLM 闸门默认启用**：`DESKMATE_REFLECTION_LLM=0` 可先埋框架、后置生成（命中即返回 `reason="llm_disabled"`，不消费素材）。temperature 0.6 / max_tokens 600 / timeout 30，与指令一致；provider 闸门走 `memory_llm_available()`。
7. **错峰（默认）**：worldview 用「攒够 5 条理解」触发，反思用「最近变化 ≥ `min_changes=3`」触发，各过 7 天冷却——素材与批次天然不同，避免同 tick 双重 LLM 轰炸（对应 §七.1 默认）。

## 3. 自带测试（pytest 真实输出）

- 定向：`.venv/bin/python -m pytest tests/test_p3_reflection.py -q` → **`6 passed in 0.37s`**。
- 定向 + 相关回归：`.venv/bin/python -m pytest tests/test_p3_reflection.py tests/test_worldview_reflection.py tests/test_p2_lessons.py tests/test_agency.py tests/test_sync_store_sqlite.py -q` → **`2 failed, 101 passed`**。
  - 2 条 failed（`test_reflection_writes_worldview_layer` / `test_worldview_is_injected_with_correct_subject`）**均为存量失败**，根因是测试夹具未对 `profile_items` 跑 sync 列 migrate（`table profile_items has no column named sync_uuid`），**与 R9 无关**——见 §5 受控对照。
- 6 例覆盖 09 §八全部：`record_reflection` 空 reason 作废 / 落 reflected 痕 / 空库 `[]` / 最近优先 + char_cap / manager 端到端 + 开关 / fail-open。

## 4. 独立实测（真实链路，LLM 打桩）

`python .tmp_r9_live_check.py`（临时脚本，已删）：
```
run1: {"reflected": true, "verdicts": 2, "summary": "回看 3 条变化，对 2 条旧看法做了裁决并留下为什么"}
reflected rows: [('wv0','revise','新证据让我改了口径'), ('wv2','maintain','这条我依然相信')]
run2(同批已回看): {"reflected": false, "reason": "not_enough:0/3"}
run3(冷却168h):   {"reflected": false, "reason": "cooling"}
run4(provider缺失): {"reflected": false, "reason": "no_provider"}
run5(LLM异常):      {"reflected": false, "reason": "llm_error"}
inject ctx 含「小桔最近的自我反思」+ 理由原文 → True
ALL LIVE CHECKS PASSED
```
→ 逐条命中 09 §五 判据：reason 空被拦、跑一次后 `item_history` 出现 `action='reflected'` 且 reason 非空、注入读取含 reason 摘要、低频冷却生效、fail-open 不抛不阻断。

另做结构自检（settings round-trip / registry / parser）：
```
settings/registry/parse OK   （含 parse_verdicts 缺理由 → 丢弃）
```

## 5. 受控对照（证明零新增失败）

- 同一组相关回归（worldview / p1_temporal / forgetting_asof / agency / item_level / sync_store_sqlite / cognitive_invalidate_digest）：
  - **改动后**：`5 failed, 122 passed`（另 cognitive_invalidate_digest 组 `5 failed, 69 passed`）。
  - **受控基线**：把本轮全部改动 `git stash` 回 `fork/base`（`7501617`），对**同一命令**重跑 → **`5 failed, 122 passed`** / **`5 failed, 3 passed`**，失败集合**逐条一致**。
- 结论：R9 **零新增失败、零修复**。全部失败均为存量类，根因同一：夹具未给 `profile_items` 补 `sync_uuid/sync_hlc/sync_tombstone`（`persist()` 硬写这三列，而测试建库后未 `migrate`）。

## 6. 红线核对

- ✅ 只改 A–F + registry 接线 + `_persist_worldview` 留痕 + 测试；未改 `worldview_reflection` 主逻辑、judge 核心、manager 其他注入块、前端。
- ✅ 所有新 LLM 调用 fail-open；不新增向量、不碰 SYNC 同步表；复用 `item_history`、不新开表。
- ✅ `reason` 空 → `record_reflection` 返回 False 且不写痕（硬断言）。
- ✅ 未 commit（HEAD 未动）。

## 7. 遗留 / 待萧哥

- 反思频度默认 `cooldown_hours=168`（7 天）+ 错峰（§七.1）；若想更稀，改配置段即可。
- LLM 裁决默认启用（§七.2）；想先埋框架可置 `DESKMATE_REFLECTION_LLM=0`。
- `item_history` 无 `subject_kind` 列、无独立 `judgment` 账本（§2.2）——后续若要「回看判断」独立成账，需另立表，本轮按红线复用。