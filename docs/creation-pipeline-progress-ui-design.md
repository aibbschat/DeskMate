# 创作流水线 · 流式进度 + 界面现代化改造 技术方案

> 状态：**方案待确认**（2026-08-28 拟稿）
> 目标：让创作流水线每一步点击后都有实时反馈（SSE 流式进度），并把界面重构成现代化、有灵动的观感。
> 本文基于对 `deskmate/creation/*`、`deskmate/roundtable/web.py`、`surfaces/gui/src/components/CreationPipelineView.tsx`、`api.ts`、`styles.css` 的逐行阅读。

---

## 1. 背景与问题

### 1.1 现状：点击后"干等"
- 后端每个**生成型** step（`draft_outline` / `draft_screenplay` / `review_outline` / `review_screenplay`）都是**同步 POST**：
  前端发请求 → 后端同步调 LLM（一次大纲/一集剧本几十秒~几分钟）→ 一次性返回最终快照。
- 前端 `run()` 只有 `busy` 布尔标志 + 完成后 `setSnap`。期间没有任何中间状态，用户只能干等。
- **拍板型** step（`lead_outline` / `lead_screen`）是瞬时的，但点击后也没有明确的成功/进行中反馈动画。

### 1.2 现状：界面"死板"
- `CreationPipelineView.tsx`（1085 行）是纵向堆叠的面板式布局：创建表单 → 信息卡 → 分集卡片网格 → 状态列表 → 按钮排。信息密度高、缺少层级与动效、步骤条是纯静态色块。

### 1.3 关键有利事实（已核实）
- **后端已具备 SSE 基建**：`service.stream_final_review` 已是 async generator + `web.api_final_review` 用 `StreamingResponse`；前端 `streamFinalReview` 已是 `for await` 消费 SSE 的 reader 模式。可直接复用同一套范式。
- **LLM 层已具备 token 级流式**：`deskmate.memory.llm.chat_stream()` 是异步 generator（独立线程 + 队列轮询），逐 token 产出。
- **`LLMExecutor.draft_screenplay` 本就是"拆场景→逐段写"的分段结构**（`_parse_scenes` + for 循环 + `_summarize_segment`），天然适合按段上报进度。
- 前端是 Tailwind（token 映射到 `--paper/--accent/--ok/--warn/...`，浅色+深色主题），已有丰富 `@keyframes` 动效可复用。

---

## 2. 目标

1. **每一步都有回应**：所有生成型 step 点击后立即出现"进行中面板"，实时显示阶段/进度/文案，完成自动收起并刷新。
2. **界面现代化**：重构为"顶部概览 + 横向流程步骤条 + 进行中面板 + 卡片化分区"的现代布局。
3. **灵动呈现**：步骤条呼吸光晕、进度条流动、事件日志逐条淡入、状态徽章动效、按钮 loading 态。

---

## 3. 整体架构

```
前端 CreationPipelineView
  │  POST /api/creation/{pid}/draft-outline   (SSE: text/event-stream)
  ▼
web.py 路由 ── StreamingResponse ──> service.stream_draft_outline()  [async generator]
                                          │  asyncio.to_thread 跑同步 pipeline step
                                          ▼
                                  pipeline.draft_outline(on_event=...)   ← 回调上报
                                          ▼
                                  executor.draft_outline(on_event=...)   ← 阶段切换点调回调
                                          │  chat_stream() 逐 token / 逐段
                                          ▼
                                  on_event 事件 → queue.Queue 桥接 → SSE 逐条推送
```

设计要点：
- **业务状态机（pipeline/guard/store）零改动**——只把"产出文本"这个耗时动作变成可观测。
- **executor 加可选回调** `on_event: Callable[[dict], None] = None`，向后兼容（不传=老行为）。
- **SSE 事件由回调实时产生**，经线程安全队列桥接给 async generator，边生成边推送。

---

## 4. 统一 SSE 事件协议

所有生成型 step 复用同一协议（与终审事件风格一致，前端一个渲染器通吃）：

```jsonc
// 阶段开始（前端据此切换步骤条状态为 running、弹出进行中面板）
{"type": "stage", "stage": "draft_screen", "label": "撰写剧本 · 第3集", "status": "running"}
// 中间进度（progress 0-100，前端渲染进度条 + 文案；可多条）
{"type": "note", "message": "正在拆解场景…", "progress": 5}
{"type": "note", "message": "正在写场景 2/3《对峙》…", "progress": 60}
{"type": "note", "message": "本集草稿完成", "progress": 100}
// 完成：携带最终快照（前端直接 setSnap，免二次请求）
{"type": "done", "snapshot": { ... CreationSnapshot ... }}
// 失败
{"type": "error", "message": "大纲尚未定稿，不能写剧本"}
```

各 step 的 `stage` 取值与推送节奏：

| step | stage | note 序列示例 |
|---|---|---|
| draft_outline | `draft_outline` | 正在撰写大纲… → 大纲初稿完成 |
| draft_screen（分集） | `draft_screen` | 正在拆解场景… → 写场景 1/3 → 2/3 → 3/3 → 本集草稿完成 |
| draft_screen（全本） | `draft_screen` | 同上（场景 3-6 个） |
| review_outline / review_screen | `review_outline` / `review_screen` | 审核专家正在逐项核验… → 已给出审核意见 |
| 拍板 lead_* | （不流式，走同步+前端动画） | — |

> stub 模式同样完整推送全部事件（瞬时完成），保证无 LLM 环境也走同一前端路径。

---

## 5. 后端改造明细

### 5.1 `deskmate/creation/executor.py`
- `CreativeExecutor` 协议三个方法各加 `on_event: Optional[Callable[[dict], None]] = None`。
- `LLMExecutor`：
  - `draft_outline`：开头 `_emit(on_event, "stage", "draft_outline", ...)` + `note(进行中)`；完稿后 `note(完成)`。
  - `draft_screenplay`：把现有 `chat()` 调用升级为 `chat_stream()`（**可选**，可先只做阶段级）；至少在每个场景循环首尾 `_emit(...)`（拆场景/写场景 i/N/完成）。
  - `review`：开头 note(审核中)、结尾 note(已给出结论)。
- `StubExecutor`：同样在各点调用 `_emit`（瞬时推完整序列），保证 stub 也流式。
- 新增小工具 `_emit(on_event, type, **kw)`（`if on_event: on_event({...})`）。

> 注：`chat_stream` 是 async，而 executor 方法当前是 sync 且被 `asyncio.to_thread` 跑。两种做法：
> A（轻）先做**阶段级**流式：`chat()` 保持 sync，只在阶段切换点回调——改动小、风险低，进度到"场景粒度"。
> B（全）`draft_screenplay` 改为 async + `chat_stream`，做 token 级打字机效果——体验最顶但改动大。
> **建议：先落地 A（阶段级，场景粒度），达到"每一步有回应"目标；token 打字机作为后续增强。**（用户可拍板）

### 5.2 `deskmate/creation/pipeline.py`
- `draft_outline` / `draft_screenplay` / `review_outline` / `review_screenplay` 各加 `on_event: Optional[Callable] = None`，透传给 executor。
- `draft_screenplay` 阶段点补一次 note（如"门禁通过，开始撰写第 X 集"）。

### 5.3 `deskmate/creation/service.py`
- 新增统一桥接：
  ```python
  async def _stream_step(pid, fn_name, *args):
      pl = get_pipeline(pid)
      q: queue.Queue = queue.Queue()
      def on_event(ev): q.put(ev)
      def run():
          try:
              getattr(pl, fn_name)(*args, on_event=on_event)
              q.put({"type": "done", "snapshot": _snapshot_noop(pl)})
          except Exception as e:
              q.put({"type": "error", "message": str(e)})
      task = asyncio.to_thread(run)
      while True:
          try: ev = q.get_nowait()
          except queue.Empty:
              if task.done(): break
              await asyncio.sleep(0.03); continue
          if ev.get("type") == "done":
              ev["snapshot"] = snapshot(pid)   # 统一在事件循环里取快照，避免线程并发
          yield _sse(ev)
  ```
- 导出 `stream_draft_outline(pid)` / `stream_draft_screen(pid, episode)` / `stream_review_outline(pid)` / `stream_review_screen(pid, episode)`。
- **保留**现有同步方法（`draft_outline` 等）供 MCP / 测试 / 兼容使用——service 层同步方法不动，MCP 不受影响。

### 5.4 `deskmate/roundtable/web.py`
- 生成型 step 路由改为返回 `StreamingResponse`（复用 `api_final_review` 模式）：
  - `POST /api/creation/{pid}/draft-outline` → `stream_draft_outline`
  - `POST /api/creation/{pid}/draft-screen` → `stream_draft_screen`（body 带 episode）
  - `POST /api/creation/{pid}/review-outline` → `stream_review_outline`
  - `POST /api/creation/{pid}/review-screen` → `stream_review_screen`
- 拍板型（lead-*）、加集、终审路由**保持现状**（lead 瞬时返回 JSON，前端做按钮动画）。
- 顺带修复上一轮发现的防御性缺陷：这些 step 路由补 `except Exception → 400 + 中文 error`（不再 500）。

---

## 6. 前端改造明细

### 6.1 `surfaces/gui/src/api.ts`
- 新增 `streamStep(pid, step, opts?: {episode?: number|null})` async generator，复用 `streamFinalReview` 的 reader 解析（`data:` 行 → JSON）。返回 `{type, stage, label, message, progress, snapshot, error}` 联合类型。

### 6.2 `surfaces/gui/src/components/CreationPipelineView.tsx`（重构）

**状态新增**：
```ts
const [runState, setRunState] = useState<{stage, label, message, progress, events:[]} | null>(null)
```
点击生成型 step → `runStream()` 替代 `run()`：
1. 立即 `setRunState({stage, label: "正在…", progress: 0, events: []})`（**点击即有回应**）；
2. `for await (ev of streamStep(...))` 逐事件更新：stage→步骤条 running、note→追加日志+进度、done→setSnap+收起面板、error→展示错误；
3. 期间禁用相关按钮（`busy || runState`）。

**布局重构（现代化）**：
- **顶部概览卡**：剧名/概念 + 制作要求 chips（风格/节奏/集数）+ 原著 + 分集完成度（`N/M 集已定稿`）。
- **横向流程步骤条**（6 步，替代原静态色块）：`写大纲 → 审大纲 → 拍板 → 写剧本 → 审剧本 → 拍板/终审`。每步 = 圆角徽章 + 图标 + 标签，状态三态：
  - `done`：绿勾（弹簧 pop-in）
  - `running`：accent 呼吸光晕 + 进度流动条
  - `pending`：灰色
  - 已完成步之间用渐变连线。
- **进行中面板**（点击后浮现，居中/顶部锚定）：
  - 阶段标题（"正在撰写剧本 · 第3集"）
  - 渐变进度条（accent→ok，`@keyframes` 流动动画）
  - 实时状态文案（note 最新一条，大字）
  - 事件日志（最近 3-5 条，每条淡入上滑）
- **分集卡片**：现代化卡片（圆角 14px、柔和阴影、hover 上浮 1px）；状态徽章（待办/撰写中/待审/待拍板/已定稿 各配色 + 小圆点/对勾）；操作按钮 loading 态（spinner + "撰写中…"）。
- **终审提案卡**：保留 `ProposalCard`，样式微调对齐新设计语言。

### 6.3 `surfaces/gui/src/styles.css`
- 新增 `@keyframes`：`cre-flow`（进度条流动）、`cre-breathe`（running 呼吸）、`cre-rise`（日志淡入上滑）、`cre-pop`（徽章弹簧出现）、`cre-pulse-dot`（状态点脉冲）。
- 新增少量 `.cre-*` 工具类（或纯 Tailwind + inline keyframes 引用）。

---

## 7. 兼容性与风险

| 项 | 处理 |
|---|---|
| MCP 同步接口 | service 同步方法保留，MCP 零改动 |
| 旧 REST 调用方 | 生成型 step 路由语义从 JSON 改为 SSE——项目内仅前端消费，前端同步升级；若需兼容可让路由按 `Accept: text/event-stream` 分流（**默认直接全量切 SSE**） |
| 线程桥接 | `asyncio.to_thread` + `queue.Queue` + 30ms 轮询；done/error 必须可靠送达；快照在事件循环线程取（避免线程并发读写 store） |
| 连点 | SSE 期间按钮禁用（`busy || runState`），前端已有防抖习惯 |
| stub / 无 LLM | 同样推完整事件序列，保证所有环境体验一致 |
| 500→400 | 顺手修上一轮 BUG 3（web 层 step 路由补异常捕获） |

---

## 8. 实施步骤与验证

1. **后端**：executor 回调 → pipeline 透传 → service `_stream_step` → web SSE 路由。
2. **后端验证**：stub 模式全链路 SSE 冒烟（`curl -N` 看事件流）+ 既有 25 项 service 脚本回归 + `tests/test_deskmate_roundtable_web.py` 等全绿。
3. **前端**：api.ts `streamStep` → CreationPipelineView 重构（状态机 + 布局 + 动效）→ styles.css 动画。
4. **前端验证**：`npx tsc --noEmit` 0 错 → `npm run build`（vite）→ 按 c8ec76e 同款方式重打包进 sidecar（内嵌 dist 校验）。
5. **联调**：真实挂载 `/roundtable` 前缀下 HTTP 冒烟 19 项（含 SSE）全绿。

## 9. 工作量评估

| 模块 | 改动量 | 说明 |
|---|---|---|
| executor.py | 中 | 回调 + 分段上报（阶段级，~80 行） |
| pipeline.py | 小 | 透传参数（~20 行） |
| service.py | 中 | `_stream_step` 桥接 + 4 个 stream 函数（~90 行） |
| web.py | 小 | 4 路由改 StreamingResponse + 异常捕获（~40 行） |
| api.ts | 小 | `streamStep`（~40 行） |
| CreationPipelineView.tsx | 大 | 状态机 + 布局重构 + 动效（~300 行净增） |
| styles.css | 小 | keyframes + 工具类（~80 行） |

---

## 11. 实施记录（2026-08-28 已落地）

> 方案经用户确认（按推荐：阶段级流式 + 全量切 SSE），已实现并验证。

### 后端（Python，editable 安装即时生效）
- `executor.py`：新增 `_emit` 回调工具；`CreativeExecutor`/`StubExecutor`/`LLMExecutor` 的
  `draft_outline`/`draft_screenplay`/`review` 均加 `on_event` 参数，阶段切换点上报
  `note{message,progress}`（拆场景→场景 i/N→完成；审核中→已给结论）。stub 同样流式。
- `pipeline.py`：`draft_outline`/`draft_screenplay`/`review_outline`/`review_screenplay`
  透传 `on_event`。
- `service.py`：新增 `_stream_step` 桥接（`asyncio.create_task(asyncio.to_thread(...))` +
  `queue.Queue` + 30ms 轮询，done 在事件循环线程取快照）；导出
  `stream_draft_outline/screen/review_outline/review_screen`。同步方法保留（MCP/测试兼容）。
- `web.py`：4 个生成型路由改 `StreamingResponse`（SSE），错误（含未知 pid）转为
  `error` 事件而非 500。
- 小功能：`manager.py` 加 `pick_native_file`（macOS osascript choose file / Windows
  OpenFileDialog / Linux zenity），`app.py` 注册 `POST /v1/files/pick`。

### 前端
- `api.ts`：`streamStep()` async generator（消费 SSE 事件）；`pickNovelFileViaServer()`；
  原同步 `draftOutline/reviewOutline/draftScreen/reviewScreen` 改为消费 SSE 聚合返回快照；
  删除无用 `_postStep`。
- `CreationPipelineView.tsx`：新增 `runState`（进行中面板）与 `pickingNovel`；
  `runStream()` 点击立即弹进行中面板，逐事件更新进度条/实时文案/事件日志，done 落快照；
  步骤条现代化（大徽章 + active 呼吸光晕 + done 弹簧 + 渐变连线 + running "…"）；
  分集卡片加完成度进度条 + 精致状态 pill + hover 上浮；创建表单原著路径加「浏览…」按钮。
- `styles.css`：新增 `cre-panel-in/cre-breathe/cre-pop/cre-rise/cre-pulse-dot/
  cre-progress-fill/cre-ep-card` 动效。

### 验证
- SSE 冒烟 12/12（note/done/error 事件、progress 递增、web StreamingResponse、未知 pid→error）
- 同步链路回归 25/25（含重启恢复、加集、打回）
- 既有 pytest roundtable 10/10；模块 import 全 OK
- 真实挂载场景（主 app `/roundtable` 前缀）SSE 完整链路通过；`/v1/files/pick` 注册成功
- 前端 `tsc --noEmit` 0 错；`vite build` 成功

### 待交付
- 前端改动需 `packaging/build_local_app.sh`（或 `--install`）重打包内嵌进 .app 才生效；
  Python 部分重开 .app 即时生效。

