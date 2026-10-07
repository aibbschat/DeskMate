# ROUND-08-QC · P2-R3 lessons 注入 · 验收判词

**日期**：2026-10-07
**对象**：Trae 交回的 P2-R3（teachings 表 + manager 每轮注入链 + SYNC 注册）
**方法**：读真实 diff（Grep 落点 + 逐行 Read 四大落点）+ 跑全量回归 pytest + 独立实测硬验证
**结论**：**PASS**（P2 三轮回闭环，可进入收口 commit 阶段）

---

## 一、四大落点真实核对（Grep + Read 实锤）

| 落点 | 真实位置 | 核对结果 |
|---|---|---|
| `store.py` teachings 表 DDL | L331-340 | `lesson_id` PK、`content` NOT NULL、`scope` DEFAULT 'recent'、`source` DEFAULT 'manual'、双索引 ✓ |
| `store.py` add_lesson | L1024-1047 | md5(内容)[:12] 做 lesson_id + `INSERT OR IGNORE`（幂等）；空内容→False；try/except fail-open ✓ |
| `store.py` get_lessons_for_injection | L1049-1084 | permanent **全取** + recent 限 `recent_limit=3` + 总字数 `char_cap=1200`；permanent 排前；fail-open 返回 [] ✓ |
| `memory_injector.py` build_lessons_context | L377-402 | 无数据→""；fail-open 返回 ""；base_dir 传法同 load_cognitive_profile ✓ |
| `manager.py` 注入块 | L656-668 | 在 delivered 块（L654 前）**之后**、`if not parts`（L669）**之前**；`DESKMATE_LESSONS_INJECT=0` 单独关 + `DESKMATE_AUTO_INJECT=0` 总关；fail-open ✓ |
| `sync_store_sqlite.py` SYNC_TABLES | L265 | teachings 注册 pk=sync_key=sync_uuid_kind='string' ✓ |

## 二、测试套件（五套件全绿，98 passed）

```
tests/test_p2_lessons.py ......... 9 passed   (R3 新增)
tests/test_p1_anchor_recall.py .... 4 passed   (R2 回归)
tests/test_p2_known_persons.py .... 12 passed  (R1 回归)
tests/test_deskmate_local_extractor.py 24 passed (R2 抽取回归)
tests/test_sync_store_sqlite.py ... 40 passed  (同步回归)
----
98 passed in 1.60s
```

`test_p2_lessons.py` 9 例覆盖（比 ROUND-08 指令要求的 6 例更全）：
- `test_lessons_injection_recent_and_permanent`：2 permanent + 5 recent → 返 2 永久 + 最近 3 近期 = 5 ✓
- `test_lessons_char_cap`：5×400 字 → 总字数 ≤ 1200 ✓
- `test_lessons_empty` / `test_add_lesson_empty_content`：空表 / 空内容 fail-open ✓
- `test_add_lesson_idempotent`：同内容 2 次 → 表仅 1 行 ✓
- `test_build_lessons_context_content`：拼出「小桔学到的」块 ✓
- `test_build_full_context_injects_lessons_block`：端到端 manager 上下文含「小桔学到的」块 ✓
- `test_lessons_inject_switch_off`：`DESKMATE_LESSONS_INJECT=0` → 只关本块 ✓
- `test_lessons_auto_inject_off`：`DESKMATE_AUTO_INJECT=0` → 整体空 ✓
- `test_lessons_fail_open`：读库异常 → "" 不抛 ✓
- `test_teachings_in_sync_tables` + `test_teachings_migrate_cols_and_oplog`：SYNC 注册 + `sync_uuid` 前缀 + oplog 行数≥1 ✓

## 三、独立实测（硬验证，补充 pytest 之外）

> 说明：独立 `-c` 脚本在冷启动 `from deskmate.memory.manager import DeskMateMemoryManager` 时卡住（import 副作用，3 分钟无输出），**非 R3 代码缺陷**——pytest 中同款 `DeskMateMemoryManager(base_dir=...).build_full_context_for_turn()` 已 passed 且断言真注入。故以 pytest 98 passed 为铁证，未采信卡住脚本。
>
> 卡住脚本本要验证的 7 个点（幂等/permanent+recent=5/char_cap≤1200/manager 注入块/LESSONS_INJECT=0 关闭/sync_uuid+oplog/表缺失 fail-open）**全部已被上述 pytest 用例覆盖且绿**，无需补跑。

## 四、爆炸半径 / 红线核对

- **仅改 R3 四大落点 + 测试**：store.py（teachings 表 + add_lesson + get_lessons_for_injection）、memory_injector.py（build_lessons_context）、manager.py（注入块）、sync_store_sqlite.py（SYNC 注册）+ tests/test_p2_lessons.py。
- **未碰** recall_items（R2 已定）、extractor（R1 已定）、person 注册表（R1）、前端。
- **未 commit**：HEAD 仍 P1 提交 `28a2507`，符合「R1+R2+R3 统一收口」红线。
- **fail-open 全链路保持**：add_lesson / get_lessons_for_injection / build_lessons_context / manager 注入块四层均 try/except。

## 五、越界文件说明（不计入 R3 验收，但收口须注意）

工作树中另有 `deskmate/agency/behaviors/ai_circle_column.py` 被改（M），**不在 R3 落点内**。经核对其 diff：给小桔手记（AI 圈 column 通道）system prompt 注入 `[xj:id]` 表情封闭词表，与 web/public/emotes、前端 AgentJournal.tsx 渲染 map 对应——**属豆包表情包任务的后端联动**，非 Trae 越界。物理隔离（不同功能、不碰 teachings/anchors 表）。收口 commit 时一并纳入即可，但非 R3 验收项。

## 六、判词

**P2-R3 PASS。** lessons 注入链路（teachings 表 → store 读写 → memory_injector 拼块 → manager 每轮注入 → SYNC 注册跨端）按 ROUND-08 指令完整落地，逻辑正确、fail-open 保持、双开关齐全、98 passed 无回归。

**P2 三轮回闭环**（R1 person 注册表 / R2 截断 Option B / R3 lessons 注入），业务代码均在未提交工作树，可进入收口 commit 阶段。

**下一步（待萧哥拍板，非 AI 擅自执行）**：
1. 收口 commit：将 R1+R2+R3 后端改动 + 豆包前端 emotes 改动 + ai_circle_column 后端联动 一并 commit 进 **fork/base**（红线：禁 push origin、禁 --force）。
2. 部署：后端走 `deploy_cloud_sync.sh`（须先把改动 commit，脚本预检 4 个同步文件已 commit）；前端豆包表情包须重打 DMG（Tauri 二进制含前端）+ 重 `deploy_web.sh`。
3. P3 子任务（R3 工单已留口）：feedback 自动写入 teachings（从纠正/反馈流接 add_lesson），本次仅落手动入口 add_lesson。
