# 小桔日记「🩷 落记忆」闭环审查

> 审查时间：2026-08-31
> 范围：日记（MomentFeed「我们的日记」）点 🩷 → MEMORY.md → 平时聊天检索注入
> （注：个人中心的「小桔日记」其实有两条线——`DiaryPanel` 是小桔单向写的「今日小结」；
> 真正双向交互式日记 + 点🩷 + 留言，是 `MomentFeed`「我们的日记」。本审查聚焦🩷落记忆，即后者。）

## 一句话结论

| 问题 | 结论 | 实证 |
|---|---|---|
| ① 日记点🩷后，会真的落入记忆吗？ | ✅ **会** | 原子写进 `~/.deskmate/MEMORY.md` 的 `## 日记沉淀` 块 |
| ② 落入记忆后，平时聊天会被检索到并体现吗？ | ❌ **不会（断点）** | `prefetch` 命中 False，注入长度 0 |

**即「懂我 → 像我」在日记这一环是半通的**：用户以为点🩷小桔就记住了，实则只是写进了文件，
小桔在之后的日常对话里想不起来、也不会主动提起。

---

## 链路追踪（证据）

### 第一问：点🩷 → 真落记忆 ✅

| 环节 | 位置 | 说明 |
|---|---|---|
| 前端点击 | `surfaces/gui/src/components/deskmate/MomentFeed.tsx:270` `toggleLove(id, m.content)` | 正确把日记原文作为 `content` 传后端 |
| 后端路由 | `coworker/server/app.py:3430` `POST /v1/deskmate/moments/{id}/love` | 真实存在，调用 `MomentsStore.love()` |
| 落盘逻辑 | `deskmate/growth/moments.py:178` `love()` → `:67` `_append_memory_line()` | 原子写（`tmp`+`os.replace`），去重、有界裁剪 2 万字符 |
| 写入格式 | `moments.py:85` | `\n\n## 日记沉淀 · {日期}\n- 🩷 萧盛的日记（{日期}）：{内容}` |

→ **确实落盘**，且 `deskmate/growth/test_moments.py` 已覆盖（只测了「落盘」，没测「检索」——这是 bug 漏网根因）。

### 第二问：记忆 → 聊天检索 ❌（断点在检索端）

| 环节 | 位置 | 说明 |
|---|---|---|
| 聊天主链路记忆注入 | `coworker/agent.py:240` `build_full_context_for_turn(last_user)` | 每轮对话前调用 |
| 它只调 prefetch | `manager.py:434` `relevant = self.prefetch(user_message)` | **不调** `build_system_prompt`（全文 MEMORY.md） |
| prefetch 三层 | `manager.py:194/199/204` Core / Recall / 全局 | 全部基于 `_parse_sections()` |
| 分节解析 | `manager.py:860` `_parse_sections()` 正则 `^§\s*` | **只认 `§ 标题 #标签` 分节**，把 MEMORY.md 切成 (preamble, sections) |
| 🩷 沉淀块 | 写入的是 `## 日记沉淀`（非 `§`、无 #标签） | 落在 **preamble**，**永远不在 sections 列表** |
| 后果 | `_get_core_memory` / `_search_recent_memory` / `_search_global_memory` 只遍历 sections | 三层全部命中不了；preamble 也不在任何兜底分支 |

→ 能带**全文** MEMORY.md 的 `build_system_prompt()` 存在（`manager.py:119`，`test_moments`/MCP `read_memory("all")` 用），
但**聊天主链路不调用它**，所以 🩷 沉淀不会每轮随 system prompt 出现。

---

## 根因

写入端（`moments.py` 写 `## 日记沉淀`）与检索端（`memory/manager.py` 的 `§ 标题 #标签` 分节约定）
**格式约定不一致**——一个写 `##`，一个只认 `§`。

---

## 实证（一次性探针，临时目录，未碰真实 MEMORY.md）

```
① love() 返回: {'loved': True, 'memorized': True, 'message': '已悄悄记进记忆'}
=== MEMORY.md 落盘内容 ===
## 日记沉淀 · 2026-08-31
- 🩷 萧盛的日记（2026-08-31）：今天终于把第三章改完了，心情很好
① 落盘?  True

② prefetch('第三章改完了') 命中日记沉淀?  False  | 注入长度: 0
③ build_full_context_for_turn 命中?  False
④ build_system_prompt(全文,仅对照) 命中?  True

=== 结论 ===
落盘: True
聊天主链路可检索(prefetch): False
```

---

## 修复方案（最小改动、最贴合现有架构）

把 `moments.py` 的 `_append_memory_line` 写入格式对齐记忆体系的 `§ 分节 + #标签` 约定：

```
\n\n§ 日记沉淀 #moment #diary\n2026-08-31：🩷 萧盛的日记：<内容>
```

要点：
1. `§` 开头 + `#moment #diary` 标签 → 进 sections，可被 `_search_global_memory` 按关键词命中；
   `CORE_TAGS` 不含这两个标签，不会被 Core 层误吞。
2. 条目以 `YYYY-MM-DD：` 开头 → Recall 层 7 天窗口能识别并保留 + 关键词匹配。
3. 去重（整行归一化比对）继续生效。
4. **补一个回归测试**：`love()` 后断言 `prefetch("关键词")` 能检索到该条（当前测试只测落盘，漏了检索）。

### 存量迁移（需授权，会动真实 MEMORY.md）
截止今日已点🩷的 `## 日记沉淀` 旧块仍是 preamble、检索不到。
建议写一次性迁移，把现有 `## 日记沉淀` 块改写为 `§ 日记沉淀 #moment #diary` 分节。
**这步会改写 `~/.deskmate/MEMORY.md`，需你明确确认后再做。**

### 顺带小问题
`love()` 写死「萧盛的日记」，但当点的是小桔自己写的日记（`author=xiaoju`）时措辞不准，
可改为按 author 区分「小桔的日记 / 萧盛的日记」。

---

## 修复记录（2026-08-31，最终落地 = 方案 B / SQLite）

> 用户先授权「修写入格式 + 补测试」，随后指出**存储本身不长久**（单文件全量读写 + 🩷 落全局
> MEMORY.md 受截断/改写影响），遂升级为完整 SQLite 持久化改造（方案 B）。以下为最终实现。

**架构变更（3 层，API 兼容）：**

1. 新增 `deskmate/growth/diary_store.py` —— SQLite 持久层（`~/.deskmate/diary.db`，WAL）：
   - 三张表 `user_posts` / `comments` / `loved_entries`；行级写入 + 索引 + keyset 分页。
   - `love()` 落 `loved_entries`（去重、独立生命周期），**不再写全局 MEMORY.md**。
   - `export_memory_view()` 把近 365 天 `loved_entries` 导出 `~/.deskmate/diary_memory.md`
     （`§ 日记沉淀 #moment #diary` 分节），复用现有 prefetch § 分节检索，**零改 manager 核心**。
   - `migrate_from_legacy()`：从 `moments.json` + `MEMORY.md` 的 `##/§ 日记沉淀` 块**幂等**迁移；
     迁移前自动 `.bak` 备份，且只删 MEMORY.md 的沉淀块、不动其它内容。
2. `deskmate/growth/moments.py` —— `MomentsStore` 改为 `DiaryStore` 的兼容包装；
   `love()` 返回 `{loved, memorized, message}`，不再追加 MEMORY.md。
3. `deskmate/memory/manager.py` —— `_load_global_memory()` 拼接读 `diary_memory.md`，
   使 prefetch 的 Core/Recall/全局三层自动覆盖🩷沉淀（**不碰 prefetch 核心逻辑**）。
4. `coworker/server/app.py` —— `GET /v1/deskmate/moments` 加 `cursor`+`limit` 分页
   （传 cursor 返回 `{items, next_cursor}`，不传则回落旧 `snapshot()` 全量，前端兼容）。

**测试中发现并修复的 3 个真实 bug（临时目录验证，未碰真实数据）：**

| # | 现象 | 根因 | 修复 |
|---|---|---|---|
| 1 | 迁移只提取 `##` 格式块，漏掉 `§` 格式块（4 条只迁 2 条） | `§` 提取正则用 `.*$` 且带 `re.DOTALL`，`.*` 跨行吞掉整块 | 改 header 用 `[^\n]*` 限定单行 |
| 2 | 翻页丢条：2 条 + limit=1 时第 2 条永不出现 | 游标取 `rows[limit]`（越界书签项）而非 `rows[limit-1]`（最后已展示项） | 游标取最后已展示项；并升级为 `(created_at, id)` 组合游标防同秒漏/重 |
| 3 | 同秒写入的日记顺序乱 | `created_at` 仅秒精度，同秒并列按 `id DESC`（随机） | `_now_iso()` 改微秒精度 |

**验证（`.venv/bin/python -m pytest deskmate/growth/test_moments.py` → 7 passed）：**
- `test_love_writes_sqlite_and_view`：🩷 落 SQLite + 导出 `diary_memory.md`，且**不再写 MEMORY.md**。
- `test_love_dedup_no_repeat`：同内容去重。
- `test_love_searchable_in_chat_memory`：🩷 后 `prefetch` / `build_full_context_for_turn` 均命中
  「日记沉淀 / 第三章」—— **原断点已闭合**。
- `test_migrate_from_legacy_json_and_memory`：`##`+`§` 两种沉淀块 → `diary.db`（4 条）、
  MEMORY.md 仅删沉淀块（保留「国子监秘闻录」等）、幂等、生成 `.bak`。
- `test_pagination_no_loss_no_dup`：游标翻页取完 7 条无丢失/重复、时间倒序。

> **现状结论（闭合）：** ① 点🩷真的落记忆（SQLite + 检索视图）；② 平时聊天 prefetch 能检索到并体现
> ——「懂我 → 像我」在日记这一环**现已全通**。持久化改为 SQLite 后长期可扩展、不被 MEMORY.md 截断影响。

## 持久化评估（已解决）

原评估中的三类瓶颈（单文件全量读写、MEMORY.md 截断/改写、无结构化查询）已由 SQLite 方案消除：
- `moments.json` 单文件读写 → 行级 SQL + 索引 + keyset 分页；
- 🩷 沉淀脱离全局 MEMORY.md → 独立 `loved_entries` 表 + 365 天窗口视图；
- 结构化查询 → `WHERE day/created_at` + 前端游标分页。

存量迁移：实测真实 `~/.deskmate/MEMORY.md` 当前**无 `日记沉淀` 块**、亦无 `moments.json`，
故真实数据迁移为**安全空操作**（仅会创建空 `diary.db`）。详见 `diary_sqlite_design.md`。
