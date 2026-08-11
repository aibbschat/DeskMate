# OpenWorker 插件化进化路线（经代码核对版）

> 配套 `plugin_center_design.md`。本文把"专家中心→插件中心"从设计落为**具体改造项**，
> 所有论断均已 `grep` + 读源码核对（2026-08-11）。

## 一、核对结论：现状痛点逐条验证

| # | 痛点（萧哥诊断） | 代码事实 | 证据 |
|---|---|---|---|
| 1 | 自家 server 手工硬编码进 `~/.config/coworker/mcp.json`，指向 repo 的 `.venv/bin/python` | 确认。`command` 写死绝对路径 `/Users/xiaocheng/workspace/openworker/.venv/bin/python`，`cwd` 写死 repo 根 | `~/.config/coworker/mcp.json`（5 个 server 全如此） |
| 2 | 代码里没有任何 bootstrap 去种这些配置 | 确认。`mcp/config.py` 只 `read`（load/add/patch/delete），**从无"写默认"逻辑**；`run.py` 只 `_spawn_selection_adapter`，不碰 mcp.json | `coworker/mcp/config.py`、`coworker/server/run.py:74` |
| 3 | 换台机器 / 换路径就废 | 确认。路径是裸绝对路径，无 `${VAR}` 解析、`${COWORKER_VENV}` 之类的自举变量 | mcp.json 第 4/11/18/25/32 行 |
| 4 | roundtable 硬写进 app（起 9920 端口） | 确认。`deskmate/roundtable/web.py` 自带 FastAPI，`--port 9920` 默认；`app.py:323` 把它 mount 进主 app，但 server 本身独立于插件层 | `deskmate/roundtable/web.py:13,203,530` |
| 5 | A2A server 焊死 | 确认。`coworker/server/a2a_server.py` + `opencode_a2a_server.py` 在 `app.py:321-323` 直接 import 并 `register_a2a` 并入本 app | `coworker/server/app.py:321` |
| 6 | `/v1/deskmate/*` 一堆 REST 路由焊进 app | 确认。`app.py:1578-2510` 共 30+ 路由（memory/vault/growth/badges/digest/report/proofread/manuscript…），全手写装饰器 | `coworker/server/app.py` 1578 行起 |
| 7 | proactivity 后台进程焊死 | 确认。`deskmate/proactivity/background.py` 独立后台；`run.py` 未自举，靠 feature flag `DESKMATE_PROACTIVE_BACKGROUND` 在运行时开关，无插件注册 | `deskmate/proactivity/background.py` |
| 8 | Catalog 封闭，加新能力=改 Python 源码 | 确认。`catalog.py:10` 明示 **"platform-owned and closed"**，所有 capability 硬编码在 `_CAPS` 列表，第三方只能"我们加"或靠 MCP | `coworker/catalog.py:10,104` |
| 9 | Persona 只能引用已有能力，不能自带代码 | 确认。persona 的 `tools:` 是 catalog id 列表，`expand()` 只从 `CATALOG` 取；无"persona 自带 server/脚本"通道 | `coworker/catalog.py:173`（expand） |

**关键认知（萧哥补全，已确认）**：`mcp.json` 已是 Claude Desktop / Cursor / Codex 兼容格式
（`config.py:8` 明写 "Paste-compatible with Claude Desktop / Cursor / Codex"）。
→ **"兼容大厂插件"在文件格式层面已经成立**，缺的只是：
① 自家 server 的自举安装；② 一键连接/安装的 UI 与流程；③ 把"装 server"和"装 persona"打包。

## 二、自带 5 个 server 即"预装插件"（实锤清单）

目前 `~/.config/coworker/mcp.json` 里已存在、可作为**第一批预装插件**的 5 个 MCP server：

| server 名 | 模块 | 能力 |
|---|---|---|
| `novel-memory` | `deskmate.novel_memory.server` | 人物圣经 v2 工具（可核验实体增删查） |
| `perception` | `deskmate.perception.server` | 感知/环境上下文 |
| `deskmate-memory` | `deskmate.memory.server` | 分层记忆（journal/longterm） |
| `deskmate-proactivity` | `deskmate.proactivity.server` | 主动推送能力暴露为工具 |
| `deskmate-skills` | `deskmate.skills.server` | 技能/专家工具集 |

这 5 个本身就是标准 MCP server（stdio + `-m` 模块入口）。
**改造把它们"转正"为随包预装的插件**，而不是用户手填路径。

## 三、改造一：自家 server 自举注册 + 随包安装

### 目标
换机器、换 venv 路径、换工作区，`openworker-server` 启动即自动种好这 5 个预装 server，
不再依赖人工写死的 `~/.config/coworker/mcp.json`。

### 做法
1. **新增"内置 server 清单"模块**：`coworker/mcp/builtin.py`，声明 5 个 server 的逻辑定义
   （模块名 + 默认 enabled + 权限等级），`command`/`cwd` **不写死**，运行时由 `sys.executable`
   和包根推导：
   ```python
   BUILTIN_SERVERS = {
       "novel-memory": {"module": "deskmate.novel_memory.server", "requires_approval": False},
       # ... 其余 4 个
   }
   def _render_builtin(name, cfg) -> dict:
       return {
           "command": sys.executable,                       # 指向当前运行解释器
           "args": ["-m", cfg["module"]],
           "cwd": str(importlib.resources.files("deskmate").parent),  # 包根
           "enabled": True,
           "requires_approval": cfg["requires_approval"],
       }
   ```
2. **bootstrap 函数**：`ensure_builtin_servers()`，在 `run.py` 启动时（紧随 `_load_env_file`）
   调用：读取现有 global mcp.json，对 5 个内置 server **只在缺失时写入**（不覆盖用户后续改动）；
   用 `sys.executable` 替换裸绝对路径，根治"换机器就废"。
3. **随包安装钩子**：在 `pyproject.toml` 的 `coworker` entry point 或 Tauri 安装后脚本里调用
   同一 `ensure_builtin_servers()`，保证新装/升级后预装 server 始终在位。
4. **安全不变**：沿用现有 trust 闸门（workspace `.coworker/mcp.json` 仍需 trust），
   内置 server 只写 global 文件，不碰 workspace 层。

### 收益
- 跨机器 / venv 路径迁移零成本；
- 5 个创作护城河能力**默认即开**，新用户体验"开箱有插件"；
- 为"预装插件"概念立模板，后续新增内置能力不再手改用户文件。

### 风险
- 用户可能已手改了某 server 的字段 → bootstrap 必须"缺失才写、存在不覆盖"，且保留 `patch` 语义。
- ⚠️ **`sys.executable` 冻结坑（已验证真实并已修复，2026-08-11）**：
  冻结 DMG（`build_dmg.sh` / PyInstaller onedir）里 `sys.executable` 指向 `OpenWorker.app/Contents/Resources/sidecar/openworker-server`
  这一**冻结二进制**，不是 python 解释器；PyInstaller 二进制不吃 `-m`，故原本的 `sys.executable -m deskmate.novel_memory.server`
  会变成"再拉起一个 uvicorn 主服务"而彻底起不来。同时 spec 原先只 `collect_submodules` 了
  `coworker/aisuite/mcp/ddgs/croniter/docstring_parser`，**不含 `deskmate`**，冻结包里根本没有这些 server 模块。
  修复：`builtin.py` 检测 `sys.frozen` 后改为发射 `openworker-server --mcp-run <module>`，`run.py::main`
  在 argparse 前拦截该标记走 `runpy.run_module`（等价 `python -m`）；spec 补收 `deskmate`。
  → 本地 `.app`（build_local_app.sh 走 venv 回退链，`sys.executable`=venv python）仍走原 `-m` 分支，不受影响。
  roadmap 原第 79-80 行"需验证"已闭环。

## 四、改造二：add_mcp / 一键连接成为标准安装路径

### 目标
外部 server（大厂官方 / 社区）的安装，从"手抄 JSON 进 mcp.json"升级为
**标准 API + UI 一键连接**，复用现有 `put_global_server` 写入通道。

### 做法
1. **已有地基**：`coworker/mcp/config.py:126` 已有 `put_global_server/patch/delete`，
   支持 REST 改 global 文件。无需重建，只需**暴露到插件中心 UI + 加校验**。
2. **一键连接流程**：插件中心"添加 MCP"表单 = URL（http/sse/streamable-http）或
   命令（stdio + args + env）。提交即 `put_global_server(name, config)`，并重载 MCP client
   （改造三的"热插拔"依赖此）。
3. **OAuth 直连**：`MCPServerDef.auth="oauth"` 已支持浏览器 OAuth 2.1+PKCE（mcp/oauth.py），
   大厂托管服务（如阿里百炼 One Key、腾讯元器）可直接走这条，无需用户手抄 token。
4. **连接校验**：写入前 `try_connect()` 探活（list tools），失败回滚并提示；
   成功后把 server 暴露的 tools 自动并入 catalog 之外的"外接能力池"。

### 收益
- 大厂/社区 MCP 插件"填 endpoint/key 即用"，正是"接入即用"的可运行落地；
- 复用 Claude/Cursor/Codex 兼容格式，用户甚至能把别人分享的 mcp.json 直接导入。

## 五、改造三：catalog 开放 mcp 引用 + 插件包（server+persona 打包）

### 目标
- 改造三A：让 persona 的 `tools:` 能引用外接 MCP server 的工具（`mcp:<server>:<tool>`），
  治"catalog 封闭 / persona 不能自带代码"（核对事实 #8/#9），且**不开放第三方改 Python 源码**。
- 改造三B：一个"插件包"= 一个或多个 MCP server + 一个或多个 persona 的原子安装，
  把"装能力"体验闭环。

### 改造三A：catalog 开放 mcp 引用分支（已实现）
1. `catalog.py` 新增 `is_mcp_ref` / `parse_mcp_ref` / `MCP_REF_PREFIX`：
   - `expand(ids, ctx)` 遇到 `mcp:` 引用**跳过**（MCP callable 由 session 层注入，不在离线 catalog 里展开）。
   - `risk_summary` 同样跳过 mcp 引用（其风险由 server 的 `requires_approval` 闸门治理）。
2. `personas/manifest.py::_validate_tools` 放行**格式合法**的 `mcp:<server>:<tool>` 引用
   （畸形 ref 仍报错），不再把外接引用当"未知能力"拒绝。
3. `SessionManager.prepare_mcp_tools` 新增 `mcp_refs` 参数：若 persona 声明了某 server 的工具，
   该 server 的 callable 只暴露**被引用的那几个**（`_mcp_tool_local_name` 从 `mcp__<s>__<t>`
   反解本地工具名后按白名单过滤）；未声明的 server 仍按原逻辑全量注入（行为不变）。
4. `app.py` WS 会话入口计算 `_persona_mcp_refs(manager, agent)` 传入，打通"persona → 外接工具"。

### 改造三B：插件包（已实现）
1. **插件包格式**（两种都接受）：
   - 单文件 `.json`/`.yaml`：`{"name", "servers":[{name, config}], "personas":[manifest文本 | {meta, body}]}`
   - 目录：`mcp.json`（`mcpServers`）+ `personas/*.md`
2. `coworker/mcp/package.py`：
   - `validate_plugin_package(path)`：dry-run 预校验（server 需 command/url、persona 需能 parse），供 UI 预检。
   - `install_plugin_package(path)`：**先写 server（全局 mcp.json 单次原子重写，便于回滚）→ 再装 persona
     （复用 `PersonaRegistry.install_from_dir`，快照 + disabled 待用户同意）**；任一步失败回滚 server 集，
     不留半装状态。persona 仍遵循治理红线（不自启、不提权）。
3. `app.py` 新增 REST：`POST /v1/plugins/validate`（预检）、`POST /v1/plugins/install`（安装，
   支持 `path` 或 inline `payload`），本地操作。

### 关键约束（来自核对事实 #9）
persona 之前**不能自带代码**——这是 catalog 封闭的直接后果。本改造让 persona 能"指向"
外接 server（代码在 server 侧，不在 persona 侧），从而在**不开放改 Python 源码**的前提下
兼容 catalog 封闭现状，又把能力边界从"只能引用内置"扩到"可引用外接 MCP"。

## 六、策展口径（已与萧哥确认）

- **国内**：走"大厂官方打包"——阿里百炼 One Key、腾讯元器、字节扣子、百度千帆的官方 MCP。
  质量可控、OAuth 直连。
- **国外**：走"cherry-pick 优秀独立 server"——GitHub、Brave Search、Fetch、高德国际版等
  社区/独立 server，**不铺国外大厂量**（OpenAI/Google 自身暴露的 MCP 端点极少）。
- **默认安全策略**：沿用 `requires_approval` 默认 `True` + CRITICAL 拒杀 + 写入路径作用域；
  插件中心做厂商/领域/权限等级分类与评级，写操作默认需确认。

## 七、落地排序（ROI 从高到低）

| 顺序 | 改造 | 工作量 | 为什么先 | 依赖 | 状态 |
|---|---|---|---|---|---|
| 1 | 改造一：内置 server 自举注册 | ~1 天 | 直接治"换机器就废"，5 个预装插件开箱即用 | 无 | ✅ 已提交 `07b34b0` |
| 2 | 改造二：add_mcp 一键连接 + 探活 | ~1-2 天 | 解锁大厂生态"接入即用"，最有说服力 | 无 | ⚠️ 后端 80% 已存在（`POST /v1/mcp`/`/connect`/`/reload`/`mcp_tools` 齐全）；缺前端"添加→连接→探活"串联 UI + 同步写入即探测护栏（探测须走异步 connect，同步会跨 loop 冲突） |
| 3 | 圆桌 MCP 热插拔（build-before-swap） | ~2 天 | 让改二装的 server 不重启即生效 | 改二 | ⬜ 待做 |
| 4 | 改造三：插件包（server+persona 打包） | ~2-3 天 | 把"装能力"体验闭环 | 改二 | ✅ 已落地（三A catalog 引用 + 三B 插件包，本回合） |
| 5 | catalog 开放 mcp 引用分支 | ~1 天 | 让 persona 引用外接工具，治封闭 | 改三 | ✅ 并入改造三A |
| 6 | Spawn Subagent（隔离子代理） | ~2-3 天 | 治续聊重跑债、长任务隔离 | 圆桌现状 | ⬜ 待做 |

**建议起点**：先做改造一 + 改造二——前者把"预装插件"从概念变现实，后者把"大厂插件接入即用"
从 PPT 变可跑。两项合起来即可在插件中心展示"5 个预装 + 一键加外部"，生态骨架就立起来了。

## 七·五、本轮实施状态小结（2026-08-11 第二回合）

- **改造一（自举注册）**：`coworker/mcp/builtin.py` + `run.py` 启动自举 + `ensure_builtin_servers`
  幂等非破坏。`test_mcp_builtin.py` 5 passed。真实 `~/.config/coworker/mcp.json` 已迁移
  （5 个 server command 改写为 `sys.executable`，并盖 `_builtin` 标记）。提交 `07b34b0`。
- **改造三A（catalog mcp 引用）**：`catalog.py` 新增 `is_mcp_ref`/`parse_mcp_ref`，`expand`/`risk_summary`
  跳过 mcp 引用；`manifest._validate_tools` 放行合法引用；`prepare_mcp_tools` 支持 persona 工具级
  过滤；`app.py` 打通 `_persona_mcp_refs` 传入。
- **改造三B（插件包）**：`coworker/mcp/package.py`（`validate`/`install` 原子+回滚）+ `app.py`
  `POST /v1/plugins/validate|install`。`tests/test_plugin_center.py` 10 passed。
- **测试现状**：MCP 回归 18 + persona 加载 + 插件中心 10 = 40 passed。
- **下一步**：改造二前端串联（纯 UI）+ 圆桌 MCP 热插拔 + Spawn Subagent。

## 七·六、marketplace 预设区已填充（2026-08-11 第四回合）

- **改造二前端串联 + 圆桌热插拔 + Spawn Subagent**：已落地（commit 7f8374a）。ManageTabs AddForm 结构化（stdio/HTTP + args/env + OAuth），add→connect→探活回显「connected · N tools」；圆桌热插拔 build-before-swap + `hot_reload_mcp` 注入运行中 session；Spawn Subagent `per_persona_history` 隔离上下文。
- **冻结 DMG 坑**：已验证真实并修复（f60227f + d013994），`runpy` 分发前重置 `sys.argv`。
- **marketplace 预设区（MCP_PRESETS）已填充高使用度办公 server**：阿里百炼·联网搜索（HTTP+Bearer，`DASHSCOPE_API_KEY`）、腾讯元器·办公工具（HTTP+Bearer，yuanqi-tool：OCR/PDF/搜索/文生图/文档阅读/翻译/写作）、Fetch（uvx，无需密钥）、Brave 搜索（`BRAVE_API_KEY`）、Notion（`NOTION_TOKEN`）、Granola（OAuth）。预设卡支持 key 内联输入（HTTP 用 `header` 模板、stdio 注入 `env`），点击即 add+connect。字节扣子因精确插件 MCP URL 需按插件发凭证、未硬编码进预设（用户可经 Add 表单粘贴）。
- **仍属增强项（未做）**：插件包安装前端入口；圆桌热插拔注入运行中 session 的 UI 接线（当前按钮走全局重载）。

## 八、不做的（明确排除）

- **不逐个对接腾讯/阿里私有 API**：统一走 MCP 这一层适配，一层 = 全部大厂，避免维护 N 套 SDK。
- **不开放第三方改 `catalog.py` 源码**：catalog 封闭是安全决策（能力需 vetted），外接能力走 MCP 旁路。
- **不自建插件市场**：依赖 MCP 社区 11000+ server + 大厂官方托管，自己做分发不现实（萧哥原话）。
