# ROUND-12 QC · 实施记录（意图执行闭环 · 出海口后半程）

**结论先行**：✅ **验收 PASS**。R12 七条验收判据（指令 §四）逐条实测通过；R9/R10/R11 全套回归零破坏；红线/护栏在代码里钉死。可提交 → 推 backup → 部署云端对齐。

**实测命令**（去 shim、项目 venv）：
```
env -u PYTHONPATH PYTHONPATH=. .venv/bin/python -m pytest \
  tests/test_p3_intent_execution.py tests/test_p3_reflection.py \
  tests/test_teaching_feedback.py tests/test_intent_generation.py \
  tests/test_worldview_reflection.py tests/test_p2_lessons.py tests/test_agency.py \
  -q --basetemp=/tmp/pt_r12qc
```
**结果**：`115 passed, 3 warnings`（`3 warnings` 来自第三方 `docstring_parser` 的 Python 3.14 弃用告警，与本工单无关）。

---

## 一、验收判据逐条对照（指令 §四）

| # | 判据 | 实测 | 证据 |
|---|------|------|------|
| 1 | 主路径：`executed` → 产出 `intent_outputs`、intent.status=done、关联 task_log | ✅ | `test_executed_intent_produces_output`：`len(outs)==1` / `status==done` / `intent_id` 匹配 / `rec.extra.execution.executed is True` |
| 2 | 护栏·原稿：命中红线 → no-op、不产出、不抛 | ✅ | `test_manuscript_intent_is_noop`：`outs==[]` / `status==dropped` / `reason==sacred_manuscript`；`test_path_guard_uses_sacred_paths`：显式路径经 `guarded_write`（唯一事实源）判定 |
| 3 | 护栏·外发：只出草稿 + 二次人审，绝不自动发；二次点头才真发 | ✅ | `test_external_intent_only_makes_draft`：`sent==[]` / `kind==draft_external` / `pending_review` / 生成 `intent_outbound` 风险=high 提案；`test_second_approval_actually_sends`：再点一次头 → `len(sent)==1` / 状态 done |
| 4 | 裁决分支：`rejected`/`never` 不触发执行 | ✅ | `test_non_executed_decision_does_not_run`：`outs==[]` / intent 仍 `active` / `teachings==1`（R10 教学回流照旧） |
| 5 | fail-open：LLM 异常 → 回退 active、不抛、lesson 仍写 | ✅ | `test_llm_error_rolls_back_and_keeps_lesson`：`resolve_proposal` 不抛 / `outs==[]` / `status==active` / `teachings==1` |
| 6 | 注入：`build_intent_context` fail-open 可取最近产物 | ✅ | `test_build_intent_context_injects_recent_outputs` / `test_build_intent_context_fail_open`（库炸→`""`） |
| 7 | 回归：R9/R10/R11 零破坏 | ✅ | 上述全套 115 passed（含 `test_p3_reflection`/`test_teaching_feedback`/`test_intent_generation` 等） |

---

## 二、护栏钉死核对（不可被「拔掉」）

| 红线/护栏 | 落点 | 验证 |
|-----------|------|------|
| 替你写作 = 永久红线 | `intent_executor.is_manuscript_intent`：显式路径走 `sacred_paths.guarded_write` + 语义词兜底；执行器闸门 1 直接 no-op | `test_path_guard_uses_sacred_paths` 真调 `guarded_write` |
| 绝不自动外发 | 执行器 `external` 分支只落 `draft_external`+`pending_review`，**不调** `post_topic`；真外发只在 `_send_intent_outbound`（二次人审后才走） | `test_external_intent_only_makes_draft` 断言 `sent==[]` |
| 查史料只取本地 | `collect_materials` → `_local_history_materials`（`<base>/history/`）+ 已有世界观察；**不联网** | `test_research_intent_uses_local_history_only` |
| 全链路 fail-open | `run_intent_execution` 内每步异常→仅告警、意图回退 `active`、不抛 | `test_llm_error_rolls_back_and_keeps_lesson` |
| `executed` 才执行 | `resolve_proposal` 仅 `decision==EXECUTED` 且 `action_type=="intent"` 走执行；`rejected`/`never` 只写 lesson | `test_non_executed_decision_does_not_run` |
| intents/产物**不进 SYNC** | `sync_store_sqlite.SYNC_TABLES` 仅注释扩展，未登记 `intents`/`intent_outputs` | diff 确认无新表登记 |

---

## 三、接线核对（trust-but-verify，非推断）

- `engine.propose_from_intent`（R11）产 `action_type="intent"`、`extra={"intent_id","how","source":"intent","origin"}` → 与 R12 分支 `rec.action_type == "intent"` 一致。
- `resolve_proposal`：`_record_adjudication_lesson` 在**所有**裁决上调用（L401），`rejected`/`never` 在 L428 提前返回；`EXECUTED` 后才进意图执行分支（L454-459）。教学回流不被破坏。
- `_send_intent_outbound` 调 `claim_outbound(base_dir, "intent", rate_cfg=, content=, dedupe=False, max_per_day=)` —— 签名 `claim_outbound(base_dir, kind, *, rate_cfg, content, dedupe, max_per_day)` 匹配；再调 `post_topic("日常", title, content)` —— `post_topic(tag, title, content)` 匹配。
- `settings.DEFAULTS["intent_execution"]` 三处全覆盖（dataclass field / to_dict / load 按 key 覆盖）；`outbound_rate_limit` 字段已存在。
- 注入：`memory_injector.build_intent_context` 经 `manager.build_full_context_for_turn`（L691）挂上，与 lessons 同层、`DESKMATE_INTENT_INJECT=0` 可单独关、随 `DESKMATE_AUTO_INJECT=0` 整体关。

## 四、前端（AgencyPanel）修正

`RISK_LABEL` 补登记 `reflection`/`intent_generation`/`intent`/`intent_outbound` 中文名（R9/R11/R12 新动作漏登记会向用户泄漏内部英文键）。属正确性修复，非新功能；随 1.2.0 统一重打包生效（本轮不单独重打 DMG）。

## 五、已知限制 / 留待后续（与指令 §零一致）

- `intents`/`intent_outputs` 暂不进 SYNC：各端各自执行会生成不同 id 产物，强行跨端搬运无意义。待萧哥拍板「单写窗口」后与 teachings 同法补业务列+幂等。
- `memory_edges` 显式边仍延后（意图素材继续走 `memory_anchors` 共现簇降级）。
- 外发通道：对外表达类经二次人审后由 `claim_outbound` 纯机械防刷屏 + `post_topic` 发出；`AGENCY_INTENT_OUTBOUND_ENABLED=0` 可一键关总闸。

## 六、部署

- 提交：本工单全部后端/测试/文档变更（**排除** `:memory:.ses` / `design/` / `dist.tar.gz` 垃圾文件）。
- 推 `backup/fork/base`（禁 origin / 禁 --force）。
- 云端对齐：`scripts/deploy_cloud_sync.sh`（已含 `intent_executor.py` 进 `AGENCY_BEHAVIORS_MODULES`）→ 重启 `openworker-server` → 触发同步窗口。
