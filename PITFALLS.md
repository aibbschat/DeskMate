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
