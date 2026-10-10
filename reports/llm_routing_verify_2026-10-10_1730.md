# LLM 路由改动验证（commit 50cbce0 + 修复 76c27b8）— 2026-10-10 17:30

> 对象：fork/base 的 `free_only` 路由改动运行数小时后实际效果。
> 上一轮（16:13）结论 🔴 BROKEN（`GROUP_FREE` 未 import → NameError 崩）。本轮验证修复是否真生效。

## 结论：✅ 修复已生效，free_only 名副其实，无付费泄漏（附时间线备注）

| 核查项 | 结果 | 关键证据 |
|---|---|---|
| **① 免费组健康度** | 🟡 可用但退化 | `zen_catalog.json` 年龄 4.21h（<6h，未过期）。免费组 12 条 opencode 通道：**8 条熔断(open)**，4 条存活(mimo-v2.6-flash-free、space-bunny-free + 2 从未熔断 step-5-preview-free、ling-3.1-flash-free)。 |
| **② 自主/反思层走免费组** | ✅ 是 | 修复后（≥17:08:30）审计日志出现 `opencode:*-free` 通道调用：3 次成功(close) 经 `mimo-v2.6-flash-free`；被熔断的 opencode 通道(jev/exo/muse)被优雅 skip。7 个行为文件全部 `grep free_only=True`。 |
| **③ 付费燃烧量** | ✅ 零泄漏 | 修复后付费成功 33 次 **全部是 `xiaomi:mimo-v2.5-pro`**——属 `chat()` 主链路（本就该走付费）。free_only 路径按代码保证只走 `GROUP_FREE`(opencode)，**不可能触达 xiaomi/ark/tencent**。零来自自主/反思层。 |
| **④ 失败处理** | ✅ graceful | 免费组限流时 free_only 任务记 `skip` 审计行、不抛异常带崩整轮；`understand_observations` 等 fail-open。无 Traceback。 |
| **⑤ 云端一致性** | ✅ 一致 | `llm.py` + 7 个行为文件 md5 本地(fork/base)↔云端(/opt/openworker) **8/8 逐字节一致**（llm.py=`25be9cce…`，其余见下）。 |

## 时间线备注（重要）
- 修复 commit `76c27b8` 在 16:13 已提交，但云端日志显示 `name 'GROUP_FREE' is not defined` NameError **一直持续到 17:08:30**（pid 3643551）。说明 16:13 那次 deploy/重启当时未真正把修复码加载上（云端 llm.py 当时仍是旧 md5 `c66b6a46`）。
- 约 **17:08–17:09** 才真正同步固定码并重启生效（行为文件 mtime 17:08–17:09；当前云端 llm.py md5=`25be9cce` 与本地一致）。
- 当前进程 pid 3660953（~17:30 起）已无 NameError，仅剩 `chat()` 主链路 ark→xiaomi 的正常付费降级日志。

## 附带观察（非 bug，但影响功能）
免费组严重退化（8/12 熔断），导致 free_only 自主/反思任务**大部分时间处于 skip**，实际 LLM 加工产出很少。这是「省钱不崩」的设计意图，但意味着：免费通道集体趴窝时，小桔的后台反思/理解基本不干活。若希望后台层在免费组趴时仍有最低产出，需另议（如放宽到 1 条保底付费或本地小模型）。

## 明细证据
- 免费组 post-fix(≥17:08:30) 审计计数：
  - 成功 close：xiaomi 33（主链路付费）、opencode:mimo-v2.6-flash-free 3（免费成功）
  - 熔断 open：ark:doubao-seed-evolving 11、opencode jev/exo/muse×2 共 4
  - skip：ark doubao+deepseek 各 22、opencode jev/exo/muse 共 22
- 熔断表 `llm_circuit_breaker` 当前 opencode 状态：open=8（exo/jev/ling-3.0/longcat/muse-1.2/muse-1.3/nemotron-3-ultra/nemotron-3.5），closed=2（mimo-v2.6-flash-free/space-bunny-free），另 2 条从未熔断。
- md5 一致清单：llm.py `25be9cce`；ai_circle_column `beab726d`；ai_circle_reply `133fcf26`；ai_circle_share `e21afc84`；reflection `e62eac50`；worldview_reflection `008023f2`；understand_observations `a16aa5c0`；creative_suggest `cdc68c5c`（本地=云端）。
