# LLM 路由改动验证报告（commit 50cbce0 / 修复 76c27b8）

- **验证时间**：2026-10-10 19:36 GMT+8
- **对象**：fork/base 上 `free_only` 路由改动（chat_memory 新增 free_only 形参；7 个自主/反思行为 free_only=True；云端已重启加载新码）
- **运行窗口**：修复 76c27b8 于 ~17:08–17:30 生效，本次核查覆盖修复后 ~2.5h（17:30–19:36）
- **结论**：✅ **正常**，自主/反思层名副其实只走免费组，无付费泄漏、无崩溃、失败优雅跳过。

## 一、简明结论表

| 核查项 | 状态 | 关键证据 |
|---|---|---|
| 免费组健康度 | 🟡 退化但可用 | 12 条 opencode 通道：≥4 可用（mimo-v2.6-flash-free + space-bunny-free 已闭合；step-5-preview-free / ling-3.1-flash-free 未试用），8 条熔断 |
| 自主/反思层走免费组 | ✅ 名副其实 | 云端代码 7 处 `free_only=True` 已落地；审计日志 FREE 组 close 5 次、skip 23 次，零 ark/xiaomi/tencent 调用 |
| 付费额度燃烧 | ✅ 零泄漏 | 付费组 close 全为 `xiaomi:mimo-v2.5-pro`（123 次，chat 主链路），free_only 代码路径只进 GROUP_FREE，不可能触达付费 |
| 异常处理 | ✅ 优雅 | 免费组熔断时记 `skip`（"熔断中"）不崩；understand_observations 异常 `return []`（fail-open）；17:30 后 journal 零 Traceback |
| 云端代码一致性 | ✅ 8/8 一致 | llm.py + 7 行为文件 md5 本地↔云端逐字节相同（25be9cce 等） |

## 二、关键证据

### 1. 免费组健康（云端熔断表 `llm_circuit_breaker`，/var/lib/openworker/.deskmate/deskmate.db）
- 可用（state=closed）：`opencode:mimo-v2.6-flash-free`、`opencode:space-bunny-free`
- 未试用（无熔断行，默认可用）：`step-5-preview-free`、`ling-3.1-flash-free`
- 熔断（open）：jev-1.13-free / exo-free / muse-spark-1.3 / muse-spark-1.2 / longcat-2.5-preview / ling-3.0-flash-fin / nemotron-3-ultra / nemotron-3.5-lightning（8 条）
- 付费组：ark-agent-plan-cn 三通道全 `quota_exhausted`（火山额度耗尽）；`xiaomi:mimo-v2.5-pro`、`tencent-tokenplan:hy3` 健康
- 路由 `route(GROUP_FREE)` 过滤 is_open，故实际可用免费通道 ≥4 条。

### 2. 自主/反思层只走免费组（审计日志 /var/lib/openworker/.deskmate/llm_channel_audit.jsonl，窗口 17:30–19:36）
- FREE 组事件：`close 5`（全部 `opencode:mimo-v2.6-flash-free`）、`skip 23`、`open 4`
- PAID 组事件：`close 123`（**全部** `xiaomi:mimo-v2.5-pro`）、`open 12`（ark 主链路尝试失败→降级）、`skip 226`（ark 熔断跳过）
- FREE close 时间戳样本：17:33:50 / 17:36:06 / 17:36:10 / 17:36:28 / 18:45:18
- 结论：付费组 close 全部来自 chat 主链路（`memory_group()` 内部降级 ark→xiaomi），**无一条来自 free_only 行为**。

### 3. 失败处理（graceful skip + fail-open）
- FREE skip 样本：`{"event":"skip","channel":"opencode:exo-free","detail":"熔断中"}`（18:45:09）
- `understand_observations.py:257/273/278-284` 异常均 `return []`（fail-open），且该批 `free_only=True`（行 295）。
- journal（openworker-server，17:30+）过滤 RateLimitError/401 后**无任何 Traceback/Exception**。

### 4. 云端一致性（md5 本地↔云端）
| 文件 | md5 |
|---|---|
| deskmate/memory/llm.py | 25be9cce091c30da67fd628164d9e98f |
| agency/behaviors/ai_circle_share.py | e21afc8479854a332326d9f719fd0719 |
| agency/behaviors/ai_circle_reply.py | 133fcf268f521074c0dc10575f5f156a |
| agency/behaviors/ai_circle_column.py | beab726d46f7bde1d3eb232cdff99ec6 |
| agency/behaviors/reflection.py | e62eac5077940780c3d6be44df6d66d2 |
| agency/behaviors/worldview_reflection.py | 008023f25e487e4bccc3f401d5cab05d |
| agency/behaviors/creative_suggest.py | cdc68c5c5d2908a8f05c4201d20e55ff |
| agency/behaviors/understand_observations.py | a16aa5c057fc2333bf4a62a8dcd6dbd4 |

本地工作树与云端逐字节一致；llm.py:527 已正确 `from deskmate.llm_channels import GROUP_FREE, ...`；llm.py:589 `group = GROUP_FREE if free_only else memory_group()`。

## 三、附带观察（非阻断）
- **zen_catalog 已过期**：云端 9.73h、本地 6.29h（>6h TTL）。路由仍用缓存 12 条模型正常工作，但不会自动拾取新上架的免费模型；opencode 远端刷新似乎未触发，建议后续顺手查一下 refresh 逻辑。
- **免费组退化效应**：因 8/12 免费通道熔断，自主/反思层大部分轮次 graceful skip（23 次），实际后台加工产出很少——这是设计意图（省额度优先），但功能上近乎停工。一旦免费通道恢复即自动复用。
