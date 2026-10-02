# 小桔 local-first 回转：迁移步骤

> 日期：2026-10-01
> 目标模型：**Mac = canonical 满真身**（大脑回本地、直接读稿协助写作、L2=0）
> 　　　　**云端 = 镜像 + L2 独占 worker**（大脑镜像、7×24 看世界/AI圈、稿子加密备份、远程只读壳）
> 状态：**待萧哥确认后执行**——本文件是步骤方案，尚未执行任何生产变更。
>
> ⚠️ **2026-10-01 22:45 审阅后状态更新**：DSH Agent 审阅意见（`reports/小桔_local-first迁移步骤_审阅意见_2026-10-01.md`）指出
> **步骤 5 存在结构性缺陷**——「大脑在 Mac（canonical）+ 云端 L2 写大脑」本质是**双写（多主复制）**，而大脑是不可替代
> 资产（3.7G），不应押在尚未实现的自研合并脚本上。该意见同时列出步骤 5 的 5 个具体缺陷，并指出安全项比本文更紧迫
> （8080 隧道是**另一个 site 块**，只给 443 加 basicauth 会留旁路；token 已暴露数小时**必须轮换**）。
> **本文件步骤 1–4、6–7 在架构方向确认前不执行**；方向取决于「Mac 合盖后云端是否仍需 7×24 自主成长（L2）」这一根本问题，见审阅意见第八节。
>
> 🔴 **2026-10-01 22:5x 定案：本方案作废（superseded）。** 萧哥洞见——「小桔与 WorkBuddy 最大不同：小桔要在云端看世界、
> 上 AI 圈，WorkBuddy 只同步用户资料、不需要看世界」。L2（看世界/AI圈）**必须常驻且写大脑**，故大脑唯一写者须在云端
> ⇒ 采用**版本 A**（大脑留云单写者 + 稿子单向推云只读镜像 + 挂工作区会话 + ingest）。大脑回 Mac 与「云端看世界」互斥。
> 后续以 `reports/小桔_写作工作区互通方案_2026-10-01.md`（单向镜像 + 反向批注目录）为准执行。

---

## 〇、模型一句话

Mac 是权威满真身，云端退为"持 Mac 大脑镜像 + 独占跑 L2（看世界/AI圈）+ 稿子加密备份 + 远程只读壳"。
云端 L2 产出（世界观/记忆画像）回灌 Mac，共享同一份"越来越懂你"；写作主场回 Mac，稿子够得到、隐私不出 Mac。

---

## 一、当前状态快照（迁移前，实测）

| 项 | 状态 |
|---|---|
| 云端大脑 | `/var/lib/openworker/.deskmate` 活脑（3.7G / 1445 文件，19:0x 后跑过 L2，有新记忆） |
| 云端服务 | `openworker-server@8765` + caddy(8080/443/80)；`L2=1`；`xj.aibbs.chat` 直连通 |
| 云端配套 | AI圈 MCP 已迁（59 工具，两份 mcp.json）、`secrets.json` 已配、fastembed 本地 BGE 已装 |
| Mac 大脑 | `~/.deskmate` 为 19:45 **空脑**（180KB）；老脑归档在 `~/.deskmate.local-archive-20261001`（3.7G） |
| Mac 客户端 | 本地 `openworker-desktop` 已停；`~/.ssh/id_ed25519` 可连服务器 |
| ⚠️ 暴露风险 | 公网 `xj.aibbs.chat` 裸奔（token 明文在 HTML + CT 日志可被检索） |

---

## 二、执行顺序总览

| 步 | 动作 | 影响 | 可逆性 |
|---|---|---|---|
| **0** | Basic Auth 止血（安全最紧迫，**建议最先做**） | 公网访问需口令 | 去掉指令即还原 |
| 1 | 冻结云端 L2 + 热备大脑 | 看世界/AI圈暂停数分钟 | 快照已有 |
| 2 | rsync 云端脑 → Mac（恢复 canonical） | 云端暂停 | 归档+快照双保险 |
| 3 | Mac 恢复本地满真身 + 写 `L2=0` | 本地 app 起 | 可逆 |
| 4 | 云端改造：保持 `L2=1`，脑变镜像 | — | 可逆 |
| 5 | 部署同步回路（双向合并脚本） | 周期同步 | 停脚本即停 |
| 6 | 部署稿子加密备份 | 稿子上云备份 | 删脚本/目录 |
| 7 | 验证 + 观察 24h | — | — |

---

## 三、分步命令

### 步骤 0：Basic Auth 止血（建议最先）

```bash
# 服务器：生成口令 hash
sudo caddy hash-password          # 输入口令 → 得到 $2a$14$... 
```

在 `/etc/caddy/Caddyfile` 的 `xj.aibbs.chat:443` 站点块内加入：

```
basicauth {
  萧哥 <粘贴上面的 hash>
}
```

重启（Caddyfile 有 `admin off`，**须 restart 非 reload**）：

```bash
sudo systemctl restart caddy
```

验证：浏览器打开应弹登录框；`curl` 不带口令 → 401。

---

### 步骤 1：冻结云端 L2 + 热备大脑

```bash
ssh ubuntu@81.69.163.170
sudo systemctl stop openworker-server
sudo cp -a /var/lib/openworker/.deskmate /var/lib/openworker/.deskmate.pre-localfirst-20261001
```

⚠️ 红线：**运行中的库必须 `sqlite3 .backup`，禁 `cp` 运行库**。此处先停服务，故 `cp -a` 安全。

同时备份回滚件：`/etc/openworker/env`、两份 `mcp.json`、`secrets.json`、token。

---

### 步骤 2：rsync 云端脑 → Mac

```bash
# 本机 Mac
mv ~/.deskmate ~/.deskmate.empty-20261001        # 先挪走 180KB 空脑
rsync -avz --rsync-path="sudo rsync" \
  -e "ssh -i ~/.ssh/id_ed25519" \
  ubuntu@81.69.163.170:/var/lib/openworker/.deskmate/ ~/.deskmate/
```

⚠️ 云端 `.deskmate` 属主是 `openworker`，`ubuntu` 读取须加 `--rsync-path="sudo rsync"`。

验证：`du -sh ~/.deskmate` 应约 3.7G；`ls ~/.deskmate/deskmate.db` 存在。

---

### 步骤 3：Mac 恢复本地满真身（写作主场）

- 不设 `DESKMATE_DIR` → 自动回落 `~/.deskmate`（`paths.py:19-22`）。
- **写 `L2=0`（核心，防两端重复发帖）**：
  在 `~/.deskmate/l2_settings.json` 设 `{"enabled": false}`
  （`settings.py:366/369` 证明开关就落在这个**独立文件**，不碰含 API key 的 `settings.json`，符合红线）
  或用环境变量 `DESKMATE_L2_ENABLED=0`。
- 启动本地客户端：`open ~/Applications/OpenWorker.app`（sidecar 连本地 8765 + 本地脑）。
- 验证：让小桔读一章稿子——能读到桌面稿，即本地 canonical 生效。

---

### 步骤 4：云端改造为镜像 + L2 独占

```bash
sudo systemctl start openworker-server           # L2=1 保持，独占看世界/AI圈
```

云端 `l2_settings.json` 保持 `enabled=true`；此后云端脑由步骤 5 的同步回路维护为 Mac 镜像。

---

### 步骤 5：同步回路（双向合并，核心新工程）

`sync_brain.sh`（Mac 侧 launchd，默认每 30min）：

```bash
#!/bin/zsh
set -e
# 1) 暂停云端 L2，取得静止快照
ssh ubuntu@81.69.163.170 'sudo systemctl stop openworker-server'

# 2) 云端 L2 产出 → 回灌 Mac（增量；SQLite 走 merge_brain.py 按表 upsert）
rsync -avz --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/id_ed25519" \
  ubuntu@81.69.163.170:/var/lib/openworker/.deskmate/ ~/.deskmate/

# 3) Mac（canonical，已含云产出）→ 对齐云端镜像
rsync -avz --delete --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/id_ed25519" \
  ~/.deskmate/ ubuntu@81.69.163.170:/var/lib/openworker/.deskmate/

# 4) 恢复云端 L2
ssh ubuntu@81.69.163.170 'sudo systemctl start openworker-server'
```

⚠️ **关键工程点（不能省）**：`deskmate.db` 是单文件 SQLite，**两端都写**；整文件 rsync 按 mtime LWW 会**丢掉一端的行级修改**。因此必须配 `merge_brain.py` **按表合并**：

- 对参与双向写入的表：`INSERT ... ON CONFLICT(id) DO UPDATE SET ... WHERE excluded.updated_at > 表.updated_at`（LWW）。
- 首版需先梳理表清单（哪些云写 / 哪些 Mac 写 / 哪些重叠），脚本按表处理。
- 文件层（`.md` VaultStore、`json_blobs`）用 rsync mtime LWW 已足够。
- **冲突留痕**：冲突行写 `conflict_log.jsonl`，绝不静默覆盖。

**顺序铁律：先回灌（云→Mac），再对齐（Mac→云）。** 反了会丢云端 L2 产出。

---

### 步骤 6：稿子加密备份（Mac → 云只读镜像）

```bash
rsync -avz --delete -e "ssh -i ~/.ssh/id_ed25519" \
  /Users/xiaocheng/Documents/已出版/ \
  ubuntu@81.69.163.170:/var/lib/openworker/manuscripts/已出版/
```

- 服务端目录 `openworker:openworker`，**只读用途，云绝不改稿**（神圣红线）。
- 传输层 ssh 已加密；静止加密可选（`age`/`gpg`）。首版建议 ssh 传输 + 服务端明文备份，若要零知识再加静止加密。
- ⚠️ 待确认：同步范围、频度、是否静止加密。

---

### 步骤 7：验证清单

- [ ] Mac：app 打开，让小桔读一章 → 本地 canonical + 读稿 OK
- [ ] Mac：`L2=0` 生效（本地不发帖、不跑看世界）
- [ ] 云端：L2 仍在跑（日志 `last_world_search` 更新、AI圈回信无报错）
- [ ] 同步：跑一次 `sync_brain.sh`，两端记录数一致、增量到达、`conflict_log` 空或已留痕
- [ ] 稿子：云端 `manuscripts` 出现文件，Mac 原稿**未被改动**
- [ ] Basic Auth：无口令 401，带口令可进；WS 也通
- [ ] 24h 观察：AI圈**无重复发帖**（证明 Mac `L2=0` 生效）

---

## 四、风险与红线警示

- ⚠️ **两端 L2 绝不同开**——否则重复发帖/回信。本地必须 `L2=0`，云端独占。
- ⚠️ 数据库备份：运行库用 `sqlite3 .backup`，**禁 `cp` 运行库**。
- ⚠️ `rsync --delete` 只用于"Mac→云"，且必须在"云产出已回灌 Mac"**之后**。
- ⚠️ `deskmate.db` 两端都写 → 整文件 rsync 会丢修改，必须按表 upsert 合并。
- ⚠️ 公网域名仍在暴露 → 步骤 0（Basic Auth）建议**先于一切**执行。
- ⚠️ 迁移窗口：云端服务会短暂停止，AI圈/看世界暂停数分钟。
- ⚠️ 神圣稿件：云端镜像只读，任何"云改稿"路径不启用。

---

## 五、回滚预案

- 云端脑快照 `.deskmate.pre-localfirst-20261001`（步骤 1）+ Mac 归档 `.deskmate.local-archive-20261001` 均在。
- 回 flavor C：Mac 停本地 app + 归档本地脑 → 浏览器连 `xj.aibbs.chat` → 云端恢复快照启动即可。
- **零锁定已证**：`paths.py` 无写死云端路径，全仓 grep `/opt/openworker|/var/lib/openworker|/home/ubuntu` **零命中**。

---

## 六、待萧哥确认

1. **步骤 0（Basic Auth）现在就做？**（最紧迫，公网裸奔中）
2. 稿子同步范围：`已出版/`（`国子监`/`夜不收`/`大明提塘官`）？`Desktop/大明快报_全书/`？其他？
3. 同步周期（默认 30min）与 L2 暂停窗口（数分钟/次）可接受？
4. 确认后我开始执行：先步骤 0，再 1–7 逐步验证。
