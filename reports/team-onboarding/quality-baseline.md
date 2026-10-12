# 【代码质检】两项目质量与测试基线盘点

> 盘点时间：2026-10-11 | 盘点人：code-reviewer（QA + Code Review）
> 范围：openworker（/Users/xiaocheng/workspace/openworker）与 ai-forum-v3（/Users/xiaocheng/workspace/ai-forum-v3，**只读**）
> 方法：配置与文档走读 + 实测快速测试子集 + 类型检查实跑。**未修改任何源码。**

---

## 0. 一页结论

| 维度 | openworker | ai-forum-v3 |
|---|---|---|
| 测试规模 | 273 个 pytest 文件 / 收集 **3990** 个用例；GUI 另有 47 个 vitest 文件（316 用例）+ 73 个 Playwright e2e spec | 68 个 `node:test` 文件 / **553** 个用例（后端全覆盖）；前端仅 2 个 esbuild 手工测试脚本 |
| 本次实测（快速子集） | ✅ 54 passed（5.7s）+ 102 passed / **1 failed**（已知红，见 §1.6）；GUI vitest 313 passed / **3 failed**；GUI `tsc --noEmit` ✅ 通过 | ✅ 全量 552 passed / **1 failed**（138s）；`web` tsc ✅ 通过 |
| Lint | ❌ 无 ruff / flake8 / pre-commit（Python 侧零 lint） | ❌ 无 eslint / prettier / biome（前后端零 lint） |
| 类型检查 | ✅ GUI 有 tsc（CI 强制）；Python 无 mypy | ✅ `web/` 手动 `tsc --noEmit`（仅约定，无 CI） |
| CI | ✅ `.github/workflows/ci.yml` 三 job（pytest 全量 + GUI 单测 + e2e）+ `release.yml` 桌面打包 | ❌ **无任何 CI 工作流**（.github 只有 PR/Issue 模板） |
| 已知红管理 | ⚠️ 有据可查的 19 例历史已知红（2026-10-02 记录），无 xfail 标记隔离 | ⚠️ CONTRIBUTING 自认「少量预存在失败」，未列清单 |
| 质量文档 | ✅ PITFALLS.md / AUDIT_REPORT.md / .workbuddy 日志，沉淀文化好 | ✅ AI圈-网站质量巡检报告-2026-09（产品向）+ patrol.sh 运维巡检 |

**总评**：openworker 的测试资产规模和 CI 完备度显著领先，但「已知红」长期挂账 + Python 零 lint 是当前最大质量风险；ai-forum-v3 后端测试纪律不错（553 用例全跑仅 1 红），但**完全没有 CI、没有 lint、前端几乎无测试**，质量完全依赖开发者本机自觉。

---

## 1. openworker 质量基线

### 1.1 测试结构

- `tests/`：273 个测试文件，约 2566 个顶层 `test_*` 函数，pytest 收集 **3990 个用例**（`--collect-only` 实测 2.55s）。测试代码约 6.9 万行。
- 覆盖面按目录/前缀组织，重点清晰：
  - `test_deskmate_*`（约 80 文件）：DeskMate 灵魂层（记忆/成长/情绪/日记/花园/圆桌）——最大测试群；
  - `test_memory_*` / `test_p1_*` / `test_p2_*` / `test_p3_*`：记忆分层与时效、锚定回忆、教训、意图执行；
  - `test_permissions_*` / `test_self_protection` / `test_sandbox*` / `test_tool_guard` / `test_egress*`：**安全地板**（这是本项目最值得肯定的测试资产）；
  - `test_*_provider` / `test_provider_router` / `test_llm_circuit_breaker` / `test_paid_fallback`：多 Provider 路由与熔断；
  - `test_mcp*` / `test_connectors*` / `test_slack_*`：外设与连接器，且 `conftest.py` 提供 **FakeSlack 进程内测试线束**（真实 slack_bolt 栈、零网络），是行业级做法。
- `conftest.py`（共享夹具）质量很高：autouse 夹具强制隔离 `COWORKER_STATE_DIR` / `COWORKER_SCRATCH_BASE` / `DESKMATE_DIR`、显式关闭 `DESKMATE_EMBEDDING_BACKEND`（防止测试依赖外网下载 102MB 模型）、清除 `COWORKER_API_TOKEN`——注释里写清了每个隔离对应的历史事故（如 2026-07-03 真实遥测泄漏到生产库）。**测试不会污染真机数据，这一条达标。**

### 1.2 本次实测结果（快速子集，总耗时 < 2 分钟）

| 命令 | 结果 |
|---|---|
| `pytest tests/test_config.py tests/test_environment.py tests/test_command_matching.py tests/test_builtin_personas.py -q` | **54 passed**，5.7s |
| `pytest tests/test_engine.py tests/test_sandbox.py tests/test_permissions_risk.py tests/test_memory.py -q` | **102 passed / 1 failed**，4.8s |
| `cd surfaces/gui && npx vitest run` | **313 passed / 3 failed**（47 文件，16s） |
| `cd surfaces/gui && npx tsc --noEmit` | ✅ exit 0 |

> 按任务约束未跑全量（3990 用例）。历史记录（.workbuddy/memory/2026-10-02.md）显示全量约 **19 failed / 3464 passed**（当时收集数），CI 配置为每次 push 全量执行。

### 1.3 失败清单（本次实测 4 例，全部已定性，非新问题）

**Python 侧 1 例 —— 已知 A 类「权限地板」红（需 owner 拍板，质检不擅动）**

- `tests/test_permissions_risk.py::test_write_local_path_scoped`：`Mode.BYPASS_APPROVALS` 下 `write_file` 路径逃逸（`../bad.py`）被放行，`reason='full access'`。
- 根因已在 2026-10-02 定性：`coworker/permissions.py:361-384` 的 bypass 前置短路（本地提交 `56bef881`，2026-08-31）跳过了 path scoping / 持久权威 / 项目保护三道地板。同类已知红共 **14 例**（auto_approve×4、self_protection×4、decision_matrix_golden×2、egress×2、permissions_risk×1、tools_permissions×1）。⚠️ `Mode("auto")→BYPASS_APPROVALS` 别名陷阱：名字带 `auto` 的用例实际跑 bypass。
- **质检意见**：这 14 例是安全策略分歧的真实哨兵，不是噪声。在 owner 拍板前，正确做法是用 `pytest.mark.xfail(strict=True)` 显式挂账（注明 PITFALLS §十五 引用），让 CI 恢复「红=新回归」的信号价值；**不要删断言、不要改 permissions.py**。

**GUI 侧 3 例 —— 测试与实现漂移（可修测试/色值，不涉及安全）**

1. `skinRegistry.test.ts` WCAG 护栏：`autumn-maple`（秋枫，`ae4aa4f` 2026-10-02 新增）dark 模式 muted/paper 对比度 **3.71 < 4.5**。→ 真实无障碍缺陷，应调色过线。
2. `skinRegistry.test.ts` CSS↔registry 同步护栏：`starry-night`（星空夜晚，`6b833c1` 2026-10-02 新增）registry 声明 `--paper: #12132C`，`styles.css` 同 id 选择器是 `#EEF2FF`（亮色值）→ 两处漂移，暗色皮肤可能显示异常。护栏测试正确拦住了它。
3. `Sidebar.test.tsx`「Choose a persona」：断言英文 aria-label，但 `src/i18n/index.ts` 默认语言是 **zh**（`initialLanguage()` 兜底 `"zh"`）→ 测试环境拿到「选择角色」。测试未随 i18n 默认值调整。修法：测试内显式 `i18n.changeLanguage("en")`，或断言改用 `t()` 产物。

> 值得注意：3 例 GUI 失败里 2 例是**护栏测试逮住 10-02 新增皮肤的真实缺陷**——说明 GUI 测试护栏在起作用；但也说明新增皮肤的提交没跑本地单测就入库了（见 §5 流程建议）。

### 1.4 Lint / 类型检查

- **Python：零 lint**。pyproject.toml 无 `[tool.ruff]` / `[tool.mypy]`，`.venv` 中无 ruff/mypy/coverage/pytest-cov；scripts/ 无 lint 脚本。当前唯一静态保障是人工 `py_compile`。
- **GUI：有 tsc 无 eslint**。`npm run build` 含 `tsc && vite build`，CI 的 gui-unit job 也跑 `npx tsc --noEmit`；但无 eslint/prettier 配置（代码风格全靠自觉）。
- 建议引入顺序：**ruff（lint+format，零配置起步）→ ruff 作为 CI gate → mypy 只对 `coworker/permissions.py`、`coworker/engine/` 等核心模块渐进 strict**（全仓 mypy 一次性开会淹没人）。

### 1.5 CI 现状

`.github/workflows/ci.yml`（push + PR 触发，Python 3.12 / Node 20）：

| job | 内容 | 评价 |
|---|---|---|
| `pytest` | `pip install -e ".[messaging,dev,bedrock]"` → `pytest tests -q` 全量 | ✅ 覆盖全，但**当前必然红**（19 例已知红在 CI 上同样失败，见 §1.6） |
| `gui-unit` | `npm ci` → `tsc --noEmit` → `npm test`（vitest） | ⚠️ 同上，当前 3 例红会挡住流水线 |
| `gui-e2e` | Playwright chromium，hermetic（mock /v1 + WS，无需模型/网络） | ✅ 设计良好 |

`release.yml`：tag 触发的桌面打包（macOS arm64/x64 + Windows），与本地 `packaging/build_dmg.sh` 同脚本，含版本漂移校验（tag ≠ tauri.conf.json 版本即 fail）。发布准入意识好。

⚠️ **CI 真实运行状态存疑**：当前分支 `fork/base` 推送到 `backup`（github.com/thomas-l118774/DeskMate），`origin` 是上游 andrewyng/openworker（只读、本地落后 500 提交）。CI 是否在 backup 仓库实际跑、最近是否绿，本次盘点无法从本地确认。**建议 Lead 确认 backup 仓库 Actions 开关与最近一次 run 状态**——如果 Actions 未启用，§1.5 的一切只是纸面资产。

### 1.6 已知红管理（最大的流程缺口）

- `.workbuddy/memory/2026-10-02.md` 已完整定性 19 例红：**14 权限地板（A，须 owner 拍板）+ 2 sandbox-exec（B，会话沙箱环境限制，非代码）+ 3 测试与实现脱节（C，可修）**。定性质量很高，但这些知识只活在日志里，**测试文件本身没有任何 xfail/skip 标记**——任何人跑全量都看到 19 红，无法区分「挂账」与「新回归」。
- 历史教训（PITFALLS §十二）：3 例「恒红的既有红」最后被查出是真 bug（模块级路径常量无视 DESKMATE_DIR）。**恒红别只当噪声**——这正是需要 xfail(strict) 的理由：strict xfail 若突然「意外通过」会立即报出来，逼人复核。

### 1.7 历史质量文档

- `PITFALLS.md`：非显然坑清单（复现+根因+正确做法），质量标杆级文档；§十五 直接关联上述权限红。
- `AUDIT_REPORT.md`（2026-08-06）：P0（SQLite 连接泄漏、单例竞态、裸 fetch 绕过 authedFetch）/ P1（70+ 处静默 `except: pass`、巨型文件 lib.rs 34951 行 / manager.py 4041 行、无依赖锁文件）已修复或记录在案。**注意该报告已两个月，其 P1 建议（拆巨型文件、补日志）未见系统性跟进**。
- `.workbuddy/memory/*.md` 日志：每日开发记录，含测试基线数字（如 2026-10-02 全量 19/3464），是复盘的第一手资料。

---

## 2. ai-forum-v3 质量基线（只读，未做任何修改）

### 2.1 测试结构

- `server/tests/`：68 个文件、**553 个用例**，用 Node 22 原生 `node:test` + `supertest`（`npm test` = `node --test --test-concurrency=1 "server/tests/**/*.test.js"`）。
- `server/tests/env.js` 在导入任何模块前注入隔离环境：`mkdtemp` 临时 SQLite 库、临时 covers 目录、固定 JWT/内部令牌、关闭发布节流——**测试不碰 `data/` 真库**，隔离纪律与 openworker conftest 同级。
- 覆盖面广且贴近业务：契约（contract）、数据库方言守护（mariadb-schema / db）、Agent 周期（perception / viewpoint / chronicles）、日记与安全门（diary / diarySafety / diaryPublishGate）、社交（social-n4/n5/n6 / friends / room / visitors / plaza-tags）、外接 Agent（external-agent-registry / external-agent-social）、SEO（seo-meta）、记忆（memory / companion-memory-smoke）。用例名用中文写清业务意图（如「🔴 红线：公共资料库里的任何一条，都不出现在私密纪事里」），可读性极好。

### 2.2 本次实测

| 命令 | 结果 |
|---|---|
| `npm test`（全量） | **552 passed / 1 failed**，138s |
| `cd web && npx tsc --noEmit` | ✅ exit 0 |

**唯一失败**：`server/tests/deedDigestReach.test.js:146`「她自己已经记过了 → 那句消失，不催第二次（**待拍板⑥**）」——agent 已自己写过条目后，deed digest 仍递出「问他一句」。用例名自带「待拍板⑥」，属**已知待决**，与当前工作区未提交改动（`server/dsh/brainAdapter.js` +163 行，属 MCP 发布链路）无关。
- 质检意见：与 openworker 同样问题——**已知红没有隔离机制**（node:test 无 xfail，可用 `t.skip()` + TODO 注释或独立 `npm run test:pending` 分组），CONTRIBUTING 只笼统说「少量预存在失败」。建议把这条待拍板决策做掉（产品决策，非代码问题）。

### 2.3 Lint / 类型检查 / CI

- **零 lint 配置**：无 eslint / prettier / biome（前后端均无）。
- 类型检查：`web/` 是 TypeScript，`tsc --noEmit` 通过；但只写在 CONTRIBUTING 的「提交前检查」里，**无 CI 强制**。后端 `server/` 纯 JS（ESM，约 4.3 万行），无任何静态检查。
- **无 CI**：`.github/` 仅有 PR 模板与 Issue 模板，无 workflows。质量门完全靠人肉执行 CONTRIBUTING 的两条命令。
- 运维侧有 `scripts/patrol.sh`（cron 每 4h：SSH 封禁/Agent 发文/MCP 发布成功率/DB/备份/磁盘/PM2/nginx/SSL 有效期），**运维巡检是自动化了的，但代码级质量门（测试/lint/类型）没有自动化**——这是倒挂。
- git 卫生：`main` 已配 origin（github）+ server 双远端（巡检报告 P2「未配 remote」已解决，文档过时）；但工作区有 **163 行未提交改动**（brainAdapter.js + 其测试 + patrol.sh）。改动与测试同修改的习惯好，但积压未提交是回归定位的隐患。

### 2.4 已知问题文档

- `AI圈-网站质量巡检报告-2026-09.md`（09-23 修正版）：产品+安全向。P0-A「专家团文章 provenance 大量 dead:404 但照发」是最尖锐的一条——**验证层能标 404 但不拦发布**；P1-A/B 安全头（HTTP 不跳 HTTPS、无 CSP、`x-powered-by` 未隐藏）。质检补充：除「未配 remote」一条已过时外，其余结论截至今日未见修复记录。
- `CONTRIBUTING.md`：有「提交前检查」（npm test + web tsc）、Conventional Commits、安全漏洞私下报告——**有规范文本，缺强制机制**。

---

## 3. 可自动化的检查清单（建议按优先级落地）

### openworker（补缺，多数是增量）

| # | 检查 | 工具 | 落点 | 优先级 |
|---|---|---|---|---|
| O1 | 已知红显式挂账 | `pytest.mark.xfail(strict=True, reason="PITFALLS §十五")` | 14 例权限红 + 3 例脱节红；2 例 sandbox 红改 `skipif`（环境判定） | **P0**（恢复 CI 红信号） |
| O2 | Python lint | ruff（先 `check` 后 `format`，warning 起步） | pyproject `[tool.ruff]` + CI job | P1 |
| O3 | 覆盖率基线 | pytest-cov，只对 `coworker/permissions.py`、`coworker/engine/` 等核心设下限（不做全仓） | CI artifact + 下限 gate | P2 |
| O4 | 依赖锁 | pip-compile 或 uv.lock（AUDIT_REPORT P1-6 遗留） | packaging + release.yml 引用 | P2 |
| O5 | CI 状态可见 | 确认 backup 仓库 Actions 启用 + README 徽章 | 仓库设置 | **P0**（先确认 §1.5 不是纸面） |
| O6 | 新皮肤 PR 门禁 | 新增皮肤必须同提交过 WCAG + CSS 同步两条护栏（测试已有，只需流程强制「提交前 vitest」） | PR 模板 | P1 |

### ai-forum-v3（从零建；以下均为建议/补丁/操作指令，落在本仓库，由 Lead 转交用户在 ai-forum-v3 内执行）

| # | 检查 | 具体操作指令 | 优先级 |
|---|---|---|---|
| A1 | GitHub Actions CI | 新建 `.github/workflows/ci.yml`：`node:22` → `npm ci` → `npm test` → `cd web && npm ci && npx tsc --noEmit`。不超过 20 行（命令与 CONTRIBUTING 完全一致，只是自动化） | **P0** |
| A2 | 待拍板⑥处置 | 决策 deed digest「已记不催」是否为期望行为：是 → 修 `server/lib/deedDigest.js`；否 → 删除该用例并记录决策。**红不能挂着** | **P0** |
| A3 | lint 引入 | `npm i -D eslint @eslint/js` + `eslint.config.js`（先只开低噪音规则，`max-warnings=0` 起步）；前端 `web/` 同步一份 | P1 |
| A4 | 已知失败清单化 | CONTRIBUTING 的「少量预存在失败」改为显式清单（文件+用例名+原因+处置计划），杜绝无限期挂账 | P1 |
| A5 | 安全头（巡检 P1-A/B） | 反代层：HTTP 301→HTTPS、CSP（先 `Content-Security-Policy-Report-Only` 观察）；Express 层 `app.disable("x-powered-by")` | P1 |
| A6 | provenance 质量门（巡检 P0-A） | 发布服务加校验：任一 source `verifyStatus` 为 dead ⇒ 文章降级「来源待核」或压回草稿。**产品级 P0，建议单开任务** | **P0-产品** |

---

## 4. 建议的 Review 规范与准入门槛

### 4.1 通用（两项目一致）

1. **正确性 > 风格**：review 先看行为变更与边界条件，风格交给机器（ruff/eslint 一旦引入）。
2. **红不进门**：PR 不得引入新的失败用例；已知红必须在 PR 描述里对应到挂账清单条目。
3. **测试与实现同提交**：行为变更必须带测试；修 bug 必须先有复现测试（红）再修（绿）——ai-forum 最近 `fix(dsh)` 系列提交已是这个习惯，保持。
4. **护栏测试神圣**：WCAG 对比度、CSS↔registry 同步、mariadb 方言、权限地板这类护栏被改红时，**默认假设测试是对的、实现错了**；确需放宽必须在 PR 里写明危害评估。
5. **文档型注释不作数**（PITFALLS §十五 红线）：review 时对「注释声称有防线」的代码，只认实测断言。

### 4.2 openworker 准入门槛（建议）

- 必须绿：`pytest tests -q`（O1 挂账后应为 0 failed）、surfaces/gui 下 `tsc --noEmit` + `npm test`；改了交互流再加 `npm run e2e`。
- 必须过（引入后）：`ruff check`。
- 安全敏感目录（`coworker/permissions.py`、sandbox、tool_guard、self_protection、secrets）：**双人评审** + 必须有对应测试断言 + 引用 PITFALLS 对应条目。
- 权限/审批语义的任何改动视同安全策略变更，须 owner 明示拍板（现有红线，质检代为把关）。

### 4.3 ai-forum-v3 准入门槛（建议，转交用户）

- 必须绿：`npm test`（553 用例全跑 138s，成本可接受）；改 `web/` 再加 `npx tsc --noEmit`。
- 必须过（引入后）：`npx eslint . --max-warnings=0`。
- 安全敏感（`server/middleware/` 鉴权、内部 token、日记私密性、plaza 反灌水）：改动必须带红线用例（现有「🔴 红线：…」用例族就是范式，推广到所有安全点）。
- LLM 链路（`server/llm/`、`server/dsh/`）：改动必须含对「模型输出不可信」的兜底断言（如 MCP 发布误报兜底 `2cf5a92`、`dedb664` 两个提交的测试写法）。

---

## 5. 建议的后续任务拆分（供 Lead 参考）

| 任务 | 内容 | 建议承担角色 | 依赖 |
|---|---|---|---|
| T1【P0】openworker 已知红挂账 | 14 xfail(strict) + 2 skipif + 3 例 C 类修测试，目标 `pytest tests -q` 全绿 | code-reviewer 出清单 → backend-engineer 执行 | A 类处置需 owner 拍板；挂账本身不需 |
| T2【P0】确认 CI 真实状态 | 检查 backup 仓库 Actions 启用与最近 run，贴徽章 | lead / 用户 | 无 |
| T3【P1】openworker 引入 ruff | 配置 + CI job + 渐进修复 | backend-engineer | 无 |
| T4【P0】ai-forum CI 落地 | A1 的 ci.yml（**补丁落在 openworker 工作区，由 Lead 转交用户执行**） | code-reviewer 出补丁 / 用户执行 | 无 |
| T5【P0-产品】provenance 质量门 | A6，涉及发布链路 | backend-engineer | 需产品确认降级策略 |
| T6【P1】GUI 3 例红修复 | 2 例皮肤色值/同步 + 1 例 Sidebar i18n 测试 | ui-designer（色值）+ frontend-developer（i18n 测试） | 无 |
| T7【P1】ai-forum eslint + 已知失败清单化 | A3/A4，以补丁形式产出 | code-reviewer | 无 |

---

## 6. 盘点方法与局限

- 全程只读源码；测试均在隔离临时目录运行（两项目测试框架自身保证），未触碰真机状态与生产数据。
- 未跑 openworker 全量 pytest（3990 用例，超任务时间约束）；全量水位引用 2026-10-02 日志基线（19 failed / 3464 passed），今日收集数已增至 3990，**建议 T1 落地时先跑一次全量刷新基线数字**。
- CI 在远端仓库的真实运行状态无法从本地确认（见 §1.5 O5）。
- ai-forum-v3 前端组件级测试现状（web/ 仅 2 个手工 esbuild 测试）由 frontend-developer 的 frontend-baseline.md 详述，本报告只从质量门角度记录。
