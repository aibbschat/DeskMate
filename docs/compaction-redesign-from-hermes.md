# 上下文压缩（compaction）重构方案 — 借鉴 Hermes 设计

> 背景：萧哥反馈「每问一个问题都弹 Context compaction failed 窗、长任务反复弹几次、压缩后丢内容」。
> 已确诊问题（实证会话 `eef171de-5e7`，~/.config/coworker/conversations/）。本文在摸清 Hermes
> Agent 压缩设计（`/Users/xiaocheng/.hermes/hermes-agent`，6179+9246 行核心代码）后，给出借鉴式修复方向。
> **本文只定方向，不含代码改动。**

---

## 1. 我们的现状（已确诊的坑，均带行号）

| 现象 | 根因（代码实证） |
|---|---|
| 反复弹窗 | `_compact_now` 的 `while True` Retry 循环（`engine.py:657-680`）+ 每轮 `_compaction_due()` 触发 + 摘要器持续失败（时段性 429 高度吻合）。用户点 Retry 失败就再弹 |
| 越压越大（丢内容） | 兜底 `trim_state` 按**消息条数**剪 10%（`compaction.py:544` `step = max(1, int(remaining * fraction))`）。token 大头在少数几条巨型 tool result，条数剪不掉 → 上下文 42万→57万 反升 |
| 失败黑箱 | `engine.py:654` `except Exception: failed=True` 吞光异常，无日志、无 notice |
| 无冷却/防抖 | 摘要失败立刻重试+弹窗，没有任何 cooldown / anti-thrash 闸门 |
| 截断风险 | 摘要 `summarize_span` 传 `max_tokens=3000`（`compaction.py:473`） |
| 压缩模型无回退 | `engine.py:636` `cfg.get("model")` 取了 pin 但失败就 raise，没有回退主模型 |
| 无有效性校验 | 用 `estimate_tokens` 粗估触发，压缩后不校验真实 token 是否真下降 → 死亡螺旋 |

---

## 2. Hermes 怎么做的（精读结论，带行号）

三层职责：`conversation_compression.py`（对话级编排）+ `context_compressor.py`（微压缩引擎，
运行时两层）；`trajectory_compressor.py` 是离线训练数据后处理，不接入主循环。

**核心设计思想：**

1. **真实 token 用量校验，而非消息条数**
   `ContextCompressor.update_from_response`（`context_compressor.py:3768`）在收到 provider 真实
   `prompt_tokens` 后，若仍 `>= threshold` 则记一次 ineffective 打击；`record_rejected_compaction`
   （`2948`）在 commit 前若摘要会让 transcript 变大则丢弃原样。→ 根治「越压越大/无限循环」。

2. **cooldown 阶梯 + 持久化 + 手动强制重试**
   瞬态失败冷却 `_TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)s`（`context_compressor.py:3158`），
   持久化到 session DB（`record_compression_failure_cooldown` `3106`）。`/compress force` 清冷却立即重试
   （`compress` `8115`）。→ 既不反复弹窗，也不永久锁死。

3. **anti-thrash 防无效循环，带恢复探针而非硬禁用**
   `_ineffective_compression_count >= 2` 触发（`4087`）；`_ANTI_THRASH_RECOVERY_SECONDS = 300s`
   （`3335`）后放一次试探（`4110-4113`）；计数持久化、重启不解除守卫（#54923）。→ 防抖但不把不可压缩会话判死刑。

4. **辅助模型失败先回退主模型，默认不丢消息**
   `auxiliary.compression.model` 独立通道（`agent_init.py:2416`）；失败 `_fallback_to_main_for_compression`
   （`5131`）回退主模型立即重试（`5766`）。`abort_on_summary_failure=False`（默认 `2300`）时
   失败不中止，插入**确定性静态回退摘要**（`_build_static_fallback_summary` `8553`，丢中间窗口但不无限重试）；
   命中终态错误（鉴权/配额/网络断流/空内容/截断）则**中止压缩、原样保留会话、不丢消息**（`8433-8526`）。

5. **摘要不传 max_tokens + 提交栅栏不阻塞主回答**
   `_generate_summary` 刻意不传 `max_tokens`（`5511-5520`），仅 prompt 级「Target ~N tokens」引导；
   空/截断输出当失败（`5589-5611`）。`CompressionCommitFence`（`conversation_compression.py:680`）+ 流式进度钩子
   让慢摘要模型只在真正静默时被杀；preflight 用真实用量校准延迟（`turn_context.py:1132` `_preflight_deferred`）。

用户交互：不是「Retry/Trim」弹窗，而是终端/日志警告 + 持久化 locales 文案（如 `aux_failed` 提示已用主模型恢复、上下文完整）。

---

## 3. 现状 vs Hermes 逐项对比

| 维度 | 我们（openworker） | Hermes | 差距 |
|---|---|---|---|
| 触发判定 | `estimate_tokens` 粗估 >= 阈值即触发 | 真实 `prompt_tokens` + 冷却/anti-thrash 闸门 | 我们无闸门，每轮都触发 |
| 兜底裁剪 | 按**消息条数**剪 10%（`compaction.py:544`） | Phase1 先无 LLM 剪旧 tool result；micro-compaction 滚动摘要 | 我们按条数→越压越大 |
| 摘要模型 | 主模型或 pin 的 model，**无回退** | `auxiliary.compression.model` 独立；失败回退主模型 | 我们无回退 |
| 摘要 max_tokens | 3000（截断风险） | 不传，prompt 级引导 | 我们可能截断 |
| 失败处理 | `except` 吞光→trim 丢内容→弹 Retry 窗 | cooldown + 默认不丢消息(abort 优先) + 回退 | 我们黑箱+弹窗循环 |
| 防反复 | 无 | cooldown(60/300/900s 持久化) + anti-thrash 恢复探针(300s) | 我们无 |
| 有效性校验 | 无（死亡螺旋） | 真实 token 用量校验 + 拒绝变大的候选 | 我们无 |
| 阻塞主回答 | 同步 `asyncio.to_thread` | 提交栅栏 + 流式进度 + preflight 真实用量校准延迟 | 我们尚可但不精细 |

---

## 4. 借鉴的修复方向（按优先级）

> 借鉴 ≠ 照搬。Hermes 是 6000+ 行工业级实现；我们取其**核心思想**做最小可用改造。

### P0 — 急救：止住「反复弹窗 + 越压越大」（小改动、低风险）

- **P0-1 加 cooldown 闸门**
  摘要器失败后进入阶梯冷却（60→300→900s），冷却期内 `_compaction_due()` 直接返回 False，不再重试/弹窗；
  手动 force 可清冷却。状态持久化（session DB 或 prefs），跨进程存活。
  - 涉及：`engine.py` `_compaction_due` / `_compact_now`；新增 `compaction.py` 冷却状态。
  - 借鉴：Hermes `_TIMEOUT_COOLDOWN_LADDER` + `record_compression_failure_cooldown`。

- **P0-2 兜底裁剪改按 token**
  `trim_state` 不再按条数剪 10%，改为按 token 预算推进边界，优先跨过巨型 tool result
  （复用 `pick_boundary` 已有的 token 逻辑，`compaction.py:169`）。
  - 涉及：`compaction.py` `trim_state`。
  - 借鉴：Hermes Phase1 `_prune_old_tool_results` + 按 token 预算保护尾。

- **P0-3 失败可见、不阻断**
  摘要失败写日志 + 给用户**一次非阻断** notice（「压缩摘要失败，已临时裁剪；可在设置指定独立压缩模型」），
  **删除 `while True` Retry 弹窗循环**（`engine.py:657-680`）。
  - 涉及：`engine.py` `_compact_now`。
  - 借鉴：Hermes `aux_failed` / `summary_failed` 文案（警告而非阻断式弹窗）。

### P1 — 根治：消灭死亡螺旋

- **P1-1 真实 token 有效性校验**
  压缩后对比 provider 真实 `prompt_tokens`；仍 `>= threshold` 记 ineffective；连续 2 次触发 anti-thrash
  （停自动压缩 + 给 notice）。不靠条数判断。
  - 涉及：`engine.py` 接 `usage`；`compaction.py` 加 ineffective 计数。
  - 借鉴：Hermes `update_from_response` `3768` + `record_rejected_compaction` `2948`。

- **P1-2 压缩模型失败回退主模型**
  配置项启用独立 `compression.model`（可指备用端点，如 DeepSeek 官方 API）；失败先回退主模型重试；
  都不行才降级 trim（且不弹窗）。
  - 涉及：`compaction.py` `build_state` / `summarize_span`；`engine.py`。
  - 借鉴：Hermes `_fallback_to_main_for_compression` `5131`。

### P2 — 健壮性（顺手做）

- **P2-1** 摘要不传 `max_tokens`（或设大值），空/截断输出当失败处理（借鉴 `5511-5520` / `5589-5611`）。
- **P2-2** abort 优先于占位符：默认不丢消息（保留原会话），除非显式配置 `abort_on_summary_failure`
  （借鉴 `8433-8526`）。极端情况需配合「上下文超硬上限 warning」提示用户手动 /reset。

---

## 5. 落地节奏建议

1. **先做 P0**（3 个小改动）→ 直接止住最痛的两个现象（反复弹窗、越压越大），风险最低。
2. **再做 P1**（真实 token 校验 + 回退）→ 彻底根治死亡螺旋。
3. **P2** 顺手补健壮性。

---

## 6. 待拍板问题

1. **是否启用独立压缩模型**？启用则需指定备用端点（避免和主对话抢同一配额，否则回退也救不了）。
   候选：DeepSeek 官方 API（deepseek-v4-pro）、OmniRoute 免费模型。
2. **cooldown 默认值**？建议 60/300/900s 阶梯（与 Hermes 一致）。
3. **是否接受「默认不丢消息（abort 优先）」**？代价是极端长会话可能涨到硬上限，需配合超上限 warning。
4. **P0 是否足够**？还是直接 P0+P1 一次做到位（改动量更大但一劳永逸）。
