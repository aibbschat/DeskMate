# 服务器 81.69.163.170 可复用组件评估（卸 DSH 前必看）

> 时间：2026-10-01｜目的：在卸 DSH 前，找出能给小桔(DeskMate)复用的服务器组件
> 配套：server_81.69.163.170_dsh_recon.md

## 一、那个"在线界面"到底是什么（架构澄清）
- 控制台 `lightagent.cloud.tencent.com/...&settingsView=webuiControl` 是**腾讯云轻量应用服务器(Lighthouse)的"安全访问 WebUI"功能**，不是 DSH 自建。
- 证据（`/etc/dsh-auth/install-state.json`）：
  - `"behindTlsProxy": true`、`"listenAddress": "127.0.0.1"`、`"httpPort": 8080`、`"httpsPort": 443`
  - `"loginTokenEnabled": true`，错误提示："请到腾讯云轻量应用服务器控制台点击'安全访问 WebUI'重新登录"
- 流量链：**浏览器 → 腾讯云边缘(终结 TLS + 登录令牌鉴权) → 转发到本机 127.0.0.1:8080(带 X-Forwarded-Proto/Host/Real-IP) → Caddy → forward_auth 到 harness:3080**
- 结论：**公网 HTTPS 入口 + 鉴权是腾讯平台能力，与本机服务解耦**。只要本机 8080 有东西监听，隧道就会把公网流量导过去。

## 二、可复用组件清单
| 组件 | 现状 | 对小桔的价值 |
|---|---|---|
| **腾讯"安全访问 WebUI"隧道** | 平台能力，转发到 127.0.0.1:8080 | ★★★★★ 直接给小桔一个带鉴权的公网 HTTPS 入口，免域名/证书 |
| **Caddy 2.11.4** | 系统 dpkg 包已装（另有 DSH 私有副本 /usr/lib/dsh-auth/caddy 同版本） | ★★★★ 复用为小桔的反向代理/静态服务器，写极简 Caddyfile 即可 |
| **Python 3.12.3 + venv** | /usr/bin/python3，venv 可用 | ★★★★ 小桔后端(Python)直接 native 跑，无需容器 |
| **ufw 关闭 / 端口由安全组控** | ufw inactive | ⚠ 以后开端口走腾讯云安全组，不是本机 ufw |
| **dsh-auth 鉴权层** | 登录令牌 + forward_auth（DSH 专用） | ★★ 可选择性复用，但复杂且与 DSH 强绑定；既有腾讯隧道鉴权，建议不搬 |

## 三、不可复用（应卸）
- `deepseek-harness.service` + `/home/ubuntu/.dsh`（DSH 本体，被小桔取代）
- `/usr/lib/dsh-auth/*`（DSH 私有 caddy 副本 + 状态）
- `/etc/dsh-auth/*`（dsh-auth Caddyfile/env/secret/install-state）
- `dsh-auth-caddy.service` + `deepseek-harness.service.d/50-dsh-auth.conf`

## 四、修订后的"卸+留"方案（相比 recon.md 第四节）
**保留（复用）：**
1. 系统 caddy：`/usr/bin/caddy`（dpkg），不动；只删 DSH 私有副本 `/usr/lib/dsh-auth/caddy`
2. 本机 `127.0.0.1:8080` 这个监听位 → 交给小桔复用（让小桔 Web 层监听 8080，隧道即生效）
3. 腾讯"安全访问 WebUI"隧道（控制台里保持开启）

**删除（DSH 专属）：**
- `sudo systemctl stop+disable deepseek-harness.service dsh-auth-caddy.service`
- 备份 `/home/ubuntu/.dsh`（28M，无密钥）
- `sudo rm`：`/etc/systemd/system/deepseek-harness.service`、`.../50-dsh-auth.conf`、`/etc/systemd/system/dsh-auth-caddy.service`、`/home/ubuntu/.dsh`、`/home/ubuntu/.local/share/dsh-runtime`、`/home/ubuntu/.local/share/pnpm/dsh`、`/home/ubuntu/.local/share/pnpm/dsh-auth`、`/home/ubuntu/.local/share/pnpm/store`、`/etc/dsh-auth`、`/usr/lib/dsh-auth`
- `sudo systemctl daemon-reload`

**新建（小桔接入）：**
- 极简 `/etc/caddy/Caddyfile`：`http://127.0.0.1:8080 { reverse_proxy 127.0.0.1:<小桔端口> }`（沿用隧道要求的 X-Forwarded 头语义）
- 用系统 caddy 起干净 systemd 服务（标准 `caddy run` 单元）
- 验证：控制台"安全访问 WebUI"应落到小桔而非 404

## 五、仍需你拍板的两件事
1. **小桔以什么形态跑在 headless 服务器**：✅ 已查本地源码确认——**直接用方案 A（Web 服务化）**，无需 VNC。
   - 后端 = 独立 FastAPI/uvicorn 服务 `openworker-server`（`coworker/server/app.py:254` `app=FastAPI`；`run.py:182` `uvicorn.run`；`pyproject.toml:70` 入口点）。它**本来就是 headless 进程**，Tauri 只是 sidecar 父进程（`lib.rs:840` `Command::new(server_bin())`），且只有 Tauri 设了 `COWORKER_EXIT_WITH_PARENT=1` 才随 GUI 退（`orphan.py:60`）；自己启动即常驻。
   - 前端 = React+Vite+TS 单代码库 SPA，**Tauri 调用全部惰性降级**（`tauri.ts:1` `isTauri()` 判断，浏览器里 inert），API 走 `fetch` 非 Tauri invoke（`api.ts:27`），`vite.config` 设 `base:"./"` 可静态托管。
   - 端口默认 `127.0.0.1:8765`，可 `--host/--port` 或 `config.toml` 覆盖（`config.py:65-66`）。
   - 前端唯一缺口：浏览器构建需在 `index.html` 注入 `__COWORKER_HTTP__/__COWORKER_WS__/__COWORKER_API_TOKEN__`（当前仅 Tauri/dev 注入）；CORS 白名单仅 localhost（`app.py:36-41`）——用**同 origin 反代**规避，无需放宽正则。
   - 既有的 `reports/小桔上云方案_2026-09-23.md` 已把"云端 headless 实例"定为既定方向，本次代码核查完全一致。

## 六、确认后的 headless 落地拓扑（方案 A，最大化复用现有组件）
```
浏览器 ──公网HTTPS──> 腾讯"安全访问 WebUI"隧道(边缘TLS+鉴权)
        ──转发 127.0.0.1:8080──> Caddy(系统 caddy 2.11.4, 极简Caddyfile)
            ├─ 静态托管 surfaces/gui/dist/ (小桔SPA)
            └─ 反代 /v1 /ws /roundtable /a2a ──> openworker-server @127.0.0.1:8765
```
- openworker-server 起法：`COWORKER_API_TOKEN=<强随机> COWORKER_STATE_DIR=/var/lib/openworker DESKMATE_DIR=/var/lib/openworker/.deskmate openworker-server --host 127.0.0.1 --port 8765`（**不设** COWORKER_EXIT_WITH_PARENT，systemd 常驻；划词适配器可 `DESKMATE_SELECTION_DISABLE=1` 关）。
- 复用项：系统 caddy + `127.0.0.1:8080` 监听位 + 腾讯隧道（全保留）；DSH 专属全卸。
- 待办（部署阶段，非本次侦察）：前端 `index.html` 加地址/token 注入脚本；写极简 Caddyfile + systemd 单元；建 `/var/lib/openworker` 数据目录。
2. **Lighthouse 安全访问隧道目标端口**：✅ 已确认固定 8080（控制台侧写死，实例无修改选项）。小桔 Caddy 监听 127.0.0.1:8080 即对接，无需控制台改动。
