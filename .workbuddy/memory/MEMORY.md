# openworker 二开(桌搭/DeskMate)长期备忘
> 只留**红线判据·路径·开关·禁做**（代码里推不出来的）。细则一律 `.workbuddy/memory/PITFALLS.md`；模块架构 `docs/项目架构与模块备忘_2026-09-02.md`；过程见按日日志。**新内容优先写 PITFALLS.md**。

## 一、发布 / 部署（违反即事故）
- **Git**：origin 绝不 push；只推 `fork/base` 到 backup=`https://aibbschat@github.com/aibbschat/DeskMate.git`（禁 --force/rewrite）；main=上游只 merge。SSH key 仍绑旧账号 ⇒ backup 走 HTTPS，PAT 在钥匙串（`username=aibbschat`）；新仓有 Initial commit，推前 `git merge --allow-unrelated-histories`；临时 PAT 用 `-c http.extraheader`，**绝不写进 `.git/config`**。
- 🔴 **推送代理**（两度踩坑）：`~/.gitconfig` 有 `http.https://github.com.proxy=127.0.0.1:7897`（**死端口**），直连又超时。正解 `git -c http.https://github.com.proxy="$HTTPS_PROXY" push backup fork/base:main` —— 沙箱出口代理**每会话轮换**，禁写死端口。
- **本地↔云端一致**：commit→`scripts/deploy_cloud_sync.sh`（预检须已 commit→scp 经 /tmp→`sudo -u openworker cp`→清 `__pycache__`→md5）；前端另跑 `deploy_web.sh`。🔴 **清单已治本（`b9785824`）**：`MAIN_SET`=find 全量生产 .py 自动发现，漏登记结构上不可能；**红线反转：不上云的必须登记 `MAIN_EXCLUDE` 并写理由**（当前空）。对账三项（不一致/仅本地/仅云端）**全判红**、失败非零退出。
- **停机保号**：`openworker-server`/`xiaoju-agency.timer`/`caddy` disable+stop；`xiaoju-sync-window.timer` 保留（每小时 :02、零 LLM）。复活=enable --now+还原脚本，先 `systemctl list-units --all` 认真名。
- **Web 前端**：重打包 App ≠ 前端已上云；云端 `/opt/openworker/frontend` 只能 `deploy_web.sh`。停机期公网不可用 ⇒ 判据改**云端逐文件 md5** 对本地 `dist/`，排除 `runtime-config.json` 须 0 差异；`main-*.js` 换新才硬证据。⚠ `deploy_web.sh` 末尾 PWA 检查**必然 exit 1**，别为刷绿启 caddy。token 绝不构建时烘焙；`.env.local` 会烘进 bundle → dev 只放 `.env.development.local`。后端 8765 无 token→401 正常。

## 二、数据 / 路径安全
- **路径守卫**唯一 `deskmate/sacred_paths.py`；`guarded_write` 红线：目录含 `.deskmate/` 时原稿正文绝不直改写/删；禁 `rm -rf` deskmate 数据目录；副本迁移用 `sqlite3 .backup`；测试库放 /tmp。**持久化须原子写**（`growth/_atomic.py#unique_tmp`）+ 多入口写同文件须 `fcntl.flock`（`memory/_fs_lock.py`）。
- **API key** 写 `~/.config/coworker/secrets.json:provider:<vendor>`（0600）；改默认模型先退 app 再改 `prefs.json:default_model`。
- 统一库 `~/.deskmate/deskmate.db`；圣经索引=`<小说目录>/.deskmate/entities.db`；产出 `/Users/xiaocheng/OpenWorker/<project-id>/`；Automations 真身 `~/.config/coworker/automation.db:scheduled_tasks`（30s 生效）。技能两套 `~/.deskmate/skills/`、`~/.config/coworker/skills`；**加了技能≠她会用**，要在 instructions 点名。
- 🔴 **已写正文＝唯一事实基准**（`e35d84e1`，`docs/memory-discipline-design.md` §9-11）：记忆/大纲/圆桌结论与正文冲突以正文为准。**零 schema**：复用 `learned_history.source`（同步硬白名单），注入只认 `status='active'`；写侧不传 source 不默认 live；`book_scope.py` 认不出书**不过滤**；进度类 `valid_until` 30 天、`memory_lifecycle.py` 只出计划不落库；正文效力 `novel_memory/canon.py`（`manuscript` 须逐字引文命中否则降级）。改链路先跑 `test_memory_canon_discipline/_evidence/_book_scope`。圣典主角丢失只能 `upsert_records` MERGE，**绝不用 `build_bible`**（DELETE 全表，PITFALLS §P1 塌库前科）。
- 🔴 **USER.md 归属闸**：唯一写入口 `save_user_ex`（护栏+归属闸+留痕 `traces/user_layer_gate.jsonl`）；硬规矩新增须〔人工确认〕、删除被拦+补回、读旧失败 fail-closed、改写比对先去日期前缀、bullet 认 `-*•·`。锁门 `test_user_layer_gate.py`。dream/journal/after_action 全收口此闸或 `file_write_lock`。

## 三、Bot 群 / 模型
- 🔴 **双方向契约（萧哥终裁）**：**小桔本体**＝越来越懂你／像个人（人格/情绪/意志/日记/关系/proactivity）；**附体专家**＝唯一目标「让专家更专业」＝「判断更可核验」，绝不叠加自传/活感/关系/情绪/自主回访，**禁借用小桔的人格/记忆/意志/proactivity 模块**。判据：先问「这让他的判断更可核验吗」，不是就砍。
- 🔴 **1v1 读稿地基**：`file_reader.read_chapter` 开头必带【覆盖率】；`coverage=False` 只给 `creation/novel_index.py` 这类内部消费者。1v1 工具=`list_chapters`/`read_chapter`，群聊 `read_book`/`list_books` 另一套。
- 🔴 **专家手艺必走 OPE-58 bundle**：11 位=`builtin/<id>/manifest.md`+`skills/<id>-method/SKILL.md`+frontmatter `skills:` 单人白名单，**绝不在扁平 `<id>.md` 挂 skills**；`memory_enabled=False` 不挡 persona bundle。改人设形态必同步 `deploy_cloud_sync.sh:PERSONA_FILES`。
- 🔴 **群聊手艺**：`runtime.py::BotRuntime.engine()` 按 manifest 派生 `(skills/, allow)` 双闸；`save_skill` 在白名单外。门禁 `test_bot_group_craft_skills.py`。7 位 roundtable＝historian/lit-editor/proofreader/reader/publisher/adaptation/genre。
- 🔴 **保质优先**：省时省钱只在**不降单次输出质量**前提下；禁降 `reasoning_effort`/减唤醒/压字数/共享材料压摘要；允许群级一次备料/并发落库/缓存前缀。验收同题对照（锚点命中≥基线、空泛句=0）。（「授权协议排最前⇒废料」已修复关闭）
- 🔴 **降本两件**：①账本 `coworker/llm_usage.py`（表 `llm_usage`，挂 OpenAIProvider 出口含中断，fail-open；**不进 SYNC_TABLES**）——降本先看基线。②读稿预算多章点名按章均分+保底 `_MULTI_CHAPTER_MIN=1000`、总量封顶（单章仍吃满）；改 `reading.py::read` 须跑 `test_bot_reading_coverage.py`。
- 🔴 **Bot 群模型＝GUI 主模型**：默认 `prefs.json:default_model`，`DESKMATE_BOT_MODEL` 优先；缺失/`auto`→付费组兜底且 `/api/health` 标 `model_source=paid_fallback`。**绝不自拼裸 `OpenAIProvider`**（丢 UA/session 头必 400）。
- 🔴 **全付费模型（2026-11-16 确认）**：免费组已撤、「主力=Kilo」作废，遵循类判断按付费口径；`zen_catalog.ZEN_LANE_ENABLED=False`。记忆抽取默认付费组。

## 四、GUI 入口 / 退役
- 🔴 两条道同一个 persona：下拉读 `/v1/personas`，专家中心读 `experts.ts:EXPERTS`；展示字段差异≠两套人设。
- 🔴 **入口收敛**：`Sidebar.tsx:ALLOWED_NEW_SESSION` 只留 `cowork`/`deskmate`；空态名片唯一源=`experts.ts:expertIntroFor(agent)`；右栏「我的伙伴」是 6 位圆桌老专家唯一入口，别删。门禁 `Sidebar.test.tsx`+`experts.intro.test.ts`。
- 🔴 **主编派活退役**：撤「新建 lead 会话」入口即达成；**数据不删**（persona/teams/TeamView 全保留）。禁做：`enabled=false` 不可逆归档历史会话；删目录打断 `validate_creative_personas()`+群聊复用。
- 🔴 **划词退役（`b2be102c`）**：`_spawn_selection_adapter` 恒不拉起；本体 `deskmate/selection/` 与接口测试**保留勿删**；复活＝按 git 历史恢复函数体。

## 五、环境 / 打包 / 测试
- 🔴 **本文件已入本地 git（`238796b8`），但未推 backup**（萧哥 2026-11-16 决定：含内部结构暂不公开）⇒ 远端公开仓**没有**本文件，别以为有备份。`.gitignore` 逐级豁免只开一个口子（`!.workbuddy/memory/MEMORY.md`）；**PITFALLS.md 与按日日志刻意不入库**——含云端主机 IP 与 SSH 身份。改本文件须跟提交，否则等于改了不带历史。
- Tauri 2.x arm64：改 Python 重启 app 即生效；**前端改动须重打包**（`./packaging/build_local_app.sh --install`）；版本三处+`Cargo.lock`。判新代码三件套：`lsappinfo` launch time > commit、`__pycache__` 重编译、真实行为证据。🔴 双副本：`/Applications` 与 `~/Applications` 须同步或只留一个。
- STT：SenseVoiceSmall int8（`~/.config/coworker/models/sensevoice/`），缺入口 fail-fast 不回退；正式 DMG 须把 `openworker-stt`+`sherpa-onnx==1.13.8` 打进 sidecar。
- pytest 须 `dangerouslyDisableSandbox`+`env -u PYTHONPATH`+`--basetemp=/tmp/pt_<新目录>`。**既有红基线＝41 红 / 5076 passed**（11-11 口径）⇒ 判回归必须**同口径**比对失败清单。🔴 取 HEAD 基线禁 `git worktree`（`cp` 备份+`trap`+`git show HEAD:`）。前端 vitest **0 红 / 462 passed** ⇒ 再红即新回归。🔴 肤色护栏「修一个冒一个」：每个 `it` 首断言失败即中断，同 for 循环其余皮肤不暴露；改配色前先脚本复刻 `_contrast` 全量算。`npx tsc --noEmit` 须 0 错。

## 六、交互送达 / proactivity
- **送达闭合四件套**：①发起点②承载通道③出口呈现④凭证(冷却/去重/计数)。交互分支禁后台跑→`tick(allow_interactive=False)`。运维巡检类＝信箱收、开场不念（`ledger.py:OPS_MAILBOX_ONLY_KINDS`）；遥测 `*_error` 走 `_is_telemetry_kind()` 三出口同源。
- 🔴 **多书并行（`cfa3e49e`）**：写作状态唯一源 `proactivity/writing_context.py`；「当前在写」=最近动笔且<2 天，其余=「其他项目·搁置 N 天」（**换书≠停笔**）。`guard_writing_suggestion` 双入口硬门禁。负反馈按**书名对象**冷却 7 天写 `deskmate.db:proactivity_meta`（SQL 单键原子；禁写 `disturb_state.json`）。后台 WS 广播经 `ledger.record_suggestion_delivery` 落 **status=delivered**。
- 🔴 **调度器让路**：`_foreground_turn_running()` 只认 `_foreground_sessions`（WS `claim_turn` 时 `mark_foreground`），**不能拿 `_running_sessions`**（会饿死日更）；`mark_idle` 一并清。杀开关 `COWORKER_BACKGROUND_YIELD=0`。

## 七、小桔（人格 / 真实优先）
- 🔴 **真实优先**：遵一个原创、尽量不约束。**情绪/表达类禁硬约束/模板/映射表**；系统只给真实状态不给表达方式；人格给完整表达许可含负面；兜底用她自己的话；冷却约束「打扰节奏」非「表达内容」（`reports/活感方案v2_真实优先_2026-10-13.md`）。
- 🔴 **AI 味溯源**：真漏气口=bullet 思维外化+JSON title 逼正式腔+小模型难循禁单。三通道已撤负面清单+title；禁语气映射表/禁词表；日记 narrator 口吻不碰；日记不加「聊一下」；🔴「她读」日记链路**禁语义层截断**（展示上限归前端）。
- 人格单一事实源 `deskmate/persona.py`（经 `deskmate.md` 的 `persona_base:` 注入）。🔴 **人格旁路已关闭（靶子③，`test_persona_bypass_closed.py`）**：6 处真旁路（moments／understanding／ai_circle column·reply·share／emotion_engine 悬事预期）全改 `for_scene()`，**场景 prompt 只留职责与体例、身份只由底座说一次**；JSON 场景 `brief=True`。**明确不挂**：`emotion_engine._EMOTION_SYSTEM`（内部状态机）、`memory_injector` 的「了解他不等于成为他」（澄清句）。萧哥拍板：日记留言要「真实的搭档」（给反驳权）、ai_circle 公开形象不介意变化。日记 `diary_inspiration.py` 纯规则零 LLM，人称「我=小桔/你=萧盛」，禁 `re.sub(r"\s+")`；**日记绝不落本文件**。情感层 日记自述>行为推断>平静（kill `DESKMATE_EMOTION_SENSE=0`）；意志层 `will/`（kill `DESKMATE_WILL_ENABLED=0`），**5 个** `modulate_*` fail-open。
- 🔴 **vault 静态档案已退役出注入链（`b93d441d`）**：`vault/小桔眼中的萧盛.md` 是 08-30 派生视图、无导出器续命，曾 2645 字霸占 cap=4500 头部、把尾部 worldview「她是谁」整段切掉。`build_cognitive_context` 不再注入、`has_cognitive_data` 不以它为依据；`load_persona_base` 保留供按需 `vault_read`。**禁接回注入链**（再建导出器＝重复），契约 `test_cognitive_persona_archive_retired.py`。
- 认知层 profile_items：layer×dimension 双轴，加层=插行零 DDL；主体「他」(萧盛)，小桔世界观 `layer='worldview'`。🔴 **注入顺序「关于她」在前、「关于他」在后（`00166938`）**：cap=4500 是保头切尾，他层 15959 字自己就吃满 ⇒ 他前她后会让「她是谁」整段被切；实测现 窗口内 她 3674/他 839 字。契约 `test_cognitive_worldview_priority.py`。L2 边界 `agency/`；写文件/发消息/删除=高风险必走提议卡；**无据 gap 不许提问**；关键词≥2 字才算证据；外部 credibility<0.7→L2 提议卡。书稿记忆家 `§ 创作 #stable`。

## 八、其它契约（细则全在 PITFALLS.md）
- **小桔形象永不被皮肤替换**：新视觉字段自描述(`hero`/`decorativeStamp`)，禁 `mascot/image/picture/icon/character/figure`；皮肤图纯景物放边角。**mood 是派生值**：唯一写入口 `engine.set_mood_from_emotion()`。史料版权仅收作者去世满 50 年者（钱穆2040/黄仁宇2050 禁入库）。
- 通用陷阱：`provider:model` 只对 Router 有效；真聊天模型=`prefs.json:default_model`+会话级 `sessions.model`。`DESKMATE_DIR` 调用时解析，禁模块级常量。数值兜底禁 `x or default`→`coerce.py`；判限额用 `len(str)`。同文件多处改分开发 Edit。
- 🔴 **语音**：豆包 host 必 `/api/v3/plan/tts/unidirectional`、结束码 `20000000`、**只有真出音频才算音色可用**（仅 9 个 `*_uranus_bigtts`）；三出口 `request_voice_reply`(afplay)／`send_voice_message`(气泡≤1200 字不落盘)／`set_voice_reply_voice`(prefs)，朗读须 `turn_done` 后 `to_thread`，非 darwin 短路。细则 PITFALLS §二十七。
- **同步连通性**：判连通唯一可信＝集合级对账（两端三表 `sync_uuid` 排序 comm 比），游标会骗人；**改任一处 LWW 必查另两处**。窗口外连接失败是架构预期（`/var/log/xiaoju-sync-window.log` 的 ALIGNED）。
- 检索：FTS5 `search_across_dbs`（史料 `search_history`／私人 `search_private` 须标「私人资料」）；语义检索换后端须重刷全表向量、`digested` 无向量=搜不到。细则 PITFALLS §十一/§二十七。

## 九、已知未决
- 权限 14 红已定性：`permissions.py` bypass 前置短路跳过三道地板；**改安全策略须萧哥拍板**。定时/自主会话 `_build_task_engine` 硬编码 `BYPASS_APPROVALS`。
- 访谈卡「点稍后只关窗不上报→白吃 3 天冷却」+ `useProactiveTick.ts:57-58` 起新对话丢整包 → 需前端重打包。
- 工作区遗留：`workbuddy-skin/` 110 个资源已删除状态；`.trae/`、`.qwenwork-media-result-*`、`:memory:.ses` 为外部工具残留，禁入提交。
