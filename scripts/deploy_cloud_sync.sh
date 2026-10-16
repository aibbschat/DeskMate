#!/usr/bin/env bash
# 本地 → 云端 sync 代码自动对齐脚本（一致性红线兜底）
#
# 背景：小桔已上云，本地（仓库 deskmate/memory/sync_*.py + scripts/sync_daemon_runner.py）
# 与云端平铺包（/var/lib/openworker/.deskmate/sync/ + sync_runner.py）必须是同一份代码。
# 两端 import 已同形（云端 sync/ 是带 __init__.py 的包，模块用相对导入；runner 靠
# --cloud-pkg-path 自动选 sync.* 或 deskmate.memory.*），所以对齐 = 直接拷贝 + 清 pycache。
# 差异一旦累积，每次刷新都像一场灾难（萧哥 2026-10-05 拍板的红线）。
#
# 红线：
# - 本脚本只 scp 已提交（committed）的代码；工作区脏则拒绝（强制先 commit 进 fork/base）。
# - 绝不 push origin（只把文件推到云端服务器，不动 git remote）。
# - 云端文件属 openworker 用户，经 /tmp 中转 + sudo -u openworker cp（不直接覆盖）。
#
# 用法：
#   bash scripts/deploy_cloud_sync.sh            # 默认 host=openworker-mirror
#   SSH_HOST=myhost bash scripts/deploy_cloud_sync.sh
#   bash scripts/deploy_cloud_sync.sh --force    # 跳过「必须已提交」校验（不推荐）
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SSH_HOST="${SSH_HOST:-openworker-mirror}"
CLOUD_DESKMATE="/var/lib/openworker/.deskmate"
CLOUD_SYNC_DIR="$CLOUD_DESKMATE/sync"
CLOUD_RUNNER="$CLOUD_DESKMATE/sync_runner.py"

# 本地源文件 → 云端目标文件（一一对应）
LOCAL_MODULES=(
  "deskmate/memory/sync_transport.py"
  "deskmate/memory/sync_daemon.py"
  "deskmate/memory/sync_store_sqlite.py"
)
LOCAL_RUNNER="scripts/sync_daemon_runner.py"

# 主动产出台账模块（主程序包，不是 sync 平铺包）：随 sync 文件一起上云，
# 避免每次改 proactivity 都手动 scp。目标云端 /opt/openworker/deskmate/proactivity/。
# ⚠️ confidence.py（§11.14 should_act 闸门）+ health_producers.py（P2 三生产者）务必纳入，
# 否则云端跑旧码 → §11.14 修复与 P2 离线生效（10-07 实测：漏传 confidence.py 会让
# 「没发现异常也递卡」在云端照旧复发）。
PROACTIVITY_MODULES=(
  "deskmate/proactivity/ledger.py"
  "deskmate/proactivity/tick.py"
  "deskmate/proactivity/judge.py"
  "deskmate/proactivity/understanding.py"
  "deskmate/proactivity/server.py"
  "deskmate/proactivity/confidence.py"
  "deskmate/proactivity/health_producers.py"
  "deskmate/proactivity/disabled_producers.py"
  # ⚠️ 2026-10-13 补：companion.py 属「真实优先」活感 WIP 批，被云端代码 import，
  # 漏传 → 云端跑旧码（与 ledger/her_voice 同类漏传教训）。随 tick/ledger 同批同步。
  "deskmate/proactivity/companion.py"
)
# ⚠️ 2026-10-09 新增：disabled_producers.py（automation 误报止血模块）。
# tick.py 与 xiaoju_agency_tick.py 均 `from deskmate.proactivity.disabled_producers import ...`，
# 漏传 → 云端 tick 每轮抛 ImportError（被 except 吞掉）→ 云端**悄悄停跑所有健康生产者**，
# 且不留任何日志线索。务必与 tick.py 同批同步。

# ⚠️ 2026-10-09 新增：主链路付费组回退。
# router.py 改为 `from .paid_fallback import complete_with_fallback / stream_with_fallback`。
# 漏传 paid_fallback.py 的后果特别隐蔽：import 写在**函数体内**，故导入期不报错、
# 服务能正常启动，但**每次调用主链路都抛 ModuleNotFoundError** ⇒ 云端小桔彻底不能对话。
# router.py 与它必须同批同步（本次实测：火山周额度撞顶后必须能自动切小米/hy）。
# 两者已列入下方 CHANNEL_MODULES，复用其传输与 md5 校验循环。
# ⚠️ judge.py 必须随 tick.py 一起上云：tick.py:30 `from .judge import JudgmentResult,
# judge_proactive_action`，而云端 xiaoju_agency_tick.py:146 跑 run_producers_all → 调 tick 内的
# `_cloud_audit_producer`，该生产者用 `judgment.finding_key`（2026-10-09 提交 1729942 新增）。
# 漏传 → 云端跑旧 judge.py，JudgmentResult 无 finding_key → 锚点合并特性在云端静默失效
# （退回内容指纹），且违反「本地与云端代码须完全一致」红线。务必与 tick/ledger 同批同步。
CLOUD_PROACTIVITY_DIR="/opt/openworker/deskmate/proactivity"

# P3-c 手动 oplog GC 工具（§11.15）：随 runner 一起上云到 /var/lib/openworker/.deskmate/，
# 与 sync_runner.py 同目录（--cloud-pkg-path 默认即该目录，可 `python3 gc_sync_oplog.py --mode cloud`）。
LOCAL_GC_SCRIPT="scripts/gc_sync_oplog.py"
CLOUD_GC_SCRIPT="$CLOUD_DESKMATE/gc_sync_oplog.py"

# 云端 L2 自主层常驻 tick（路径乙·让云端也是活的副本）：随 sync 文件一起上云到
# /var/lib/openworker/.deskmate/sync/xiaoju_agency_tick.py（systemd xiaoju-agency.service
# 每小时触发，ExecStart 即该路径）。⚠️ 它内部调用 run_producers_all（P2 三健康生产者），
# 漏传会让云端自主副本永远不跑 P2（10-07 实测：deploy 脚本原本漏了它 → 云端跑旧码不递健康卡）。
LOCAL_AGENCY_TICK="scripts/xiaoju_agency_tick.py"
CLOUD_AGENCY_TICK="$CLOUD_SYNC_DIR/xiaoju_agency_tick.py"

# 云端窗口内的 drift 检测脚本（2026-10-07 三个待决落地·清单第1/3步）：Mac 日历型 tick 后
# drift 由云端窗口接管，脚本随 sync 一起上云到 sync/sync_drift_check.py，窗口在 ALIGNED 后调用
# （旁路观测，调用处必须 `|| true`，不得因它失败把整个窗口判 failed）。
# ⚠️ 漏传会让清单第 3 步「云端窗口加 drift 调用」直接 FileNotFound → 窗口报错（10-07 实测缺）。
LOCAL_DRIFT_CHECK="scripts/sync_drift_check.py"
CLOUD_DRIFT_CHECK="$CLOUD_SYNC_DIR/sync_drift_check.py"

# LLM 通道组+ 熔断（2026-10-06，设计 §二/§3）：本地与云端**必须同码同熔断表**，
# 否则会出现「本地熔断了、云端还在撞」——与记忆里「48 条身份分裂」同类的事故。
# 目标：/opt/openworker/deskmate/{memory,proactivity}/ 与 /opt/openworker/coworker/{server,providers}/
CLOUD_DESKMATE_ROOT="/opt/openworker/deskmate"
CLOUD_COWORKER_ROOT="/opt/openworker/coworker"
CHANNEL_MODULES=(
  "deskmate/llm_circuit_breaker.py"       # 熔断表 + 错误解析
  "deskmate/llm_channels.py"              # 通道组（付费/免费 + 组内自动排）
  "deskmate/memory/llm.py"                # chat/chat_memory 接入熔断与通道组
  "coworker/providers/zen_catalog.py"     # Zen 免费模型动态清单（远端驱动）
  # ⚠️ 聊天框免费模型动态刷新（2026-10-09 a6a1e54）：matrix.py 的 entry_for 接入
  # opencode 免费模型的动态清单（opencode: 前缀先查动态清单、降级冷启动/离线兜底）。
  # 它正是『云端聊天框还是老模型』的根因——原清单独漏 matrix.py，导致云端停在 10-06 旧码
  # （实测 mtime=Oct 6 10:25、a6a1e54 特征串命中 0、entry_for 在 372 行无动态分支）。
  # 漏传即云端永远跑老矩阵、免费模型不刷新且不报错，必须随 router/paid_fallback 同批同步。
  "coworker/providers/matrix.py"          # ProviderMatrix（opencode 免费模型动态刷新）
  # ⚠️ 2026-10-13 Kilo 免费车道（Our Free Model 插件移植）：免费组主力换 Kilo。两文件
  # **必须同批**，且必须随 matrix/llm_channels/memory-llm 一起上云：
  #   - deskmate/llm_channels.py:free_channels → `from coworker.providers.kilo_catalog import fetch_catalog`
  #   - deskmate/memory/llm.py:chat_memory 分派 → `_kilo_complete` → `kilo_provider.KiloProvider`
  #   - coworker/server/app.py 启动钩子 → `kilo_catalog.start_catalog_refresh`
  # 漏传 kilo_catalog → 云端免费组 fetch_catalog() 全挂 → 免费组返空 → 后台任务无通道；
  # 漏传 kilo_provider → 命中的那一路 chat() 抛 ModuleNotFoundError（熔断记账后换下一条，
  # 表象是「免费组莫名全熔断」）。两者都是静默失效，只能靠本清单闭合。
  "coworker/providers/kilo_provider.py"    # KiloProvider（免密钥 + 剥 Authorization 头）
  "coworker/providers/kilo_catalog.py"    # Kilo 免费模型动态清单（远端驱动 + 自动路由置顶）
  # ⚠️ 2026-10-11 MiMo v2.5→v2.6 切换：registry.py（recommended_model / endpoint_help）
  # 与 selection/server.py（mimo 别名 → xiaomi:mimo-v2.6）必须随 matrix 同批同步，否则
  # 云端聊天框推荐模型/别名仍指向已下架的 v2.5，且本地云端代码不一致（红线）。
  "coworker/providers/registry.py"        # Provider 注册表（recommended_model / endpoint_help）
  "deskmate/selection/server.py"          # 模型别名解析（mimo → xiaomi:mimo-v2.6）
  # ⚠️ 主链路付费组回退三件套（2026-10-09 新增）：实测火山周额度撞顶 429 时，
  # router.py 裸奔（不查熔断、不换通道）导致主聊天每 35 秒撞一次墙，而熔断器在旁边
  # 正确记账——两套配置脱节。修法是把跨套餐回退（火山→小米→腾讯 hy3）接进 router。
  # 三者**必须同批**：router import paid_fallback，且 import 写在函数体内，
  # 漏传时服务照常启动但每次调用都 ModuleNotFoundError ⇒ 云端小桔彻底不能对话。
  # ⚠️ 2026-10-15 补：dynamic_route.py（P0 动态路由 judge 层）。router.py L24 顶层
  # `from .dynamic_route import ...` 直接引用它——漏传 ⇒ 云端 router.py 导入期即崩 ⇒
  # openworker-server 起不来。它位于 coworker/providers/，而本脚本全量对账只扫
  # deskmate + coworker/server（扫不到 providers/，属对账盲区），故必须显式列此清单
  # 才能推上去；否则对账永远假绿。务必随 router.py 同批。
  "coworker/providers/dynamic_route.py"     # P0 动态路由 judge 层（router.py 顶层 import）
  "coworker/providers/router.py"           # ProviderRouter（已接入 paid_fallback）
  "coworker/providers/paid_fallback.py"   # 付费组跨套餐回退 + 熔断记账
  "coworker/server/app.py"                # /v1/llm-channels* 端点 + LLM 专用池/超时中间件
  "coworker/server/run.py"                # uvicorn 启动入口（access log 时间戳，2026-10-13）
  # ⚠️ 2026-10-13 补录：coworker/server/manager.py **此前只在集合级对账里被检查、
  # 从未被传输**（对账查≠推得上去）—— 云端因此长期跑旧 manager.py。本轮它新增了
  # `model_vendors`（picker 按厂商分组的数据源），漏推的后果是「同步显示成功、云端
  # 选择器仍按旧两档平铺」，且**不报错**，只能靠对账抓。
  # 它必须与 matrix.py / registry.py 同批：三者共同决定 picker 的模型清单与分组。
  # ⚠️ 云端 openworker-server 每轮经 manager.py 构建小桔上下文，改动语义面很宽，
  # 推完务必确认 systemctl 状态（当前为「停机保号」态，重开即生效）。
  "coworker/server/manager.py"            # SessionManager（每轮上下文 + 模型清单/分组）
  # ⚠️ 主程序包的同步三件套（2026-10-06 补）：云端 `/opt/openworker/deskmate/memory/`
  # 另有一份 sync_store_sqlite.py，而上面的 CHANNEL_MODULES 里的 llm.py 正是 import它。
  # 只同步平铺包（LOCAL_MODULES）会导致「主程序包跑旧 SYNC_TABLES」——
  # 实测踩过：migrate() 抛 KeyError: 'llm_circuit_breaker'，熔断表建了但没触发器/同步列。
  "deskmate/memory/sync_store_sqlite.py"
  "deskmate/memory/sync_daemon.py"
  "deskmate/memory/sync_transport.py"
  # ⚠️ 每轮上下文注入（§11.16 层一）：build_full_context_for_turn 挂「今天已递待办」块；
  # 云端聊天（openworker-server 走 manager.py 构建小桔每轮上下文）必须同码，否则小桔
  # 在云端「不知道自己递过什么」。2026-10-07 之前 deploy 脚本漏了它，云端 manager.py
  # 停在 Sep 30 旧码——本轮必须把层一注入推上去（红线：本地与云端代码须完全一致）。
  "deskmate/memory/manager.py"
  # ⚠️ P1 汇流最小版（2026-10-07）：认知层两件套。store.py 新增 memory_anchors 表 +
  # recall_items 顺锚走一跳；extractor.py 抽取时顺手挂锚。云端 openworker-server 每轮
  # 经 memory_injector → CognitiveStore.recall_items 构建上下文；漏传 → 云端跑旧码
  # （无锚表 / 不顺锚），本地同步上去的锚数据也召不回邻座（同 manager.py 漏传教训）。
  "deskmate/cognitive/store.py"
  "deskmate/cognitive/extractor.py"
  # ⚠️ P2-R3 lessons 注入（2026-10-07）：memory_injector.py 含 build_lessons_context，
  # manager.py L662 函数内 `from ... import build_lessons_context` 直接引用它。
  # 漏传 → 云端旧 memory_injector 无此函数，R3 lessons 注入因 fail-open 静默失效
  # （小桔在云端「学不会」你教的规矩）。本轮补足，确保 P2-R3 云端真生效。
  "deskmate/cognitive/memory_injector.py"
  # ⚠️ 用户事实回灌（c06ab7e / 2026-10-08）：manager.py:642 与 understanding.py:161
  # 都 `from deskmate.memory.user_fact_context import build_user_fact_context`。
  # 本清单是**逐文件显式列举**，加新文件若忘了往这里加 → 云端静默缺文件，
  # 那两处 import 抛 ImportError 被 fail-open 吞掉 ⇒ 云端小桔「不知道自己知道」
  # （库里存着的事实/日记回灌不进理解与聊天），且**不报错**，只能靠对账发现。
  # 2026-10-09 实测：集合级对账（211 共有文件 0 漂移）抓出本条唯一差异。
  "deskmate/memory/user_fact_context.py"
  # ⚠️ P2 情绪底色（2026-10-11）：小桔自己的连续内在情绪引擎。
  # manager.py（L616 注入 + L2774 after-turn hook）和 tick.py（L249 衰减 + L527 注入）
  # 均 import 它。漏传 → 云端 manager/tick 跑旧码，情绪注入因 fail-open 静默失效
  # （小桔在云端「没有自己的天气」），且**不报错**（同 user_fact_context 漏传教训）。
  "deskmate/growth/emotion_engine.py"
  # 活感第4层（2026-10-13）：想法流存储模块。emotion_engine.describe_turn_context
  # 每轮 import 它召回「最近在想的事」；reflection/worldview_reflection 写入。
  # 漏传 → 云端 ImportError（非 fail-open 吞掉的静默失效，会直接崩注入块）→ 小桔
  # 在云端「没有自己的想法」。务必随 emotion_engine.py 同批上云。
  "deskmate/thought_stream.py"
  # 活感第3层（2026-10-13）：小桔自己的声音来源。moments/companion/diary/ledger 四处兜底
  # 与陪伴语都 import 它取「她写过的第一人称原句」；漏传 → 云端 import 崩（非 fail-open
  # 吞掉的静默失效），小桔在云端 LLM 不可用时退回客服模板（失去真实优先）。务必随上方
  # 四文件同批上云。
  "deskmate/growth/her_voice.py"
  # ⚠️ 认知访谈/验证引擎（2026-10-11 补）：interview.py（③b 深度访谈闭环）与
  # validation_engine.py（认知 Turing test 验证）均被云端代码 import 且云端真实跑——
  #   - proactivity/tick.py:362   `from deskmate.cognitive.interview import maybe_trigger_interview`
  #   - proactivity/tick.py:427   `from deskmate.cognitive.validation_engine import run_validation`
  #   - coworker/server/app.py:3762 `from deskmate.cognitive.validation_engine import run_validation`
  # 原清单漏了这两文件 → 云端一直跑 9月/10月旧码：「永不换题」(generate_scenario 永远
  # 取最高置信第一条+template[0])、验证答对「沉淀新认知」静默丢弃（apply_feedback 抽取失败
  # 只告警不落）、访谈回答抽取失败静默丢弃（_absorb_response 仅 llm_not_available 才兜底）
  # 三处 2026-10-11 修复全没生效。这正是「同步成功≠云端完整」陷阱：清单逐文件列举，
  # 漏了就云端缺/旧且不报错（同 user_fact_context 漏传教训）。现补入，确保认知引擎
  # 本地与云端同码（红线）。
  "deskmate/cognitive/interview.py"
  "deskmate/cognitive/validation_engine.py"
  # ⚠️ 情绪态常量单一真相源（P0#1 / commit e710796）原子部署单元：deskmate/constants/ 是
  # 新包（云端此前根本没有），emotion_profile.py / emotion_sense.py / emotion_timeline.py
  # 已重构为 `from deskmate.constants.emotion import ...`。三者必须和**新包同批**上云——
  # 只推任一部分，云端要么 ImportError 崩、要么继续跑重构前旧码（旧码不依赖 constants）。
  # 这 5 个文件是集合级对账发现的**存量漂移**（非本轮改动）：云端缺 constants 包、3 个
  # 情绪文件停留在重构前旧版。收掉才满足「本地与云端代码须完全一致」红线。本地
  # cognitive(92)+growth(131) 全绿已验证该重构行为保持，上云即与本地真相源对齐。
  "deskmate/constants/__init__.py"
  "deskmate/constants/emotion.py"
  "deskmate/cognitive/emotion_profile.py"
  "deskmate/growth/emotion_sense.py"
  "deskmate/growth/emotion_timeline.py"
)

# 小桔手记行为（ai_circle_column.py）：含「专属表情 [xj:id] 封闭词表」注入（2026-10-07
# 247d4e2）。此前 deploy 清单不含 agency/behaviors/ 包，导致云端跑 10-01 整包旧码、手记
# 无表情块。现纳入同步 → 目标 /opt/openworker/deskmate/agency/behaviors/（与 CHANNEL_MODULES
# 同款 deskmate/* 落点 + 清 __pycache__ + md5 核对）。⚠️ 该文件改动须先 commit 进 fork/base，
# 否则预检会拒（与清单内其它文件同一红线）。
AGENCY_BEHAVIORS_MODULES=(
  "deskmate/agency/behaviors/ai_circle_column.py"
  # R9 反思层 R1（2026-10-08）：新增反思行为
  "deskmate/agency/behaviors/reflection.py"
  # R9/R10：世界观反思行为（也调 record_reflection）
  "deskmate/agency/behaviors/worldview_reflection.py"
  # R11 意图层（2026-10-08）：起心动念行为
  "deskmate/agency/behaviors/intent_generation.py"
  # R12 出海口后半程（2026-10-08）：意图执行器（放行后真做出来；护栏钉死）
  "deskmate/agency/behaviors/intent_executor.py"
  # R13 意图讨论流（2026-10-08）：start/run/finalize/cancel + 反馈分类；
  # /v1/deskmate/agency/intent/discuss/* 端点 lazy import 本模块——漏传会让云端端点
  # ImportError → 500（与上面同类漏传事故）。必须随行为包一起上云。
  "deskmate/agency/behaviors/intent_discuss.py"
  # 2026-10-10 免费优先路由（free_only）：以下 4 个自主/反思行为文件同步纳入，
  # 否则云端跑旧码（不带 free_only）→ 本地与云端不一致（"同步成功≠云端完整"陷阱）。
  "deskmate/agency/behaviors/ai_circle_share.py"
  "deskmate/agency/behaviors/ai_circle_reply.py"
  "deskmate/agency/behaviors/creative_suggest.py"
  "deskmate/agency/behaviors/understand_observations.py"
)

# 自主层核心（engine / registry / settings）：R9 反思 + R10 教学回流 + R11 意图层 + R12 意图执行
# 全改在这三文件。云端 openworker-server 与 xiaoju_agency_tick 经
# /opt/openworker/deskmate/agency/ 加载；漏传 → 云端跑旧码（反思/教学/意图层/执行
# 在云端不生效，与 P2/P1 漏传教训同类事故）。目标 /opt/openworker/deskmate/agency/。
AGENCY_MODULES=(
  "deskmate/agency/engine.py"
  "deskmate/agency/registry.py"
  "deskmate/agency/settings.py"
)

# 花园私密层（P2 花园 / 个人中心数据源）：maybe_bloom 性能根治（2026-10-13 新事件闸门）在此。
# deskmate/garden 整个包相互 import（clock → flowers/season/state，flowers → state），必须整包
# 同批上云，否则云端跑旧码 → 花园端点 ImportError 被 fail-open 吞掉 → 个人中心/全息屏在云端
# 「取不到数」且不报错（同 user_fact_context 漏传教训，10-09 实测）。
# ⚠️ 本清单逐文件显式列举，漏加即静默不同步（"同步成功≠云端完整"陷阱，2026-10-13 补入）。
GARDEN_MODULES=(
  "deskmate/garden/__init__.py"
  "deskmate/garden/flowers.py"
  "deskmate/garden/state.py"
  "deskmate/garden/clock.py"
  "deskmate/garden/season.py"
  "deskmate/garden/storybook.py"
)

# 成长/日记/人格核心（2026-10-13 补）：发日记真回复异步落库 + 单条 GET 委托 + 「真实优先」活感 WIP。
# 这些文件被云端 openworker-server 运行时 import：app.py 的 /v1/deskmate/moments/* 端点直接调用
# MomentsStore.get_post（定义在 moments.py，经 diary_store.py 落地）；diary.py/persona.py 是「真实优先」
# 活感 WIP。漏传 → 云端复活后该端点 AttributeError 崩 / 云端跑旧人格码（与 user_fact_context 同类
# 「同步成功≠云端完整」陷阱）。务必随 app.py 同批同步，否则云端与本地代码不一致（红线）。
GROWTH_MODULES=(
  "deskmate/growth/moments.py"
  "deskmate/growth/diary_store.py"
  "deskmate/growth/diary.py"
  "deskmate/persona.py"
)

# 天气感知（2026-10-16 补）：weather.py 被 manager.py:649（聊天注入 weather_context）与
# tick.py:263（apply_weather_mood 情绪通道）import——云端 openworker-server 每轮上下文与
# 云端 agency tick 都会走到。**10-15 天气感知落地时它不在任何清单里**：集合级对账只
# 「报告」不「传输」（manager.py 同款教训），实测云端 /opt/openworker/deskmate/perception/
# 根本没有 weather.py ⇒ 那处 import 一直 ImportError 被 fail-open 吞掉 ⇒ 云端小桔
# 「没有天气」且不报错。目标 /opt/openworker/deskmate/perception/。
PERCEPTION_MODULES=(
  "deskmate/perception/weather.py"
)

# 办公技能工具（2026-10-15 P1 原子替换+恢复副本）：office_safe_deliver.py 被
# office-word/excel/pptx 技能的交付步经 `python -m deskmate.skills.tools.office_safe_deliver`
# 调用；云端自主副本（xiaoju_agency_tick）经仓库 cwd 跑同一条命令，故必须随主链路同批上云，
# 否则云端技能交付步 ImportError（"同步成功≠云端完整"陷阱，与 user_fact_context 同类漏传事故）。
SKILLS_TOOLS_MODULES=(
  "deskmate/skills/tools/office_safe_deliver.py"
  # 2026-10-15 补：seed_office.py（办公技能幂等 seed / ensure_office_skills）。虽当前未被
  # 云端运行时 import（纯 seed 脚本，对账「本地独有」告警即它），但补入可消除告警、并让云端
  # 具备同款 seed 能力；随 office_safe_deliver.py 同批上云到 /opt/openworker/deskmate/skills/。
  "deskmate/skills/seed_office.py"
)

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

echo "==> 仓库根: $REPO_ROOT"
echo "==> 云端 host: $SSH_HOST"
echo "==> 云端同步包: $CLOUD_SYNC_DIR + $CLOUD_RUNNER"

# ---- 0. 连通性 + 云端路径校验 ----
if ! ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_HOST" "test -d '$CLOUD_SYNC_DIR' && test -f '$CLOUD_RUNNER'" >/dev/null 2>&1; then
  echo "✗ 无法连到 $SSH_HOST 或云端同步包路径不存在（$CLOUD_SYNC_DIR / $CLOUD_RUNNER）" >&2
  exit 1
fi

# ---- 1. 必须已提交（红线：本地改动先 commit 进 fork/base）----
ALL_FILES=("${LOCAL_MODULES[@]}" "$LOCAL_RUNNER" "${PROACTIVITY_MODULES[@]}" "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}" "${AGENCY_MODULES[@]}" "${GARDEN_MODULES[@]}" "${GROWTH_MODULES[@]}" "${SKILLS_TOOLS_MODULES[@]}" "${PERCEPTION_MODULES[@]}" "$LOCAL_AGENCY_TICK" "$LOCAL_DRIFT_CHECK")
if [[ "$FORCE" -eq 0 ]]; then
  DIRTY=""
  for f in "${ALL_FILES[@]}"; do
    if ! git diff --quiet -- "$f" 2>/dev/null; then
      DIRTY="$DIRTY  $f (工作区未提交)\n"
    fi
    if ! git diff --cached --quiet -- "$f" 2>/dev/null; then
      DIRTY="$DIRTY  $f (已暂存未提交)\n"
    fi
    # 文件不在 HEAD（全新未跟踪）也算脏
    if ! git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      DIRTY="$DIRTY  $f (未纳入版本控制)\n"
    fi
  done
  if [[ -n "$DIRTY" ]]; then
    echo "✗ 以下同步文件尚未提交，先 commit 进 fork/base 再对齐（或用 --force 绕过）：" >&2
    printf "%b" "$DIRTY" >&2
    exit 1
  fi
  echo "==> 预检通过：sync 模块 + runner + gc 工具 + proactivity 模块 + 通道/熔断模块 + 云端 L2 agency tick + 云端 drift 脚本均为已提交状态"
else
  echo "==> --force：跳过「必须已提交」校验（仅在你明确知道在做什么时用）"
fi

# ---- 2. 语法预检（本地）----
echo "==> 本地语法预检"
for f in "${ALL_FILES[@]}"; do
  python3 -c "import ast,sys; ast.parse(open('$f', encoding='utf-8').read())" \
    || { echo "✗ 语法错误: $f" >&2; exit 1; }
done
# AGENCY_MODULES 也过一遍语法（上面 ALL_FILES 已含，这里冗余保险）
for f in "${AGENCY_MODULES[@]}"; do
  python3 -c "import ast,sys; ast.parse(open('$f', encoding='utf-8').read())" \
    || { echo "✗ 语法错误: $f" >&2; exit 1; }
done

# ---- 3. scp 经 /tmp 中转 + sudo -u openworker cp ----
# 注意：scp 把 /tmp 中转文件建成登录用户（ubuntu）所有，故清理用登录用户直接 rm；
# 落到云端的 cp 必须 sudo -u openworker（目标文件属 openworker）。两步分开，互不牵连。
echo "==> 推送模块文件到云端 $CLOUD_SYNC_DIR"
for f in "${LOCAL_MODULES[@]}"; do
  base="$(basename "$f")"
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/sync_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo chmod 644 /tmp/sync_deploy_$base || true"   # scp 以 ubuntu 写入，默认 0600；openworker 无读权限（实测 Permission denied）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_$base' '$CLOUD_SYNC_DIR/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_$base'" || true
  echo "    ✓ $base"
done

echo "==> 推送 runner 到云端 $CLOUD_RUNNER"
scp -q -o ConnectTimeout=15 "$LOCAL_RUNNER" "$SSH_HOST:/tmp/sync_deploy_runner.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_runner.py' '$CLOUD_RUNNER'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_runner.py'" || true
echo "    ✓ sync_runner.py"

echo "==> 推送 GC 工具到云端 $CLOUD_GC_SCRIPT"
scp -q -o ConnectTimeout=15 "$LOCAL_GC_SCRIPT" "$SSH_HOST:/tmp/sync_deploy_gc.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/sync_deploy_gc.py' '$CLOUD_GC_SCRIPT'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/sync_deploy_gc.py'" || true
echo "    ✓ gc_sync_oplog.py"

echo "==> 推送云端 L2 自主 tick 到云端 $CLOUD_AGENCY_TICK"
scp -q -o ConnectTimeout=15 "$LOCAL_AGENCY_TICK" "$SSH_HOST:/tmp/agency_deploy_tick.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/agency_deploy_tick.py' '$CLOUD_AGENCY_TICK'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/agency_deploy_tick.py'" || true
echo "    ✓ xiaoju_agency_tick.py"

echo "==> 推送云端 drift 检测脚本到云端 $CLOUD_DRIFT_CHECK"
scp -q -o ConnectTimeout=15 "$LOCAL_DRIFT_CHECK" "$SSH_HOST:/tmp/drift_deploy_check.py"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/drift_deploy_check.py' '$CLOUD_DRIFT_CHECK'"
ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/drift_deploy_check.py'" || true
echo "    ✓ sync_drift_check.py"

# ---- 3b. 推送 proactivity 模块到云端主程序包 ----
echo "==> 推送 proactivity 模块到云端 $CLOUD_PROACTIVITY_DIR"
for f in "${PROACTIVITY_MODULES[@]}"; do
  base="$(basename "$f")"
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/proactivity_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo chmod 644 /tmp/proactivity_deploy_$base || true"   # scp 以 ubuntu 写入，默认 0600；openworker 无读权限（实测 Permission denied）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/proactivity_deploy_$base' '$CLOUD_PROACTIVITY_DIR/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/proactivity_deploy_$base'" || true
  # 清 proactivity 的 __pycache__，避免旧 .pyc 复用（与手动 scp 同风险）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$CLOUD_PROACTIVITY_DIR/__pycache__'" || true
  echo "    ✓ $base"
done

# ---- 3c. 推送通道组/ 熔断模块（本地与云端必须同码，见 CHANNEL_MODULES 注释）----
echo "==> 推送通道组/熔断模块 + 自主行为包 + 天气感知到云端主程序包"
for f in "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}" "${AGENCY_MODULES[@]}" "${GARDEN_MODULES[@]}" "${GROWTH_MODULES[@]}" "${SKILLS_TOOLS_MODULES[@]}" "${PERCEPTION_MODULES[@]}"; do
  base="$(basename "$f")"
  # 目标目录按本地相对路径推（deskmate/xxx.py → $CLOUD_DESKMATE_ROOT/xxx.py；
  # coworker/xxx.py → $CLOUD_COWORKER_ROOT/xxx.py），必要时建目录
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  scp -q -o ConnectTimeout=15 "$f" "$SSH_HOST:/tmp/channel_deploy_$base"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo chmod 644 /tmp/channel_deploy_$base || true"   # scp 以 ubuntu 写入，默认 0600；openworker 无读权限（实测 Permission denied）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker mkdir -p '$dest_dir'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker cp '/tmp/channel_deploy_$base' '$dest_dir/$base'"
  ssh -o ConnectTimeout=15 "$SSH_HOST" "rm -f '/tmp/channel_deploy_$base'" || true
  # 清该目录 __pycache__（否则旧 .pyc 复用 = 云端跑旧码，10-05 已踩）
  ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$dest_dir/__pycache__'" || true
  echo "    ✓ $f → $dest_dir"
done

# ---- 4. 清云端 __pycache__（强制下次窗口重编译新码）----
echo "==> 清云端 __pycache__"
ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker rm -rf '$CLOUD_SYNC_DIR/__pycache__' && echo '    ✓ cleared'"

# ---- 5. 摘要（md5 对齐核对）----
echo "==> 对齐核对（本地 vs 云端 md5）"
for f in "${LOCAL_MODULES[@]}"; do
  base="$(basename "$f")"
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_SYNC_DIR/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done
local_md5="$(md5 -q "$LOCAL_RUNNER")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_RUNNER' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ sync_runner.py  $local_md5"
else
  echo "    ✗ sync_runner.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi
local_md5="$(md5 -q "$LOCAL_GC_SCRIPT")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_GC_SCRIPT' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ gc_sync_oplog.py  $local_md5"
else
  echo "    ✗ gc_sync_oplog.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi
local_md5="$(md5 -q "$LOCAL_AGENCY_TICK")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_AGENCY_TICK' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ xiaoju_agency_tick.py  $local_md5"
else
  echo "    ✗ xiaoju_agency_tick.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi
local_md5="$(md5 -q "$LOCAL_DRIFT_CHECK")"
cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_DRIFT_CHECK' 2>/dev/null | awk '{print \$1}'")"
if [[ "$local_md5" == "$cloud_md5" ]]; then
  echo "    ✓ sync_drift_check.py  $local_md5"
else
  echo "    ✗ sync_drift_check.py  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
  exit 1
fi

# ---- 6. proactivity 模块对齐核对 ----
echo "==> proactivity 模块对齐核对"
for f in "${PROACTIVITY_MODULES[@]}"; do
  base="$(basename "$f")"
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$CLOUD_PROACTIVITY_DIR/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done

# ---- 7. 通道组/熔断模块对齐核对 ----
echo "==> 通道组/熔断模块 + 自主行为包 + 天气感知对齐核对"
for f in "${CHANNEL_MODULES[@]}" "${AGENCY_BEHAVIORS_MODULES[@]}" "${AGENCY_MODULES[@]}" "${GARDEN_MODULES[@]}" "${GROWTH_MODULES[@]}" "${SKILLS_TOOLS_MODULES[@]}" "${PERCEPTION_MODULES[@]}"; do
  base="$(basename "$f")"
  case "$f" in
    deskmate/*)   dest_dir="$CLOUD_DESKMATE_ROOT/$(dirname "${f#deskmate/}")" ;;
    coworker/*)   dest_dir="$CLOUD_COWORKER_ROOT/$(dirname "${f#coworker/}")" ;;
    *)            dest_dir="$CLOUD_DESKMATE_ROOT" ;;
  esac
  local_md5="$(md5 -q "$f")"
  cloud_md5="$(ssh -o ConnectTimeout=15 "$SSH_HOST" "sudo -n -u openworker md5sum '$dest_dir/$base' 2>/dev/null | awk '{print \$1}'")"
  if [[ "$local_md5" == "$cloud_md5" ]]; then
    echo "    ✓ $base  $local_md5"
  else
    echo "    ✗ $base  本地=$local_md5 云端=$cloud_md5  (不一致!)" >&2
    exit 1
  fi
done

echo ""
echo "✅ 本地→云端代码已对齐（3 个 sync 文件 + runner + gc 工具 + 6 个 proactivity + 5 个通道/熔断模块 + 3 个 agency 核心 + 5 个 agency 行为 + 天气感知 + 云端 L2 agency tick + 云端 drift 脚本）。"

# ---- 对账（2026-10-09 加）：本清单是逐文件显式列举，漏加即静默不同步 ----
# 起因：user_fact_context.py（c06ab7e）被 manager.py/understanding.py import 却不在
# 清单里，云端缺文件 → ImportError 被 fail-open 吞掉，「小桔不知道她自己知道」且**不报错**。
# 同步成功≠云端完整；必须做集合级对账（md5 全量比对），别只信脚本自称。
echo ""
echo "==> 对账：本地 vs 云端 md5 全量比对"
_reconcile() {
  local tmp_l tmp_c
  tmp_l="$(mktemp)"; tmp_c="$(mktemp)"
  # ⚠️ 对账范围 = **生产代码**（2026-10-13 修）。原先把 `test_*.py` 也算进来，
  # 而云端只跑 openworker-server + 同步窗口、**从不跑测试** ⇒ 这5 个测试文件永远
  # 「不一致」，把真漏项埋进噪音里（本次即4 真差异 + 5 测试噪音混在一起）。
  # 判据：只有**会被云端 import 的模块**才可能造成静默失效，测试文件不属于此类。
  # 测试文件差异仍会打印（供人工知悉），但不参与红绿判定。
  local XTRA='-not -name test_*.py'
  # 本地：deskmate/coworker 全量 .py（抓「本地有、云端没有」的漏项）
  ( cd "$REPO_ROOT" && find deskmate coworker/server -name '*.py' $XTRA -not -path '*__pycache__*' -type f \
      | while read -r f; do printf '%s  %s\n' "$(md5 -q "$f")" "$f"; done ) | sort > "$tmp_l"
  ssh "$SSH_HOST" "sudo find /opt/openworker/deskmate /opt/openworker/coworker/server -name '*.py' $XTRA -not -path '*__pycache__*' -type f 2>/dev/null | while read -r f; do printf '%s  %s\n' \"\$(sudo md5sum \"\$f\" 2>/dev/null | cut -d' ' -f1)\" \"\${f#/opt/openworker/}\"; done" 2>/dev/null | grep -vE "Warning|setlocale" | sort > "$tmp_c"
  local only_l only_c diff
  only_l="$(comm -23 <(cut -d' ' -f3- "$tmp_l" | sort) <(cut -d' ' -f3- "$tmp_c" | sort) | wc -l | tr -d ' ')"
  only_c="$(comm -13 <(cut -d' ' -f3- "$tmp_l" | sort) <(cut -d' ' -f3- "$tmp_c" | sort) | wc -l | tr -d ' ')"
  diff="$(join -j 2 -o 0,1.1,2.1 <(sort -k2 "$tmp_l") <(sort -k2 "$tmp_c") 2>/dev/null \
          | awk '$2 != $3' | wc -l | tr -d ' ')"
  echo "   共有文件内容不一致: $diff"
  echo "   仅本地有（云端缺，可能静默失效）: $only_l"
  echo "   仅云端有（云端有旧漂移）: $only_c"
  # 测试文件差异：云端不跑测试，故仅提示不判红（列出来供人工知悉）。
  local _t
  _t="$( { comm -3 <(cd "$REPO_ROOT" && find deskmate coworker/server -name 'test_*.py' -not -path '*__pycache__*' -type f | sort) \
             <(ssh "$SSH_HOST" "sudo find /opt/openworker/deskmate /opt/openworker/coworker/server -name 'test_*.py' -not -path '*__pycache__*' -type f 2>/dev/null | sed 's|^|/opt/openworker/|'" 2>/dev/null | grep -vE "Warning|setlocale" | sed 's|^/opt/openworker/||' | sort) ; } 2>/dev/null | wc -l | tr -d ' ')"
  [ "$_t" != "0" ] && echo "   （提示）测试文件差异 $_t 处 —— 云端不跑测试，不影响运行"
  if [ "$diff" != "0" ] || [ "$only_c" != "0" ]; then
    echo "   ❌ 对账未通过——上面两项非 0 即云端与本地不一致，请逐条查（红线：须完全一致）"
  elif [ "$only_l" != "0" ]; then
    echo "   ⚠️ 有本地独有文件未上云。若它们被云端代码 import，跑到那处会 ImportError"
    echo "      被 fail-open 吞掉（功能静默失效且不报错）。须补进本脚本清单后重跑。"
  else
    echo "   ✅ 对账通过：云端与本地完全一致"
  fi
  rm -f "$tmp_l" "$tmp_c"
}
_reconcile
echo "   同步平铺包仅每小时 :02 窗口加载；主程序包需**重启 openworker-server** 才生效："
echo "     sudo systemctl restart openworker-server"
echo "   ⚠️ 通道/熔断模块若与云端不一致，会出现「本地熔断了、云端还在撞」"
echo "      ——与记忆里「48 条身份分裂」同类事故，故务必跑本脚本对齐。"
echo "   如需立即生效，可手动跑一次：sudo /usr/local/sbin/xiaoju-sync-window.sh"
