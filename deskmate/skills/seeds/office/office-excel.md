---
name: office-excel
description: 用 officecli 创建/编辑/分析 Excel(.xlsx)——建表、导入 CSV/TSV、公式、图表、透视表、条件格式。遵循「神圣稿件红线」与「理解→构建→核验→交付」四阶段框架。
tags: [办公, Excel, 表格, xlsx, 数据, CSV]
trigger: 用户让你创建、编辑、分析、生成 Excel 表格(.xlsx)，或提到「表格/数据/CSV/统计/图表/报表」时
category: office
created: 2026-10-15
updated: 2026-10-15
uses: 0
---

# 办公 · Excel 表格（officecli-xlsx）

> 本技能驱动小桔用 `officecli`（已装于 `~/.local/bin/officecli`）处理 `.xlsx`。
> 完整命令 schema 见深度参考：`skill_view officecli-xlsx`（已导入 `imported/`，只读）。
> 属性名/枚举不确定时先 `officecli help xlsx <element>`，**不要猜**。

## ⛔ 红线（最高优先，触犯即事故）

1. **神圣稿件绝不触碰**：编辑任何**已存在** `.xlsx` 前，先用 `deskmate/sacred_paths.py`
   解析真实路径，判断是否在含 `.deskmate/` 的目录（作者原稿）。原稿数据绝不自动改写/删除；
   命中 `is_author_manuscript()` 即停，需改动时一律在**副本**上做，不覆盖原稿。
2. **新产出写新路径**：生成类任务落到用户工作目录下全新文件名。
3. **真实优先 / 不编造**：数字、统计结论只来自用户给的真实数据，不臆造行/列/汇总。
4. **破坏性操作前先留 `.bak` 恢复副本**。

## 四阶段框架

1. **理解**：已有文件先 `view outline` 看表结构/sheet 数，绝不盲改。
2. **构建**：`create`+`open` → 增量 `add`/`set`/`import`/`batch`。编辑**已存在**文档前第一步先留恢复副本：`python -m deskmate.skills.tools.office_safe_deliver protect "$FILE"`。
3. **核验**：`view html`/`issues`/`stats` + `validate`（视觉 QA 用 officecli 自带，无需 LibreOffice）。
4. **交付**：`python -m deskmate.skills.tools.office_safe_deliver deliver --source "$FILE"`（原子替换 + 恢复副本）→ `save`/`close` 落盘。

## Shell 纪律

- **含方括号的 DOM 路径必须单引号**：`'/Sheet1/chart[1]'`、`'/Sheet1/A1:B3'`（范围）。`[N]`/`[A1:B3]` 在 zsh 里是 glob，不加引号报 `no matches found`，命令静默失败。
- **含 `()` 的 `--prop` 值必须加引号**：如 `--prop 'value=合计(万)'`。括号在 zsh 里是 glob 限定符，裸写报 `unknown file attribute` 且命令静默失败。
- 路径引号包裹：`"/Sheet1/A1"`。
- 含 `$` 的值单引号：`--prop value='$50M'`。
- `--prop value=` 数字默认存为数字；要当文本加 `--prop type=string`。
- 批量用 `batch`（JSON 数组），逐步独立、查 exit code。

## 通用流程

```bash
FILE="数据表.xlsx"
officecli create "$FILE"
officecli open "$FILE"
# 新建 sheet + 冻结首行 + 自动筛选
officecli add "$FILE" / --type sheet --prop name=汇总 --prop freeze=A2 --prop autoFilter=true
# 从 CSV 导入（首行作表头、冻结、自动筛选）
officecli import "$FILE" /汇总 --file 源数据.csv --header --start-cell A1
# 直接写单元格（公式不带前导 =）
officecli set "$FILE" /汇总/B2 --prop value="=SUM(A2:A10)"
# 转为智能表（带表头、样式）
officecli add "$FILE" /汇总 --type table --prop ref=A1:D10 --prop headerRow=true --prop displayName=销售
# 原生图表（可编辑，非贴图）
officecli add "$FILE" /汇总 --type chart --prop chartType=column --prop dataRange=汇总!B2:C5 --prop categories=汇总!A2:A5
officecli save "$FILE"
officecli validate "$FILE"
```

## 关键能力速查

- **新建/重命名 sheet**：`add / --type sheet --prop name=xxx`；`set /Sheet1 --prop tabColor=4472C4`。
- **导入数据**：`import "$FILE" /Sheet1 --file x.csv --header --delimiter ';' --decimal ','`
  （欧陆 `;` 分隔 + `,` 小数时用）；也可 `--stdin` 从管道读。
- **单元格**：`set /Sheet1/A1 --prop value=42`（数字）/ `--prop value='文本'`（文本）
  / `--prop formula="SUM(A1:A10)"`（公式）。
- **智能表**：`add /Sheet1 --type table --prop ref=A1:D10 --prop headerRow=true`。
- **图表**：`chartType` ∈ bar/column/line/pie/area/scatter/radar；
  `dataRange` 取数据区，首列默认作类别（`categories=` 可显式指定）。
- **透视表 / 切片器 / 条件格式 / 迷你图**：元素已支持（`pivottable`/`slicer`/`conditionalformatting`/`sparkline`），
  具体 prop 见 `officecli help xlsx <element>`。
- **读取/定位**：`view outline|text|issues|stats`、`get /Sheet1/A1`、`query cell`、`query 'cell[type=formula]'`。

## 交付门禁（任一不过 = REJECT）

```bash
FILE="数据表.xlsx"
officecli close "$FILE" 2>/dev/null
officecli validate "$FILE" | grep -q "no errors found" || { echo "REJECT: schema"; exit 1; }
officecli view "$FILE" issues | grep -qE "error|损坏" && { echo "REJECT: 结构问题"; exit 1; }
echo "DELIVERY OK"
```

## 诚实边界

`validate` 只抓 schema 错误；数值/公式正确性靠 `view text`/`html` 核对。
图表/透视为原生 XML，跨查看器渲染可能略有差异（renderer 差异，不追）。
