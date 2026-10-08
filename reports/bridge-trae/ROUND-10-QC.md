# ROUND-10 QC · 验收判词（WorkBuddy trust-but-verify）

> 承接 `reports/bridge-trae/ROUND-10-指令.md`。Trae 交回 R10，本文件为独立验收结果。

## 结论：✅ PASS（零回归，两项闭环均真落地）

| 验收项 | 指令要求 | 实际实现 | 判词 |
|---|---|---|---|
| 反思 R2 · 裁决落地 | `apply_reflection_verdict` 让 revise/overturn 真正改条目 | `store.py` 新增该方法：revise→confidence×0.6、overturn→status='superseded'、maintain 不动；并写 `reflected_apply` 痕 | ✅ |
| 防自循环 | `reflected_apply` 不进反思素材口径 | 复用 `_CHANGE_ACTIONS`（本就不含 reflected/reflected_apply），`run_reflection` 落地后不会反复追问 | ✅ |
| 反思痕跨 persist 存活（§3.1） | worldview 层整层重写不吞反思痕 | 孤儿清理改为 `action NOT IN ('reflected','reflected_apply')` 豁免；且 persist 用 `INSERT OR REPLACE (id,...)` 保留原 id → 反思行真实存活 | ✅ 已专测 |
| 教学回流 | `resolve_proposal` 裁决后自动写 lesson | `engine._record_adjudication_lesson`：executed→recent「可继续自主」、rejected→recent「先问你」、never→permanent「别再提」；user_note 并入正文 | ✅ |
| fail-open | 留教/落地失败不影响主裁决 | 两层 try/except 包 `add_lesson`/`apply_reflection_verdict`；专项测试 `test_resolve_fail_open_when_lesson_write_breaks` 覆盖 | ✅ |
| sync 夹具存量债 | `test_worldview_reflection` 补 sync 三列 | `@pytest.fixture(autouse)` 建库后跑 `sss.migrate`，不碰被测逻辑 | ✅ |

## 一、代码核验（读真实 diff，非看声明）

### `deskmate/cognitive/store.py`（+68）
- `apply_reflection_verdict(item_id, verdict, reason)`：maintain 直接返回 True（R1 已留 reflected 痕）；revise 在 `confidence in cols` 时 ×0.6；overturn 在 `status in cols` 时置 `superseded`（且 `AND status='active'` 防止重复置墓碑）；两种真实变更都追加 `reflected_apply` 行（reason 透传）。缺列/缺条目/异常 → 告警返回 False，**绝不抛、绝不阻断 tick**。
- `persist_layer` 孤儿清理：`DELETE FROM item_history WHERE action NOT IN ('reflected','reflected_apply') AND item_id NOT IN (...)`。配合上方 `INSERT OR REPLACE INTO profile_items (id,...)`（`it.id` 稳定），确保本层条目重写后反思行仍挂得住。**这是真修复，非空转**——已用 `test_reflection_survives_layer_overwrite` 端到端钉死。

### `deskmate/agency/behaviors/reflection.py`（+6）
- `run_reflection` 落痕循环内：命中 `revise/overturn` 时同步调 `store.apply_reflection_verdict`，让「改了看法」在数据与召回里可见。

### `deskmate/agency/engine.py`（+53）
- 新增 `_record_adjudication_lesson(rec, decision)`，`resolve_proposal` 在 `log.resolve` 成功后调用。标签取自 `get_spec(action_type).label`（无则 action_type 兜底）；裁决词→人话规则映射；`source='adjudication'`。`base_dir` 从 `rec.extra` 取，缺则回退 `self.base_dir`。

## 二、测试核验（跑真实 pytest）

```
pytest tests/test_p3_reflection.py tests/test_teaching_feedback.py \
       tests/test_worldview_reflection.py tests/test_p2_known_persons.py \
       tests/test_p2_lessons.py tests/test_p1_anchor_recall.py \
       tests/test_settings.py tests/test_sync_store_sqlite.py \
       tests/test_deskmate_local_extractor.py -q
→ 144 passed, 4 warnings in 50.04s
```
- 基线 111 + R10 新增（test_p3 +R2 apply/防自循环/跨 persist 存活、test_teaching_feedback 7 例、test_worldview_reflection sync 夹具修复）合计 **144 passed，零失败、零回归**。
- 注：全套 `pytest tests/`（含 3 个真出网文件 `test_github_installs/test_slack_relay/test_slack_status`）按红线惯例不计入 QC 基线，本次未跑（会卡配额/超时）。

## 三、红线核对
- 仅改后端 Python（store/reflection/engine）+ 测试，**无新表、无新前端、无 token 改动**。
- 本次 QC 不做 commit（收口 commit 由本回合统一执行，见下）。垃圾文件 `:memory:.ses`/`design/`/`dist.tar.gz` 仍排除。

## 四、残留 / 待办
1. **R10 收口 commit**：本回合将 R10 后端 5 文件 + `test_teaching_feedback.py` + ROUND-10-QC/实施记录 精确 add → commit `fork/base` → push backup（红线）。
2. **seed 全量（欠着）**：本地 `known_persons` 仍 104 条（seed 进程曾反复被回收/卡首本）。按用户许可「先欠着」，下回本地重跑 `scripts/seed_known_persons.py` 补齐 14 本。幂等，跑完由同步 daemon 自动上云。
3. **ROUND-11 预告**（见 `ROUND-11-指令.md`）= P3 自主闭环：`intents` 表 + 提案放开到「意图」层 + lessons 注入同层。

---
验收人：WorkBuddy（异步桥接 QC）
日期：2026-10-08
