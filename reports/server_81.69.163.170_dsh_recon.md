# 服务器 81.69.163.170 侦察报告（DSH 预装 / 拟卸载搬小桔）

> 侦察时间：2026-10-01｜操作者：逍遥｜目的：评估把 DSH 卸掉、给小桔(DeskMate)腾出云端服务器

## 一、服务器基本信息
- 公网 IP：`81.69.163.170`（腾讯云段）
- 系统：Ubuntu 24.04.4 LTS（Noble），内核 `6.8.0-124-generic`
- 登录用户：`ubuntu`（uid=1000，在 sudo 组）
- sudo：`sudo -n true` 返回成功 → **已配 NOPASSWD，无需密码即可提权**
- 磁盘：`/dev/vda2` 40G，已用 6.5G，剩余 32G（使用率 18%）
- 内存：total 3.6Gi / available 3.0Gi
- SSH：22 端口，ubuntu 密码登录（已连通）

## 二、DSH 构成（= DeepSeek Harness）
| 服务单元 | 作用 | 监听端口 | 安装/数据位置 |
|---|---|---|---|
| `deepseek-harness.service` | Web UI 主程序，ExecStart=`/home/ubuntu/.local/share/pnpm/dsh --profile web`，User=ubuntu，WorkingDir=`/home/ubuntu/.dsh` | 后端 `127.0.0.1:3080`；UI `127.0.0.1:8080` | DSH_HOME=`/home/ubuntu/.dsh` |
| `dsh-auth-caddy.service` | Caddy 公共边缘代理（DynamicUser，Capability BIND 80/443） | **实测 `ss` 未见 80/443 LISTEN**（未对外暴露） | 二进制 `/usr/lib/dsh-auth/caddy`；配置 `/etc/dsh-auth/Caddyfile`；环境变量 `/etc/dsh-auth/dsh-auth.env` |

服务 drop-in：`/etc/systemd/system/deepseek-harness.service.d/50-dsh-auth.conf`（仅注入 `EnvironmentFile=/etc/dsh-auth/dsh-auth.env`）

## 三、相关数据位置
- `/home/ubuntu/.dsh`（28M）：`profiles/`、`sessions/`、`storages/`、`weixin/`、空文件 `.credentials.yaml`/`.env`/`.settings.yaml`
  - **关键：三个凭据文件均为 0 字节** → 新机未配置任何 API key，无密钥泄露风险
- `/home/ubuntu/.local/share/pnpm/dsh` → 软链到 `/home/ubuntu/.local/share/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js`
- `/home/ubuntu/.local/share/pnpm/dsh-auth` → 软链到 `.../dsh-runtime/node_modules/dsh-auth/lib/cli.js`
- `/home/ubuntu/.local/share/pnpm/store`（pnpm 缓存）
- `/home/ubuntu/.local/node`（node 运行时，疑似 DSH 镜像自带）
- `/etc/dsh-auth/`：`Caddyfile`、`admin-bootstrap.password`、`dsh-auth.env`、`install-state.json`、`session-secret`
- `/usr/lib/dsh-auth/caddy`（caddy 二进制）
- 无独立 dsh 系统用户（仅 caddy 用临时 DynamicUser `dsh-auth-caddy`）
- **无官方卸载脚本**（`find` 未找到 `dsh-uninstall*`）

## 四、建议卸载步骤（待用户确认后执行）
1. `sudo systemctl stop deepseek-harness.service dsh-auth-caddy.service`
2. `sudo systemctl disable deepseek-harness.service dsh-auth-caddy.service`
3. 备份：`sudo tar czf /home/ubuntu/dsh-backup-$(date +%F).tgz -C /home/ubuntu .dsh /etc/dsh-auth`（28M，成本低）
4. `sudo rm /etc/systemd/system/deepseek-harness.service /etc/systemd/system/deepseek-harness.service.d/50-dsh-auth.conf /etc/systemd/system/dsh-auth-caddy.service`
5. `sudo systemctl daemon-reload`
6. `sudo rm -rf /home/ubuntu/.dsh /home/ubuntu/.local/share/dsh-runtime /home/ubuntu/.local/share/pnpm/dsh /home/ubuntu/.local/share/pnpm/dsh-auth /home/ubuntu/.local/share/pnpm/store /etc/dsh-auth /usr/lib/dsh-auth`
7. node 运行时（`/home/ubuntu/.local/node`）：若仅 DSH 使用、小桔后端是 Python 不需要，则一并删；若用户另有用途则保留 → **待确认**
8. 复验：`systemctl list-units | grep -i dsh` 应为空；`ss -tlnp` 不再有 3080/8080

## 五、待决策
- [ ] 卸载时 `.dsh` 数据是直接删还是先备份（建议备份）
- [ ] node 运行时是否保留
- [ ] 小桔部署形态：Tauri 桌面 app 如何在 headless Ubuntu 上跑（VNC 远程桌面 / 仅后端服务化）——需另立方案，不阻塞本次卸载
