---
name: office-pptx
description: 用 officecli 创建/编辑 PowerPoint(.pptx)——幻灯片、文本框/形状、图表、表格、图片、演讲备注。遵循「神圣稿件红线」与「理解→构建→核验→交付」四阶段框架。
tags: [办公, PPT, 演示, pptx, 幻灯片, 汇报]
trigger: 用户让你创建、编辑、生成演示文稿(.pptx)，或提到「PPT/幻灯片/汇报/演示/路演」时
category: office
created: 2026-10-15
updated: 2026-10-15
uses: 0
---

# 办公 · PowerPoint 演示（officecli-pptx）

> 本技能驱动小桔用 `officecli`（已装于 `~/.local/bin/officecli`）处理 `.pptx`。
> 完整命令 schema 见深度参考：`skill_view officecli-pptx`（已导入 `imported/`，只读）。
> 属性名/枚举不确定时先 `officecli help pptx <element>`，**不要猜**。

## ⛔ 红线（最高优先，触犯即事故）

1. **神圣稿件绝不触碰**：编辑任何**已存在** `.pptx` 前，先用 `deskmate/sacred_paths.py`
   解析真实路径，判断是否在含 `.deskmate/` 的目录（作者原稿）。原稿绝不自动改写/删除；
   命中 `is_author_manuscript()` 即停，需改动时一律在**副本**上做，不覆盖原稿。
2. **新产出写新路径**：汇报/路演类任务落到用户工作目录下全新文件名。
3. **真实优先 / 不编造**：每页文字、数据只来自用户给的真实材料；不虚构案例/数字/引述。
4. **破坏性操作前先留 `.bak` 恢复副本**。

## 四阶段框架

1. **理解**：明确受众/页数/风格；已有文件先 `view outline` 看 slide 列表，绝不盲改。
2. **构建**：`create`+`open` → 逐页 `add / --type slide` → 每页 `add --type shape/textbox` 放标题与正文。编辑**已存在**文档前第一步先留恢复副本：`python -m deskmate.skills.tools.office_safe_deliver protect "$FILE"`。
3. **核验**：`view html`/`issues`/`outline` + `validate`（视觉 QA 用 officecli 自带，无需 LibreOffice）。
4. **交付**：`python -m deskmate.skills.tools.office_safe_deliver deliver --source "$FILE"`（原子替换 + 恢复副本）→ `save`/`close` 落盘。

## Shell 纪律

- **含方括号的 DOM 路径必须单引号**：`'/slide[2]'`、`'/slide[1]/shape[2]'`。`[N]` 在 zsh 里是 glob，不加引号直接报 `no matches found: /slide[2]`，命令**静默失败**（最易踩）。
- **含 `()` 的 `--prop` 值必须加引号**：如 `--prop 'text=合计(万)'`。括号在 zsh 里是 glob 限定符，裸写报 `unknown file attribute` 且命令静默失败。
- 含 `$` 的值单引号：`--prop text='$15M'`。
- `--prop text=` 里 `\n`=段内换行、`\t`=制表；长文本分段用多段 paragraph。
- 批量用 `batch`（JSON 数组），逐步独立、查 exit code。

## 通用流程（16:9，画布约 33.87×19.05cm）

```bash
FILE="汇报.pptx"
officecli create "$FILE"
officecli open "$FILE"
# 封面（空白版式 + 纯色/渐变/图片背景）
officecli add "$FILE" / --type slide --prop layout=blank --prop background=1E2761
officecli add "$FILE" /slide[1] --type shape --prop text="FY26 战略综述" \
  --prop x=2cm --prop y=7cm --prop width=29.87cm --prop height=3cm \
  --prop font=Georgia --prop size=44 --prop bold=true --prop color=FFFFFF --prop align=center
# 内容页
officecli add "$FILE" / --type slide --prop layout=blank --prop background=FFFFFF
officecli add "$FILE" /slide[2] --type shape --prop text="营收同比增长 18%" \
  --prop x=1.5cm --prop y=1.2cm --prop width=30cm --prop height=2cm \
  --prop font=Georgia --prop size=36 --prop bold=true --prop color=1E2761
# 每页演讲备注（给讲者，不给观众看）
officecli add "$FILE" /slide[2] --type notes --prop text="先抛 18% 亮点，再带出 EMEA。"
officecli save "$FILE"
officecli validate "$FILE"
```

## 关键能力速查

- **幻灯片**：`add / --type slide --prop layout=blank --prop background=1E2761`
  （渐变 `background=1E2761-CADCFC-180`；图片背景 `background=image:/path/to/hero.jpg`）。
- **形状/文本框**：`add /slide[N] --type shape --prop text=.. --prop x/y/width/height(cm)
  --prop font --prop size --prop bold --prop color --prop align --prop fill=none`。
- **段落/混排样式**：形状内 `paragraph[K]`/`run[K]`；`add --type paragraph` 同形状样式 prop；
  行内不同样式追加带样式的 run。
- **智能图形**：`--prop geometry=roundRect|ellipse|leftArrow|rightArrow`（箭头/流程图形）。
- **原生图表**：`add /slide[N] --type chart --prop chartType=bar --prop categories="EMEA,APAC"
  --prop data="2026:120,150,180"`（可编辑，非贴图）。
- **表格**：`/slide[N]/table[M]`（`table-column`/`table-row`/`table-cell`，同 docx）。
- **图片**：`--type picture --prop src=.. --prop width=..`。
- **读取/定位**：`view outline|html|issues|stats`、`get /slide[N]`、`query slide|shape`。

## 交付门禁（任一不过 = REJECT）

```bash
FILE="汇报.pptx"
officecli close "$FILE" 2>/dev/null
officecli validate "$FILE" | grep -q "no errors found" || { echo "REJECT: schema"; exit 1; }
officecli view "$FILE" text | grep -cE '(\$[A-Za-z_]+\$|\{\{[^}]+\}\}|<TODO>|xxxx|lorem)' | grep -q '^0$' || { echo "REJECT: token 泄漏"; exit 1; }
officecli query "$FILE" 'slide' --json | grep -q '"results"' || { echo "REJECT: 无幻灯片"; exit 1; }
echo "DELIVERY OK"
```

## 诚实边界

`validate` 只抓 schema 错误；排版美观（对齐/节奏/留白）靠 `view html` 视觉过一遍。
无法解读图片时标注「未视觉核验」；跨查看器图表/字体渲染可能略有差异（renderer 差异，不追）。
