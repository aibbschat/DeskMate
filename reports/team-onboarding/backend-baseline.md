# 后端/核心引擎工程基线盘点（task-1）

> 角色：工程开发（backend / core engine）· 日期：2026-10-11
> 范围：openworker（可写工作区）与 ai-forum-v3（只读，`/Users/xiaocheng/workspace/ai-forum-v3`）
> 本文只做「现状 + 风险 + 建议」，**未修改任何产品源码**；对 ai-forum-v3 的一切修改建议均以指令/补丁建议形式写在文中，需由 Lead 转交用户执行。

---

## 0. 一页摘要

| 维度 | openworker | ai-forum-v3 |
|---|---|---|
| 形态 | 桌面个人 AI 助理（本地优先，开源） | AI 社交/内容网站（生产 aibbs.chat） |
| 后端 | Python 3.10+（venv 3.12.13），FastAPI + uvicorn，aisuite（git 钉版） | Node 22（本机 v22.23.1），Express 4（ESM） |
| 数据 | 本地文件 + SQLite（memory/history 私有库） | SQLite（开发）/ MariaDB（生产），双 schema 方言层 |
| 前端 | React 18 + Vite 5 + Tauri 2 + Tailwind | React 18 + Vite 5（纯 Web，Express 静态托管 dist） |
| 测试 | pytest 273 文件（约 6.9 万行）+ GUI vitest + Playwright e2e | node:test + supertest，server/tests 70+ 文件 |
| CI | GitHub Actions 3 job（pytest / GUI 单测 / GUI e2e） | **无 CI**（.github 仅 ISSUE/PR 模板） |
| Lint/类型 | 无（Python 侧无 ruff/mypy；GUI 有 tsc --noEmit） | 无（纯 JS、无 eslint） |
| 发布 | packaging/ 打 DMG / Windows 包 + 自动更新清单 | PM2 fork 单实例 + nginx + scripts/deploy.sh + 手工灰度 |
| 代码量 | coworker 6.2 万行 + deskmate 7.2 万行 + tests 6.9 万行 | server 约 2.7 万行（routes 5.6k / services 13k / lib 4.3k） |

---

## 1. openworker 工程基线

### 1.1 技术栈与依赖

- **Python**：`requires-python >= 3.10`；本机 `.venv` = Python 3.12.13，已装约 130 个包，pytest 9.1.1。
- **核心依赖**（`pyproject.toml`，包名 `coworker`，version 1.7.0）：
  - LLM：`openai` / `anthropic` / `google-genai`（Gemini 3 thought signature 需 2.x）/ `google-auth`（Vertex）；
  - 运行时：`fastapi` + `uvicorn[standard]` + `pydantic>=2` + `textual`（TUI）；
  - **aisuite 钉在 git commit**（`git+https://github.com/andrewyng/aisuite.git@cb29165…`）——PyPI 0.1.x 已移除 `toolkits/agents/tool`，只能钉 main 分支 commit；
  - MCP：`mcp>=1.28.1,<2`（2.0 移除 streamablehttp_client；下限为安全修复）；
  - 连接器：`httpx`（出站）、`websockets`（Slack Socket Mode relay）；
  - 自动化：`croniter`；文档：`pypdf`+`pypdfium2`+`python-docx`；搜索：`ddgs`；
  - A2A：`a2a-sdk>=1.1.2`（对外 agent 互操作）。
- **extras**：`dev`（pytest/pytest-asyncio/httpx）、`messaging`（python-telegram-bot/slack-bolt/aiohttp）、`browser`（playwright）、`bedrock`（boto3，lazy import）、`deskmate-embed`（fastembed + BGE-small-zh，约 285MB，不装自动降级为关键词检索）。
- **已知依赖冲突**：a2a-sdk 拉 `docstring_parser<0.16`，aisuite 要 `>=0.16`，pip 有解析警告（pyproject 注释已记录，实测可 import）。

### 1.2 入口与启动方式

4 个 console scripts：

| 命令 | 入口 | 作用 |
|---|---|---|
| `openworker` | `coworker.cli:main` | 通用 CLI（75 行薄壳） |
| `openworker-server` | `coworker.server.run:main` | 本地 agent 服务器（uvicorn + FastAPI），桌面 GUI sidecar 也走它 |
| `openworker-connectors` | `coworker.connectors.cli:main` | 连接器安装/授权 CLI |
| `ocw` | `coworker.teams.cli:main` | board/journal 开放面，含 `ocw board mcp`（外部 harness 挂的 stdio MCP） |

标准开发启动（README「Run from source」）：

 ```shell
bash packaging/setup_dev_env.sh                    # 创建 .venv 并 pip install -e ".[messaging,dev]"
.venv/bin/openworker-server --cwd ~/some/project --port 8765
cd surfaces/gui && npm install && npm run dev      # 浏览器 UI；或 npm run tauri dev 起桌面壳
 ```

- 服务器每次启动在 `<state-dir>/sidecar-8765.token` 写一次性 token；API 调用带 `X-OpenWorker-Token` 头；桌面壳用内存 token 不落盘。
- 服务器还加载 `~/.config/coworker/.env`（不覆盖已有环境变量，喂 deskmate 功能开关如 `DESKMATE_AUTO_INJECT`）。
- 测试：`.venv/bin/pytest`；GUI：`npm test`（vitest）、`npm run e2e`（Playwright，mock /v1 + WS 的封闭式 e2e，无需真模型）。

### 1.3 模块地图（coworker/ + deskmate/，按行数）

| 模块 | 行数 | 职责 |
|---|---:|---|
| `coworker/server/` | 14,767 | `app.py`（**7,369 行** FastAPI + WebSocket 全部 /v1/* 路由）+ `manager.py`（SessionManager）+ `a2a_server.py` / `opencode_a2a_server.py` |
| `coworker/connectors/` | 13,561 | 25+ 集成（Slack/GitHub/Gmail/GCal/Jira/Notion…）：`senders.py`、`relay_client.py`（Slack Socket Mode）、`gateway.py`、`setup.py`、accounts/portals/installs |
| `coworker/providers/` | 6,938 | openai / anthropic / gemini / bedrock / vertex / codex / opencode + `router.py`（多 provider 路由与降级）/ `registry` / `matrix`（能力矩阵）/ `paid_fallback` |
| `coworker/tools/` | 2,214 | files / shell / git / search / plan / todo / subagent / a2a / ask / toolreq |
| `coworker/history/` | 2,096 | FTS5 全文检索 + 私有存储 + vendored OpenCC（简繁变体，纯数据） |
| `coworker/mcp/` | 1,721 | MCP 客户端（stdio + streamable-http）+ OAuth + store + builtin |
| `coworker/memory/` | 565 | SQLite 记忆存储 + 检索工具 |
| `coworker/personas/` | 1,081 | YAML frontmatter manifest + builtin 人设（.md 随包分发） |
| `coworker/automation/` | 810 | cron 调度器（croniter）+ store + tools |
| `coworker/skills/` | 823 | 技能 catalog.json + store |
| `coworker/teams/` | 3,871 | board/journal 开放面 + stdio MCP server（OPE-100） |
| `coworker/web/` | 708 | 网页抓取工具 + guard |
| `coworker/sandbox/` | 463 | macOS sandbox-exec 写范围限制 |
| `coworker/governance/` | 158 | tool_guard + rules（写操作审批门） |
| `coworker/agents/` | 349 | 预置 agent（chat/code/cowork/editor/myhelper）+ registry |
| `coworker/` 根模块 | 11,509 | `engine.py`（**2,387 行** agent 主循环）`agent.py`（822）+ `config` `permissions` `risk` `audit` `secrets` `sessions` `conversations` `inbox` `compaction` `subscriptions` `workspace_trust` `selfwake` `unattended` `emotes` `provenance`… |
| **`deskmate/`** | **71,620** | **仓库最大子应用（小桔/桌搭）**：perception / memory / creation / garden / will（意志层）/ cognitive（认知层）/ emotion / agency / proactivity / roundtable / alter_ego / novel_memory 等 40 个子目录 |
| `tests/` | 69,159 | 273 个测试文件，`test_deskmate_*` 占大头 |

要点：**deskmate（7.2 万行）比 coworker 本体（6.2 万行）还大**，且与 coworker 同等打包（setuptools `include = ["coworker*", "deskmate*"]`），是当前复杂度与回归风险的中心。

### 1.4 测试组织

- `pyproject [tool.pytest.ini_options]`：`testpaths=["tests"]`、`pythonpath=["."]`（reviewer-eval 测试要 import 仓库根 scripts/）、`asyncio_mode="auto"`。
- `tests/`：conftest.py + fixtures/ + corpora/（史料语料）+ memory_bench/，273 个 test_*.py（约 6.9 万行）。
- GUI：`surfaces/gui` vitest 单测 + Playwright（hermetic：mock /v1 与 WS，不烧模型）。

### 1.5 脚本 / 打包 / CI

- `scripts/`（47 个）：一半是数据运维（wikisource 抓取、史料索引构建、层叠语料校验），一半是一次性迁移（migrate_memory_work_to_*、restore_cognitive_layer）+ 评测（eval_reviewer、bench_memory_recall）+ 部署（deploy_cloud_sync.sh、deploy_web.sh、install_sync_daemon.sh、xiaoju-sync.service）。
- `packaging/`：`setup_dev_env.sh`（dev bootstrap）、`build_dmg.sh` / `build_windows.ps1`（桌面打包）、`openworker-server.spec` + `server_entry.py`（PyInstaller sidecar）、`make_update_manifest.py`（自动更新清单）。
- CI（`.github/workflows/ci.yml`，push + PR）：
  1. `pytest`：Python 3.12，`pip install -e ".[messaging,dev,bedrock]"`，全量 `pytest tests -q`；
  2. `gui-unit`：Node 20，`tsc --noEmit` + `npm test`；
  3. `gui-e2e`：Playwright chromium，`npm run e2e`。

### 1.6 openworker 风险点

1. **aisuite 钉 git commit**：安装依赖 GitHub 可达 + git；断网/内网环境无法复现安装；升级需人工改 SHA。建议在文档里记录「当前 commit + 升级检查清单」。
2. **无 lint / 无 Python 类型检查**：CI 只有 pytest；`app.py` 7,369 行、`engine.py` 2,387 行是两个巨型热点文件，改错的兜底只有测试。
3. **deskmate 边界不清**：7.2 万行、40 个子模块（认知/情感/意志层），与 coworker 同包发布，缺一份独立架构文档；新成员无法快速判断「这个功能属于平台还是小桔」。
4. **测试全量单跑**：CI 与本地都是 `pytest tests` 全量，无 slow/fast 分层标记、无 xdist；单次回归成本高，容易让人「本地就不跑了」。
5. **仓库根部杂物**：`output/`（1,070 项）、`.preview`、`.novel_pipeline_fix`、`workbuddy-skin/`、`xiaoju-profile-center/`、根级 `*.html` 预览与旧研究报告 md —— 增加检索噪音与误打包风险（打包 `packages.find` 目前只圈了 coworker*/deskmate*，暂无实际打包污染，但根级 `__pycache__` 已存在）。
6. **工作树不干净**：当前有未提交的 reports/ 修改与未跟踪的 `.trae/`、`surfaces/gui/src-tauri/icons.backup-20261009/`，与上游开源 fork 同步时有冲突隐患。
7. **环境开关散落**：功能开关既在 `~/.config/coworker/.env`（`DESKMATE_*`）又在 config.py / GUI `flags.ts`，排查「为什么没生效」成本高。

---

## 2. ai-forum-v3 工程基线（只读）

### 2.1 技术栈与运行方式

- **运行时**：Node 22（本机 v22.23.1）；根 `package.json` `"type":"module"`（全 ESM）。
- **后端依赖**：express 4、cors、dotenv、jsonwebtoken、bcryptjs、better-sqlite3（开发/小站）、mysql2（生产 MariaDB）、@resvg/resvg-js（OG 卡片渲染）。dev：supertest。
- **命令**：
  - `npm start` → `node server/index.js`（端口 3001）；
  - `npm run dev` → `node --watch server/index.js`；
  - `npm test` → `node --test --test-concurrency=1 "server/tests/**/*.test.js"`；
  - `npm run dsh:install` → `bash dsh/install.sh`（DSH agent 预设安装）。
- **前端**（`web/`，独立 package.json）：React 18 + Vite 5 + react-router 6 + react-markdown/rehype-sanitize/remark-gfm + qrcode；`npm run dev`（5173）/ `npm run build`（产物 `web/dist` 由 Express 静态托管 + SPA fallback + SEO `publicPageGuard`）。
- **环境变量**（`.env.example`，130 行，注释详尽）：最小必填 `PORT`、`AI_FORUM_INTERNAL_TOKEN`（DSH 发布桥）、`JWT_SECRET`、`WEB_ORIGIN`、`DB_DRIVER=sqlite|mariadb`；LLM 路由 `LLM_{VOLCENGINE|DEEPSEEK|XIAOMI}_*`，按 purpose（CHAT/INSIGHT/VISION/PERCEIVE/MEMORY/WRITING）可 `LLM_MODEL_<PURPOSE>=<vendor>` 覆盖；另有封面图生成、活源池灰度位（`LIVING_POOL_ON`、`SEARCH_MONTH_BUDGET`）、MCP 桥等。
- **配置加载**（`server/config/index.js`，28 行）：`.env` → 叠加 `.env.brain`（override，由 `scripts/switch-brain.sh` 维护，只放模型路由覆盖，切大脑不动含密钥的主 .env）。设计很干净，值得 openworker 借鉴。

### 2.2 后端结构（server/，约 2.7 万行）

| 目录 | 行数 | 内容 |
|---|---:|---|
| `routes/` | 5,604 | 25 个路由文件：posts / agents / join / owners / auth / search / users / comments / engagement / messages / insight / snippets / chat / companions / social / admin / plaza / roles / security / entries / customerService / rooms / track / og / seo + `internal.js`（`/api/internal/dsh` 发布桥） |
| `services/` | 13,047 | 50+ 服务：`agentCycle.js`（342 行，感知→选题→写作→发布→记忆沉淀主循环 `runCycle`）、`agentService`、companion* 六件套、`memory/`（三层记忆）、`perception/`、plaza / moderation / seoPage / courier / dongaiCurator / webSearch / imageGen / chronicle / debate / growth… |
| `lib/` | 4,292 | 21 个横切件：ownerDeeds / ownerRecall / ownerImpressions / ownerSelfNotes（主人画像）、circleDigest、livingPool（活源池配额账）、rateGuard、trafficTrack、grayGate、dutyCard、skillCatalog |
| `database/` | 2,476 | `db.js` 方言层（sqlite / mariadb）+ `schema.sql` / `schema.mariadb.sql` **双 schema** |
| `dsh/` | 815 | `agents.js`（**10 个垂直 Agent 单一事实源**：username/displayName/domains/四层 persona 人格底色/cron schedule/maxArticlesPerDay/presetId）+ `brainAdapter.js`（457 行，调用外部大脑 + 结果解析/误报兜底） |
| `llm/` | 327 | 统一 OpenAI 兼容路由：多家供应商按 purpose 路由 + 自动降级 |
| `plugins/` | 199 | `loader` + `contracts` + perceive-rss / perceive-seed / perceive-web-search 三个感知插件 |
| `middleware/` | 227 | auth / userAuth / asyncHandler |
| `config/` | 28 | 见上 |

### 2.3 数据库

- **双方言**：`DB_DRIVER=sqlite`（默认，`./data/aiforum.db`）或 `mariadb`（生产，mysql2 连接池）。两份 schema（`schema.sql` / `schema.mariadb.sql`），有 `mariadb-schema.test.js` 做护栏，但 **schema 演进仍需人工双写**。
- **三层记忆**（`server/services/memory/`，README 写得非常好）：
  - episodic（TTL 90 天，容量 10 万/Agent）→ `consolidate`（LLM 驱动，失败降级纯函数）→ semantic（置信 <0.4 标疑→复核→作废，容量 1 万）；
  - procedural（写作戒律/引用规范，版本化追加，容量 1 千）；
  - 生命周期主线：**编码 → 巩固 → 提取 → 遗忘**，写入来源枚举 `perceive|publish|feedback|consolidate`。

### 2.4 Agent 运行链路（与 openworker 的关键差异）

 ```text
DSH 侧（dsh/run-agent.sh + presets/ + bundles/ + plugins/，npm run dsh:install 安装）
   │  cron schedule（server/dsh/agents.js 里每个 Agent 自带，如 "30 9,15 * * *"）
   ▼
感知（server/plugins/perceive-*：RSS / 种子源 / 网页搜索）
   ▼
选题（素材池阈值 MATERIAL_MIN_*）→ 写作（brainAdapter 调 LLM/外部大脑）
   ▼
发布（两条路：① mcp/aiforum stdio MCP publish_article；② /api/internal/dsh）
   │   鉴权 X-Internal-Token；24h 同 (agentId,title) 幂等
   ▼
runCycle 收尾：素材标记 + episodic 记忆沉淀 + externalPublished 防重复发布
   ▼
反思 / 读者评论回写 → 反馈记忆（source=feedback）
 ```

- `mcp/aiforum`：stdio MCP server，工具 `publish_article` / `health`；`mcp/baijia`：百家学院桥。
- **要点**：server 本身常驻只接请求，**Agent 的「脑子」跑在 DSH 侧**；server 提供发布桥与记忆沉淀。这与 openworker「引擎在本地进程内」的形态完全不同；两个项目真正的公共资产是「LLM 按 purpose 路由 + 降级」和「分层记忆」两套思路。
- 附注：根目录 `daily-cycle.sh.bak-20260923-194030` 是更早的 cron 自主周期方案（v2，用 opencode run 注入定时任务），已被 DSH 预设链路取代，属遗留物。

### 2.5 部署与运维

- 进程：PM2 fork 单实例（`ecosystem.config.js`：`ai-forum-v3`，cwd ./server，NODE_ENV=production）。
- 反代：nginx（`deploy/aibbs.chat.nginx.conf` / `.locations.inc`），`/api/*` → 3001，`/` → `web/dist`。
- 脚本：`scripts/deploy/{deploy.sh, rollback.sh, post-receive, server-setup.sh}` 及 `cutover-aibbs-chat.sh`；`docs/DEPLOY.md` 灰度红线明确（工作树必须干净、push 前备份 .env、reset 前有快照、真值不进文档）。
- 巡检：`scripts/patrol.sh`（git 最近提交里仍在改，是实际在用的运维面）。
- 注意项：`COVERS_DIR` 必须放在 releases/current 之外，否则跨版本部署封面 404（.env.example 已注明）。

### 2.6 ai-forum-v3 风险点

1. **无 CI/无 lint/无类型**：`.github` 只有 ISSUE/PR 模板；跑在生产的就是 main 分支 HEAD。测试虽有 70+ 文件且结构不错（node:test + supertest + 临时库），但没有自动触发。
2. **双 schema 人工同步**：两份 schema 双写，靠 `mariadb-schema.test.js` 兜底；schema 演进仍是最容易踩的坑。
3. **密钥双处存放**：`.env.example` 明确警告 WEB_SEARCH_API_KEY 在生产**另存了一份**在 `~/.dsh/.credentials.yaml`，轮换漏一处即「平台问答正常、大脑搜索静默失败」。归一未做。
4. **发布链路是最大稳定性风险面**：git log 显示 brainAdapter「误报成功 → 停更」出过生产事故（多次 fix：PUBLISHED 标记兜底、retryPolicy、反查 DB 补 postId）。这条链路值得单独立质量任务。
5. **生产单实例 fork**：无 cluster、JSON body 6MB、限流仅 `lib/rateGuard.js` 局部覆盖（需确认覆盖面）。
6. **仓库卫生**：根目录 40+ 篇「AI圈-*.md」设计文档与代码同级，`.trae/documents/` 还有一批未跟踪文档，`web/` 里 4 个 `vite.config.ts.timestamp-*.mjs` esbuild 崩溃残留，根 `node_modules` 152 项。`.env` 与 `.env.brain` 物理放在仓库目录内（已 gitignore，但 rsync/备份时易被带走）。
7. **工作树不干净**：`scripts/patrol.sh`、`server/dsh/brainAdapter.js`、`server/tests/brainAdapter.test.js` 有未提交修改，而 DEPLOY.md 红线要求部署时工作树必须干净——「开发中」与「要部署」状态在互相打架。

---

## 3. 两项目「开发—测试—发布」流程对比与缺口

### 3.1 现状流程

| 阶段 | openworker | ai-forum-v3 |
|---|---|---|
| 开发 | .venv + `openworker-server --port 8765` + GUI `npm run dev`（或 tauri dev） | `npm run dev`（node --watch 3001）+ `cd web && npm run dev`（5173） |
| 测试 | pytest 全量 + vitest + Playwright e2e；CI 三 job 自动跑 | `npm test`（node --test 串行）；**手工跑，无 CI** |
| Lint/类型 | 仅 GUI `tsc --noEmit` | 无 |
| 发布 | `packaging/build_dmg.sh` / `build_windows.ps1` + 自动更新清单；云端 sync 脚本 | git push（post-receive hook）→ deploy.sh → PM2 重启 → nginx；回滚 rollback.sh |
| 巡检 | 无专门机制（靠测试） | `scripts/patrol.sh` + DSH 日志 |
| 文档 | docs/ 40 篇设计/排查文档 | 根 40+ 篇设计文档 + docs/（TECH-SPEC/ROADMAP/DEPLOY） |

### 3.2 缺口清单（按优先级）

1. **ai-forum 无任何自动检查**（最高优先级）：生产站，改一行就可能上线；至少要 node --test + 一个 lint 工具。
2. **ai-forum 双 schema 无自动 diff 工具**：目前靠测试发现不一致，而非开发时阻止。
3. **openworker 无 Python lint/类型基线**：6.2 万行平台代码 + 7.2 万行 deskmate，全靠 pytest 兜。
4. **openworker 测试无分层**：273 文件全量跑，无 fast/slow 标记，本地回归成本高。
5. **两项目都缺「schema/契约变更」协作规约**：openworker 的 /v1/* 与 ai-forum 的 /api/* 均无契约测试（ai-forum 有 contract.test.js，算半个）。
6. **仓库卫生**：两项目根目录都有大量遗留物（详见 1.6-5 / 2.6-6），影响检索与打包安全。
7. **密钥管理不统一**：ai-forum 密钥双处存放；openworker 散在 `~/.config/coworker/.env` 与 SecretStore，缺一份「哪些 key 存在哪、怎么轮换」的清单。
8. **部署态与开发态打架**（ai-forum）：工作树常驻未提交修改，与 DEPLOY.md 红线冲突，需要「发布分支/发布前检查」的规约。

---

## 4. 建议的后续任务拆分（供 Lead 立项参考）

| # | 任务 | 项目 | 优先级 | 备注 |
|---|---|---|---|---|
| 1 | ai-forum 补最小 CI：node --test + eslint（或先只加 node --test 的 Actions / 本地 pre-commit） | ai-forum | P0 | 只读仓库 → 以「操作指令 + workflow 文件补丁」形式交用户执行 |
| 2 | ai-forum schema 双写 diff 工具（对比两份 schema.sql，挂进 npm test） | ai-forum | P0 | 纯脚本，收益大 |
| 3 | ai-forum 发布链路（DSH → MCP/internal API → publish → 记忆沉淀）端到端冒烟与可观测性方案 | ai-forum | P0 | 历史事故集中地 |
| 4 | openworker 引入 ruff（+ 可选 mypy 分层严格度）并挂 CI | openworker | P1 | 需任务明确授权才动源码/CI |
| 5 | openworker pytest 分层（fast/slow marker + CI 全量、本地默认 fast） | openworker | P1 | 同上 |
| 6 | deskmate/ 架构边界文档（40 子模块地图 + 与 coworker 的调用面） | openworker | P1 | 新成员 onboarding 刚需 |
| 7 | 两项目仓库卫生清理清单（含 AI圈-*.md 归档到 docs/design 的方案） | 双项目 | P2 | ai-forum 侧只出建议 |
| 8 | 密钥清单与轮换 SOP（含 WEB_SEARCH_API_KEY 双处归一方案） | 双项目 | P2 | 安全项 |
| 9 | 公共资产评估：两项目「LLM purpose 路由 + 分层记忆」是否值得抽共享库/共享规约 | 双项目 | P3 | 先做设计评估，不急于动手 |

---

## 5. 附：快速启动速查

**openworker**

 ```shell
bash packaging/setup_dev_env.sh
.venv/bin/openworker-server --cwd /path/to/project --port 8765   # API: X-OpenWorker-Token
cd surfaces/gui && npm install && npm run dev                     # 浏览器 UI；npm run tauri dev = 桌面
.venv/bin/pytest tests/test_config.py -q                          # 后端单测示例
cd surfaces/gui && npm test && npm run e2e                        # GUI 单测 + e2e
 ```

**ai-forum-v3**

 ```shell
cp .env.example .env            # 最小必填：PORT / AI_FORUM_INTERNAL_TOKEN / JWT_SECRET / WEB_ORIGIN / DB_DRIVER
npm start                       # 或 npm run dev（node --watch），端口 3001
npm test                        # node --test --test-concurrency=1 server/tests/**
cd web && npm install && npm run dev    # 前端 5173，/api 反代到 3001
cd web && npm run build         # 产物 web/dist，生产由 Express/nginx 静态托管
 ```

---

## 6. 数据来源说明

- openworker：`pyproject.toml`、`README.md`、`.github/workflows/ci.yml`、`packaging/setup_dev_env.sh`、`surfaces/gui/package.json`，以及对 `coworker/`、`deskmate/`、`tests/`、`scripts/`、`docs/` 的目录与行数统计；`.venv` 实测 Python 3.12.13 / pytest 9.1.1。
- ai-forum-v3：`package.json`、`web/package.json`、`.env.example`、`server/index.js`、`server/config/index.js`、`server/dsh/agents.js`、`server/services/memory/README.md`、`server/services/agentCycle.js`、`mcp/aiforum/index.js`、`ecosystem.config.js`、`docs/DEPLOY.md`，以及两仓库 `git log` / `git status` 现场状态。
- ai-forum-v3 全程只读，未做任何修改。
