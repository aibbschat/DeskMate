# openworker 二开(桌搭/DeskMate)长期备忘
> 只留**红线判据·路径·开关·禁做**（代码里推不出来的）。细则/历史全文在 `.workbuddy/memory/PITFALLS.md`（§二十八=11-17 下沉，§二十九=11-22 压缩前存档）；架构 `docs/项目架构与模块备忘_2026-09-02.md`；过程见按日日志。新内容优先写 PITFALLS，判据本身变化才改本文件。

## 一、发布 / 部署（违反即事故）
- Git：origin 绝不 push；只推 backup=`https://aibbschat@github.com/aibbschat/DeskMate.git`（禁 --force/rewrite）；main=上游只 merge；推前确认 fast-forward（backup/main 是 HEAD 祖先），推 `HEAD:main`。SSH key 绑旧账号 ⇒ HTTPS + 钥匙串 PAT（`aibbschat`），临时 PAT 用 `-c http.extraheader`，绝不写 `.git/config`。
- 🔴 推送代理：`~/.gitconfig` 的 127.0.0.1:7897 是死端口，直连超时；用 `git -c http.https://github.com.proxy="$HTTPS_PROXY" -c http.version=HTTP/1.1 push backup HEAD:main`（代理每会话轮换，禁写死）。
- 本地↔云端：commit → `scripts/deploy_cloud_sync.sh`（预检已 commit→scp /tmp→sudo cp→清 pycache→md5）；前端另跑 `deploy_web.sh`。🔴 `MAIN_SET` 自动发现全量生产 .py ⇒ 不上云必登记 `MAIN_EXCLUDE` 并写理由（当前空）；三项对账（不一致/仅本地/仅云端）全判红、非零退出。
- 停机保号：disable+stop `openworker-server`/`xiaoju-agency.timer`/`caddy`；保留 `xiaoju-sync-window.timer`（每小时 :02、零 LLM）；复活先 `systemctl list-units --all` 认真名。
- Web：重打包 App ≠ 前端上云；云端 `/opt/openworker/frontend` 只能 deploy_web.sh；停机期按云端逐文件 md5 对本地 dist/（排除 runtime-config.json，0 差异）。`deploy_web.sh` 末尾 PWA 检查必然 exit 1，别为刷绿启 caddy；token 禁构建时烘焙，dev 只放 `.env.development.local`。

## 二、数据 / 路径安全
- 路径守卫唯一 `deskmate/sacred_paths.py`；含 `.deskmate/` 时原稿正文绝不直改写/删；禁 rm -rf 数据目录；副本迁移 `sqlite3 .backup`；测试库 /tmp。持久化原子写（`growth/_atomic.py#unique_tmp`）+ 多入口 fcntl.flock（`memory/_fs_lock.py`）。
- API key `~/.config/coworker/secrets.json:provider:<vendor>`（0600）；改默认模型先退 app 再改 `prefs.json:default_model`。
- 统一库 `~/.deskmate/deskmate.db`；圣经索引 `<小说目录>/.deskmate/entities.db`；产出 `/Users/xiaocheng/OpenWorker/<project-id>/`；Automations 真身 `~/.config/coworker/automation.db:scheduled_tasks`（30s 生效）。技能两套 `~/.deskmate/skills/`、`~/.config/coworker/skills`；加了技能≠会用，须在 instructions 点名。
- 🔴 已写正文=唯一事实基准：记忆/大纲/圆桌与正文冲突以正文为准；`novel_memory/canon.py` 逐字引文命中才认 manuscript。记忆零 schema 四铁律（source 白名单/只注入 active/book_scope 认不出不过滤/进度 30 天）见 PITFALLS §二十八；改链路跑 `test_memory_canon_discipline/_evidence/_book_scope`。圣典主角丢失只能 `upsert_records` MERGE，禁 `build_bible`（DELETE 全表）。
- 🔴 USER.md 归属闸：唯一写入口 `save_user_ex`（jsonl 留痕 11-20 停写）；新硬规矩须〔人工确认〕、删除被拦补回、读旧 fail-closed、比对先去日期前缀、bullet 认 `-*•·`；锁门 `test_user_layer_gate.py`，dream/journal/after_action 同收口。

## 三、Bot 群 / 模型
- 🔴 双方向契约：小桔本体=越来越懂你/像个人；附体专家=唯一目标「判断更可核验」，禁叠加人格/情绪/关系/proactivity，禁借用小桔模块。判据：这让他的判断更可核验吗？不是就砍。
- 🔴 1v1 读稿：`file_reader.read_chapter` 开头必带【覆盖率】；`coverage=False` 只给内部消费者（如 `creation/novel_index.py`）。1v1=`list_chapters`/`read_chapter`，群聊=`read_book`/`list_books`。
- 🔴 专家咨询=后台：`consult_experts` 派发即返回（禁改回同步）；900s；`~/.deskmate/consult_runs.json` 唯一事实源；完成经 manager notifier → 广播+`deliver_to_session`；超时保留已产出；无引擎不许标 delivered（留 tick 兜底）。
- 🔴 专家查证=本地优先→联网兜底：本地 search_history/history_library_check/search_private（5 db/3.4G）→ web_search（豆包主/ddgs 备，带 degraded/failed）；**豆包就是联网层，别当第三层**；白名单 11 工具/每工具 60s 见 §二十八。基线 196s/reads=2/13.1万字/search_history×2/web_search 0 次；web_search 多次且交付为空先怀疑它不知道本地库。
- 🔴 引文=正文未见就是未见（`deskmate/bots/citations.py`）：出口机械核验（省略号切段、逐字命中），未见标「（正文未见原句）」+文末清单；投递正文最前面亮「N 处引文正文未见」；禁 LLM 判断像不像；门禁 `test_consult_citations.py`。
- 🔴 专家手艺 OPE-58 bundle：`builtin/<id>/manifest.md`+`skills/<id>-method/SKILL.md`+frontmatter `skills:` 单人白名单，禁扁平 `<id>.md` 挂 skills；`memory_enabled=False` 不挡 bundle；改形态同步 `deploy_cloud_sync.sh:PERSONA_FILES`。
- 🔴 群聊手艺：`runtime.py::BotRuntime.engine()` 按 manifest 派生 `(skills/, allow)` 双闸；save_skill 白名单外；门禁 `test_bot_group_craft_skills.py`；7 位 roundtable=historian/lit-editor/proofreader/reader/publisher/adaptation/genre。
- 🔴 保质优先：省钱只在**不降单次质量**前提下；禁降 reasoning_effort/减唤醒/压字数/共享材料压摘要；验收同题对照（锚点命中≥基线、空泛句=0）。
- 🔴 降本：①`coworker/llm_usage.py` 账本（表 llm_usage，挂 OpenAIProvider 出口含中断，fail-open，不进 SYNC_TABLES）；②多章读稿按章均分+保底 `_MULTI_CHAPTER_MIN=1000`、总量封顶（单章吃满），改 `reading.py::read` 跑 `test_bot_reading_coverage.py`。
- 🔴 Bot 群模型=GUI 主模型（prefs.json:default_model，DESKMATE_BOT_MODEL 优先；缺失/auto→付费兜底且 /api/health 标 paid_fallback）；禁自拼裸 OpenAIProvider（丢 UA/session 头必 400）。全付费（免费组已撤、「主力=Kilo」作废，ZEN_LANE_ENABLED=False）；记忆抽取默认付费。

## 四、GUI 入口 / 退役
- 两条道同一 persona：下拉 `/v1/personas`，专家中心 `experts.ts:EXPERTS`；展示差异≠两套人设。
- 🔴 入口收敛：`Sidebar.tsx:ALLOWED_NEW_SESSION` 只留 cowork/deskmate；空态名片唯一源 `experts.ts:expertIntroFor(agent)`；右栏「我的伙伴」6 位圆桌老专家唯一入口别删；门禁 Sidebar.test.tsx + experts.intro.test.ts。
- 🔴 主编派活退役=撤新建 lead 入口；数据不删（persona/teams/TeamView 保留）；禁 enabled=false 归档、禁删目录。
- 🔴 划词退役（`b2be102c`）：`_spawn_selection_adapter` 恒不拉起；本体 `deskmate/selection/` 与测试保留；复活按 git 恢复函数体。

## 五、环境 / 打包 / 测试
- 🔴 本文件已入库（`238796b8`）但未推 backup（含内部结构暂不公开）；PITFALLS/按日日志刻意不入库（含主机 IP/SSH 身份）；改本文件须跟提交。
- Tauri 2.x arm64：Python 改动重启 app 生效；前端改动须重打包（`./packaging/build_local_app.sh --install`）；版本四处（Cargo.toml/tauri.conf.json/package.json/Cargo.lock）；判新代码三件套（lsappinfo launch time>commit、pycache 重编译、真实行为证据）；双副本 `/Applications` 与 `~/Applications` 同步或只留一个。
- STT：SenseVoiceSmall int8（`~/.config/coworker/models/sensevoice/`），缺入口 fail-fast；DMG 须打进 `openworker-stt`+`sherpa-onnx==1.13.8`。
- 🔴 跑 pytest 必须 `.venv/bin/python`（系统 python 3.14 缺 aisuite ⇒ 177 collection errors）+ `dangerouslyDisableSandbox`+`env -u PYTHONPATH`+`--basetemp=/tmp/pt_<新目录>`。基线 41 红/5076 passed（11-11）；11-17 45 红；前端 0 红/484 passed。判回归同口径比失败清单且看红的内容；取 HEAD 基线禁 git worktree（cp+trap+git show HEAD:）。tsc 用 `surfaces/gui/node_modules/.bin/tsc --noEmit -p surfaces/gui/tsconfig.json`（npx 会误装 tsc@2.0.4 卡交互）。

## 六、交互送达 / proactivity
- 送达四件套：发起点/承载通道/出口呈现/凭证（冷却·去重·计数）；交互分支禁后台跑→`tick(allow_interactive=False)`；运维巡检信箱收开场不念（`ledger.py:OPS_MAILBOX_ONLY_KINDS`）；遥测 `*_error` 走 `_is_telemetry_kind()` 三出口同源。
- 🔴 多书并行：唯一源 `proactivity/writing_context.py`；「当前在写」=最近动笔<2天，其余=搁置 N 天（换书≠停笔）；`guard_writing_suggestion` 双入口硬门禁；负反馈按书名冷却 7 天写 `deskmate.db:proactivity_meta`（SQL 单键原子，禁写 disturb_state.json）；后台 WS 广播经 `ledger.record_suggestion_delivery` 落 delivered。
- 🔴 调度器让路：`_foreground_turn_running()` 只认 `_foreground_sessions`（WS claim_turn 时 mark_foreground），不能用 `_running_sessions`（饿死日更）；mark_idle 一并清；杀开关 `COWORKER_BACKGROUND_YIELD=0`。

## 七、小桔（人格 / 真实优先）
- 🔴 真实优先：遵一个原创、尽量不约束；情绪/表达禁硬约束/模板/映射表；系统只给真实状态、人格给完整表达许可含负面；兜底用她自己的话；冷却约束打扰节奏非表达内容。
- 🔴 AI 味溯源：漏气口=bullet 思维外化+JSON title 逼正式腔+小模型难循禁单；三通道已撤负面清单+title；禁语气映射表/禁词表；日记 narrator 口吻不碰、不加「聊一下」；「她读」日记链禁语义层截断（展示上限归前端）。
- 人格单一源 `deskmate/persona.py`（经 deskmate.md 的 persona_base 注入）。🔴 人格旁路已关闭（`test_persona_bypass_closed.py`）：场景 prompt 只留职责体例、身份底座说一次、JSON 场景 brief=True；6 处真旁路/明确不挂两项见 §二十八。日记 `diary_inspiration.py` 纯规则零 LLM，人称我=小桔/你=萧盛，禁 `\s+` 压缩；日记绝不落本文件。情感层 日记自述>行为推断>平静（kill `DESKMATE_EMOTION_SENSE=0`）；意志层 `will/`（kill `DESKMATE_WILL_ENABLED=0`），5 个 modulate_* fail-open。
- 🔴 vault 静态档案退役出注入链（`b93d441d`）：禁接回注入链、禁再建导出器；`load_persona_base` 仅供按需 vault_read；契约 `test_cognitive_persona_archive_retired.py`。
- 认知层 profile_items=layer×dimension 双轴；主体「他」，小桔世界观 layer='worldview'。🔴 注入顺序「关于她」在前、「关于他」在后（`00166938`）：cap=4500 保头切尾，他层会吃满；契约 `test_cognitive_worldview_priority.py`。

## 八、其它契约
- 小桔形象永不被皮肤替换：新视觉字段自描述（hero/decorativeStamp），禁 mascot/image/picture/icon/character/figure；皮肤图纯景物放边角。mood 是派生值，唯一写入口 `engine.set_mood_from_emotion()`。史料版权仅收作者去世满 50 年者。
- 通用：`provider:model` 只对 Router 有效；真聊天模型 prefs default_model + sessions.model；`DESKMATE_DIR` 调用时解析禁模块级常量；数值兜底禁 `x or default`→coerce.py；判限额 `len(str)`；同文件多处改分开发 Edit。
- 🔴 语音：豆包 host 必 `/api/v3/plan/tts/unidirectional`、结束码 20000000、只有真出音频才算音色可用（9 个 `*_uranus_bigtts`）；朗读须 turn_done 后 to_thread，非 darwin 短路；细则 §二十七。
- 同步连通性：唯一可信=集合级对账（两端三表 sync_uuid 排序 comm 比），游标会骗人；改任一处 LWW 必查另两处；窗口外连接失败是架构预期（ALIGNED）。
- 检索：FTS5 `search_across_dbs`（私人 search_private 标「私人资料」）；语义检索换后端须重刷全表向量、digested 无向量搜不到。细则 §十一/§二十七。

## 九、已知未决
- 权限 14 红已定性：permissions.py bypass 前置短路跳过三道地板；改安全策略须萧哥拍板；定时/自主会话 `_build_task_engine` 硬编码 BYPASS_APPROVALS。
- 访谈卡「点稍后只关窗不上报→白吃 3 天冷却」+ `useProactiveTick.ts:57-58` 起新对话丢整包 → 需前端重打包。
- 工作区遗留：`workbuddy-skin/` 110 资源已删状态；`.trae/`、`.qwenwork-media-result-*`、`:memory:.ses`、`*.bak-*`、`icons.backup-*` 为残留，禁入提交。

## 十、模块去留判据（萧哥终裁 2026-11-20）
- 🔴 消费端判据：任何运转机制必须有可指认下游消费者（人/流程/代码），否则空转=负担，拆掉优于留着；「将来可能有用」不算。有消费者的沉淀=记忆，无消费者=自己记录自己。审计先问谁读/谁用，答不出列拆除候选报萧哥。
- 已据此退役：主编派活入口、划词适配、vault 静态档案注入、圣典四处自动写入。
- 🔴 11-20 二次拍板：①will 语气只留真实状态接主链，禁复活旧映射表；②归属闸留痕停写、闸本体不动；③遥测门收窄不撤，timeline_error 显式豁免（39=改前全口径实拦、36=豁免后实拦，报告 30/33 是仅后缀口径）；④curator 报告停产/默认关/opt-in 用 `.curator_last_run` 节流；⑤deskmate-growth MCP 出口摘除（server.py 留）；⑥roundtable MCP 只接 export_docx，11 个 pid 工具摘掉，run_roundtable 保留实现但不注册；⑦chat 文风桶停每轮写，书稿桶/record_reading 保留；⑧GrowthState.mood 不动（驱动 Mascot 动画+日记心情行）；⑨分身 `~/.deskmate/alter_ego_config.json={"enabled":true}`，decision_profile 不动。

## 十一、后台子任务派发（2026-11-22）
- 🔴 目标：长任务里 agent **自己判断**某块没把握/可独立做，派只读子任务到后台并行跑，完成后以**新消息回灌**主会话（含自动任务），agent 接着干。不是断点续跑、不是圆桌、不是 consult 专属。
- 链路：`delegate_task` → `coworker/orchestration/host.py:JobHost` → `deskmate/delegation.py:build_delegate_engine`（只读 subagent，PLAN，无写文件/shell/发布工具）→ finished notifier → manager `deliver_to_session`。查重/投递幂等走 side_effects 账本（`delegate-delivery:<job_id>`）；多会话路由按 `job.meta["session_id"]`，禁全局闭包。
- 安全/边界：host.recover 只接管登记 source 的 job（automation 绑定运行时 session 不登记）；后台线程切回 loop 用 run_coroutine_threadsafe，禁工作线程直接 await；缺 host/deliver 时 build_engine 不注册工具（CLI/tests 行为不变）；禁改回同步等待。门禁 `test_delegation.py`/`test_orchestration_host.py`。
