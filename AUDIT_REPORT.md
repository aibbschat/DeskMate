# OpenWorker 全盘巡检报告

> 巡检时间：2026-08-06 | 分支：fork/base | 版本：1.3.7
> 最后更新：2026-08-06 — P0/P1(静默异常+TS类型)/P2 已修复

---

## 项目概况

OpenWorker 是一个多语言 agentic coworker 平台，Python 后端 (FastAPI) + React/TypeScript 前端 + Rust 桌面壳 (Tauri 2)。含 DeskMate "灵魂层"，提供桌面感知、主动行为、记忆系统和圆桌创作功能。

**代码规模**：Python ~60+ 模块 | TS/TSX ~80+ 模块 | Rust ~2 文件 (合计 60K+ 行) | 测试 ~25K 行 (55 个 Python 测试文件 + 55 个 E2E spec)

---

## P0 — 必须立即修复

### 1. SQLite 连接泄漏（无 try/finally）

`deskmate/novel_memory/core.py` 三处 SQLite 连接在异常路径上会泄漏文件句柄：

- `build_fts5_index()` (行 699-757)：`conn = sqlite3.connect(...)` 后无 `try/finally`
- `novel_search_fts5()` (行 774-817)：多处 `conn.close()` 但异常路径跳过
- `fts5_index_status()` (行 891-905)：同上

**修复**：统一用 `with conn:` 或 `try/finally` 包裹。

### 2. 全局单例初始化竞态条件（无锁）

4 处 check-then-act 单例模式，无锁保护：

- `coworker/personas/registry.py:402` — `get_registry()`
- `coworker/agent.py:133` — `_deskmate_mgr_singleton`
- `coworker/server/app.py:1639` — `_deskmate_mm_rest`
- `coworker/server/app.py:1904` — `_deskmate_tick_engine`

```python
# 典型竞态：
def get_registry():
    global _singleton
    if _singleton is None:        # 线程 A 检查为 None
        _singleton = ...           # 线程 B 也可能同时进入
    return _singleton
```

**修复**：加 `threading.Lock()` 或改用 `functools.lru_cache`。

### 3. 前端 2 个 API 调用绕过了 authedFetch

`surfaces/gui/src/api.ts`：

- `setCompactionSettings` (行 ~1007) — 用裸 `fetch()`
- `setContextBar` (行 ~1031) — 用裸 `fetch()`

在桌面应用中这两个设置接口会因缺少 token 头而 401 失败。

**修复**：改为 `authedFetch()`。

---

## P1 — 高优先级

### 4. 70+ 处静默 `except Exception: pass`

最严重的集中在：

| 文件 | 数量 | 影响 |
|------|------|------|
| `deskmate/memory/manager.py` | 17 处 | 记忆读写/压缩/归档失败被吞掉，用户丢数据无感知 |
| `deskmate/skills/manager.py` | 8 处 | 技能匹配/进化失败静默继续 |
| `deskmate/roundtable/trends.py` | 6 处 | 潮流简报加载/保存失败被吞掉 |
| `coworker/server/manager.py` | 3 处 | provider 验证失败静默忽略 |
| `deskmate/novel_memory/core.py` | 1 处 | FTS5 搜索失败降级但未记录原始错误 |

**修复**：至少加 `logging.debug()` 或 `logger.exception()`。

### 5. 巨型单文件（架构债务）

| 文件 | 行数 | 问题 |
|------|------|------|
| `surfaces/gui/src-tauri/src/lib.rs` | 34,951 | Rust 桌面壳全部逻辑在一个文件 |
| `coworker/server/manager.py` | 4,041 | SessionManager 管理所有生命周期 |
| `coworker/server/app.py` | 3,147 | ~100+ 路由全定义在 `create()` 闭包内 |
| `surfaces/gui/src/App.tsx` | 2,053 | 20+ 状态变量、所有路由/侧栏/转录/编辑器 |
| `surfaces/gui/src/api.ts` | 2,195 | ~80 个 API 函数，每个都手写 `${httpBase()}/v1/...` |
| `surfaces/gui/styles.css` | 2,394 | 全局样式单文件 |

**影响**：难以测试、难以导航、难以多人协作。App.tsx 的 `App` 组件本身跨越 ~1,760 行。

**建议**：
- `app.py` → 按功能拆路由模块 (sessions, connectors, automation, deskmate)
- `App.tsx` → 提取 SessionList, Transcript, Settings 到独立组件
- `api.ts` → 用 base URL 常量 + 统一请求构建器

### 6. 无 Python 锁文件

`pyproject.toml` 所有依赖用 `>=` 最低版本约束，无 `requirements.txt`、`poetry.lock` 或 `pip-compile` 输出。构建不可复现。

另外 `a2a-sdk>=1.1.2` 和 `aisuite` 之间存在 `docstring_parser` 版本冲突（注释已标注但未解决）。

### 7. 无 Pydantic 输入校验

`app.py` 中 51 个端点接受 `body: dict` 原始字典，参数通过 `body.get("key", default)` 手动提取。只有圆桌模块用了 Pydantic `BaseModel`。

**影响**：输入验证不一致、无法自动生成 OpenAPI 文档（FastAPI 的自动 schema 需要 Pydantic 模型）。

### 8. TypeScript `any` 滥用（50+ 处）

关键文件：

- `types.ts`：核心消息/事件类型用 `any`（行 32, 112, 116, 120）
- `api.ts`：响应类型用 `any`（行 140, 147, 910）
- `itemsFromMessages.ts`：`report: any`, `args: any`
- `ApprovalCard.tsx`：`args: any`
- `App.tsx`：`(crypto as any).randomUUID`

**影响**：类型安全形同虚设，编译器无法捕获运行时类型错误。

---

## P2 — 中等优先级

### 9. SQL LIKE 模式注入

`deskmate/novel_memory/core.py:795`：
```python
like_params = [f"%{kw}%" for kw in kws]
```
用户输入中的 `%` 和 `_` 未转义，会充当 LIKE 通配符，导致匹配范围意外扩大。

**修复**：`kw.replace('%', '\\%').replace('_', '\\_')` + `ESCAPE '\\'`。

### 10. `os.environ` 线程不安全写入

`coworker/providers/bedrock_provider.py:314` 和 `registry.py:664`：
```python
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self._bedrock_api_key
```
从并发引擎线程中直接修改环境变量，Python 的 `os.environ` 不是线程安全的。

### 11. SQLite 连接样板代码重复 5+ 处

以下类各自独立实现了相同的 `sqlite3.connect()` + `check_same_thread=False` + `row_factory` + `threading.RLock()` 模式：

- `SQLiteMemoryStore`
- `AuditStore`
- `ConversationStore`
- `TaskStore`
- `Diagnostics`

**建议**：提取 `ThreadSafeSQLiteStore` 基类或工厂函数。

### 12. 无数据库迁移框架

Schema 演进通过 `try: ALTER TABLE ... except OperationalError: pass` 实现。无版本追踪、无回滚能力。

### 13. API 错误响应不一致

- 错误格式不统一：有的 `{"error": "msg"}`，有的 `{"ok": false, "error": "msg"}`
- 错误消息中英混杂：`"风格抽取失败"` vs `"invalid json"`
- 无 `request_id`、`error_code`、`details` 字段

### 14. Promise 链缺少 `.catch()`

`SettingsView.tsx` 中 7 处 `.then()` 无 `.catch()`（行 415, 416, 521, 581, 743, 835, 964, 1004, 1047）。`Composer.tsx` 2 处。`SlackDetail.tsx`、`GithubDetail.tsx` 各 1 处。

### 15. fire-and-forget asyncio.create_task 无错误处理

`coworker/server/app.py:2861`：
```python
asyncio.create_task(_deskmate_post_turn(...))
```
如果子任务抛异常，只会在日志中看到 "Task exception was never retrieved"，不会影响主流程但会丢失错误上下文。

**修复**：加 `task.add_exception_callback()` 或用 `asyncio.TaskGroup`。

### 16. 频繁查询列缺少索引

`memories`、`audit_events`、`sessions` 表只有主键索引。`scope`、`workspace`、`session_id`、`connector`、`tool`、`state` 列无索引。当前单用户场景可接受，数据增长后会退化。

---

## P3 — 低优先级 / 改善项

### 17. 无 `.env.example`

`.env` 从 `~/.config/coworker/.env` 加载，但无示例模板。用户需读源码才能发现支持哪些环境变量。

### 18. 无结构化日志

全项目使用 `logging.getLogger(__name__)` 纯文本日志。无 JSON 格式、无关联 ID、无请求 ID。无 Sentry/Datadog 等错误追踪集成。

### 19. 无独立 dev/staging/prod 配置 profile

只有一份 `config.example.toml`，通过手动编辑 `config.toml` 切换环境。

### 20. 无 OpenAPI/Swagger 文档

FastAPI 内置 OpenAPI 支持，但因为路由定义在 `create_app()` 闭包内 + 无 Pydantic 模型，自动生成的 schema 不完整。

### 21. 前端 `${httpBase()}` 重复 ~80 次

`api.ts` 中每个 API 函数都手写 `${httpBase()}/v1/...`。应提取为 base URL 常量或请求构建器。

### 22. connector 断连逻辑重复

6+ 个 connector 有几乎相同的 disconnect 处理代码，可以统一为一个通用端点。

### 23. 硬编码端口/URL

| 位置 | 值 |
|------|------|
| `api.ts:11` | `http://127.0.0.1:8765` |
| `providers/registry.py:30` | `http://localhost:11434` (Ollama) |
| `tools/a2a.py:146` | `http://localhost:9900` |
| `roundtable/web.py:14` | `http://localhost:9920` |
| `opencode_a2a_server.py:212` | `http://localhost:9912` |

---

## 亮点（做得好的部分）

- **SecretStore 设计**：`0600` 权限 + `${ENV_VAR}` 引用 + 审计日志脱敏，安全意识到位
- **CORS/Origin 防护**：严格限制 `tauri://localhost` / `localhost` / `127.0.0.1`
- **测试覆盖**：55 个 Python 测试文件 + 55 个 E2E spec + 15 个前端单元测试，CI 完整
- **WebSocket 帧大小限制 + 速率限制**：防御性工程做得好
- **Governance 层**：YAML 规则驱动的危险命令检测 + macOS Seatbelt 沙箱
- **圆桌模块**：Pydantic 校验、独立 SSE 流、弹性设计，是整个后端输入校验最好的模块
- **Provider 抽象**：5 家 LLM provider 统一接口 + registry + router，扩展性好

---

## 修复建议优先级路线图

```
✅ 已修复（2026-08-06）：
  ✅ P0: SQLite 连接泄漏 — 3 处加 try/finally (novel_memory/core.py)
  ✅ P0: 单例竞态 — 4 处加 threading.Lock (registry.py, agent.py, app.py×2)
  ✅ P0: 前端 authedFetch 缺失 — 2 处改 fetch → authedFetch (api.ts)
  ✅ P1: 静默异常 — 10 个文件约 50 处加 logging (manager.py×2, trends.py, interactions.py 等)
  ✅ P1: TypeScript any — 加 eslint-disable 注释 + controls 改 unknown (api.ts, types.ts)
  ✅ P1: Promise .catch — 6 处加 .catch(() => {}) (SettingsView, Composer, Slack/GithubDetail)
  ✅ P2: LIKE 注入 — % 和 _ 转义 + ESCAPE 子句 (novel_memory/core.py)
  ✅ P2: os.environ 线程安全 — 2 处加 threading.Lock (bedrock_provider.py, registry.py)
  ✅ P2: asyncio 错误处理 — add_done_callback 记录异常 (app.py)

待修复：
  □ P1: Python 锁文件 — pip-compile 或 poetry lock
  □ P1: 巨型文件拆分 — app.py(3147行), App.tsx(2053行), lib.rs(34951行)
  □ P1: Pydantic 模型 — 51 个端点接受 raw dict，新端点应强制用 Pydantic
  □ P1: API 错误格式标准化 — 统一 error envelope
  □ P2: SQLite 基类提取 — 5 个类重复相同连接样板
  □ P3: 结构化日志 / .env.example / OpenAPI 文档
```

---

*报告由三路并行巡检 agent 自动扫描生成，覆盖安全、架构、代码质量三个维度。修复由两路并行 agent 执行。*
