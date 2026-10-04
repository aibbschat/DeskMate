# 阶段 3b「工作区会话 + ingest」执行方案

> 交付给：WorkBuddy（执行方）· 拍板：萧哥
> 日期：2026-10-04
> 前置依据：`reports/sync_stage3_验收清单_2026-10-04.md`（C 段 + §1 关键交互 + §0 D6）
> 上游状态：**阶段 3a 已收口，遗留项 0**（见 `2026-10-04_复验_dan_review_阶段3a_回执.md` 五轮复验）
> 验收方：二蛋

---

## 0. 一句话任务

让小桔在云端**打开一个指向已出版镜像的工作区会话**，并跑通 `ingest/dirs` 全链路；**动库前必须先做双写压测**，因为存在一道阶段 1 时期写下的启动护栏，它可能挡住整个 3b。

> **进展注记（2026-10-04 18:2x）**：护栏决策已定为 **C（时间窗切换）**；且 **18:09–18:14 已实际跑过一轮 C 窗口**（C1 工作区会话 + C2 ingest 均返回 200，游标已追平）。本方案剩余工作：按硬性要求完成正式验收与取证。

## 1. 当前云端实况（2026-10-04 17:5x 实测，非推测）

| 项 | 实测值 |
|---|---|
| `openworker-server.service` | **inactive dead**（后端没跑——这正是 3b 要解决的现状） |
| `xiaoju-sync.service`（B 类同步 daemon） | **active running** |
| `syncthing.service`（A 类镜像） | **active running**，22000 LISTEN，UMask 已固化 |
| 主库 `/var/lib/openworker/.deskmate/deskmate.db` | 4.68 MB，属主 openworker |
| 磁盘 | 40G 用 12G，**剩 26G（31%）**，版本化空间充足 |
| 写作工作区 | `/var/lib/openworker/写作/{原稿,批注}`，原稿 344 文件 |

## 2. 唯一的真卡点：启动护栏（必须先解决，否则 3b 无法开工）

**护栏代码**（`scripts/sync_daemon_runner.py:84-97`，调用点 `:359-360`）：

```python
def _cloud_server_running(db_path: str) -> bool:
    """云端护栏：若 openworker-server 在跑，双写会抢锁，拒绝启动。"""
    r = run(["systemctl", "is-active", "--quiet", "openworker-server.service"], ...)
    return r.returncode == 0
...
if args.side == "cloud" and _cloud_server_running(args.db_path):
    LOG.error("检测到 openworker-server.service 正在运行，双写会抢锁——拒绝启动同步 daemon。")
```

**双重锁定**（比验收清单记的还多一层）：

1. **代码层**：`sync_daemon_runner.py:359` 检测到 server 在跑就**拒绝启动**同步 daemon；
2. **systemd 层**：`scripts/xiaoju-sync.service:24` 有 `Conflicts=openworker-server.service` —— 一方起来另一方被 systemd 拉停。

**含义**：3b 需要后端在跑，而现有配置是**后端与同步 daemon 互斥**。所以 3b 不是"跑个 API"这么简单，**必须先做双写压测并由萧哥拍板护栏怎么改**（验收清单 C0 / §1 已明确要求）。

## 3. 执行顺序（严格按序，前一步不过不进入下一步）

### 第 1 步 · 冷备（红线，动库前必做）

```bash
ssh openworker-mirror 'sudo -u openworker sqlite3 /var/lib/openworker/.deskmate/deskmate.db \
  ".backup '"'"'/var/lib/openworker/.deskmate/deskmate.db.bak-20261004-3b'"'"'"
ssh openworker-mirror 'sudo -u openworker sqlite3 /var/lib/openworker/.deskmate/deskmate.db.bak-20261004-3b "PRAGMA integrity_check;"'   # 期望 ok
```

- SQLite 一律 `.backup()`，**禁止 cp 活库**（v1.5 §十一 红线 2）。
- 记录备份路径与 `integrity_check` 输出，进回执。

### 第 2 步 · 双写压测（决策已定 C；本步改为量化窗口期积压与恢复）

护栏方案已定为 C（不改护栏、不改代码）。本步目的调整为：**量化"验收窗口内停 xiaoju-sync"造成的 B 类同步积压，以及窗口结束恢复后的补齐行为**。压测设计：

1. **基线**：记录当前 `xiaoju-sync` 的 oplog 写入速率、`busy_timeout` 生效情况（WAL + `busy_timeout=5000` + 触发器捕获 app 写，阶段 2 已落地）；
2. **受控双写**：在一个**测试库副本**上同时起后端与同步 daemon，跑 `N` 分钟，注入并发写（建议 N≥30，覆盖 WAL checkpoint 窗口）；
3. **观测项**：
   - 是否出现 `database is locked` / `SQLITE_BUSY`，次数与恢复时间；
   - 触发器是否把 app 写全部捕获进 oplog（**这是阶段 2 的核心设计意图，必须验证**）；
   - 是否有数据丢失/不一致（压测前后 `fp` 集合 diff）；
4. **产出报告**：`reports/sync_stage3b_双写压测报告_2026-XX-XX.md`，含命令、原始输出、结论。

**决策：已定 C（时间窗切换）** —— 萧哥 2026-10-04 拍板。

| 选项 | 含义 | 状态 |
|---|---|---|
| A. 维持互斥 | 3b 期间同步 daemon 停，3b 验收后再恢复 | 未选 |
| B. 放宽为告警 | 护栏改"告警不拒启"（需改 `sync_daemon_runner.py:359` 与 service 的 `Conflicts=`） | **未选**——不改护栏语义，零代码改动 |
| **C. 时间窗切换** | **按需停启：验收窗口内停 `xiaoju-sync` + 起后端，窗口结束恢复** | **✅ 已选** |

**选 C 的理由与已验证前提**：

1. **不碰护栏 = 零代码改动**。B 要改 `sync_daemon_runner.py:359` 与 systemd `Conflicts=` 两条链；C 完全不动它们，护栏原样保留 → 阶段 1 的防双写设计不被削弱，回滚也只需停/启两个服务。
2. **A 类镜像不受影响（17:5x 实测确认，C 的硬前提）**：`syncthing.service` 的 `Conflicts=shutdown.target`，**与 `openworker-server` 无任何互斥关系** → 停启后端不会停掉镜像，3a 成果零风险。仅 `xiaoju-sync.service` 带 `Conflicts=shutdown.target openworker-server.service`。
3. **同步中断窗口可控**：只覆盖 C1–C3 验收时段，验收完立刻恢复；C4 回滚演练本身就是"停后端 → daemon 能拉起"的反向验证。

**C 的执行动作（WorkBuddy 直接照做）**：

```bash
# 进窗口（做 C1–C3 前）
ssh openworker-mirror 'sudo systemctl stop xiaoju-sync && sudo systemctl start openworker-server'
# …执行 C1 / C2 / C3 验收…
# 出窗口（验收结束立即恢复）
ssh openworker-mirror 'sudo systemctl stop openworker-server && sudo systemctl start xiaoju-sync'
```

### 窗口期硬性要求（2026-10-04 18:2x 实测后新增，不可省）

**背景：18:09–18:14 已实际跑过一轮 C 窗口，暴露两个必须写进流程的问题。**

#### 硬要求 1 · 游标追平 = 验收判据（取代"建议记录日志"）

窗口内 Mac daemon 仍按老地址 8765 拉取，而 8765 已被后端占用 → 后端回 **401**（`sync_transport.py:239` 注释已说明反隧道本应绑 **18765** 以避开 server，runner 显式设之）。这属**地址撞车，非凭据问题**，且 401 打在**后端 journal**，daemon 自己日志仍报 `ok=True 连续失败=0` → **监控是盲区，窗口停摆无人知晓**。

但数据**不会丢**：`CursorStore` 原子写 JSON 持久化游标，恢复后自动补齐。实测已验证：

| 侧 | 游标（18:2x 实测） | oplog 最大 seq |
|---|---|---|
| Mac | `pull_after_seq: 1076` / `push_after_seq: 1077` | 1077 |
| 云 | `pull_after_seq: 1077` / `push_after_seq: 1076` | 1076 |

云端已补齐卡住的 926 之后全部记录；窗口期 18:13:07 那条 `learned_history`（`[本地笔记] 明末辽东大事年表…`）确认在云端。

**因此验收判据改为游标交叉对齐，不看日志**：

```bash
# 进窗口前：记游标基线
ssh openworker-mirror 'sudo cat /var/lib/openworker/.deskmate/deskmate.db.sync_cursor.json'
cat ~/.deskmate/deskmate.db.sync_cursor.json

# 出窗口后：等 2–3 个 tick（15s/轮），核对两端游标交叉对齐
#   期望：Mac.push_after_seq ≥ 云 oplog max(rowid)，且 Mac.pull_after_seq 追平云端 push
#   两端差集必须为空 —— 不对齐即视为窗口失败，需重跑本轮，不得放行
```

**补充**：仍建议存服务状态 + syncthing 索引状态，证明窗口内 A 类镜像持续在同步；但**它只是佐证，游标才是判据**。

#### 硬要求 2 · 窗口时长上限 30 分钟

实测 4 分半窗口跑完 C1 + C2 全部 ingest。按 **30 分钟封顶**：超时则**中途恢复一次再进下一项**，不要一路停到底。

理由：B 类同步停摆期间 Mac 端记忆不入云，且如上所述**无告警**——窗口越长，事后不可见的静默积压越大。

### 不做的事（已评估，避免过度设计）

- **不给后端补只读 oplog 端点**：那等于让后端实现同步协议，破坏"后端不知道同步存在"的隔离；而游标持久化已证明能自愈，补端点是用架构复杂度换一个已被解决的问题。

**压测仍要做，但目的变了**：不再是"要不要放宽护栏"，而是**量化窗口内 B 类暂停的积压量与恢复行为**（窗口结束恢复后 oplog 是否能补齐、有无遗漏），供阶段 5 决策参考。

### 第 3 步 · C1 工作区会话（需后端在跑）

1. `POST /v1/workspaces/open` 指向 `/var/lib/openworker/写作/原稿`；
2. `GET /v1/workspaces/trusted` 可见；
3. **让她真的干活**（防假绿，不接受"API 200"当通过）：
   - 列出目录；
   - 读指定章节；
   - 检索一条素材；
   - 并要求**她复述内容片段**——读不到就是没读到。

### 第 4 步 · C2 ingest 全链路

1. `ingest/dirs` 注册镜像目录 → `scan` → 勾选入库；
2. `learned_history` 新增事实；
3. **跨模块联动验收**：这些新事实须经阶段 2 同步出现在 Mac 端。判据：按 `fp` 集合 diff，**两端差集为空**（这条同时验证"触发器捕获 app 写"在云端生产环境真实生效）。

### 第 5 步 · C3 她不写原稿

让她尝试直接修改某章正文 → 工具层应拒绝 / 只能写批注目录。注意 **3a 的权限兜底此刻必须仍在**：
`sudo -u openworker` 写 `原稿` = DENIED、写 `批注` = OK（每轮复测都要重验，别假设它一直在）。

### 第 6 步 · C4 回滚演练

- 停 `openworker-server` → 同步 daemon 能正常拉起（护栏语义与新决策一致）；
- 冷备文件 `integrity_check=ok` 可用。

## 4. 出口标准（对照 v1.5 §十）

| 标准 | 判据 |
|---|---|
| 可单独回滚 | C4 通过；回退 = 停共享 + 删镜像（Mac 原件无损） |
| 双端对账一致 | A1 保持 344/344、0 失配；C2 的 `fp` 差集为空；**出窗口后两端游标交叉对齐（硬要求 1）** |
| 不碰原稿写权 | C3 + B4 权限层复测 |
| 不引入史料传输 | A3 排除项复测（`.deskmate*`、会话目录、`history/` 仍 0） |

## 5. 文档收尾（3b 完成后）

1. 更新 `reports/小桔_逻辑大脑多副本同步架构设计_2026-10-03.md` §十（:264-267）阶段 3 状态行 → 标「3a 完成（含 600 权限根治与实证）/ 3b 完成」；
2. 验收记录（命令 + 输出）落 `reports/`；
3. 若 3b 已做：把"镜像文件集一致"并入 `verify` 常驻验收脚本；
4. 本方案与压测报告一并归档。

## 6. 风险与红线提醒

- **动库前必冷备**，SQLite 只用 `.backup()`；
- **不要为赶 3b 直接删掉护栏**——护栏存在的理由是防双写抢锁，须压测+拍板后再改；
- 圣经只在本地：任何改动不得让 `.deskmate*` 进入云镜像（A3 复测项）；
- 796 会话目录、3.6G 史料库**不搬**；
- 密钥/device ID 不入文档不入库（v1.5 红线 5）——本方案刻意不含任何密钥与 device ID。

---

## 附：给 WorkBuddy 的三个必须回答的问题

1. **窗口期 B 类积压多大？** 停 `xiaoju-sync` 期间 oplog 积压多少条/字节，恢复后能否补齐、有无遗漏？（护栏已定 C 不改；**18:09–18:14 首轮实测已证明可补齐**，本题改为量化规模，供阶段 5 参考）
2. **触发器捕获 app 写在云端真实生效吗？** C2 的 `fp` 差集是否为空？
3. **小桔能真的读到镜像里的稿子吗？** C1 三件事她是否都复述出了内容片段（不是"API 200"）？

---

## 阶段 5 衔接 · 后端常驻方向（D 方案：端口分离 → 压测 → 决策 → 常驻）

> 本章为**阶段 5 的前置技术路径**，不属3b 验收范围。起因：萧哥 2026-10-04 指出"**后端常驻是上云的必要条件**"（否则无法随时在 `https://xj.aibbs.chat` 见到她），而 C 方案本质是"用 B 类同步中断换后端在线"——**窗口能开一次，不能开一个月**。

### 7.1 问题定性：8765 冲突是配置缺陷，不是架构必然

| 事实 | 证据 |
|---|---|
| 云端 daemon 的 oplog server 绑 `local_port` | `sync_daemon_runner.py:182` |
| `local_port` **有命令行开关**（`--local-port`，默认 8765） | `sync_daemon_runner.py:339` |
| 云端 unit **未传该参数** → 默认占用后端的 8765 | `scripts/xiaoju-sync.service:13-17` |
| 反向隧道早已避开（显式绑 18765） | `sync_transport.py:239` 注释原文 |
| 实测云端 oplog server 此刻正占 8765 | `ss -tlnp` → pid 1146175 @ `/system.slice/xiaoju-sync.service` |
| 后端同样绑 8765，Caddy 14 处反代到它 | `openworker-server.service` + `/etc/caddy/Caddyfile` |

**结论**：`sync_transport.py:239` 的注释说明作者**早已意识到该撞车**并给反向隧道留了 18765，却漏了自己的 oplog server。当前互斥**一半来自配置偷懒，而非护栏所必需**。

### 7.2 为什么优于"长期跑 C"

| 维度 | C（时间窗） | 端口分离 |
|---|---|---|
| 需停 B 类同步 | **是** | **否** |
| 静默积压风险 | 有，且无告警（见硬要求 1） | 无 |
| 对"后端常驻"的影响 | 常驻 = 长期窗口 = 长期积压 | 无影响 |
| 改动量 | 0 代码，靠人操作 | unit + 参数，可回滚 |

### 7.3 四步执行（顺序不可颠倒）

**第 1 步 · 端口分离（低风险，可立即做）**

给云端 oplog server 换端口，彻底避开 8765/18765/18766：

```bash
# 云端 unit ExecStart 追加 --local-port 18767（改后 daemon-reload + restart）
# Mac 侧同步改 --remote-port 18767（指向云端新 oplog 端口）
# 验收：ss -tlnp 确认 daemon 的 oplog server 已不在 8765；后端可同时起，无 401
```

-可回滚：改回参数即可，无需改代码。
- 这一步单独就能让窗口期 401 消失。

**第 2 步 · 真双写压测（决定第 3 步的闸门）**

端口分开后两进程仍写同一个 `deskmate.db`——**护栏防的正是这个**。沿用第 2 步的压测设计，观测锁竞争、触发器捕获、数据丢失三项。

**第 3 步 · 二选一（由压测数据决定，不拍脑袋）**

- **压测干净**（无丢失、WAL 兜得住）→ 删 `Conflicts=` 与代码层护栏，两进程长期共存；
- **压测有风险** → 走 7.4 兜底路径。

**第 4 步 · 常驻**

`systemctl enable openworker-server` + 开机自启；入口收敛为 `https://xj.aibbs.chat`（Caddy + Let's Encrypt 已就位，仅 basicauth）。

### 7.4 兜底路径：错峰常驻（压测不过时用，建议长期保留）

后端**常驻**，同步 daemon 改为 **systemd timer 定期跑**（如每小时几分钟），非常驻。

- 优点：后端在线（满足上云必要条件）+ 无长期静默积压（积压变成可见、可预期的定时差）；
- 比"两进程长期同跑一个 SQLite"更保守，**即使压测通过也值得考虑**。

### 7.5 待萧哥拍板事项

1. 是否认可"先端口分离、再压测、据数据决定删不删护栏"的顺序（不直接跳到删护栏）？
2. 阶段 5 入口收敛为域名 `https://xj.aibbs.chat` 还是继续走 SSH 隧道（隧道更保守，域名更便利）？
3. 兜底的"错峰常驻"是否作为**默认长期形态**，即便压测通过也采用？

### 7.6 与 3b 的关系

3b 的 C 方案**不依赖本章**即可完成（首轮窗口已跑通）。本章是 3b 之后的独立阶段，切勿为了"提前常驻"而跳过压测直接删护栏。
