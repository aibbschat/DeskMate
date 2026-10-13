# opencode 免费车道死亡排查 + Our Free Model 插件核查报告

**日期**：2026-10-13 14:40–15:10 · **结论先行**：情况全部属实。

| # | 萧哥的判断 | 核查结果 |
|---|---|---|
| 1 | 免费组全挂，影响后端处理 | ✅ 属实。小桔熔断表 12 个 `opencode:*` 通道全部 `open`，错误 `403 free tier unavailable`；`route()` 已无可试通道 → 后台任务（记忆抽取/理解/发帖草稿）全挂 |
| 2 | 可能是全天候调用导致 | ⚠️ 部分是。jev-1.13-free 累计 313 次 strike（双副本 24h 轮替实锤）；但插件自己的轻量探针（同出口 IP，15 分钟一轮）今天同样全挂 → 更像 opencode 上游 10-11~10-13 间收紧了匿名车道，hammering 最多是加速项 |
| 3 | Our Free Model 插件模型更多，且包含 opencode 的模型 | ✅ 属实。插件目录 = 10 个 opencode 条目 + 16 个 Kilo 免费模型（其中 nemotron-3.5-lightning / step-5-preview / ling-3.1-flash 与 opencode 重叠）+ 6 个 EAC（需 GitHub 授权）+ 13 个账号渠道（需登录） |
| 4 | 移除 opencode 免费模型，改用 Our Free Model | ✅ 方向正确，但「直接用」有个重要前提要拍板（Kilo 隐私声明，见下） |

---

## 一、证据链

### 1.1 小桔熔断表（`~/.deskmate/deskmate.db` → `llm_circuit_breaker`）

| 通道 | 状态 | 错误 | strike | 时间 |
|---|---|---|---|---|
| opencode:jev-1.13-free | open | 403 free tier unavailable | **313** | 10-13 12:05 |
| opencode:ling-3.1-flash-free | open | 同上 | 29 | 10-13 12:16 |
| opencode:muse-spark-1.2/1.3-contributor-free | open | 同上 | 27 / 33 | 10-13 11:53–11:57 |
| opencode:space-bunny / longcat / step-5 / nemotron-3.5-lightning / mimo-v2.6 / cat-jam | open | 同上 | 12–19 | 10-13 11:43–14:22 |
| opencode:ling-3.0-flash-fin-free（cloud） | open | status=400 | 13 | 10-12 03:16 |
| opencode:exo-free（cloud） | open | status=410 | 268 | 10-12 05:37 |
| opencode:nemotron-3-ultra-free（cloud） | closed | upstream overloaded | — | 10-12 03:18 |
| local:qwen3-4b-instruct | open | network error | 18 | 10-13 14:36（Mac 无 llama-server，设计预期） |

`free_channels()` 全部候选熔断 → `route()` 返回 None →「免费组无可用」。prefs.json 确认 `memory_llm_group: "free"`，后台加工确实走这条已死的组。

### 1.2 本机出口实测（118.254.99.36 · CN）

| 测试 | 结果 |
|---|---|
| opencode `space-bunny-free`（按小桔协议完整伪装） | `{"type":"error","error":{"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode..."}}` —— **整条车道死，不是 429** |
| opencode `GET /models` | 200，清单 84 个模型中 10 个 `*-free` —— **清单活着，推理死** |
| Kilo `kilo-auto/free`（流式） | SSE 秒回，自动路由到 `dots-studio/dots-3-note-preview:free`（AtlasCloud），reasoning 正常流出 |
| Kilo `kilo-auto/free`（非流式） | 正常返回完整 chat.completion（小桔记忆抽取用 `complete()` 非流式，**兼容**） |
| Kilo `inclusionai/ling-3.1-flash`（流式） | 上游 Novita 429（单供应商容量问题，网关错误信封规范，熔断可处理） |

### 1.3 插件侧证据（`~/.dsh/our-free-model/`）

- **availability.json**（今天 14:37 探测）：10 个 opencode 模型全部 `unknown` + FreeTierError；muse-spark 1.2/1.3 `region-blocked`；nemotron-3-ultra / fledge-alpha 在 10-08/10-11 还 `available`。
- **stats.json**：10-11 当天 opencode 车道 388 ok / 12 fail（mimo-v2.6-flash 用了 310 次，kilo-auto/free 8 次）→ **车道死于 10-11 之后，不是从来就不通**。
- **catalog.json**：entries 10 个 opencode + sealIds 6 个 EAC + kiloRows 16 个 Kilo。
- 版本：rollback/ 为 v2.0.0 源码；updates.json 显示 2.1.0 已下载但 `activated: false`（插件运行中，数据文件今日仍在更新）。

### 1.4 关于死因的判断

- 支持「我们打死」：313 次 strike、双副本 24h 轮替、匿名额度按会话记账。
- 反对：插件自己的探针是**轻量全新会话**（每 15 分钟每模型 1 次），今天同样全挂 FreeTierError。若只是我们的会话桶被封，插件的探测不该全灭。
- 更可能：opencode 在 10-11~10-13 间收紧了匿名车道（客户端指纹校验升级或区域策略），我们的 hammering 是加速项不是根因。
- **我们侧无修复面**：协议是 v0.3.3 插件的忠实移植且 10-11 实测能跑；除非给 opencode 付费 key，这条车道救不回来。

---

## 二、Kilo 免费池完整清单（实测 `api.kilo.ai/api/gateway/models`，391 个模型中 16 个免费）

| 模型 | 上下文 | 视觉 | 备注 |
|---|---|---|---|
| `kilo-auto/free` | 256K | — | **自动路由**，优先排第一 |
| `openrouter/free` | 200K | ✓ | 自动路由 |
| `stepfun/step-5-preview-free` | **1M** | ✓ | 与 opencode 重叠 |
| `dots-studio/dots-3-note-preview:free` | 512K | ✓ | 中文强，auto 实测选中它 |
| `inclusionai/ling-3.1-flash` | 256K | — | ≈opencode `ling-3.1-flash-free` |
| `nvidia/nemotron-3.5-lightning:free` | **1M** | — | 与 opencode 重叠 |
| `nvidia/nemotron-3-super-120b-a12b:free` | 256K | — | |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | **1M** | — | |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 256K | ✓ | 推理 |
| `nvidia/nemotron-3.5-content-safety:free` | 128K | ✓ | |
| `thinkingmachines/inkling-small:free` | **1M** | ✓ | 推理 |
| `poolside/laguna-s-2.1:free` | 256K | — | |
| `poolside/laguna-xs-2.1:free` | 256K | — | |
| `cohere/north-mini-code:free` | 256K | — | |
| `liquid/lfm-2.5-2.6b:free` | 64K | — | 轻量快 |
| `stealth/glyph-cluster` | 256K | — | |

协议极简：`POST https://api.kilo.ai/api/gateway/chat/completions`，**无 key、无伪装、无指纹**，OpenAI 兼容 SSE；`GET /models` 拉清单。比 opencode 车道（CLI 伪装 + 门面工具 + 会话 id 派生）简单一个数量级。

---

## 三、需要萧哥拍板的三个决策点

### 3.1 移除范围
- **A（推荐）**：只从 `llm_channels.free_channels()` 路由里摘掉 opencode 通道，`opencode_provider.py`/`zen_catalog.py` 代码保留（零风险，上游若恢复随时可回）。
- **B**：连 provider 代码一起删——但 UI 模型 picker（`matrix.py` 的 `opencode:*` entries）也会消失，恢复成本高。

### 3.2 Kilo 隐私 trade-off（⚠️ 最重要）
插件 README 原话：Kilo 免费池「prompt 可能被上游提供方记录，并用于改进其服务。**不适合生产或敏感数据 workload**」。
小桔后台加工 = **记忆抽取**（输入是萧哥的对话片段）+ 发帖草稿（公开内容，无所谓）。三个选项：
- **甲**：全量上 Kilo（零成本，对话片段可能被上游记录）；
- **乙**：记忆抽取留付费组（ark→小米→腾讯，当前基本健康），Kilo 只跑发帖草稿等非敏感任务；
- **丙**：暂不移植，后台任务整体切付费组（最稳，费 token）。

### 3.3 实施方案（若拍板移植）
1. 新 `coworker/providers/kilo_provider.py`（~120 行：OpenAI 兼容 SSE + 非流式聚合，无伪装）；
2. 新 `coworker/providers/kilo_catalog.py`（远端 isFree 清单 + 6h TTL + seed 兜底，同 `zen_catalog` 范式）；
3. `llm_channels.free_channels()`：Kilo 通道入组，**auto 路由排第一**，非推理模型优先（后台批量活要速度），本地 `local` 兜底保留排最末；
4. 熔断表自动接（channel key = `kilo:<model>`）；
5. `deploy_cloud_sync.sh` 清单补两个新文件（漏传=云端 ImportError 被 fail-open 吞）；
6. 先红后绿写测试（`tests/test_kilo_*.py`，禁真出网，用本地假网关）。

---

## 四、附：关键文件位置

| 内容 | 位置 |
|---|---|
| 小桔 opencode 车道（移植源 v0.3.3） | `coworker/providers/opencode_provider.py`、`coworker/providers/zen_catalog.py` |
| 免费组组装 | `deskmate/llm_channels.py`（`free_channels` / `route`） |
| 熔断表 | `~/.deskmate/deskmate.db` → `llm_circuit_breaker` |
| Our Free Model 插件数据 | `~/.dsh/our-free-model/`（catalog/availability/feed/stats/settings） |
| 插件 v2.0.0 源码 | `~/.dsh/our-free-model/rollback/`（src/kilo.js = Kilo 车道移植参考） |
| Kilo 车道协议参考 | `rollback/src/kilo.js`（168 行，全部协议细节） |
