---
name: office-word
description: 用 officecli 从零生成/重排/编辑 Word(.docx) 新文档——报告、信件、提纲、表格、目录、页眉页脚。遵循「神圣稿件红线」与「理解→构建→核验→交付」四阶段框架。读取/查阅/核对萧盛原稿请走 office-doc 技能。
tags: [办公, Word, 文档, docx, 排版, 报告, 生成]
trigger: 用户让你新建、重排、生成、编辑 Word 文档(.docx)（报告/信件/提纲/合同/纪要），且不是「读取/查阅原稿」时
category: office
created: 2026-10-15
updated: 2026-10-15
uses: 0
---

# 办公 · Word 文档（officecli-docx）

> **与 `office-doc` 的关系**：`office-doc` 负责**读取/查阅/核对**萧盛的原稿与章节；
> 本技能负责**从零生成**或**在副本上编辑**新的 `.docx`（报告/信件/提纲等）。
> 两者互补——读到"要看稿子"优先用 `office-doc`。
>
> 本技能驱动小桔用 `officecli`（已装于 `~/.local/bin/officecli`）处理 `.docx`。
> 完整命令 schema 见深度参考：`skill_view officecli-docx`（已导入 `imported/`，只读）。
> 当属性名/枚举不确定时，先 `officecli help docx <element>` 再动手，**不要猜**。

## ⛔ 红线（最高优先，触犯即事故）

1. **神圣稿件绝不触碰**：在编辑任何**已存在**的 `.docx` 之前，先用
   `deskmate/sacred_paths.py` 的 `resolve` 解析真实路径，判断它是否落在含 `.deskmate/`
   的目录（即作者原稿）。**原稿正文绝不自动改写/删除**；`is_author_manuscript()` 命中即停。
   若任务是处理稿件，一律在**副本**上操作，**绝不覆盖原稿**。
2. **新产出写新路径**：生成类任务落到用户工作目录下全新的文件名，默认不动用户现有文件。
3. **真实优先 / 不编造**：不虚构事实、日期、引用、数据。数字/结论只来自用户给的材料。
4. **破坏性操作前先留恢复副本**（`.bak`），再执行 `officecli` 写操作。

## 四阶段框架（每条文档任务都走）

1. **理解（orient）**：已有文件先 `officecli view "$FILE" outline` 看结构，绝不盲改。
2. **构建（build）**：`create` + `open`，结构性先、内容后、格式最后；小步增量、每步查 exit code。
   若任务是编辑**已存在**文档（非新建），构建第一步先留恢复副本：
   `python -m deskmate.skills.tools.office_safe_deliver protect "$FILE"`。
3. **核验（verify）**：`view html`/`issues`/`outline` + `validate`（视觉 QA 用 officecli 自带，
   无需 LibreOffice；Mac 上 `view html` 即可看分页/层级/留白）。
4. **交付（deliver）**：构建完成后做原子替换 + 恢复副本：
   `python -m deskmate.skills.tools.office_safe_deliver deliver --source "$FILE"`
   （若先生成到独立候选文件再用 `--candidate <候选>`）；随后 `save`/`close` 落盘；
   交付门禁全过才声明完成。

## Shell 纪律（zsh 下极易错）

- **含方括号的 DOM 路径必须单引号**：`'/body/p[1]'`、`'/footer[1]'`、`'/body/tbl[1]/tr[2]'`。`[N]` 在 zsh 里是 glob 字符类，不加引号直接报 `no matches found: /footer[1]`，命令**静默失败**（最易踩）。
- **含 `()` 的 `--prop` 值必须加引号**：如 `--prop 'c2=营收(万)'`。括号在 zsh 里是 glob 限定符，裸写 `营收(万)` 报 `unknown file attribute` 且命令静默失败。整个 `--prop` 参数加单/双引号即可（`--prop "formula=SUM(A1:A10)"` 也安全）。
- **含 `$` 的值单引号包住**：`--prop text='$50M'`；双引号 `"$50M"` 会被 shell 吞成 `M`。
- `--prop text=` 里 `\n`=换行、`\t`=制表；要字面反斜杠用 `\\n`。
- 长脚本用 `batch`（一次 JSON 数组），每步独立；出错不级联。

## 通用流程

```bash
FILE="交付稿.docx"
officecli create "$FILE"        # 新文件先建
officecli open "$FILE"          # 常驻（后续读都看到编辑）
# 结构 → 内容 → 格式
officecli add "$FILE" /body --type paragraph --prop text="季度综述" --prop style=Heading1 --prop size=18pt --prop bold=true --prop spaceAfter=12pt
officecli add "$FILE" /body --type paragraph --prop text="营收同比增长 18%，超计划。" --prop size=11pt --prop spaceAfter=8pt
# 表格
officecli add "$FILE" /body --type table --prop rows=4 --prop cols=3 --prop width=100%
officecli set "$FILE" "/body/tbl[1]/tr[1]" --prop header=true --prop c1=季度 --prop c2=营收 --prop c3=增速
# 页脚活页码（>1 页文档必加）
officecli add "$FILE" / --type footer --prop type=default --prop align=center --prop size=9pt --prop text="第 " --prop field=page
officecli save "$FILE"          # 落盘
officecli validate "$FILE"      # schema 校验
```

## 关键能力速查

- **标题层级**：`style=Heading1/Heading2` + 显式 `size`（H1≥18pt，H2=14pt 粗，正文 11–12pt）。
- **目录**：3+ 标题才加 `--type toc --prop levels="1-3"`；随后 `set /settings --prop updateFields=true`
  （页码交给 Word 打开时重算，不填静态假页码）。
- **页眉页脚/页码**：`--prop field=page`（PAGE 活字段），`get /footer[1] --depth 3` 应见到 `fldChar`
  链而非静态文字。
- **列表**：`--prop listStyle=bullet`（项目符号）；多级编号先建 `abstractNum`→`num`→`numId`。
- **图片**：`--type picture --prop src=.. --prop width=.. --prop alt=..`（alt 必填）。
- **图表**：`--type chart --prop chartType=bar --prop categories=.. --prop data="2026:120,150"`（原生可编辑，非 PNG）。
- **模板合并**：`officecli merge <template> <output>` 替换 `{{key}}` 占位；交付前清掉 `{{` 残留。
- **读取/定位**：`view outline|text|issues|html|stats`、`get /body/p[1]`、`query 'paragraph[style=Heading1]'`。

## 交付门禁（任一不过 = REJECT，不交付）

```bash
FILE="交付稿.docx"
officecli close "$FILE" 2>/dev/null
officecli validate "$FILE" | grep -q "no errors found" || { echo "REJECT: schema"; exit 1; }
officecli view "$FILE" text | grep -cE '(\$[A-Za-z_]+\$|\{\{[^}]+\}\}|<TODO>|xxxx|lorem)' | grep -q '^0$' || { echo "REJECT: token 泄漏"; exit 1; }
officecli query "$FILE" 'field[fieldType=page]' --json | grep -q '"results"' || { echo "REJECT: 无活页码"; exit 1; }
echo "DELIVERY OK"
```

## 诚实边界

`validate` 只抓 schema 错误，不抓设计错误（假标题、占位符当正文、空封面页脚）。
务必做 `view html` 视觉过一遍；无法解读图片时标注「未视觉核验」。
