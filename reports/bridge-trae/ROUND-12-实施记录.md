# ROUND-12 实施记录（P3 自主闭环 · 后半程：执行 → 回流）

> 依据：`ROUND-12-指令.md`；上游：`ROUND-11-QC.md` PASS（R11 已收口 `1a8f6ea`，deploy 补丁 `27c035f`）。
> 设计底稿：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md` §四·第三段（自主）、§4.4 护栏、§八.4。
> 范围：补上四段闭环最后一跳——意图被裁决 `executed` 后**真正做出来**（受护栏的自主动作 + 可复核产物），
> 并把「做成了什么」回流进上下文（`intent_outputs` 表 + `intent_executor` 行为 + 注入块）。
> 状态：**待 WorkBuddy re-QC**。未 commit（工作树改动，符合收口红线）。

## 0. 一句话

R11 让「她会想事、会提案」，但裁决 `executed` 之后**只写 lesson、什么也不做**——只提不干。
R12 补上「你同意了，然后呢」：意图放行后真的产出一份**可复核产物**（她写她的：综述/手记/查证笔记），
落 `intent_outputs` 表，并由 `build_intent_context` 回流进她后续每一轮的上下文。
护栏钉死四条：**绝不替你写原稿、绝不自动外发、查证只用本地史料、全链 fail-open**。

### ⚠️ 本回合与指令的一处关键偏差（已获萧哥拍板，见 §2.1）

指令 §二.2/§五默认口径是「执行器**绝不**调用 ai-circle 外发」。萧哥在开工前明确选择**更激进**并补充原话：
**「选择虽然激进，但符合我们当下在做这个项目的意图，不能因为危险就一刀切，而是审核后再去做」**。
据此，本回合在**不破坏「不自动外发」**的前提下，把「对外表达」做成**两道人审闸**：
第一道（R11 已有）意图提案 `executed`；第二道（R12 新增）草稿提案 `intent_outbound` **再点一次头**才真发。
即：**允许外发，但必须再过一道人审**——不是一刀切禁止，也不是无脑自动。

## 1. 真实落点

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | `_init_schema()`（L365-375） | 新增 `intent_outputs` 表 DDL（`id/ts/intent_id/goal/artifact/kind/status` + 2 索引）——**本回合唯一新表**，`CREATE TABLE IF NOT EXISTS` 非破坏性 |
| 同上 | 新增 `get_intent`（L1347） | 按 id 取意图（**不限 status**，R11 的 `get_open_intents` 只看 open，执行路径需要跨状态取回） |
| 同上 | 新增产物 CRUD（L1371-1426） | `add_intent_output`（空 artifact/异常 → `""`）、`get_recent_intent_outputs(limit)`（倒序）、`set_intent_output_status`；全 fail-open |
| `deskmate/agency/behaviors/intent_executor.py` | **新增**（约 330 行） | 执行器：护栏闸门 + 主路径产出 + `parse_intent_output` + 本地史料素材；**本模块自身绝不外发**（外发在 engine 二次人审后，见下） |
| 同上 | `is_manuscript_intent` | 原稿红线：显式路径过 `sacred_paths.guarded_write`（唯一事实源），无路径退关键字兜底 |
| 同上 | `collect_materials` / `_local_history_materials` | 查证类**只读 `<base>/history/`**，不联网；其余用已有世界观察 |
| `deskmate/agency/engine.py` | `resolve_proposal` 扩分支（L452-460） | `action_type=="intent"` → `_execute_intent_proposal`；`=="intent_outbound"` → `_send_intent_outbound`；**仅 `executed` 走到这里**（`rejected`/`never` 不触发） |
| 同上 | 新增 `_execute_intent_proposal`（L465-514） | 取意图 → 调执行器 → 回填 `rec.extra["execution"]`；失败 fail-open 回退 `active`（撞原稿红线则 `dropped`）；`pending_review` → `_propose_intent_outbound`，否则意图 `done` |
| 同上 | 新增 `_propose_intent_outbound`（L516-542） | **二次人审闸**：对外表达草稿 → `action_type="intent_outbound"`、`risk="high"`、`status=PROPOSED` 提案 |
| 同上 | 新增 `_send_intent_outbound`（L544-592） | **全系统唯一外发口**：只在人**二次点头**后可达；先过 `claim_outbound` 纯机械防刷屏 → `post_topic("日常", …)` |
| `deskmate/cognitive/memory_injector.py` | 新增 `build_intent_context`（L432） | 取最近 3 条产物 → 「## 小桔自己做过的事」块；`pending_review` 标注「还没发」；无数据/异常 → `""` |
| `deskmate/memory/manager.py` | 注入链（L685-692） | 反思块之后挂意图产物块；随总开关 `DESKMATE_AUTO_INJECT`，单独开关 `DESKMATE_INTENT_INJECT` |
| `deskmate/agency/settings.py` | `DEFAULTS`（L139-146）+ dataclass/`to_dict`/`from_dict` | 新增 `intent_execution={enabled, allow_outbound, max_tokens, max_chars}` |
| `deskmate/memory/sync_store_sqlite.py` | `SYNC_TABLES` 尾部**仅注释** | `intent_outputs` **不进 SYNC**（同 R11 的双写风险处置），待「单写窗口」拍板 |
| `scripts/deploy_cloud_sync.sh` | `AGENCY_BEHAVIORS_MODULES`（L122） | 补 `deskmate/agency/behaviors/intent_executor.py`；✅ echo 由「4 个」改「5 个」 |
| `tests/test_p3_intent_execution.py` | **新增**，16 例 | 见 §3 |

未碰：`route_task` 的 `self` 路由 / `visibility_gate` / kill switch / 出网审批闸 / 前端 / token / SYNC 表。

## 2. 关键决策 / 与指令的偏差

1. **【最重要·偏差】外发从「一刀切禁止」改为「二次人审放行」**：指令 §二.2/§五默认「执行器绝不调外发」。
   萧哥开工前选择更激进并明确「**审核后再去做**」。落实为：
   - `intent_executor.py` **自身仍绝不外发**（守住指令对执行器的字面要求）；
   - 外发被单独隔离到 `engine._send_intent_outbound`，**只在用户对 `intent_outbound` 提案二次点头后才可达**；
   - 对外表达类先落 `draft_external` + `pending_review` 草稿，永远先给萧哥看。
   即「允许外发 ≠ 危险就一刀切」，而是**多一道人审闸**。风险敞口：`intent_outbound` slug **未注册进 registry、
   不在白名单** → `tick()` 天然不迭代它，不会被自动跑。
2. **`pending_review` 时意图回退 `active`（非 `done`）**：指令 §3.1 说落库后 `intent.status=done`。但对外表达类
   只是「出了草稿、还没发」，语义上没做完——故 `_execute_intent_proposal` 对 `pending_review` 保持 `active`，
   待二次点头真发后才 `done`（由 `_send_intent_outbound` 收尾）。这是对「done = 真做完」语义的收紧。
3. **`intent_outbound` 用独立 slug + `risk="high"` + `PROPOSED`**：复刻 R11「未注册动作 → 天然走人审」的机制，
   不新造白名单口子。`_send_intent_outbound` 里**仍过 `claim_outbound`**（与自主发声同一条机械防刷屏护栏），
   并对 `intent` kind 关闭内容级 `dedupe`（外发内容由人逐条点头，不宜被去重吞掉）。
4. **原稿红线走 `sacred_paths` 单一事实源**：`is_manuscript_intent` 先对 goal 里的显式路径逐个调 `guarded_write`，
   返回 False（受保护）即命中；**无路径才退关键字兜底**。命中 → `reason="sacred_manuscript"` + 意图 `dropped`
   （不反复试同一条替写稿意图）。
5. **fail-open 全链**：闸门/素材/LLM/落库/外发任一步异常 → 仅 `logger.warning`，意图回退 `active`，**lesson 照写**
   （裁决闭环不破，R10 的「拦必留教」不受影响）。
6. **`collect_materials` 不联网**：查证类只读 `<base>/history/`（`.md/.txt/.markdown`）；目录缺失 → `[]`
   再退世界观察，绝不触发网络。
7. **跨端同步**：`intent_outputs` 与 `intents` 一致**本回合不进 `SYNC_TABLES`**（仅注释），待萧哥拍板单写窗口。

## 3. 自带测试

- 新增 `tests/test_p3_intent_execution.py`（**16 例**），对齐指令 §四验收判据：
  - 主路径 1：`executed` → 产出 `intent_outputs`、`intent.status=done`、`task_log.extra.execution` 回填；
  - 护栏·原稿 2：命中 → no-op + `dropped` + `reason=sacred_manuscript`；显式路径走 `guarded_write`（非原稿路径不受影响）；
  - 护栏·外发 3：只出 `draft_external/pending_review` + `intent_outbound` 提案（`risk=high`）、**断言 `post_topic` 未被调**；
    二次点头后**真发**；env `AGENCY_INTENT_OUTBOUND_ENABLED=0` 关死外发；
  - 裁决分支 1：`rejected`/`never`（parametrize）**不触发执行**，lesson 照写；
  - fail-open 1：LLM 异常 → 回退 `active`、不抛、lesson 仍写；
  - 注入 2：`build_intent_context` 含产物 + `pending_review` 标注「还没发」；库异常 → `""`；
  - 执行器单测 4：闸门（`no_goal`/`disabled`/env 关）、解析容错、查证只用本地史料、白名单无 intent slug。
- 定向（R12 相关 9 文件）：
  `.venv/bin/python -m pytest tests/test_p3_intent_execution.py tests/test_intent_generation.py tests/test_p3_reflection.py tests/test_worldview_reflection.py tests/test_teaching_feedback.py tests/test_agency.py tests/test_p2_lessons.py tests/test_settings.py tests/test_sync_store_sqlite.py -q`
  → **162 passed**（基线 146 + 新增 16）。

## 4. 独立实测（脱离 pytest 夹具，`.tmp_r12_live_check.py`，跑后已删）

> 全程离线：`deskmate.memory.llm.chat` / `llm_available` 与 `aicircle.post_topic` 均打桩，不联网。

```
[PASS] 提案生成且停 PROPOSED
[PASS] 执行产出 intent_outputs 一条
[PASS] 产物 kind=diary / status=done
[PASS] 意图置 done
[PASS] task_log 回填 output_id
[PASS] 回流注入块含产物
[PASS] 原稿意图 no-op（无新产物）
[PASS] 原稿意图置 dropped
[PASS] 对外表达未自动发出
[PASS] 落 draft_external + pending_review
[PASS] 意图未 done（外发未完，仍 active）
[PASS] 生成二次人审提案（risk=high）
[PASS] 二次点头后真发出
[PASS] 外发后产物置 done
[PASS] 外发后意图置 done
[PASS] rejected 不触发执行（产物无新增）
ALL LIVE CHECKS PASSED
```

对应验收判据：§四.1（第 2-5 项）、§四.2（第 7-8 项）、§四.3（第 9-12 项）、§四.4（第 16 项）、§四.6（第 6 项）。

## 5. 全量回归（受控对照，证明零新增失败）

**运行口径**（两轮完全一致，唯一差异是「是否含 R12 改动」）：
```
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m pytest -q --tb=line -rfE
```

- **当前态（R12，7 tracked 改动 + 新测试）**：`44 failed, 3837 passed, 1 skipped`
- **基线态（R11 = `git stash` R12 tracked 改动 + `--ignore=tests/test_p3_intent_execution.py`）**：`44 failed, 3821 passed, 1 skipped`
- **只在当前态失败（= R12 新增回归）**：**无**（失败集合差为空）✅
- **只在基线失败**：**无**
- 通过数差 `3837 − 3821 = 16 = 16（新增用例）`，账目自洽；失败数两轮均 44、清单逐条一致。

> 本轮两轮均带 `HF_HUB_OFFLINE=1`（防 `tests/test_proactive_ledger.py::test_relevance_tiers` 触发
> fastembed 联网下载 BGE 卡死）。既有 44 条失败在 R11 收口前即存在，属环境/既有债，非本轮目标。

## 6. 红线核对

- ✅ **未 commit**（改动全在工作树），不推 origin、不 `--force`。
- ✅ 唯一的 DDL 是新表 `intent_outputs`（`CREATE TABLE IF NOT EXISTS`），**无破坏性 DDL**；无 token 改动、无新 SYNC 表。R12 主体无前端改动；**补登记 `RISK_LABEL` 中文标签**为应萧哥要求追加的极小前端改动（见 §7）。
- ✅ **绝不替你写原稿**：原稿红线在 provider 之前 no-op（复用 `sacred_paths.guarded_write` 单一事实源）。
- ✅ **不自动外发**：对外表达类只出草稿 + 二次人审；`intent_outbound` slug 未注册、不在白名单、`risk=high`、`PROPOSED`；
  唯一外发口 `_send_intent_outbound` 仅在**用户二次点头**后可达（对齐萧哥「审核后再做」）。
- ✅ **查证只用本地史料**：`_local_history_materials` 只读 `<base>/history/`，不联网。
- ✅ 所有环节 fail-open（闸门/素材/LLM/落库/外发任一异常 → 仅告警、意图回退 `active`、lesson 照写、不阻断 tick）。
- ✅ 不改 `route_task`（仍 self）、不改 kill switch / `visibility_gate` / 出网审批闸。
- ✅ 部署清单已补 `intent_executor.py`（`deploy_cloud_sync.sh`），md5 核对前置条件不变。
- ✅ 垃圾文件（`:memory:.ses` / `design/` / `dist.tar.gz`）不进库、未 commit。

## 7. 遗留 / 备注

- **二次人审闸的前端展示**（2026-10-08 已补）：两个 slug 原先**未在 `RISK_LABEL` 登记**，面板会显示原始英文键。
  萧哥指出「这是件容易后面忘记的事，现在做了吧」，遂补登记——顺带发现 R9 的 `reflection`、R11 的 `intent_generation`
  也漏了（同一类问题），一并补齐：
  `reflection→自我反思`、`intent_generation→起心动念`、`intent→她想做的事`、`intent_outbound→她想对外说`。
  落点：`surfaces/gui/src/components/deskmate/AgencyPanel.tsx`（`RISK_LABEL`）+
  `AgencyPanel.test.tsx` 新增 1 例（锁「两张新卡显示中文名、内部键不漏出」）→ 前端 **8 passed**、`tsc --noEmit` 干净。
- **跨端同步**（2026-10-08 已拍板）：`intents` / `intent_outputs` **不进 `SYNC_TABLES`**——萧哥定「工作主要在 Mac，两端不同步可接受」，
  不再等「单写窗口」。代码注释已由「待拍板」改为「已拍板·不同步」。
- **`memory_edges` 未上线**：意图素材继续走 R11 的 `memory_anchors` 共现簇降级；P1 汇流显式边表上线后可切回。
- **外发内容的可撤回性**（2026-10-08 已拍板）：**不建撤回通道**——萧哥定「真发出去了要撤也是去接收端撤，
  小桔客户端去撤 AI 圈的稿不现实」。故维持现状：靠「二次人审 + 防刷屏」两道上游闸兜底，不做下游撤回。