# Domain Docs

工程技能在探索代码库时如何消费本仓的领域文档。

## Before exploring, read these

- **`GLOSSARY.md`**（仓根）
- **`docs/adr/`**：读与你要动的区域相关的 ADR

任一文件不存在时，**静默继续**。不要特意提出它们缺失，也不要建议预先创建。`/domain-modeling` 技能（由 `/grill-with-docs` 和 `/improve-codebase-architecture` 驱动）会在术语或决策真正定下来的那一刻懒生成它们。

## File structure

本仓是单上下文（single-context）：

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── coworker/
```

## Use the glossary's vocabulary

当你的输出要给某个概念命名（issue 标题、重构提案、假设、测试名），用 `GLOSSARY.md` 里定义的那个词。不要漂移到 glossary 明确回避的同义词。

如果你需要的概念还不在 glossary 里，那是一个信号：要么你在发明项目不用的语言（重新考虑），要么存在真实的空缺（记下来交给 `/domain-modeling`）。

## Flag ADR conflicts

如果你的输出与某条已有 ADR 相矛盾，显式抬出来，而不是悄悄覆盖：

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
