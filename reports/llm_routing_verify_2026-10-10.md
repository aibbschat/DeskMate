# LLM 路由改动验证报告（2026-10-10，commit 50cbce0）

**结论先行：🔴 BROKEN（不是"免费优先生效"，是功能失效）**

`chat_memory()` 在 `deskmate/memory/llm.py` 引用了未导入的常量 `GROUP_FREE`，导致所有
7 个 `free_only=True` 的自主/反思行为在**发起任何 LLM 调用之前**就抛 `NameError`。
它们既没有"只走免费组"，也没有"偷偷烧付费"——而是每轮空转崩溃、无产出。

---

## 简明核查表

| 核查项 | 结论 | 关键证据 |
|---|---|---|
| **免费组健康度** | ⚠️ 严重退化（非代码问题） | `zen_catalog.json` 新鲜（13:18 拉取，未超 6h TTL）；但 10 条 opencode 免费通道中 **8 条熔断**，仅 `mimo-v2.6-flash-free` + `space-bunny-free` 存活（本地/云端 breaker 表一致） |
| **自主/反思层状态** | 🔴 失效（NameError，未走免费组） | `ai_circle_column` 云端每 ~50s 报一次 `name 'GROUP_FREE' is not defined`（14:06:43 起，共 124 次至 16:28 仍持续）；`ai_circle_reply` 同错以 TaskGroup 形式抛出。7 个行为共用同一 `chat_memory` 入口，代码级全崩 |
| **付费额度燃烧** | ✅ 这 7 行为未烧付费（但原因是崩了，不是 free_only 生效） | `group_fallback(free→paid)` 本地/云端窗口内均 =0；付费组 `ark-agent-plan-cn` 三通道 `quota_exhausted`（火山额度耗尽），`xiaomi`/`tencent` 健康——付费消耗来自主对话 `chat()`，与这 7 行为无关 |
| **异常/失败处理** | 🔴 违反设计 | 设计意图：免费组全趴时 `free_only` 任务 **graceful skip**（记 skip 审计、不崩）。实际：`chat_memory` 因 `GROUP_FREE` 未定义直接 `NameError`，属代码 bug 而非优雅降级；行为层 catch 后无产出并持续空转重试 |

---

## 根因（代码级，已本地+云端双确认）

`deskmate/memory/llm.py`：

- **L527**（导入）：`from deskmate.llm_channels import GROUP_PAID, memory_group, route`
  —— **漏了 `GROUP_FREE`**
- **L589**（使用）：`group = GROUP_FREE if free_only else memory_group()`
  —— 引用未定义常量 → `NameError: name 'GROUP_FREE' is not defined`

7 个行为均调用 `chat_memory(free_only=True)`（已 grep 确认）：
`ai_circle_share / ai_circle_reply / ai_circle_column / reflection / worldview_reflection /
creative_suggest / understand_observations`。任一调用都在 L589 处崩，根本没进入 `route()`
选路逻辑，所以既不会走 opencode 免费通道，也不会触发付费兜底。

> 注：`understand_observations` 的 fail-open（`except: return []`，L297-298 / L322-324）仍然生效——
> 它把 NameError 吞成"无理解结果"，所以不会带崩整轮，但本批观察理解直接丢失。

---

## 云端一致性（核查步骤 5）

- ✅ 代码 md5 一致：本地 `llm.py` = 云端 `/opt/openworker/deskmate/memory/llm.py`
  = `c66b6a46f0ff1bcfd3c991b0ac1386b7`（**两端都含此 bug**）。
- ✅ 4 个新增 agency 文件已同步进 `scripts/deploy_cloud_sync.sh`
  （`ai_circle_share.py` / `ai_circle_reply.py` / `creative_suggest.py` /
  `understand_observations.py`，脚本 L167-170），`llm.py` 在 `CHANNEL_MODULES`（L98）同步。
- ⚠️ 一致性通过，但同步上去的是**坏代码**——"同步成功 ≠ 云端完整/正确"再次应验。
- 云端服务 `openworker-server.service` 已于 11:57 重载新码（目录 mtime 对齐）。

---

## 免费组健康度细节（本地=云端，breaker 表 `llm_circuit_breaker`）

- **免费组 OPEN（熔断）**：`opencode:jev-1.13-free`、`muse-spark-1.3-contributor-free`、
  `muse-spark-1.2-contributor-free`、`longcat-2.5-preview-free`、`exo-free`、
  `ling-3.0-flash-fin-free`、`nemotron-3-ultra-free`、`nemotron-3.5-lightning-free`
- **免费组健康**：`opencode:mimo-v2.6-flash-free`、`opencode:space-bunny-free`
- **付费组 OPEN**：`ark-agent-plan-cn:deepseek-v4-flash` / `doubao-seed-evolving` /
  `doubao-seed-2.1-turbo` 均 `quota_exhausted`（火山 Agent Plan 周额度耗尽）
- **付费组健康**：`xiaomi:mimo-v2.5-pro`、`tencent-tokenplan:hy3`

审计日志（`llm_channel_audit.jsonl`）窗口内事件：skip 为主，免费通道有少量 `open`（撞墙）
与 `close`（成功恢复，集中在存活的 2 条）；付费组 `ark` 全 skip，`xiaomi`/`tencent` 有 close。

---

## 修复建议（一行）

`deskmate/memory/llm.py:527` 改为：

```python
from deskmate.llm_channels import GROUP_FREE, GROUP_PAID, memory_group, route
```

随后：本地先 `git commit` 进 fork/base → 跑 `scripts/deploy_cloud_sync.sh` → 云端
`sudo systemctl restart openworker-server` 加载新码。修复后建议复查审计日志确认
`ai_circle_*` 不再刷 NameError，且免费通道有正常 `close`（成功）记录。

> 本验证任务未改动任何代码（自动任务仅做核查）。修复需萧哥确认后由人工或单独任务执行。
