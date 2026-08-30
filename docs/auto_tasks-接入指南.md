# auto_tasks 统一框架 — 定时任务接入指南

## 概述

所有与"我/写作/作品"相关的自动任务（每日读书、进度跟踪、风格复盘…）都通过 `auto_tasks` 统一框架沉淀，实现：
- **自动落地**：任务执行完调用一次 API，自动写时间线+洞察+详情
- **自动分类**：按 `category` 字段自动归到「作品与创作」等栏目
- **自动展示**：自动出现在「它眼中的我 → 作品与创作」栏目
- **自动入记忆**：标记 `push_to_vault` / `push_to_style` 的洞察自动进入长期记忆

## 数据目录

```
~/.deskmate/auto_tasks/
├── registry.json              # 任务注册表
├── timeline.jsonl             # 统一时间线
├── insights/<task_type>/<date>.jsonl  # 结构化洞察
└── details/<task_type>/<file>.md       # 详情文件（大段心得/简报）
```

## API 端点

### 记录一条任务沉淀

```
POST /v1/deskmate/auto-tasks/record
Content-Type: application/json

{
  "task_type": "daily_reading",
  "target": "17-明朝的裂变",
  "action": "finish",
  "summary": "全书5689行读完，风格为说书人腔调口语化历史随笔",
  "insights": [
    {
      "text": "说书人腔调，口语化历史随笔，与大宋梦华散文体形成对比",
      "tags": ["风格"],
      "push_to_vault": true,
      "push_to_style": true
    }
  ],
  "tags": ["风格", "叙事结构"],
  "detail_content": "# 完整心得\n\n...",
  "task_name": "每日读书"
}
```

### 查询统计

```
GET /v1/deskmate/auto-tasks/stats
```

### 查询时间线

```
GET /v1/deskmate/auto-tasks/timeline?limit=50&task_type=daily_reading
```

### 查询洞察

```
GET /v1/deskmate/auto-tasks/insights?task_type=daily_reading&limit=20
```

## 现有定时任务接入方法

### 方法一：在定时任务 prompt 里加沉淀指令（推荐）

在你的定时任务描述末尾加上：

```
**第五步：沉淀结果（必须执行）**
任务完成后，调用以下 API 把结果沉淀到 auto_tasks 框架：
POST http://localhost:<port>/v1/deskmate/auto-tasks/record
Body: {
  "task_type": "daily_reading",
  "target": "<书名>",
  "action": "start|continue|finish",
  "summary": "<一句话摘要>",
  "insights": [{"text": "<洞察内容>", "tags": ["<标签>"], "push_to_vault": true}],
  "tags": ["<标签>"],
  "detail_content": "<完整心得>"
}
必须实际调用 API，不能只在回复里说"已沉淀"。
```

### 方法二：任务执行完后手动调用

如果定时任务不支持 API 调用，可以在任务执行完后，用 curl 手动沉淀：

```bash
curl -X POST http://localhost:<port>/v1/deskmate/auto-tasks/record \
  -H "Content-Type: application/json" \
  -d '{
    "task_type": "daily_reading",
    "target": "17-明朝的裂变",
    "action": "finish",
    "summary": "全书读完",
    "insights": [{"text": "说书人腔调", "tags": ["风格"], "push_to_vault": true}]
  }'
```

## 新设自动任务的步骤

1. **登记任务**（可选，不登记会自动归到 other 分类）：
   ```python
   from deskmate.auto_tasks import register_task
   register_task("style_monthly_review", "月度风格复盘", category="works")
   ```

2. **任务执行完调用沉淀 API**（见上）

3. **自动出现在「它眼中的我 → 作品与创作」栏目**，无需改前端

## 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| task_type | string | 任务类型唯一标识（如 daily_reading） |
| target | string | 作用对象（书名/项目名/文章名） |
| action | string | 动作：start/continue/finish/analyze/remind |
| summary | string | 一句话摘要（展示在卡片上） |
| insights | array | 结构化洞察列表，每条含 text/tags/push_to_vault/push_to_style |
| tags | array | 标签（用于检索和分组） |
| detail_content | string | 完整心得/简报（大段内容，存为详情文件） |
| task_name | string | 展示名（未登记时必填） |

## 已抢救的历史数据

从 session 记录中抢救了 5 条历史记录：
- 2026-08-26 进度跟踪：国子监秘闻录
- 2026-08-28 进度跟踪：国子监秘闻录（2 次）
- 2026-08-28 每日读书：《大清钱币王》
- 2026-08-29 每日读书：书17《明朝的裂变》（含 4 条洞察）

更早的 session 记录已被清理，无法抢救。建议从现在起，所有定时任务都接入 auto_tasks 框架。
