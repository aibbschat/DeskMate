# ROUND-04 QC 判词（R4 · 端到端集成 + 部署前置）

> 判定：**PASS（无阻塞）**。独立读 git diff HEAD + 跑测试 + 核对部署脚本路径逻辑，非凭实施记录自述。
> 时间：2026-10-07 18:18。指挥员：WorkBuddy。

## 一、真实改动落点（读 git diff HEAD 实锤）
R4 **未改生产代码**。本次真正新增/改：
- `scripts/deploy_cloud_sync.sh`（+6）：`CHANNEL_MODULES` 补入 `deskmate/cognitive/store.py` + `deskmate/cognitive/extractor.py`（含漏传风险注释，解决 R1 踩过的 manager.py 漏传坑）。
- `tests/test_p1_anchor_e2e.py`（新增 2 例）：真实抽取链路闭环测试（LLM 打桩，不手工 upsert 锚）。
- extractor.py / store.py / sync_store_sqlite.py / test_profile_scope_injection.py / test_sync_store_sqlite.py 的 M 是 **R1/R2/R3 累积未提交**，R4 未再改。

## 二、独立功能验证（不沿用 Trae 自测）
| 验证点 | 结果 |
|---|---|
| R4 专项 `test_p1_anchor_e2e` | **2 passed**（真实链路：抽取即落锚 + 顺锚召回带邻座 via_anchor=True；挂锚失败主链路不中断） |
| P1 全相关套件（5 文件） | **86 passed in 0.87s**（与 Trae 自述一致，非虚报） |
| e2e 断言 ① 抽取即落锚 | memory_anchors 含 (topic,历史真实性)+(book,万历十五年) 通过 |
| e2e 断言 ② 顺锚召回 | seed=FTS 命中 stance、thinking_framework 靠同 topic 锚以 via_anchor=True+anchor_evidence="同主题：历史真实性" 被带回 通过 |
| e2e 断言 ③ 挂锚失败 fail-open | upsert_anchors 抛 RuntimeError → extract_from_text 仍返回 stances==1 无 error 通过 |

## 三、部署前置核对（R4 最关键交付，独立验）
读 `deploy_cloud_sync.sh` 两段逻辑：
- **推送段**（L199-215）：`deskmate/cognitive/store.py` → `dest_dir=$CLOUD_DESKMATE_ROOT/cognitive`（case 重算 `dirname "${f#deskmate/}"` = cognitive），含 `mkdir -p` + 清 `__pycache__`。
- **对齐核对段**（L283-298，section 7）：**与推送段用同一套 `case` 路径逻辑**，对 `deskmate/cognitive/store.py` 同样算到 `$CLOUD_DESKMATE_ROOT/cognitive`。
- **结论**：推送与核对路径自洽，补入 cognitive 两件套不会漏子目录、不会触发 `md5 不一致 → exit 1`。R5 跑 deploy 时这两个文件会正确落到 `/opt/openworker/deskmate/cognitive/` 且核对命中。
- `bash -n scripts/deploy_cloud_sync.sh` → SYNTAX OK（Trae 自述 + 读码无语法问题）。

## 四、爆炸半径
- 业务代码零改动 → 无回归风险（R3-QC 的 36 存量失败属 worldview/权限引擎模块，与认知层无关，R4 未碰）。
- 仅新增测试 + 部署清单 → 即便 R5 暂缓，本地仍可跑 P1 全绿。

## 五、遗留 / 移交 R5（非阻塞）
1. **R5 部署时必须确认**：云端 `/opt/openworker/deskmate/cognitive/` 目录经 mkdir 建好 + 重启 openworker-server 干净（PID 换 + 无 traceback）。否则云端仍跑旧码（无 memory_anchors 表 / 不顺锚）。
2. **部署后云端实证**（R5 验收项）：python 探针确认云端新 store.py 的 memory_anchors 表存在 + recall_items 顺锚召回在云端真码生效（本地同步上去的锚数据能被召回邻座）。
3. person 锚仍沉睡（需配 DESKMATE_PERSON_NAMES），P1 不强制，R5 不必动。

## 六、结论
**R4 PASS，无阻塞。** P1 全链路（R1 数据层 → R2 抽锚 → R3 召回 → R4 集成+部署前置）已在本机闭环、测试全绿、部署漏传坑已堵。R5 由萧哥走红线（commit fork/base + deploy + 云端重启实证）。
