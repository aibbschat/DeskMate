# openworker — 研究笔记

> 这个文件由脑洞官插件在每次写入时重新生成。**不要手改**，改动会被覆盖。
> 最后更新：2026-11-20T14:14:43.933Z

## 待解矛盾（2 条开着 / 3 条合计）

| # | 矛盾 | 为什么是矛盾 | 要什么才能解 |
|---|---|---|---|
| C2 | 想按场景分流到更快通道，但跨通道切换会因 reasoning_content 契约直接 400；且当前通道池大面积失效 | 11-20 21:18:06 切到 deepseek 官方后立刻 400：『The reasoning_content in the thinking mode must be passed back to the API』——DeepSeek thinking 模式要求把历史 assistant 的 reasoning_content 原样回传；而 coworker/providers/base.py:63-65 明确把 reasoning 设计成 display-only sidecar（只落库展示、不回传），engine 出站消息里没有任何 reasoning_content 映射。于是『会话中途切去 deepseek』是确定性失败，不是网络慢、重试无用；用户看到的『转圈』实为反复被拒。同一窗口/mimo-v2.6-pro strike=42 quota exhausted、ark deepseek-v4-flash strike=113 network error、tencent hy3 403、opencode-go 多通道 400 全灭。 | 先做一次小实验：在 openai_provider 出站侧，把历史 assistant 的 reasoning 按目标通道契约映射成 reasoning_content（配对：仅当该轮开了 thinking 才回传），用测试钉死；再用同一会话切 deepseek 跑一轮，验证不再 400。结论落地后再谈按场景分流。 |
| C3 | 工具执行偶发吃满 120s 硬超时（UI 表现为「转圈两分钟」），命令本身 0.09 秒 | 持久 shell 的 marker 同步在部分命令后会失步：命令已输出完，但 trailer marker 迟迟不到，执行器一路等到 _DEFAULT_TIMEOUT=120.0（coworker/tools/shell.py:47），SIGINT 打断后才收尾返回。用户看到的是 model 已答完、界面「Waiting for agent...」空转两分钟，且看不到任何进度。已排除模型侧：同窗口 deepseek 连续 5 次调用首字 0.8-1.3s、1-5s 返回。 | 读 coworker/tools/shell.py 的 marker/trailer 协议与 _execute_sync 路径，复现「命令已退出但 marker 未到」的场景（重点：后台子进程继承 stdout、命令未 flush、上一条超时后的会话失步）；给出先红后绿契约测试，并在工具执行侧加可见进度与 idle watchdog，避免静默站满 120 秒。 |

## 已否决 / 已采纳（8 条）

| 点子 | 结论 | 理由 |
|---|---|---|
| 补长篇连续性 / 自动维护人物圣经 | reject | 已有圣经机制，每天会记；萧哥 2026-11-15 确认长篇连续性没问题，不构成待解矛盾。 |
| 以「本地优先」作为对外差异化方向 | reject | 云端已经跑通，只是暂时没开；本地/云端不是待决问题，别拿它当卖点论证。 |
| 本轮续做巨型文件架构重构：SessionManager/EmotionEngine/TeamStore 类主体拆 mixin、app.py 325 路由模块化、api.ts 剩余域续抽 | defer | 萧哥 2026-11-19 确认先不做。三项均为纯结构改动、无运行时收益：①类主体拆 mixin 动 MRO/patch 路径，实测 EmotionEngine 32 处/9 文件、TeamStore 22 处/8 文件测试耦合，且本轮 get_engine 一行改动即造成 4 个真回归；②325 路由全捕获 manager 闭包，模块化=重写注册架构，风险最高；③api.ts 续抽三类中风险最低（barrel 模式已验证、tsc 0 错/485 passed），但仍无性能收益。性能优化本轮已净消除 4 回归、零新增红，见好就收。若将来做须独立立项、单独分支 + HEAD 基线同口径失败清单 + 先红后绿护栏，不可并入性能 PR。 |
| 把小桔大脑做成底座无关、可整体迁移的资产（一键导出/导入 + 逐步收敛脑体接口），为将来从 OpenWorker 搬家保底 | accept | 萧哥 2026-11-20 拍板：AI 发展快，搬家是早晚的事，不能一直吊在 OpenWorker 上。三路实测代码论证：①数据资产（~/.deskmate/deskmate.db 49 表 + 散文件 + 每本书 entities.db）可用 sqlite3 .backup 整体搬；②纯逻辑层（persona/cognitive store/will/feeling_store/diary/ledger/writing_context/file_reader）多为零 LLM、只认 DESKMATE_DIR，可原样搬；③接口层（LLM咽喉/engine/persona注入/skill/scheduler/session·WS）与底座双向绑定（coworker 反向 import deskmate 6 处）须重写适配。结论：大脑可搬、身体要重造；好消息是 LLM 调用收敛到 deskmate/memory/llm.py 单咽喉且全 fail-open，坏消息是底座反向依赖要摘。三个陷阱：profile_items 是 layer 共享表（cognitive/emotional/feeling/worldview 同表）、意志层肌肉在 growth/engine.py 的 5 个 modulate_*、向量须各端本地重算禁跨端搬运。配套行动已排日程：一键 export/import CLI（任务 #1，含 manifest+sha256+.backup+散文件白名单+entities.db+本地重建索引），密钥与史料库绝不打包。 |
| 「小桔比 Hermes 慢 10 倍」的根因是往返次数 × 单次生成吞吐，不是上下文总量；先做通道 A/B，不动底座 | accept | 实测 11-20 14:18 巡检任务（__run__run-bdcc290ba7，23.4min）：43 次工具调用、41 次为单发单工具，每步后必跟一整轮模型往返；时间线中 >100s 的空档 5 处合计约 11min（单轮最长 196.9s，对账 llm_usage：14:23:24 fc=4.8s stream=196.5s out=6052 → 30 tok/s）；期间 compaction 触发 4 次，每次额外一轮 LLM。llm_usage 近2日真实账本：mimo-v2.6-flash（479 次）首字 6.6s / 30-42 tok/s；deepseek 官方（52 次）首字 4.1s / ~86 tok/s；opencode.ai/go（105 次）首字 11.9s。Hermes config.yaml: provider=volcengine-coding, model=deepseek-v3-2。结论：底座换掉两个乘数都不变。 |
| 继续在 OpenWorker 内靠「砍上下文」提速 | reject | 《上下文总预算实验报告_2026-11-20》A/B 已证：可裁部分仅 193 字（1.3%），原文 tool result 与人称底座是豁免项。PR0-PR21 已把 token/缓存方向吃干，继续投入边际收益趋零。真正未动的是每步往返后的整轮重算与模型 decode 速度。 |
| 把「上游 stall 自动重试一次 + 三类超时收紧」作为读文件转圈的第一刀 | accept | 萧哥 2026-11-20 拍板「治好，不要让我选」。改动：①timeouts.py 首字 75→25s、流 idle 120→45s；②shell.py _DEFAULT_TIMEOUT 120→30s；③engine._loop 在 ProviderStall 且本轮零输出时自动重试一次（只一次，防死循环），notice kind=stall_retry。理由：实测 deepseek 正常首字 0.9-1.4s、hy4 5-7s，25s 已是 3 倍余量；工具层两次异常分别 121.0s/123.1s，正好等于旧 120s 硬超时。测试：tests/test_engine_stall_retry.py 三例 + test_shell/test_engine_empty_turn 共 18 passed，宽回归 68 passed。未解决：沙箱内单条命令为何真跑 121 秒（exit_code=0，非超时路径），LocalExecutor 本地复现仅 0.04s。 |
| 「切 DeepSeek 首字超时」是假象，真因是 reasoning_content 契约 400；出站层按目标通道决定是否回传 reasoning | accept | 实测 22:12:16 deepseek-flash 在 433 毫秒内就返回 400（The reasoning_content in the thinking mode must be passed back to the API），不是 25s 首字超时。根因：assistant 消息的 reasoning 是 display-only sidecar，出站时只被 _strip_foreign_sidecars 处理（不剥 reasoning、也不改名），DeepSeek thinking 模式要求以 reasoning_content 回传。修法：openai_provider 新增 _prepare_messages，base_url 含 api.deepseek.com 时把 assistant.reasoning 改名 reasoning_content，其它网关一律剥掉。测试 tests/test_deepseek_reasoning_passthrough.py 4 例 + provider 回归 38 passed。教训：上一轮只治了超时阈值就宣布治好，漏了这条确定性 400。 |

## 观察清单（2 条）

- 小桔主力 mimo-v2.6-flash 的 decode 速度 30-42 tok/s，是 Hermes（火山 deepseek-v3-2）的 1/2~1/3；且任务型 automation 中 41/43 轮为单工具调用，无并行。后台 proactive/judge 并发 2-3 路时单次 throughput 再降约 29%（实测 42→30 tok/s）。　→ 升级条件：若同一任务在 deepseek/ark 通道上墙钟 < 1/2 且产出可用，则升级为「按场景分流模型」方向：任务型走最快通道、创作对话保留 mimo。
- 通道池体检（11-20 21:20）：hy3 403 free-tier unavailable / strike 54；opencode-go:mimo-v2.6-pro strike 226、deepseek-v4.1-flash strike 122、glm-5.3-flash strike 189 全部 open 熔断；ark deepseek-v4-flash 113 次 network error；xiaomi mimo-pro quota exhausted；mimo-flash 主体可用但 decode 30-42 tok/s。　→ 升级条件：若有连续 30 分钟单通道零失败，可把它定为『任务型默认通道』；否则先修重试/退避与首字超时的用户可见反馈。

## 轮次（2 份）

- 2026-11-15　第零轮 账本落位与边界　`.brainstorm/rounds/2026-11-15-第零轮-账本落位与边界.md`
- 2026-11-20　小桔大脑可移植性：搬家是早晚的事　`docs/brainstorm/rounds/2026-11-20-小桔大脑可移植性.md`
