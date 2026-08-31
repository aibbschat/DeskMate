# 小桔日记持久化改造 · SQLite 方案设计

> 目标：解除单文件全量读写瓶颈 + 让🩷沉淀脱离会被截断/改写的全局 MEMORY.md + 前端分页不卡 + 保持"聊天能想起"闭环。
> 选型：SQLite（`sqlite3` 标准库，零额外依赖，WAL 并发安全，keyset 分页天然）。

---

## 一、现状问题回顾

| 组件 | 问题 |
|---|---|
| `moments.json` | 单 JSON 全量 `json.dumps` 写盘、全量 `_load` 进内存、无分页/游标/索引；前端一次返回全部 |
| `MEMORY.md`（🩷沉淀落此） | 2 万字符尾部截断 + 受 `compress`/`purify` 改写 → 旧沉淀长期会被静默丢/合并 |
| 整体 | 无归档、无冷热分离、无结构化查询 |

---

## 二、目标架构

- 持久层：`~/.deskmate/diary.db`（SQLite，WAL 模式）
- 三张表：`user_posts` / `comments` / `loved_entries`
- 检索视图（可选，见第六节）：由 `loved_entries` 导出 `~/.deskmate/diary_memory.md`（`§ 日记沉淀 #moment #diary` 分节），复用现有 prefetch § 分节检索，**零改 manager 核心**
- 前端：`GET /v1/deskmate/moments` 加分页（cursor + limit），MomentFeed 滚动加载

---

## 三、DB Schema

```sql
CREATE TABLE user_posts (
  id         TEXT PRIMARY KEY,
  author     TEXT NOT NULL DEFAULT 'me',   -- 'me' | 'xiaoju'
  content    TEXT NOT NULL,
  created_at TEXT NOT NULL                  -- ISO 8601
);
CREATE INDEX idx_posts_created ON user_posts(created_at DESC);

CREATE TABLE comments (
  id         TEXT PRIMARY KEY,
  moment_id  TEXT NOT NULL,
  author     TEXT NOT NULL,                 -- 'me' | 'xiaoju'
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_comments_moment ON comments(moment_id);

CREATE TABLE loved_entries (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  moment_id  TEXT,                          -- 关联 moment（可空）
  content    TEXT NOT NULL,                 -- 沉淀要点：🩷 萧盛的日记：...
  day        TEXT NOT NULL,                 -- YYYY-MM-DD（Recall 7天窗口用）
  created_at TEXT NOT NULL,
  tags       TEXT NOT NULL DEFAULT '#moment #diary'
);
CREATE INDEX idx_loved_day     ON loved_entries(day DESC);
CREATE INDEX idx_loved_content ON loved_entries(content);
```

> 关键词检索：现有 prefetch 已有 CJK 2-gram 匹配；先用 `LIKE`/内存匹配即可，
> 不必上 FTS5（避免引入复杂度）。若日后量大，可加 FTS5。

---

## 四、数据迁移（首次启动，幂等）

1. 读现有 `moments.json` → 导入 `user_posts` / `comments`；`loved` 列表（存 moment_id）反查 content 导入 `loved_entries`。
2. 读 `MEMORY.md`，正则提取 `## 日记沉淀` / `§ 日记沉淀` 块 → 解析条目 → 导入 `loved_entries`。
   - **⚠️ 这一项会从 `~/.deskmate/MEMORY.md` 删掉沉淀块**，属敏感操作，需你明确授权。
   - 不动 MEMORY.md 的其它内容（USER 画像、§ 偏好/纠正等）。
3. 迁移前自动备份 `moments.json` + `MEMORY.md` 为 `.bak`。
4. 幂等：若 `diary.db` 已存在且 `user_posts` 非空，跳过迁移。

---

## 五、MomentsStore 改造（保持 API 兼容）

- 构造时 `sqlite3.connect(diary.db, isolation_level=None)` + `PRAGMA journal_mode=WAL`；建表 `IF NOT EXISTS`。
- `add_user_post` / `add_comment` / `love` / `generate_reply` 改走 SQL（事务写）。
- `love()`：**不再写 MEMORY.md**，改为写 `loved_entries`，返回 `{loved, memorized, message}`（语义不变）。
- `snapshot()` 仍返回 `{userPosts, comments, loved}`（从 SQL 读），保持后端 `/moments` 返回结构不变 → 前端改动限于分页。

---

## 六、检索接入（保持"聊天能想起"闭环）

推荐方案（零改 manager 核心）：
- 新增 `MomentsStore.export_memory_view()`：把 `loved_entries` 近 N 天 + 按日期导出为
  `~/.deskmate/diary_memory.md`（格式 `§ 日记沉淀 #moment #diary\n{day}：{content}`）。
- 在 `memory/manager.py` 全局记忆源增加读 `diary_memory.md`（类似现有 vault 源），`prefetch` 自动覆盖。
- 触发：love 时即时导出；或定时/每次 prefetch 前轻量导出（数据量小，成本低）。

备选：改 `manager.prefetch` 加 `_search_loved_diary(query)` 层直接读 SQLite（侵入核心，不推荐）。

---

## 七、前端分页

- `GET /v1/deskmate/moments?cursor=<created_at>&limit=20` → 返回本页 + `next_cursor`。
- `MomentFeed.tsx`：`fetchDates` 改为分页拉取 + 滚动加载（"加载更多"或 IntersectionObserver）。
- 默认 `created_at DESC`。

---

## 八、兼容性 / 回滚

- 保留 `moments.json.bak`；`diary.db` 损坏时 fallback 读 `.bak`（fail-open，不阻断）。
- API 返回结构尽量不变（snapshot 结构不变），降低前后端风险。

---

## 九、测试

- 迁移测试：临时 dir 放旧 `moments.json` + 含 `## 日记沉淀` 的 MEMORY.md → 迁移后 `diary.db` 数据正确、`prefetch` 能检索到。
- 分页测试（cursor/limit）。
- 沿用 `test_moments.py::test_love_searchable_in_chat_memory`（love 后 prefetch 命中）。
- 回滚/fallback 测试。

---

## 十、实施顺序

1. 新建 `deskmate/growth/diary_store.py`（SQLite 封装 + 迁移 + 导出视图）。
2. `MomentsStore` 切换读写到 `diary_store`（保持 API）。
3. 检索接入：导出 `diary_memory.md` § 分节 + `manager` 加该记忆源。
4. 后端 `/moments` 加分页；前端 `MomentFeed` 加分页。
5. 测试 + 本地实测。
6. 提交并推 `backup/fork/base`（严禁 push origin）。

---

## 十一、待你确认的关键决策

- **A. 存量迁移授权**：是否允许迁移脚本改写 `~/.deskmate/MEMORY.md`，删掉 `##/§ 日记沉淀` 旧块并迁入 `diary.db`？（敏感操作，需明确点头）
- **B. 检索接入方式**：导出 `diary_memory.md` § 分节（推荐，零改 manager）/ 改 `manager` 加 `_search_loved_diary` 层。

---

## 实施状态（2026-08-31 已落地）

- A、B 两项均已获授权并实施：检索接入采用**推荐方案**（导出 `diary_memory.md` § 分节，零改 manager 核心）。
- 与本文档的**实现差异**：第七节分页 cursor 实际实现为 **`created_at|id` 组合游标**
  （而非单纯 `created_at`），以杜绝同秒写入时的漏条/重复；对应 SQL 用
  `(created_at, id) < (?, ?) ORDER BY created_at DESC, id DESC` 行值比较。
- `created_at` 精度由秒改为**微秒**（`_now_iso` 用 `timespec="microseconds"`），保证时间倒序自然。
- 测试见 `deskmate/growth/test_moments.py`（7 passed）；详见 `diary_heart_memory_audit.md` 修复记录。
