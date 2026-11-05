# OpenWorker / 小桔 二开坑表（PITFALLS）

> 本文件只收「无法从代码一眼看出的非显然坑」：复现步骤 + 历史沿革 + 正确做法。
> 红线/契约见 `.workbuddy/memory/MEMORY.md`；按日日志见 `2026-MM-DD.md`。

## §P1 — 补建国子监人物圣经：重建会清空 entities.db 只灌回部分数据

- **现象**：跑 `analyze_novel`(内部 `build_bible`+`inject_bible`) 重建 `~/Documents/已出版/20.国子监秘闻录/.deskmate/` 后，entities.db 从 317 行塌到 132 行（character 120→98 / event 115→17 / relation 44→9 / setting 38→8）。CHARACTERS.md 由 db 确定性渲染，随之丢失温伯昭/高付等大量人物与事件。
- **根因**：`build_bible` 先 `DELETE`/清空 entities.db 再重灌；但 **docx 正文第一批抽取返回空**（python-docx 解析首轮偶发空），只靠后续 gap 抽取补回一小部分实体 → 净丢失 ~185 行。清空发生在重灌之前，故"部分成功"也覆盖了好数据。
- **假绿陷阱（2026-10-02 实测）**：验收只 `grep` 6 个核心人物名（韩笑文/丁闻香/贾思文/董文正/金成泰/韦涵亮），它们恰巧都还在库里 → 误判"更干净、去噪"。**实体总数和 kind 分布才是权威指标**，核心人物名抽查会漏检。
- **正确做法（必做三件事）**：
  1. 重建前 `cp -R <稿>/.deskmate <稿>/.deskmate.bak-<时间戳>` 整目录备份；
  2. 重建后**比对 entities.db 实体总数 + 各 kind COUNT 是否 ≥ 旧版**，任一 kind 缩水即判失败、立即用备份恢复；
  3. 若 docx 正文首批解析为空，应 **ABORT 重灌、保留旧库**（或改 MERGE 而非清空覆盖），不要清空后灌部分数据。
- **恢复命令（已验证）**：`mv .deskmate .deskmate.broken-<ts>`（坏版改名保留、禁 `rm -rf`）→ `cp -R .deskmate.bak-<ts> .deskmate` → 再核 kind 分布回到 317。
- **关联**：姜阿大在好备份 entities.db 就在(kind=character,tier=manuscript)但 CHARACTERS.md 渲染层一直不画它（09-30 起就有的独立渲染过滤问题，非本次事故）；陈哥儿是重建从"对话产出"现造的 dialogue 实体，恢复后正确丢弃。

## §P2 — Caddy basicauth：v2.11.4 拒 python bcrypt 的 `$2b$` 哈希

- **现象**：python `bcrypt.hashpw` 产 `$2b$12$…`，`bcrypt.checkpw` 返回 True（假象），但 caddy v2.11.4 实际拒绝 → 正确密码也 401。
- **根因**：caddy 的 bcrypt 实现只认 `$2a$`（自产 `$2a$14$`）。
- **正确做法**：Basic Auth 哈希一律用 `printf '%s\n' <密码> | caddy hash-password` 生成，不要走 python bcrypt。
- **关联**：后端只认 `x-openworker-token` 请求头（前端发 `X-OpenWorker-Token`，HTTP 头大小写不敏感），不是 `Authorization: Bearer`；验收时别用错头导致误判 401。

## §P3 — 记忆同步隧道：SSH 反隧道远端绑定必须 127.0.0.1，绝不 0.0.0.0

- **场景**：B 类记忆走「自建 oplog over SSH 反隧道」（`deskmate/memory/sync_transport.py`）。Mac 拨出 `ssh -R <remote>:8765:127.0.0.1:8765 user@cloud` 让云端回环达 Mac。
- **坑（公网暴露事故）**：`ssh -R 8765:...` 省略绑定地址时，远端按 `GatewayPorts` 默认可能绑 `0.0.0.0` → 云端 8765 **公网可听**，同步端口（含明文记忆流）外泄。
- **红线**：`SshTunnel` 远端绑定强制 `127.0.0.1`、绝不 `0.0.0.0`（单测 `test_ssh_tunnel_args_are_secure_and_dial_out_only` 断言命令行含 `127.0.0.1` 且不含 `0.0.0.0`）。反隧道在云端绑定的端口 = `remote_bind_port`（默认 8765，单测示例 `-R 127.0.0.1:8765:127.0.0.1:8765`）；**常驻运行器 `sync_daemon_runner.py` 显式设 `remote_bind_port=18765` 以避开云端 oplog server 已占的 8765**（两端 server 各监听 8765，故 Mac 反隧道在云端绑 18765、本地转发 `-L 127.0.0.1:18766:127.0.0.1:8765` 达云端）。`remote_bind_port` 与 `remote_port`（本地转发远端目标）是**两个不同端口**，别混。
- **HTTP oplog 端点** `HttpOplogServer` 也只绑 `127.0.0.1`（不同步端口绝不绑 `0.0.0.0`）。隧道断用 `tick_safe()` fail-open 返回 `{"ok":False}` 而非 raise。

## §十五 — 权限/自理保护：14 例红已定性 + bypass 实跑事实

- **14 例红定位(10-02)**：`permissions.py:361-384` 本地提交 `56bef881`(08-31) 的 bypass 前置短路，跳过 path scope / 持久权威 / 延迟执行文件三道地板。上游立场相反：bypass 是"日常不弹卡"**不是**"没有地板"。⚠ `Mode("auto")→BYPASS_APPROVALS` 别名坑（auto 与 bypass 在代码里同义，别被字面骗）。**改它=改安全策略，仍须萧哥拍板**。
- **她实跑 Full access（两条独立来源实证）**：
  ① 定时/自主会话硬编码——`_build_task_engine`(manager.py:4651/**4672** 行) 写死 `mode=BYPASS_APPROVALS`，**GUI 选择器管不着**（有意设计：无人值守弹卡没人点会永久挂住）。这是全部 bypass 会话主力（374 例定时任务里 300 例）。
  ② GUI 手动切 Full access——按会话生效、不污染新会话：`set_mode`(app.py:5981-6031) 只改该会话 engine + `manager.save` 落库，**从不改 `manager.mode`**；新会话取 `manager.mode`(manager.py:630)，服务端启动不传 `--mode` ⇒ `load_config().mode='interactive'`；连上 `ready` 带 `mode`(app.py:5583) 推回前端、`App.tsx:703` 采纳 ⇒ 选择器会自纠。
- **10-02 误判复盘**：一度以为"切一次 Full access 永久继承"——只看了 `App.tsx:1125 startNewSession` 不重置 React state、**没查服务端 ready 覆盖**。真实只是"无留痕"，不是"会粘"。GUI 非定时会话实测 45 bypass / 21 interactive / 12 auto，与"他需要时才切"相符。
- **判"她能否做某事"必须按 bypass 语义算，不能按 interactive 假设**。bypass 下分层事实（实测）：
  **仍在** = 自保护（`~/.config/coworker/*` 写入被拒）+ **严重度恰为 CRITICAL 的 shell**（需 `tool_guard` 在，即 `execution_level != "off"`；`rm -rf`/`find -delete`/`mv`/`chmod 777`/cron·SSH 篡改都是 **HIGH ⇒ 放行** ⇒ "bypass 仍防数据损失"是错觉）+ shell 写入被 OS 沙箱限制在 roots；
  **已拆** = `write_file` 的 path scoping（可写 roots 外）、延迟执行文件（`.git/hooks` 等）、持久权威（`save_skill`/`create|update|delete_scheduled_task` ⇒ 她能装技能、自建删定时任务）、无法定位路径的写不再 fail-closed。
- ✅ 那几处"地板仍在"的假注释已于 10-02 订正（提交 `c1d78f5`，纯注释零行为变更）。**规矩不变：注释/docstring 一律不作数**，判防线只有一条路——**实测 `evaluate()` 或读短路块本身**。agency 层 L2 提议卡是另一层，仍在。

## §P4 — 外网免费车道**已删除**（2026-10-18）：代码全删，不是关闭

**背景**：opencode 官方封禁萧哥的免费使用（免费通道一条也用不了，连官方客户端都不可用）。10-18 白天先做「关闭态」（commit `b92a119`：两个 `*_LANE_ENABLED=False`，代码保留等放行）；**同日 20:35 萧哥升级拍板为删除**——「今天才知道探测多了会被封号，留着没用，就算恢复，下次也不敢再踩这个坑」。当前契约 = commit `bafa941`（删除，-2691 行）。

**现状契约（删除态）**：
- 四个模块已 git rm：`coworker/providers/{opencode_provider,kilo_provider,zen_catalog,kilo_catalog}.py`。连带删除：matrix 静态 8 项 + `_live_zen`/`_live_kilo`、registry 两个 descriptor 与 Test 探针分支、app.py 两个 6h 刷新启动钩子、`memory/llm.py` 的 `_opencode_complete`/`_kilo_complete` 分派、`test_kilo_lane.py` 整文件、deploy 清单四条目。
- **免费组仅剩本地 llama-server 兜底**（`free_channels() == [local]`，小桔自己的 CVM 设施，不探测任何官方）；Mac 端本地不可用 → 后台任务降级回付费组（`memory_group` 默认 paid，prefs 实测已是 paid）。
- **保留**：本地兜底、403 长冷却/retry-after 解析等通用熔断逻辑、`openrouter_catalog`（picker 手动选项，非后台免费组、非封号当事方——若要一并撤，另议）。
- 删除态契约测试：`tests/test_free_lane_removed.py`（8 条：四模块不可 import、registry 无 descriptor、picker 无条目、免费组仅剩 local、memory/llm 无分派函数、deploy 脚本不传输、付费组三项不变）。**谁想加回免费车道，先改这个文件**——它锁的就是「不加回」。

**测试陷阱一：全量跑必红 39+，别慌着往自己头上扣**。`tests/` 全量（排除三个真出网文件）基线即 40 红（10-18 parent 实测），其中 8 个是 chat_memory 相关用例——**单文件合跑全绿、全量下相互污染才红**。定性方法：同命令跑 parent commit 基线对比，别只看总数。删除后全量 39 failed / 4191 passed，与基线 comm 对比**零新增**（唯一差异是随 `test_kilo_lane.py` 删除消失的那条红）。

**测试陷阱二：`test_health_producers` 的两个 cron 用例有整点 30 分钟窗口**。`_cron_missed` 判据是「应跑时刻已过 30 分钟宽限仍没跑才算 miss」⇒ **整点后 0–30 分钟内执行必红**（19:0x 跑全量红 2 个、19:3x 跑全绿，同一份代码）。跑全量前看一眼时钟，落在窗口内的红不算回归。

**删除时容易漏的三处**（本次实测踩到）：
1. **云端残留**：`deploy_cloud_sync.sh` 只传不删——四个 .py 删出本地后云端 `/opt/openworker/coworker/providers/` 仍躺着旧副本（19:29 传的），须 ssh 精确 `rm` + 清 `providers/__pycache__`；云端缓存 json 在 `/var/lib/openworker/.config/coworker/`（**不是** `.deskmate/`，两段 ls 连读会看错位置），`mv` 成 `.bak-del-20261018` 留证。
2. **本地缓存残骸**：`~/.config/coworker/` 下 146 个 `kilo_catalog*`/`zen_catalog*`（正式缓存 + 上百个从未清理的 `.tmp`——原子写 tmp 名唯一但刷新线程不回收，历史泄漏），模块删除后永久孤儿，已移入 `~/.Trash/deskmate-catalog-cache-20261018/`（可恢复）。
3. **测试 fixture 借用已删 provider 名**：`test_dynamic_route.py` 用 `kilo:stub` 当路由键载体——`router._bare()` 剥前缀依赖 `get_descriptor(prefix)`，descriptor 删除后不剥了 ⇒ 断言 `["stub"]` 变 `["kilo:stub"]`。改 fixture 前缀（`omniroute:stub`）即可，别去改 `_bare`。

**复活要连带做的**（萧哥若哪天要加回）：① 从 git 历史恢复四模块（`git show b92a119:<path>`，注意矩阵/registry/app/memory-llm 五处接入点要一起回）；② 删/改 `test_free_lane_removed.py`；③ 前端 `LlmChannelsCard.tsx:62`/`api.ts:3256`「免费组省付费额度」文案（10-18 已过时未改，改前端要重打包 + deploy_web.sh）；④ deploy 清单恢复四条目。

## §P5 — release 打包在 aarch64 macOS 反复失败：mis-aligned LINKEDIT + E0463（双 bug，已根治）

**现象**：`./packaging/build_local_app.sh`（Tauri 2 / aarch64）release 构建在 Apple Silicon 上反复失败，两种错交替出现：
1. **E0463** `can't find crate for 'ctor_proc_macro'`（编译期，cargo 报找不到）。
2. **`mis-aligned LINKEDIT string pool`**（链接/加载某个 proc-macro dylib 时报，ctor_proc_macro 偏移 `0x6167C`@1.93、darling_macro 偏移 `0x2B98E4`@1.92）。

**根因 = 两条互相独立的 rustc/链接器 bug，必须同时治**：
- **(1) E0463**：`tauri-utils` 以 `default-features=false, features=["std","proc_macro"]`（即 **dtor 关**）引入 `ctor 0.8.0`；dtor 关时 ctor 的 `ctor_proc_macro` 子 crate 只被 `pub use` 重导出、没人真正 `use` → cargo 剪枝 → E0463。治本 = **vendor/ctor**（`surfaces/gui/src-tauri/vendor/ctor`，去 `#![no_std]` + 加 `extern crate ctor_proc_macro;` 强制链接 + `ctor-proc-macro` 改 non-optional + `proc_macro` 特征简化）+ `rust-toolchain.toml` 锁 **1.92.0**（1.93 产出的 ctor dylib 同样坏）。
- **(2) mis-aligned**：rustc release 默认 `-C strip=debuginfo` 在 aarch64 macOS 上把**部分 proc-macro dylib** 的 `__LINKEDIT` 段啃出 **416 字节空洞**（段偏移/声明大小不变、头部未同步、字符串池错位）→ 编译器加载宏 crate 失败。**治本 = `[profile.release] strip = "none"`**（关掉 release 默认 strip，所有宏 crate 即有效）。

**已证伪的弯路（别再走）**：Qoder 并发写坏缓存 / 沙箱 mmap 污染 / 随机竞态——都被**确定性偏移**（同 crate 同工具链下偏移恒定）推翻；`CARGO_TARGET_DIR` 隔离实验已回退，`build_local_app.sh` 的 `APP_OUT` 已还原为固定 `target/release/bundle/...` 路径。

**固化（commit `c1c8944`，已推 backup/fork/base）**：
- `rust-toolchain.toml`：`channel = "1.92.0"`。
- `src-tauri/Cargo.toml`：`[patch.crates-io] ctor = { path = "vendor/ctor" }` + `[profile.release] strip = "none"`（注释写明两条根因；**勿升 pin、勿把 strip 改回 debuginfo**，直到上游 rustc 修这俩 bug）。
- `vendor/ctor/`：ctor 0.8.0 本地副本（改造见上）。

**验证（结论先行）**：仅依赖 `Cargo.toml` 的 `[profile.release]`（**不设任何环境变量**）全量重编 → `BUILD_EXIT_RC=0`、日志全程无 mis-aligned/E0463/could-not-compile，产出 `OpenWorker.app`，内嵌前端 chunk 哈希命中 `dist/`（确认两 bug 修复随前端打进包）。

**副作用/备注**：关 strip 后最终 app 二进制不剥离调试信息 → ~41MB（可接受）。若日后要瘦身，用系统 `strip` 只剥**最终 `openworker-desktop` 二进制**（那一步只动产物、不动构建期宏 crate，不会复现 mis-aligned）。

## §P4 — 新增 persona 后前端"看不见"：新建会话 ▾ 有一层前端白名单

- **现象（2026-10-28 实测）**：`/v1/personas` 明明返回 `editor-in-chief`（`enabled=true surfaced=true ships=true`），
  但整个 GUI 找不到入口，萧哥报「前端面板里看不到主编」。
- **根因**：前端有三道独立闸门，任一道挡住＝角色不可达，且**后端接口一切正常**（所以查接口查不出问题）：
  1. `Sidebar.tsx` `NewSessionSplit` 里写死 `ALLOWED_NEW_SESSION = new Set(["cowork","deskmate"])`
     （2026-08-26 owner ask「精简下拉」的产物）。不在集合里 = 下拉菜单没有。
  2. 右侧「我的伙伴」`RightRail` 只收 `p.roundtable || ENGINEERING_IDS.includes(p.id)`。
  3. 侧栏角色折叠区默认 flat 布局不渲染；grouped 下折叠体里也**故意没有**"新建会话"按钮
     （注释：靠顶部 ▾ 统一入口）。
- **判据**：后端 `registry.sidebar()` / `list_all()` 有 ≠ 用户能点到。**新加 persona 必须同时检查上面三处**。
- **已修**：白名单补 `"editor-in-chief"`（它是 lead，只能从"一个会话"起步；团队必须由 lead 会话经
  `propose_team` 编制）。回归用例 `Sidebar.test.tsx` → `offers 主编 (a lead persona) in the ▾ picker`。
- **连带**：主编 manifest 原为 `requires_folder: false`，但 `manager.create_team` 硬检查
  lead 会话必须有 workspace（否则批准后报 `the lead session has no workspace`），上游 4 个 lead 全是 `true`。
  ⇒ 一并改 `requires_folder: true`（前端 `isProjectScoped` 读的就是这个字段，会弹选文件夹闸门）。
- **验证姿势**：前端产物字节级搜白名单数组（`new Set(["cowork","deskmate","editor-in-chief"])`）；
  装包后资源指纹 `main-<新哈希>.js` 命中 `dist/`；`md5 -q` 三处二进制一致。**Tauri 资源是 brotli 压缩，
  搜中文/类名一律搜不到，别据此判"没打进去"**（见打包 skill 坑 1 补充）。

## §P6 — 后端新增 `_team_digest` row kind，前端 BoardWakeCard 必须同步：否则渲染成「有样式无内容」的空卡

- **现象（2026-10-31 00:58 萧哥截图实测）**：lead 会话里 "Board wake / update" 折叠卡样式在，
  展开后**只剩两个橙色圆点、一行字都没有**。看着像"前端没渲染"，其实后端一切正常。
- **根因（两层，缺一就空）**：
  1. **前端**：`BoardWakeCard.tsx` 的 `rowText()` 是 `switch (row.kind)`，**没有 `case "waiting"`**
     → 落进 `default: return \`${item}${title}\``。而 worker 挂起行**天生没有 item/title**
     （`note_worker_waiting` 只在"唯一 in_progress 项"时才填 item_id；session ≠ task identity）
     → 拼出**空字符串** → 有圆点无文字。
     `summarize()` 同样不认 "waiting" → counts 为空 → 折叠行退化成兜底文案 **"update"**（截图里就是它）。
  2. **后端**：`_team_digest` 的 WORKER_WAITING 分支只给了 `item/actor/kind/tool/prompt_id`，
     **没给 `title`、没给"这是谁的决定"标记、没给 `note`** —— 就算前端修好了也拼不出话。
- **判据（很有用）**：折叠行显示 **"update"** 而**不是** "N moved / N review / …"
  ＝**没有任何 kind 被 `summarize()` 认出来**＝后端发了前端不认的新 kind。这是秒定位的指纹。
- **已修**：
  - 后端行补 `**row`（带上 title）+ `decision: bool`（True=读者是决定者 True 只对 lead）+
    `note`（UI 夹紧的 preview，worker 到底想干啥）。
  - 前端 `rowText` 加 `case "waiting"`；`summarize` 加 `bump("decision")` 且计入 attention（琥珀色）；
    `stateDot` waiting→review 色；`default` 兜底**永不返回空串**；note 按钮文案 "show request"。
  - **旧数据兼容**：`readerDecides(row) => row.decision !== false`。已在库里的旧行**没有** decision 字段，
    而"纯等待批次不会唤醒 worker"是设计（非 lead 只消费不唤醒），所以**无标记的行必然是 lead 的行**
    → 默认 true，否则你重打包后会看到旧卡文案翻转成"waiting for the lead or user"。
- **红线**：**凡后端给 `_board_source` 的 rows 加新 kind，必须同批改前端三处**
  （`rowText` / `summarize` / `stateDot`），并加一条"每个 `.boardwake-row-text` 非空"的断言测试。
  后端绿灯 ≠ 界面有字；这是跨层契约，两边都不报错。
- **验证**：先红后绿（旧码 2 后端 + 4 前端红）；新码 15 后端 + 8 前端全绿。
  真数据佐证：`~/.config/coworker/teams.db` 的 `team_events WHERE kind='worker_waiting'`
  → seq 891 kanyu/web_search、892 gouzhang/write_file（**正好两条＝截图里两个空圆点**）。

## §P7 — 团队派活「静默蒸发」：删会话不从团队摘成员 + worker 无键盘却每个工具调用都要人点

- **现象（2026-10-31 萧哥报，两轮皆空）**：主编派活给 kanyu/gouzhang/jihe，**零产出**；
  主编自己把活干完了。看板上一堆 `in_progress` 永远不动，且不报错、不告警。
- **根因是两条独立缺陷叠在一起（缺一不至于全空）**：
  1. **`delete_session` 不碰团队注册表**（`teams.json`）。worker 会话一删，
     `TeamRegistry` 仍列着它 → `assign` 校验通过（幽灵成员）→ 唤醒发往一个
     **没有 session record、没有 engine** 的 id → 永不执行。
     ⚠ 团队名册是 **JSON 文件**（`~/.config/coworker/teams.json`），
     **不是** session record 的 `team` 列 —— 二者互不同步，删会话只删后者。
  2. **worker 没有键盘**：它是预算的会话，每个 `write_file`/`web_search` 都是
     consequential → 停车等审批。人不可能一直点 → 卡死；主编等不到产出就自己干。
     实测：kanyu 卡 `web_search`、gouzhang 卡 `write_file`，两个 prompt 都在等人。
- **判据（秒定位三件套）**：
  ① `teams.json` 里 worker 的 `session_id` 去 `sessions` 表/`conversations/*.jsonl` 查 → 查不到＝幽灵；
  ② inbox 里 prompt 的 `resolution == "session deleted"`（不是 allow/deny）＝会话被删/被孤儿回收；
  ③ 服务端日志里 `DELETE /v1/sessions/<worker-id>`。
  ⚠ 反面陷阱：**上轮修的委托审批其实是通的**——主编真的调了 `decide_worker_call(allow)`，
  只因 prompt 已被 `session deleted` 结掉才返回
  *"already answered"*。**看到这条错误别再查委托审批链路，去查会话是不是没了。**
- **已修**：
  - `TeamRegistry.detach_session()` + `delete_session` 前置调用：删成员即摘名册，
    最后一个 worker 没了就整队解散（否则 `propose_team` 永远回
    *"this session already leads a team"*，萧哥被卡死）。删 lead 也整队解散。
  - `TeamRegistry.prune_missing_sessions()` + manager 启动自检：清理历史遗留幽灵
    （萧哥当前这个队就是幽灵，靠重启自愈，不用手改 teams.json）。
  - **worker lane**（萧哥拍板 option A）：`PermissionEngine.worker_scope` =
    该 worker 自己的 `~/OpenWorker/<session_id>/`。仅 worker 角色有。
    自动放行：① 查找类（web_search 等 EGRESS，排除 connector）② 写自己输出目录内的文件。
    **仍然要问**：shell/exec、删除、越权写（你的正稿、别人的会话目录）、
    persistent-authority 工具、受保护的项目文件（.git/hooks、CI）。
    实现位置刻意在 `evaluate()` **最后一条**自动放行路径——上面所有地板
    （自保护、写路径作用域、越权、受保护文件、治理护栏）一条没跳。
- **红线**：**给团队/权限层加新角色能力时，必须同时回答"它没有键盘时谁来点"**。
  另外：`teams.json`（名册）与 `sessions`（record）是两份独立事实，
  任何改会话生命周期的代码都要问"名册要不要同步"。
- **验证**：先红后绿（旧码 10 红 / 新码 13 绿）；权限+团队+信箱子集与干净 HEAD
  基线**逐条相同**（4 条既有失败，零回归）。

## §P8 — agent 的 `read_file` 按文本行读 .docx → 拿到 ZIP 二进制，worker 据此宣告"读不了正文"

- **现象（2026-10-31 实测）**：worker `jigang` 读《夜不收大纲.docx》，返回 `PK\x03\x04` 开头的
  二进制，它自己下结论「我这个环境没有 shell，读不了正文」→ 一整个 worker 卡在"读不了"上。
  同批 `dengying`/`mingcha` 连会话都没起 → 萧哥看到"派了活但没产出"。
- **根因**：`coworker/tools/files.py:read_file` 是**按行编号**的纯文本读（`open(..., "r")`），
  **没有 docx 分支**；而 .docx 是 ZIP。venv 里 `python-docx` **是装着的**，
  `deskmate/perception/file_reader.py` 也有现成抽取（段落+表格），只是没接到 agent 工具上。
  ⚠ 反面陷阱：别听 agent 说"没有 shell 所以读不了"——那是它对自己的误诊，**去查它到底拿到什么**。
- **已修**：`file_reader.py` 加公开 `extract_docx_text()`（唯一抽取实现，避免两处漂移）；
  `read_file` 对 `.docx` 走抽取再编号，保持 `path:line` 引用契约，并标 `extracted: true`；
  抽取失败（python-docx 缺 / Word 锁文件 `~$*.docx`）**原样报错**，绝不回落到二进制。
- **验**：真文件抽取《夜不收：绝命救援.docx》得 75555 字；`~$` 锁文件优雅报错。
  新增 `tests/test_read_file_docx.py` 6 测（先红后绿：旧码 4 红）。
- **红线**：**凡是"文件按文本读"的入口，都要问一遍"办公格式怎么办"**（docx/xlsx/pptx 都是 ZIP）。

## §P9 — 移植上游组件只搬 TSX 不搬 CSS：界面退化成"有结构无样式"，且**两边都不报错**

- **现象（2026-10-31 萧哥截图）**：团队看板三个 tab 在，任务行是**纯文本行**，无卡片、无状态胶囊、
  无层次；折叠行显示兜底文案 `update`。看着像"我们自己糊的看板"。
- **根因（不是自造，是移植时被砍 + 设计系统没搬）**：
  - `TeamView.tsx` 上游 **1325 行**，本地只有 **621 行（47%）**；文件头注释自己写着
    *"ported from upstream's TeamView.tsx, **trimmed** …"* 并列了两处"有意分歧"。
  - **`.team-*` CSS：上游 187 条 / 本地 0 条** —— 本地全靠 Tailwind 裸拼，所以没有卡片/胶囊/层次。
  - 连带缺失：`TeamRail.tsx`(251)、`TeamUpdateLine.tsx`(218)、`TaskChip.tsx`、`teamRoster.ts`；
    `TeamRequestCard.tsx` 本地 74 行 vs 上游 852（桩）。
- **判据（秒定位）**：① 折叠/汇总行显示**兜底文案**（如 `update`）而不是"N moved / N review"
  ＝前端不认这个 kind/结构；② `grep -c "\.team-" styles.css` == 0 ＝设计系统没搬；
  ③ 拿本地文件行数 vs `git show origin/main:<path> | wc -l`，**差一半以上就是砍过**。
- **为什么能静默通过**：后端契约两边**同构**（`teamView.ts` 的 `TeamSummary/TeamTask/TeamWorker/GROUPS`
  同名同构，api 函数齐备）→ 数据全到位 → 测试全绿 → 只有人眼能看出丑。**tsc/vitest 都抓不到"没有 CSS"**。
- **已修（萧哥选 B：抄官方设计系统 + 全量任务视图，保留两处有意分歧）**：
  - 搬上游 `styles.css` 的 **2045–2413 行整块**（369 行 / 187 条规则），加变量 shim
    （本地缺 `--canvas/--chrome/--chrome-hover/--fs-mono/--ink-2/--ink-3/--team-rail-w`；
     上游 `--ink-2/--ink-3` 其实**自身也没定义**，属遗留）。
  - 新建 `teamRoster.ts`、`TaskChip.tsx`、`TeamRail.tsx`（只取 `WorkerSearch`）。
  - `TeamView.tsx` 重做为全量版：TaskRow（圆点+标题+状态+成员chip+步数+时间）、
    MetricBar（时间/token 分段条）、Stats（4 张卡 + 按成员/角色 + 累计曲线 + 提问表 + 时间去向）、
    WorkerRows（搜索 + 筛选 + 分页）、PendingTeamRequests、TaskDetailPane（含相关协作者）。
  - **保留的本地分歧**：不做内嵌 WorkerConversation（避免 transcript 审批接线分叉成两套）；
    点 worker 直接开真会话；详情复用 `BoardPanel.ItemDetail`（给它加了 `hideTitle` 可选属性）。
  - i18n：从**上游 zh.json/en.json 直接取值**（39 键），不自己编；补 `pending_requests` 基键。
- **验证**：tsc 干净；vitest **391 passed / 4 failed**，4 条为既有失败（skins/MomentFeed），零回归；
  新增 `TeamView.test.tsx` 5 测（先红后绿：旧码 4 红），专门断言"行必须有标题+状态文本"防复发。
- **红线**：**移植上游 UI 时，CSS 与 TSX 是同一件事** —— 提交前必做
  `grep -c "<组件前缀>-" styles.css`（>0）＋ 行数比对，否则就是"有结构无样式"的静默退化。

## §P10 — 给桌宠帧叠"投影键盘"：三个都不报错、只让画面变丑的坑

- **背景**：working 段是"双手悬在肚子前空点"，底下没东西像在点空气。方案是叠一块激光投影键盘
  （`scripts/xj_holo_bake.py`，参数即定稿 C 方案：190 宽 / 7×3 键 / 青 #00B9FF / 顶沿 y=430）。
  **不用重做视频**——投影键盘本来就该在空中点按，现有动作反而变合理。
- **坑 1：`Image.transform(QUAD)` 顶点顺序是 左上→左下→右下→右上**，不是顺时针绕一圈。
  顺序写错**不报错**，只把图挤成一根竖条——看着像"图本身画歪了"，容易往素材上找原因。
- **坑 2：整图 QUAD 透视会把源图边界一起采样进来**，输出里凭空多出一块**不透明矩形底板**
  （像贴了张卡片）。正解：**按目标尺寸逐键画多边形**，透视直接算进顶点坐标（见 `procedural_holo`）。
- **坑 3：发光半径必须按"目标像素"算**。在 1206px 源图上 `GaussianBlur(20)` 再缩到 232px，
  密集键面会被摊平成**一块均匀灰板**——不是光，是板。
- **坑 4：洒光（spill）必须乘角色自身的 alpha 裁掉**。光只落在"有东西的地方"；
  落在空气里就是一块浅灰矩形，前面三个坑修完这块板还在，就是它。
- **坑 5（验收方法）**：`strings 二进制 | grep -c "pet-clips/<clip>/frames"` **会少计**——
  `strings` 会把多条路径并到同一行，而 `grep -c` 只数行。实测同一份二进制：按行数 623，
  按出现次数 `grep -o … | wc -l` = **760**（与磁盘逐 clip 一致：90/71/53/129/99/109/47/93/69）。
  **判"帧有没有进包"必须用 `grep -o | wc -l` 且按 clip 分列比对**，否则会误报"少了 137 帧"。
- **契约**：重跑 `xj_clip_build.py` 会覆盖 working 帧、把键盘冲掉，必须再跑 `xj_holo_bake.py`。
  `tests/test_pet_clips_assets.py::test_working_clip_has_holo_keyboard` 会抓（探 4 帧，
  判据"蓝远高于红"—她身上没有真正青蓝像素），并配 `test_holo_detector_is_specific`
  反向锁判据不宽（idle/happy/sleepy/angry 必须判否）。
