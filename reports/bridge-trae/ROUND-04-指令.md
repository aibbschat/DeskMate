# ROUND-04 指令（R4 · 端到端集成验证 + 部署前置检查）

> 下发：WorkBuddy（指挥员）→ Trae（一线）。依据：P1 工单 R4/R5 节；R3-QC §六 移交项。
> 范围：集成验证 + 部署清单核对。**不含生产部署（部署归 R5，由萧哥走红线）**。

## 一、目标
1. 证明 R2（抽锚）+ R3（顺锚召回）在**真实抽取链路**闭环——不是单测里手工 upsert 锚，而是经 `extractor.extract_from_text` 抽出的 stance 自动带锚，再被 recall 顺锚带回。
2. 确认 R5 部署不会因漏传 `store.py` 而失效（R1 踩过 `manager.py` 漏传，云端旧码停在 Sep 30 的坑）。

## 二、落点
- 集成验证脚本（新增 `tests/test_p1_anchor_e2e.py` 或扩 `test_p1_anchor_recall.py`）。
- `scripts/deploy_cloud_sync.sh` 的 `CHANNEL_MODULES` 清单（**只读核对 + 必要时 edit**，属 R5 部署准备）。

## 三、实现规格（必须照做）

### 3.1 端到端集成测试（核心）
- 构造真实文本（如一篇含「史料要交叉比对」「万历十五年」的短文），调 `CognitiveExtractor.extract_from_text(text, ...)` 抽 stance，**断言抽出的 stance 自动在 `memory_anchors` 落锚**（topic=史料交叉比对相关 / book=万历十五年）。
- 再抽第二条同主题短文（不同内容但共享 topic 锚），调 `recall_items(某词)` 命中第一条时，**断言第二条被顺锚带回**（带 `via_anchor=True`）。
- 这证明：R2 的 `_attach_anchors` 挂线 + R3 的 `_recall_anchor_neighbors` 在真实链路贯通，而非单测手工造数据。
- fail-open 同 R3：抽取链路异常不得因锚失败而中断。

### 3.2 部署清单核对（R5 前置，必做）
- 读 `scripts/deploy_cloud_sync.sh`，确认 `CHANNEL_MODULES` 含 `deskmate/cognitive/store.py`（R1/R3 改了它）。
- 若缺失 → **编辑补入**（参照 R1 补 `manager.py` 的写法，加一句注释说明漏传风险）。
- **不要执行部署**（部署是萧哥走红线的动作，R5 由 WorkBuddy 指挥触发）。

### 3.3 验收判据确认清单（交付时附）
- [ ] 抽取即落锚（R2 真实链路）
- [ ] 顺锚召回在真实链路命中邻座（R3 真实链路）
- [ ] `DESKMATE_ANCHOR_RECALL=0` 即时回退纯 FTS（R3 验证）
- [ ] 真·表损坏 fail-open（R3 验证）
- [ ] `deploy_cloud_sync.sh` CHANNEL_MODULES 含 store.py（R5 不漏传）

## 四、红线（爆炸半径锁定）
- 不碰 `extractor.extract_from_text` / `recall_items` 的已锁签名与返回结构（R2/R3 已锁）。
- 不引显式边表（P2+）。
- 不碰 `memory/manager.py` 注入链（lessons 是 P2+）。
- **R4 不 commit、不 deploy**（commit 归 R5 收口，deploy 由萧哥走红线）。

## 五、交付
R4 完成后写 `reports/bridge-trae/ROUND-04-实施记录.md`（含端到端测试输出 + 部署清单核对结果 + 验收判据勾选），交 WorkBuddy QC。
