# Brigade（代号「拳头」）巡检意见 · 真伪核验与可修 Bug 清单

> 用途：用户想让 Brigade 修几条「真 bug」来测它的能力。下面先把 Fist 的 6 条逐条判真伪，
> 再给出**已去代码核实**的两条真 bug，可直接交给 Brigade 修。所有结论均基于当场读源码，
> 不是凭 Fist 自述。

## 一、Fist 六条意见的真伪判定

| # | Fist 说法 | 判定 | 依据 |
|---|-----------|------|------|
| 1 | 圣经注入有 6000 字符截断，是防编造防线唯一软肋 | **部分真实（真截断，但被夸大）** | `core.py:1514` `bible["CHARACTERS"][:6000]`、`:1517` 其余三节各 `[:2000]`。但截断发生在 `_keyword_recall`（检索**回退**路径），**不是主对话注入**；中文小说主检索走 CJK LIKE 分支，不截。详见 Bug A。 |
| 2 | 增量更新只处理新增/修改，不处理「删除」，圣经会积累活死人 | **造假（核心断言不成立）** | `core.py:823` 文档明写增量每次顺带 `revoke_unsupported`；`entities.py:511` 真有实现：正文查无实据的专名置 `rejected`，圣经能收缩。**Fist 凭空说「没做删除」是错的。** 但其顺嘴提的「FTS5 不自己清」在另一层是真的 → 见 Bug B。 |
| 3 | FTS5 只关键词，语义检索为空 | **真实但属增强，非 bug** | 确为能力空白（混合召回是未来项），但这是「缺功能」不是「有缺陷」。可作为增强任务，不算 bug。 |
| 4 | 单点依赖 DeepSeek 付费 API，断网失忆 | **真实但属可靠性增强，非 bug** | 确为架构脆弱点；加 Ollama 本地 fallback 是改进项，不是缺陷。 |
| 5 | 把 brigade mcp 接回桌搭（路 A），十分钟的事 | **已被否决，勿做** | 此前已拍板不集成并摘除 `brigade-memory`。且实测 Tideline 中文召回弱（英文命中/中文无结果），Fist 此条无视该结论、重新翻案，**不可信、不可做**。 |
| 6 | 提交 brigade-research 报告 | **家务，非 bug** | `git status` 里确实是未提交；顺手 commit 即可，与能力测试无关。 |

**结论**：6 条里只有 #1 是真缺陷（且被夸大），#2 是幻觉，#3/#4 是增强，#5 是被否决提案，#6 是家务。
下面给 Brigade 的「真 bug」是 #1 的精确版（Bug A）+ #2 翻案里真正成立的那一半（Bug B）。

---

## 二、可交给 Brigade 修的真 Bug（已核实）

### Bug A — 检索回退路径里圣经摘要被硬截断（中危，范围被 Fist 夸大）
- **位置**：`deskmate/novel_memory/core.py:1511-1517`，函数 `_keyword_recall`
- **当前行为**：
  ```python
  if bible.get("CHARACTERS"):
      parts.append(bible["CHARACTERS"][:6000])
  for tag in ("OUTLINE", "TIMELINE", "NOTES"):
      if bible.get(tag):
          parts.append(f"\n## {tag}\n{bible[tag][:2000]}")
  ```
  群像小说（如《夜不收》）人物一多，`CHARACTERS` 超过 6000 字符后，排在后面的角色被整段切掉，
  检索回退时这些角色等于「不存在」。
- **精确范围（重要，别让 Brigade 误改主路径）**：
  此截断**只在 `_keyword_recall` 里**，而 `_keyword_recall` 是检索的**回退路径**：
  - FTS5 索引不存在时（`novel_search_fts5:1354`）
  - CJK 查询抽不出关键词时（`:1373`）
  - FTS5/LIKE 无结果时（`:1402`）
  - 非 CJK 查询清洗后为空时（`:1391`）
  中文小说的主检索走 `:1364` 的 CJK LIKE 分支，**不经过这段截断**。所以 Fist 说「防编造防线唯一软肋」
  是夸大——它不是主对话注入，是回退路径的圣经摘要。但作为缺陷仍成立。
- **建议修法（给 Brigade 的方向，非必须照做）**：
  回退路径里把「整段硬截」改成「按查询涉及的实体名动态取相关角色段落」，
  或按预算对 CHARACTERS 做按角色分块而非按字符盲截；至少保证回退时不超过截断线的角色也能被检索到。
  修的时候**不要动** CJK LIKE 主路径（`:1364-1385`）和 FTS5 MATCH 主路径（`:1393`）。
- **验收**：构造一个 CHARACTERS 超 6000 字的小说圣经，触发回退路径（临时改名/删 FTS5 db），
  确认排在 6000 字符后的角色仍能在 `novel_recall` 结果里被检索到。

### Bug B — 删除稿件章节后 FTS5 索引不清旧内容（中危，Fist 第 2 条里唯一成立的部分）
- **位置**：`deskmate/novel_memory/core.py:1082` `build_fts5_index`，主循环 `:1148-1208`
- **当前行为**：函数遍历**磁盘现存**文本文件做 upsert；某文件内容变了会
  `DELETE FROM novels WHERE file_path = ?` 再插新行（`:1192`）。但**全程没有一步清理「已从磁盘删除的文件」**。
  `file_tracker` 表也只增/改，不删。
- **后果**：作者删掉某章稿件后，`novels` 表（FTS5 虚拟表）里那章正文**仍在**。
  `novel_recall` / `novel_search_fts5` 用 LIKE/MATCH 查关键词，照样能返回**已删除章节**的片段
  → 检索结果混入「死章节」数据。这正是 Fist 那句「FTS5 索引不会自己清」的真相
  （只不过它错安到了圣经实体层，那层 `revoke_unsupported` 其实做了）。
- **建议修法（给 Brigade 的方向）**：
  在 `:1209` `conn.commit()` 之前，补一步垃圾回收：
  ```python
  current = {_rel_name(p, scan_root) for p in text_files}
  conn.execute("DELETE FROM novels WHERE file_path NOT IN (...) ", ...)
  conn.execute("DELETE FROM file_tracker WHERE file_path NOT IN (...) ", ...)
  ```
  注意 FTS5 的 `NOT IN` 要用参数化（按 current 集合拼占位符）。
- **验收**：对一本已建 FTS5 索引的小说，删掉其中一章文件 → 重跑 `build_fts5_index` →
  用被删章节独有词 `novel_recall`，确认结果里不再出现该章节片段。

---

## 三、给用户的提醒
- **别让 Brigade 修 #2（它说的「没做删除」）**：那件事代码里已经做了，让它去「修」只会瞎改或编个假报告。
  真要它动「删除」相关，只给 **Bug B**（FTS5 稿件层清理）。
- **#5（接回 brigade mcp）坚决不做**：这是我们已否决的决策，且 Tideline 中文召回弱已实测。
  Fist 主动翻这个案，说明它在「迎合你之前调研过的方向」，不代表它独立判断可靠。
- **#3/#4 可作为进阶能力测试**（让 Brigade 加语义检索 / Ollama fallback），但它们是增强不是 bug，
  测的是「从零加功能」能力，和「修缺陷」是两回事，分开评估。
