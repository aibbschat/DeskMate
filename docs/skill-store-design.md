# 技能广场（Skill Store）技术设计

> 承接 `plugin_center_design.md`，聚焦 MCP 插件的用户侧体验：一键安装、按需配置。

---

## 一、核心原则

| 原则 | 说明 |
|------|------|
| **安装零门槛** | 点"安装"即完成，不需要填 API Key |
| **配置后置** | 安装后在"已安装"列表里配置，或首次使用时再配置 |
| **目录本地化** | 技能目录打包在软件里，不依赖外部网站 |
| **按需下载** | 实际服务器代码在安装时通过 `npx -y` 下载，不增加安装包体积 |

---

## 二、数据模型

### 2.1 技能目录（Skill Catalog）

打包在软件里的 JSON 文件，定义所有可用技能：

```typescript
// coworker/skills/catalog.ts
interface SkillEntry {
  id: string;                    // 唯一标识，如 "brave-search"
  name: string;                  // 显示名称，如 "Brave 搜索"
  description: string;           // 简短描述
  category: SkillCategory;       // 分类
  icon?: string;                 // 图标 URL 或 emoji
  version: string;               // 版本号，如 "1.0.0"
  
  // 安装配置
  install: {
    type: "stdio" | "http" | "sse";  // 传输协议：stdio=本地命令 / http=Streamable HTTP / sse=Server-Sent Events
    command?: string;            // stdio 类型：执行命令，如 "npx"
    args?: string[];             // stdio 类型：命令参数
    url?: string;                // http 类型：服务器 URL
    env?: Record<string, string>; // 环境变量（可选）
    headers?: Record<string, string>; // HTTP 头（可选）
  };
  
  // 配置（安装后需要）
  config?: {
    type: "api_key" | "oauth" | "none";
    keyName?: string;            // 配置项名称，如 "API Key"
    keyHint?: string;            // 提示文字
    envKey?: string;             // 环境变量名，如 "BRAVE_API_KEY"
  };
  
  // 元数据
  tags?: string[];               // 标签，如 ["搜索", "联网"]
  author?: string;               // 作者
  homepage?: string;             // 主页
  downloadCount?: number;        // 下载量（可选，可从远端同步）
}

type SkillCategory = 
  | "search"        // 搜索
  | "document"      // 文档处理
  | "database"      // 数据库
  | "development"   // 开发工具
  | "productivity"  // 效率工具
  | "media"         // 媒体处理
  | "communication" // 通讯
  | "other";        // 其他
```

### 2.2 预装技能列表

```typescript
// coworker/skills/builtin.ts
export const BUILTIN_SKILLS: SkillEntry[] = [
  {
    id: "brave-search",
    name: "Brave 搜索",
    description: "联网搜索，获取实时信息",
    category: "search",
    icon: "🔍",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-brave-search"],
    },
    config: {
      type: "api_key",
      keyName: "Brave Search API Key",
      keyHint: "从 brave.com/search/api 免费申请",
      envKey: "BRAVE_API_KEY",
    },
    tags: ["搜索", "联网"],
  },
  {
    id: "filesystem",
    name: "文件系统",
    description: "读写本地文件，支持目录浏览",
    category: "development",
    icon: "📁",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-filesystem", "${workspace}"],
    },
    config: { type: "none" },
    tags: ["文件", "本地"],
  },
  {
    id: "sqlite",
    name: "SQLite",
    description: "查询本地 SQLite 数据库",
    category: "database",
    icon: "🗄️",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-sqlite"],
    },
    config: { type: "none" },
    tags: ["数据库", "SQL"],
  },
  {
    id: "github",
    name: "GitHub",
    description: "GitHub 操作：仓库、Issue、PR",
    category: "development",
    icon: "🐙",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-github"],
    },
    config: {
      type: "api_key",
      keyName: "GitHub Personal Access Token",
      keyHint: "从 github.com/settings/tokens 创建",
      envKey: "GITHUB_PERSONAL_ACCESS_TOKEN",
    },
    tags: ["代码", "版本控制"],
  },
  {
    id: "fetch",
    name: "网页抓取",
    description: "抓取网页内容，转为 Markdown",
    category: "document",
    icon: "🌐",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-fetch"],
    },
    config: { type: "none" },
    tags: ["网页", "爬虫"],
  },
  // —— 大厂平台技能（阿里百炼 / 腾讯元器）已暂缓，见 2.3 节说明 ——
  {
    id: "gaode-maps",
    name: "高德地图",
    description: "地图搜索、路线规划、POI 查询",
    category: "other",
    icon: "🗺️",
    version: "1.0.0",
    install: {
      type: "stdio",
      command: "npx",
      args: ["-y", "@anthropic/mcp-server-gaode"],
    },
    config: {
      type: "api_key",
      keyName: "高德开放平台 API Key",
      keyHint: "从 lbs.amap.com 免费申请",
      envKey: "AMAP_API_KEY",
    },
    tags: ["地图", "导航"],
  },
];
```

### 2.3 暂缓：大厂平台技能（阿里百炼 / 腾讯元器）

> **结论：连接大厂平台技能当前不值得做，已从 MVP 技能列表移除。** 以下澄清常见误区，便于后续若确有一个具体大厂技能诉求时再正确落地。

原设计把 `alibaba-bailian` / `tencent-yuanqi` 当作"一个 Key 连上大厂全部能力"的客户端 MCP 服务，经核实不成立：

- **阿里百炼**：DashScope `DASHSCOPE_API_KEY` 本身只能调**模型 API**（通义千问 / embedding / rerank），与 MCP 无关；百炼"云部署 MCP 服务"（WebSearch、代码沙箱、数据库…）需**逐个在控制台开通**，且走的是 **Responses API 把 MCP 当模型侧工具**（`tools=[mcp_tool]`），并非本系统 `MCPManager.ensure()` 那种"客户端连一个独立 MCP server"的模式。因此"安装→连接→工具注册进聊天引擎"的流程对百炼不成立。原文档所用端点 `https://dashscope.aliyuncs.com/compatible-mode/v1/mcp` 也非真实可用地址（真实端点为带实例 ID 的 SSE 形式）。
- **腾讯元器**：原 URL `https://tcb.cloud.tencent.com/mcp-server/xxx` 是占位符，实测该路径为腾讯云 COS 静态文档页（POST 被 405 拒绝），不是活 MCP。元器真正可外部调用的是其 **agent 对话接口** `POST yuanqi.tencent.com/openapi/v1/agent/chat/completions`（`appkey` + `assistant_id`），属于另一套"智能体桥接"机制，而非客户端 MCP。
- 本系统**联网搜索已内置免费**（DuckDuckGo，`agent.py` 默认注册 `web_search`），无需向大厂购买。

**若未来确要连接某个具体大厂技能，正确形态应为：**

| 诉求 | 正确机制 | 备注 |
|------|----------|------|
| 复用某大厂平台上**已发布的智能体** | 在 `coworker/connectors/descriptors.py` 新增"智能体桥接"连接器，转发到其对话 API | 走 `connectors` 框架，而非 MCP 目录 |
| 用大厂模型后端 | 复用现有 provider 体系（DeepSeek / MiMo），或新增 DashScope provider | 与 MCP / 技能广场无关 |
| 用大厂某个真实 SSE 型 MCP 服务 | 用本系统已支持的 `sse` 类型（见 2.1）粘贴其专属 SSE URL | 需该服务确实提供 SSE 端点 |

---

## 三、UI 设计

### 3.1 页面结构

```
┌─────────────────────────────────────────────────┐
│  技能广场                              [已安装 3] │
├─────────────────────────────────────────────────┤
│  [全部] [搜索] [文档] [数据库] [开发] [效率] [其他] │
├─────────────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐         │
│  │ 🔍      │  │ 📁      │  │ 🗄️      │         │
│  │Brave搜索│  │文件系统  │  │SQLite   │         │
│  │联网搜索  │  │读写本地  │  │查询本地  │         │
│  │         │  │文件     │  │数据库   │         │
│  │[安装]   │  │[安装]   │  │[安装]   │         │
│  └─────────┘  └─────────┘  └─────────┘         │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐         │
│  │ 🐙      │  │ 🌐      │  │ 🗺️      │         │
│  │GitHub   │  │网页抓取  │  │高德地图  │         │
│  │仓库/PR  │  │转Markdown│  │地图/导航 │         │
│  │         │  │         │  │         │         │
│  │[安装]   │  │[安装]   │  │[安装]   │         │
│  └─────────┘  └─────────┘  └─────────┘         │
└─────────────────────────────────────────────────┘
```

### 3.2 已安装列表

```
┌─────────────────────────────────────────────────┐
│  已安装技能                              [技能广场]│
├─────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────┐    │
│  │ 🔍 Brave 搜索                    [已配置] │    │
│  │ 联网搜索，获取实时信息                     │    │
│  │ [配置] [卸载]                            │    │
│  └─────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────┐    │
│  │ 📁 文件系统                      [待配置] │    │
│  │ 读写本地文件，支持目录浏览                  │    │
│  │ [配置] [卸载]                            │    │
│  └─────────────────────────────────────────┘    │
└─────────────────────────────────────────────────┘
```

### 3.3 配置弹窗

点击"配置"后弹出：

```
┌─────────────────────────────────┐
│  配置 Brave 搜索                │
├─────────────────────────────────┤
│  API Key:                       │
│  ┌─────────────────────────┐    │
│  │ sk-xxxx...              │    │
│  └─────────────────────────┘    │
│  从 brave.com/search/api 免费申请│
│                                 │
│  [取消]  [保存]                  │
└─────────────────────────────────┘
```

---

## 四、安装流程

### 4.1 一键安装（无需配置）

```
用户点击"安装"
    ↓
前端调用 POST /v1/skills/install
    body: { skill_id: "brave-search" }
    ↓
后端执行：
    1. 读取技能目录，获取 install 配置
    2. 如果 type=stdio：
       - 执行 command + args（如 npx -y @anthropic/mcp-server-brave-search）
       - 等待进程启动，验证连接
    3. 如果 type=http 或 type=sse：
       - 直接记录 URL，不立即连接（需要 API Key 才能连）
    4. 写入 mcp.json：
       {
         "mcpServers": {
           "brave-search": {
             "command": "npx",
             "args": ["-y", "@anthropic/mcp-server-brave-search"],
             "env": {}
           }
         }
       }
    5. 返回成功
    ↓
前端刷新已安装列表
```

### 4.2 配置（安装后）

```
用户点击"配置"
    ↓
弹出配置弹窗，填写 API Key
    ↓
用户点击"保存"
    ↓
前端调用 PATCH /v1/skills/config
    body: { skill_id: "brave-search", config: { "BRAVE_API_KEY": "xxx" } }
    ↓
后端执行：
    1. 更新 .env（明文 Key，经 SecretStore）与 mcp.json 中的 ${VAR} 引用
    2. 如果是 stdio 类型：重启连接
    3. 如果是 http / sse 类型：建立连接并验证（sse 走 Server-Sent Events 长连）
    ↓
前端刷新状态：已配置
```

### 4.3 卸载

```
用户点击"卸载"
    ↓
确认弹窗
    ↓
前端调用 DELETE /v1/skills/{skill_id}
    ↓
后端执行：
    1. 从 mcp.json 中移除该服务器配置
    2. 关闭连接（如果有）
    ↓
前端刷新已安装列表
```

---

## 五、后端 API

### 5.1 获取技能目录

```
GET /v1/skills
Response: {
  skills: SkillEntry[],
  installed: string[]  // 已安装的 skill_id 列表
}
```

### 5.2 安装技能

```
POST /v1/skills/install
Body: { skill_id: string }
Response: { ok: boolean, error?: string }
```

### 5.3 配置技能

```
PATCH /v1/skills/config
Body: { skill_id: string, config: Record<string, string> }
Response: { ok: boolean, error?: string }
```

### 5.4 卸载技能

```
DELETE /v1/skills/{skill_id}
Response: { ok: boolean, error?: string }
```

### 5.5 获取已安装技能状态

```
GET /v1/skills/installed
Response: {
  installed: Array<{
    skill_id: string,
    status: "installed" | "configured" | "connected" | "error",
    error?: string
  }>
}
```

---

## 六、文件结构

```
coworker/
├── skills/
│   ├── catalog.ts          # 技能目录（BUILTIN_SKILLS）
│   ├── types.ts            # 类型定义
│   └── installer.ts        # 安装/配置/卸载逻辑
│
surfaces/gui/src/
├── components/
│   └── SkillStore/
│       ├── SkillStore.tsx   # 技能广场主页面
│       ├── SkillCard.tsx    # 技能卡片
│       ├── InstalledList.tsx # 已安装列表
│       └── ConfigModal.tsx  # 配置弹窗
│
coworker/server/
├── routes/
│   └── skills.ts           # /v1/skills/* API
```

---

## 七、与现有代码的关系

### 7.1 复用 MCP 基础设施

技能安装本质是配置 MCP 服务器，复用现有代码：

- `coworker/mcp/config.py` → `load_mcp_servers()` / `write_mcp_config()`
- `coworker/server/app.py` → `addMcpServer()` / `connectMcp()` / `deleteMcpServer()`
- `coworker/mcp/client.py` → `MCPManager.ensure()` / `reload_server()`

### 7.2 新增 vs 复用

| 功能 | 新增/复用 | 说明 |
|------|----------|------|
| 技能目录 | 新增 | `skills/catalog.ts` |
| 安装逻辑 | 复用 | 封装 `addMcpServer` |
| 配置逻辑 | 复用 | 封装 `connectMcp` |
| 卸载逻辑 | 复用 | 封装 `deleteMcpServer` |
| 状态查询 | 新增 | 查询 mcp.json 中已配置的服务器 |
| UI 页面 | 新增 | `SkillStore/` 组件 |
| API 路由 | 新增 | `/v1/skills/*` |

### 7.3 与已上线 MCP 连接器 UI 的关系（重要）

当前 **v1.3.13** 已自带一套 MCP 连接器管理界面：`surfaces/gui/src/components/ManageTabs.tsx` 的 **Integrations → MCP servers**，具备：预设卡片（Brave / Fetch / Notion / Granola 等）、自定义添加（**stdio / http / sse** 三种类型）、一键连接与工具探测。

**因此本 Skill Store 不应与现有 MCP UI 平行重做。** 明确二者定位：

| 维度 | 现有 ManageTabs（MCP 连接器） | 本 Skill Store |
|------|------|------|
| 定位 | 底层连接器管理（增删改连、自定义 URL/命令） | **预设目录层**：把常用技能打包成"一键安装"卡片 |
| 用户入口 | Integrations 标签页 | 新增"技能广场"入口（或作为 ManageTabs 内的一个 Tab） |
| 数据 | 直连 `mcp.json` | 读取内置 `catalog`，安装时调用 ManageTabs 既有 API |

**推荐方案**：Skill Store 作为 **ManageTabs 之上的预设目录层**，复用其底层 `addMcpServer / connectMcp / deleteMcpServer` API，不另起一套连接逻辑。落地时优先让"技能广场"调用现有 MCP 接口，避免两套 UI 并行维护、状态分裂。

---

## 八、MVP 范围

### 第一步（MVP）
> 范围澄清：仅含 stdio/npm 型自有技能（Brave / filesystem / sqlite / github / fetch / 高德）。**大厂平台技能（百炼 / 元器）已暂缓**，见 2.3 节。
- [x] 技能目录数据模型
- [ ] 技能广场页面（网格布局）
- [ ] 一键安装（stdio / http / sse 类型）
- [ ] 已安装列表
- [ ] 配置弹窗（API Key）

### 第二步（完善）
- [ ] 分类筛选
- [ ] 搜索功能
- [ ] HTTP 类型技能安装
- [ ] 技能详情页
- [ ] 下载量统计

### 第三步（远端同步）
- [ ] 接 MCPFind / MCPHub API
- [ ] 定时同步技能列表
- [ ] 用户评分/评论

---

## 九、注意事项

1. **npx 缓存**：首次安装会下载 npm 包，后续复用缓存。考虑在 UI 上提示"首次安装可能需要几秒"。

2. **密钥安全**：明文 API Key **不**写入 `mcp.json`。真实密钥由 `SecretStore` 落盘到 `~/.config/coworker/.env`（权限 0600），`mcp.json` 仅保留 `${VAR}` 形式的引用（如 `"env": { "BRAVE_API_KEY": "${BRAVE_API_KEY}" }`），加载时由 `secrets.resolve()` 展开。这样密钥与配置分离，便于备份与多机同步。

3. **错误处理**：
   - npx 下载失败 → 提示网络问题
   - 连接超时 → 提示服务器不可用
   - API Key 无效 → 提示重新配置

4. **国际化**：技能名称和描述支持中英文，根据系统语言切换。

5. **版本管理**：后续可支持技能版本更新（对比本地版本 vs 远端版本）。
