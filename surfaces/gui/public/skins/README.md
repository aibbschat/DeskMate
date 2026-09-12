# 皮肤视觉资源（v1.9.7 起闲置）

> **状态**: 暂存待用。v1.9.6 试过把 `hero.jpg` 作为主聊天区 hero 大背景（仿 WorkBuddy 范式），
> 但 OpenWorker 空状态有"小桔在忙 + 全息屏"占据中央，hero 会被前景全挡。萧哥 09-08 拍板
> 撤回，等待借鉴 WorkBuddy 之后做"纯景物无人物"的新方案再启用。

## 文件

- `xiaoju-orange/hero.jpg` (234KB, 1200×1200) — doubao 09-08 裁的"小桔橙光"小桔人物立绘
  （已去掉设计稿色板/文字/装饰）。背景渐变干净，可自然融入 surface.paper。

## 处置

- 资产先保留，不要删（万一新方案要用）
- 期间**不**被任何代码引用（v1.9.6 撤了 `SkinMeta.hero` 字段和 `<SkinHero />` 组件）
- 等萧哥做新方案（纯景物 / 装饰角标 / 设置页 banner 等）时再启用

## 关联

- `src/components/deskmate/skins/skinRegistry.ts` 头部注释 + SkinMeta 字段处均有 TODO 提醒
- `docs/2026-09-08.md` 工作日志
