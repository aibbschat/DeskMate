# 桥接区 · Trae × WorkBuddy 异步协作（P1 汇流最小版）

> 协作模型：Trae（trae work）负责实施推进，WorkBuddy 负责每轮 QC（trust-but-verify）。
> 依据工单：`reports/P1_汇流最小版_施工单_2026-10-07.md`；设计：`reports/小桔_江河设计_汇流反思自主_2026-10-07.md`。
> 本目录是交接协议（非文件同步——两人同工作树 `/Users/xiaocheng/workspace/openworker`，文件天然共享）。

## 一、一轮怎么定义（R1–R5）

- **R1 数据层**：`memory_anchors` 表 + `upsert_anchors` + `SYNC_TABLES` 注册 + `migrate` 补同步三列/触发器。（含 §三 DDL 的 row_key 修正，见 ROUND-00）
- **R2 抽锚**：`extract_anchors()` + 挂线 `profile_manager.add_*`（`item_id` 取法修正，见 ROUND-00）。
- **R3 召回**：`recall_items()` 顺锚走一跳 + fail-open + 封顶/去重。
- **R4 测试**：`tests/test_p1_anchor_recall.py`（工单 §七 7 例）+ 既有回归不红。
- **R5 部署**：commit→`fork/base`（**不 push origin**）→ `deploy_cloud_sync.sh`（确认 `store.py`/`extractor.py` 在 `CHANNEL_MODULES`）→ 云端 `systemctl restart openworker-server` + 本地重开 OpenWorker.app。

每轮 = 一个 R。Trae 完成一个 R 后写 `ROUND-XX-实施记录.md`，WorkBuddy 读真实 diff + 跑测试后写 `ROUND-XX-QC.md`。

## 二、实施记录格式（Trae 写）

```
## ROUND-XX 实施记录
- 改了哪些文件 / 函数 / 行号
- 关键决策（与工单/ROUND-00 不一致的，须显式说明）
- 自带测试：pytest 路径 + 输出结论（贴关键行，勿只说"通过"）
- 自测覆盖：哪些验收判据已自证
- 遗留 / 不确定项
```

## 三、QC 格式（WorkBuddy 写）

```
## ROUND-XX QC
- 判词：PASS / PASS-WITH-NOTES / BLOCKED
- 证据：实际读到的 diff 落点（文件:行）+ pytest 真实输出
- 阻塞项（BLOCKED 才有）：必须改完才能进下一轮
- 对下一轮的输入
```
**QC 铁律**：以真实代码 + 测试为据，不凭 Trae 自述下结论。工单 §七/§八 为验收硬尺。

## 四、红线（贯穿每轮）

1. commit 进 `fork/base`，**绝不 push origin**、绝不 `--force`/rewrite。
2. 不碰神圣稿件守卫（`deskmate/sacred_paths.py`）；锚抽取只读条目文本。
3. 锚表纯文本，**绝不搬向量**；同步只走明文 `sync_uuid`。
4. 全部 fail-open：抽锚失败不影响主抽取；邻座召回失败退回纯 FTS；建表失败不影响既有查询。
5. 可回退：`DESKMATE_ANCHOR_RECALL=0` 即时退回纯 FTS；删 `memory_anchors` 表彻底回退。

## 五、回退与收口

- 任一轮 BLOCKED → 不进下一轮；先修。
- 全 R 通过 → 写落地回执（参照 `reports/2026-10-07_剩余待办收口_落地回执.md` 风格），再走 R5 部署。
