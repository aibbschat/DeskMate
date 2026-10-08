# ROUND-11 实施记录（P3 自主闭环 · 前半程：意图 → 提案）

> 依据：`ROUND-11-指令.md`；上游：`ROUND-10-QC.md` PASS（R10 已收口 `a726785`）。
> 设计底稿：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md` §四。
> 范围：新增**意图层**——让「目标」成一等公民（`intents` 表 + `intent_generation` 行为 + `propose_from_intent`）；
> 裁决/教学复用 R10 已落地的 `resolve_proposal`→`lessons`。自生成意图的**自动执行刻意不放开**（留 R11-b）。
> 状态：**待 WorkBuddy re-QC**。未 commit（工作树改动，符合收口红线）。

## 0. 一句话

R9/R10 把「反思 + 裁决留教」的闭环接上了，但**意图源头仍锁死**——小桔只能提注册好的动作
（`route_task` 恒 `self`），等于只会上发条。R11 补上源头：她从**已上线的河床素材**
（未满足的关怀 / 未接着的观察 / 长期兴趣）里，用第一人称长出「她自己想干的事」，落 `intents` 表，
再由 `propose_from_intent` 变成一张 **PROPOSED 提案卡**等你裁决。
**最高护栏**：自生成意图的提案**一律停 PROPOSED，绝不自动执行**（本回合只负责「想」，不负责「做」）。

## 1. 真实落点

| 文件 | 落点 | 内容 |
|---|---|---|
| `deskmate/cognitive/store.py` | `_init_schema()`（L342-357） | 新增 `intents` 表 DDL（`id/ts/goal/motivation/horizon/status/origin/proposed_task_id` + 2 索引）——**本回合唯一新表**，`CREATE TABLE IF NOT EXISTS` 非破坏性 |
| 同上 | 新增 4 个 CRUD（L1252-1328） | `add_intent`（空 goal/异常 → `""`）、`get_open_intents`（`status='open'` 倒序）、`link_intent_to_task`、`set_intent_status`；全 fail-open |
| `deskmate/agency/behaviors/intent_generation.py` | **新增**，`run_intent_generation` | 读三类素材 → `chat`（付费主模型）以第一人称起草 `goal+motivation` → 落 `intents`；7 天错峰；全链 fail-open |
| 同上 | `collect_materials` / `parse_intents` | 素材汇总（各自 fail-open）；JSON 解析（容忍代码围栏/杂文本/dict，坏项丢弃、origin 口径外回落默认、horizon 归一） |
| `deskmate/agency/engine.py` | `_run_low_risk` 新增 `intent_generation` 分支（L223-240） | 仿 `reflection`：`not generated → return None` 不刷屏 |
| 同上 | 新增 `propose_from_intent()`（L500-536） | 一条 open 意图 → PROPOSED 提案卡（`extra={intent_id, how, source:'intent', origin}`，`risk='high'`）→ `link_intent_to_task` + 置 `active`。fail-open |
| `deskmate/agency/registry.py` | 登记 `intent_generation`（L62-67） | `risk='low'`，label「起心动念」。**`intent` slug 故意不注册**（见 §2.2） |
| `deskmate/agency/settings.py` | 白名单 + 新配置段 | 白名单加 `intent_generation`；`DEFAULTS['intent_generation']={enabled,cooldown_hours:168,max_intents:3}`；dataclass/`to_dict`/`from_dict` 全接线 |
| `deskmate/memory/sync_store_sqlite.py` | `SYNC_TABLES` 尾部**仅注释**（L280-283） | 按指令 §二.5：`intents` 本回合**不进 SYNC_TABLES**（双写风险），仅标注「待萧哥拍板单写窗口后启用」 |
| `tests/test_intent_generation.py` | **新增**，22 例 | 见 §3 |

未碰：`route_task` 的 `self` 路由 / `outbound` / `visibility_gate` / `kill switch` / 前端 / token / person 注册表 / manager 注入链。

## 2. 关键决策 / 与指令的偏差

1. **「长期兴趣」降级到 `memory_anchors`**：设计底稿与指令 §二.2 的长期兴趣源自 `memory_edges`
   （P1 汇流**显式边表**）——实测该边表**尚未上线**（`deskmate/` 下无 `memory_edges`，
   只有无关的 `memory_skill_edges`）。指令允许「若 P1 未上线则降级跳过」，我选择**降级到已上线的
   `memory_anchors` 锚点主题簇**（同锚共现 ≥2 才算一簇，`GROUP BY ... HAVING c>=2`）——
   比「整类跳过」保留更多素材，且 `intent_generation.py` 只读该表、不新增依赖。
2. **`intent` 动作 slug 故意不注册进 registry**：指令 §二.4 只要求登记 `intent_generation`。
   护栏要求「自生成意图的提案一律停 PROPOSED」。做法：提案卡用独立 slug `action_type="intent"`，
   **不在白名单、未注册** → `tick()` 遍历白名单时天然不碰它，`get_spec("intent") is None`。
   这是「未注册动作 → 天然走人审」的复用，而非新造机制；`_run_low_risk` 的自动执行逻辑**零改动**。
   若要正名（让面板显示中文标签）需注册 `intent`，但那会给它「可被 tick 迭代」的口子，本回合不做。
3. **`horizon`/`origin` 由模型输出、调用方校验**：指令 §二.2 说「`horizon`/`origin` 由调用方依素材来源填」。
   实现上让模型在 JSON 里一并给出（它更清楚这条念头属于哪类），`parse_intents` 做**口径校验**：
   `origin` 不在四类内 → 回落「未接着的观察」；`horizon` 非 short/long → 归一 short。
   比「由调用方硬填」更贴合素材语义，且坏值有兜底。
4. **去重用 `_norm_goal`（去空白/标点后前 80 字）**：指令 §二.2 说幂等「宽松即可」。
   实现为落库前与现有 open 意图比对，近重复即跳过，避免同一批素材反复长出同一条。
5. **`AGENCY_INTENT_GENERATION_ENABLED` 双闸**：配置 `enabled` 与 env 变量**任一关闭即停**
   （仿 `reflection` 的 `DESKMATE_LESSONS_INJECT` 惯例），env `AGENCY_INTENT_COOLDOWN_DAYS` 按天覆盖冷却。
6. **§五「lessons 注入默认是否关」的核查**：已确认 `manager.py` L661 为
   `os.environ.get("DESKMATE_LESSONS_INJECT", "1") != "0"` —— **默认就是开（"1"）**，
   无需改默认值，「教进去就能注入」成立。
7. **跨端同步**：按指令 §二.5 **本回合不加**（双写风险），仅在 `SYNC_TABLES` 尾部留注释；
   `_validate_sync_tables_no_vectors` 无涉及。

## 3. 自带测试

- 新增 `tests/test_intent_generation.py`（**22 例**）：
  - CRUD 4 例：`add_intent` 空 goal→`""` / 往返字段 / `get_open_intents` 只取 open / `link`+`set_status`；
  - `parse_intents` 5 例：纯数组 / 代码围栏 / 前后杂文本 / 单 dict / 坏项丢弃 + origin·horizon 归一；
  - 闸门与正常路径 6 例：disabled（cfg/env 双闸）/ `no_material` / `no_provider` / `llm_error` / happy path / 去重 / 冷却；
  - 护栏 3 例：**`test_intent_action_not_in_whitelist_so_never_auto_runs`**（钉死 `intent` 未注册 + 不在白名单）；
  - 闭环 1 例：`test_adjudicating_intent_proposal_feeds_lessons`（裁决意图提案 → lessons 写入 → 可注入）。
- 定向（R11 相关 8 文件）：
  `.venv/bin/python -m pytest tests/test_intent_generation.py tests/test_p3_reflection.py tests/test_worldview_reflection.py tests/test_teaching_feedback.py tests/test_agency.py tests/test_p2_lessons.py tests/test_settings.py tests/test_sync_store_sqlite.py -q`
  → **146 passed**（含新增 22 例）。

## 4. 独立实测（脱离 pytest 夹具，`.tmp_r11_live_check.py`，跑后已删）

> 说明：tick 会执行白名单里**全部**低风险动作（含联网的 `world_search`），故实测把 L2 白名单
> 收窄到只留 `intent_generation`——既隔离外网，又正好用来验证「`intent` 动作不在白名单 → tick 绝不自动执行它」。

```
[PASS] add_intent 返回 id
[PASS] get_open_intents 取到 1 条
[PASS] run_intent_generation 落库 1 条
[PASS] 意图已进 open 池
[PASS] 提案生成且停 PROPOSED
[PASS] 提案带 source=intent 且 risk=high
[PASS] intent→task 回填 + 置 active
[PASS] intent 未注册进 registry
[PASS] intent 不在默认白名单
[PASS] tick 前白名单只有 intent_generation
[PASS] tick 不新增 intent 任务
[PASS] 原意图提案仍停 PROPOSED
[PASS] 裁决留教 1 条
[PASS] lesson 可注入
ALL LIVE CHECKS PASSED
```

对应验收判据：§三.1（第 5-6 项）、§三.2（第 11-12 项）、§三.3（第 13-14 项）。

## 5. 全量回归（受控对照，证明零新增失败）

**运行口径**（两轮完全一致，唯一差异是「是否含 R11 改动」）：
```
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m pytest -q --tb=line -rfE -p pytest_timeout_guard
```

- **当前态（R11，5 tracked 改动 + 新测试）**：`43 failed, 3822 passed, 1 skipped`
- **基线态（R10 = `git stash` R11 的 tracked 改动 + `--ignore=tests/test_intent_generation.py`）**：`44 failed, 3799 passed, 1 skipped`
- **只在当前态失败（= R11 新增回归）**：**无**（集合差为空）✅
- **只在基线失败**：`tests/test_health_producers.py::test_cron_missed`（**时间窗口相关 flaky**：cron 错过判定依赖当前时刻，基线失败、当前通过，与 R11 无关）
- 通过数差 `3822 − 3799 = 23 = 22（新增用例）+ 1（上面这条 flaky 本轮翻绿）`，账目自洽。

> ⚠️ **环境说明（非 R11 引入）**：本仓全量套件里有一个**真实联网测试**
> `tests/test_proactive_ledger.py::test_relevance_tiers` —— 它触发 `local_embed._load`
> 用 fastembed **下载 BGE 模型**（`httpx` → 外部镜像），无网络时会把整轮**永久挂死**
> （已用 `faulthandler` 抓栈定位：`socket.create_connection` 阻塞）。故两轮均加
> `HF_HUB_OFFLINE=1`（离线态下该用例 0.77s 通过，走缓存/fail-open），并挂一个**每测 90s 墙上时钟护栏**
> 临时插件防其它阻塞。（该 43/44 条失败在 R10 收口前即存在，属既有环境债，非本轮目标。）

## 6. 红线核对

- ✅ **未 commit**（改动全在工作树），不推 origin、不 `--force`。
- ✅ 唯一的 DDL 是新表 `intents`（`CREATE TABLE IF NOT EXISTS`），**无破坏性 DDL**；无新前端、无 token 改动。
- ✅ **自生成意图本回合禁止自动执行**（最高优先级）：提案 slug `intent` 未注册 + 不在白名单 + `risk='high'` + `status=PROPOSED`，`tick()` 不迭代它。
- ✅ 不碰 `route_task` 的 `self` 路由（原样）、不碰 `outbound` / `visibility_gate` / `kill switch`。
- ✅ 所有 LLM 环节 fail-open（素材缺失 / provider 缺失 / `chat` 异常 / 解析失败 / 写库异常 → 仅告警返回）；新增行为 **7 天错峰**，不刷屏。
- ✅ `intents` **未进** `SYNC_TABLES`（仅注释），符合指令 §二.5 的双写风险处置。

## 7. 遗留 / 备注

- **R11-b（指令 §五明确不做）**：自生成意图的**自动执行**、意图自动映射到未知 `action_type` 的真实执行——本回合刻意留白，护栏先定死。
- **跨端同步**：`intents` 是否进 `SYNC_TABLES` 待萧哥拍板「单写窗口」（Mac 常驻 + 云端 :02 窗口双跑 → 相似意图撞 LWW）。已在代码注释标注。
- **`memory_edges` 未上线**：长期兴趣暂用 `memory_anchors` 锚点簇代替；P1 汇流显式边表上线后可切回（`_interest_materials` 单一入口，改动面小）。
- **ROUND-10 遗留**：`ROUND-10-实施记录.md` §5（全量回归）已用本轮**基线态**（= R10 态）跑数回填。