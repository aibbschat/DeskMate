# ROUND-09-QC（反思层 R1 · re-QC）

> 依据：`ROUND-09-指令.md` + Trae《ROUND-09-实施记录.md》；上游：R1/R2/R3 已收口（commit `1c7fbef`）。
> 方法：读真实 diff/代码 + 跑测试 + 独立实测，不采信"完成"二字、不采信自述未验项。
> 结论：**PASS（带 1 个观察点 + 1 个存量债标注）**。

## 0. 一句话

R9 反思层 R1 真闭环——六落点全部真实落地（新 `reflection.py` + `engine` 分支 + `settings` 配置 + `registry` 注册 + `store` 裁决读写 + `memory_injector` 注入 + `manager` 注入块），**硬断言 reason 空作废双保险**、**复用 `item_history` 零 DDL**、**fail-open 全链路**、**自循环防护到位**。测试 **111 passed 零回归** + 独立实测 `parse_verdicts` 4 例全 PASS。

## 1. 改动面（git status，未 commit，符合收口红线）

| 文件 | 性质 | R9 落点 |
|---|---|---|
| `deskmate/agency/behaviors/reflection.py` | 新增 | 反思主逻辑 `run_reflection` + `parse_verdicts` |
| `deskmate/agency/behaviors/worldview_reflection.py` | 改 | `_persist_worldview` 增 `record_reflection` 留痕（worldview 维度） |
| `deskmate/agency/engine.py` | 改 | `_run_low_risk` L203-221 加 `reflection` 分支 |
| `deskmate/agency/registry.py` | 改 | L56-61 登记 `reflection`（risk=low） |
| `deskmate/agency/settings.py` | 改 | `reflection` 配置段 + dataclass + to/from_dict |
| `deskmate/cognitive/store.py` | 改 | `record_reflection` + `get_reflections_for_injection`（L1087-1166） |
| `deskmate/cognitive/memory_injector.py` | 改 | `build_reflection_context`（L405-429） |
| `deskmate/memory/manager.py` | 改 | 注入块 L669-681 |
| `tests/test_p3_reflection.py` | 新增 | 7 例 |

非 R9 混入（工作树遗留，不在 R9 验收范围）：豆包表情包前端 `Emote.tsx`/`Markdown.tsx`/`styles.css`/`Cargo.lock`、`:memory:.ses`（垃圾）、`design/`（设计源稿）、`surfaces/gui/dist.tar.gz`（打包中间产物）。

## 2. 六大落点实测核对（Grep + Read 实锤）

| # | 落点 | 真实代码 | 核对 |
|---|---|---|---|
| A | `reflection.py` | L1-343 完整 `run_reflection`：读 `item_history` 变化→LLM 裁决→落痕；`parse_verdicts` 正则解析 | ✅ |
| B | `engine` 分支 | L203-210 `if action_type=="reflection": run_reflection(str(base_dir), settings.reflection)`；`not reflected → return None`（不刷屏） | ✅ |
| C | `settings` | L96 白名单含 `reflection`；L121-129 `{cooldown_hours:168,max_recent:20,min_changes:3}`；L201/L222/L259-265/L339 全链路 | ✅ |
| D | `store` | L1089 `record_reflection`（硬断言 reason 空返回 False 不写）、L1127 `get_reflections_for_injection(limit=3,char_cap=800)` | ✅ |
| E | `memory_injector` | L405 `build_reflection_context`：仿 lessons、空库空串、fail-open | ✅ |
| F | `manager` | L669-681 注入块：`DESKMATE_REFLECTION_INJECT=0` 单独关、`DESKMATE_AUTO_INJECT=0` 总关、fail-open | ✅ |

接线：`registry.py` L56-61 登记 `reflection`（risk=low）——**指令外必要动作**，Trae 已在实施记录 §2.1 说明（`engine.tick` 先 `get_spec`，未注册即 `skipped_unknown` 走不到 `_run_low_risk`）。

## 3. 测试 + 独立实测

- **pytest 111 passed 零回归**：`test_p3_reflection.py` 7 + `test_p1_anchor_recall` 4 + `test_p2_known_persons` 12 + `test_p2_lessons` 9 + `test_deskmate_local_extractor` 24 + `test_sync_store_sqlite` 40 + `test_settings` 15。`test_p3_reflection` 覆盖：空 reason 作废、落 reflected 痕、空库 `[]`、最近优先+char_cap、manager 端到端注入+开关、fail-open。
- **独立实测 `parse_verdicts` 4 例全 PASS**（脱离夹具）：①正常 2 条含 reason ✅；②缺理由→丢弃(0) ✅；③越界编号→丢弃(0) ✅；④思维链噪声→空(0, **不假成功**) ✅。

## 4. 🟡 观察点（非 blocker）：反思裁决走 `chat_memory`

`reflection.py:289` 调 `chat_memory`（免费记忆组，撞付费配额会降级到 opencode 推理模型）。与昨天 seed 撞墙的区别：

- seed 解析 **JSON** → 推理模型思维链无 JSON → 假成功（把无人物当无人物跳过）；
- reflection 解析 **文本正则行**（编号/裁决/理由）→ 即使降级，最坏 `parse_verdicts` 返回 0 → `bailed` → **不落痕、不假成功**（独立实测 ④ 证实）；
- 调用量极小（默认 7 天一次，每次 1 次 LLM）→ 基本不撞配额，99% 走付费主模型干净返回。

**结论**：观察点，非 blocker。若日后发现反思频繁 `bailed`（日志 `reason=llm_error` 密集），建议改 `chat`（付费）或 OmniRoute 普通模型（`auto/best-coding`），不动架构。

## 5. 🟡 存量债（非 R9 引入）：`test_worldview_reflection.py` 2 failed

**独立验证**：跑 `test_worldview_reflection.py` → `2 failed, 17 passed`。失败根因日志明确：
`WARNING ... table profile_items has no column named sync_uuid`（L502/534/546）。
- 这是 **R1 同步表引入的存量夹具债**（测试未给 `profile_items` 跑 sync 列 migrate），与 R9 无关；
- 17 passed 证明世界观回望主逻辑未被 R9 破坏；
- R9 新增的 `worldview_reflection._persist_worldview` 留痕调用（L514-518，reason 非空含日期+believe）在 `persist` 失败时被 fail-open 跳过——失败在 persist 阶段（sync_uuid），不在 R9 的 `record_reflection` 阶段。
- **建议（非阻塞）**：统一在相关测试 fixture 对 `profile_items` 跑 `sss.migrate`（R1-R3 历次 QC 反复撞此坑），一次性还清技术债。

## 6. 偏差记录（Trae 自述 vs 代码，已逐条核对一致）

1. **registry 必须登记**：✅ 已核对 L56-61，否则白名单加了也不生效（指令遗漏，已补）。
2. **`subject_kind` 落 `source` 列**：✅ 真实表无 `subject_kind` 列，`record_reflection` L1115-1119 把 `subject_kind` 写进 `source`；由 `profile_items.layer` 派生（worldview→worldview，其余→profile_item）。
3. **不自循环**：✅ `_CHANGE_ACTIONS`（L50-53）**故意不含 `reflected`**，反思不读自己的痕。
4. **世界观也落痕**：✅ L514-518 真实存在，reason 非空，不自循环。

## 7. 红线核对

- ✅ 只改反思层六处 + registry 接线 + worldview 留痕 + 测试；未改 judge 核心、manager 其他注入块、前端、`recall_items`/`extractor`/person 注册表。
- ✅ 复用 `item_history`，**零 DDL 不新开表**；不碰 SYNC 同步表（反思痕不需跨端，本地即可）。
- ✅ `reason` 空 → `record_reflection` 返回 False 且不写痕（硬断言，落代码拦）。
- ✅ 所有新 LLM 调用 fail-open；不新增向量。
- ✅ 未 commit（HEAD 仍 `1c7fbef`）。

## 8. 下一步

R9 可纳入收口 commit（与豆包表情包前端增量 + ROUND-09 两文档一并进 `fork/base`，红线：禁 push origin / 禁 --force）。收口后本地桌面重打包 + 云端 `deploy_cloud_sync.sh` 对齐即生效（反思仅改后端 Python，无新表无新前端，部署与 P2 同链路）。

P3 后续子任务（江河设计第二段全会）：反馈自动写入 teachings（R3 已落手动入口）、反思 LLM 裁决调优、错峰频度实测——留待后续轮次。
