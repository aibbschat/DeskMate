# ROUND-05 指令（R5 · 部署收口，由萧哥走红线）

> 下发：WorkBuddy（指挥员）→ 萧哥（红线持有者）。依据：P1 工单 §十；R4-QC §五 移交项。
> 性质：**部署动作，非代码改动**。本轮所有代码已在 R1-R4 落定并 QC 通过。

## 一、本机收尾（commit）
1. 工作树当前已改（R1-R4 累积）：extractor.py / store.py / sync_store_sqlite.py / test_profile_scope_injection.py / test_sync_store_sqlite.py（M）+ 新增 test_p1_anchor_recall.py / test_p1_anchor_e2e.py / deploy_cloud_sync.sh（M）。
2. `git add` 上述文件 → `git commit`（message 建议 `feat(cognitive): P1 汇流最小版——共享锚 + 顺锚召回`，属 fork/base 分支特性，不碰 origin/main）。
3. **红线**：`git push backup fork/base`，**绝不 push origin、绝不 --force、绝不 rewrite**。
4. 顺手 `git status` 确认无遗漏（`:memory:.ses` 是 sqlite 临时文件，勿 commit）。

## 二、后端云端部署（deploy_cloud_sync.sh）
1. 跑 `bash scripts/deploy_cloud_sync.sh`（脚本自检 SSH host 在 `~/.ssh/config`，默认 openworker-mirror）。
2. **关键确认**：md5 对齐摘要里必须出现 `store.py` + `extractor.py`（R4 已补入 CHANNEL_MODULES）；若缺 → 脚本会 `exit 1`，此时**回退查 R4 清单**，勿硬推。
3. 云端重启：`sudo systemctl restart openworker-server`，确认新 PID > 旧 PID、journalctl 无 traceback。

## 三、云端实证（trust-but-verify，token 只留云端）
写探针走 scp 执行（勿把 COWORKER_API_TOKEN 落到本地文件）：
- 云端 python 直跑：确认 `CognitiveStore` 新码在云端的 `memory_anchors` 表存在（SELECT COUNT 非空或建表成功）+ `recall_items` 顺锚召回在云端真码生效（构造两条同 topic 的 profile_item → recall 命中邻座）。
- 也可用 HTTP：GET `/v1/deskmate/proactive/handoff` 带 token=200（结构正常，证明云端新码在跑）。
- 判据：探针三项（表存在 / 顺锚召回 / 路由活）全 True → 部署真生效。

## 四、Mac 桌面端（本地 app）
1. 重开 OpenWorker.app 载本地新码：`pkill -f openworker-desktop` 后 `open ~/Applications/OpenWorker.app`。
2. 判新码三件套（历史惯例）：① `lsappinfo` launch time > 最新 commit 时间；② 本地 `deskmate/cognitive/__pycache__/store.cpython-*.pyc` 重编译时间新；③ 真实行为（e2e 测试本地已绿）。

## 五、验收回执
部署完成后写 `reports/P1_汇流最小版_部署验收回执_2026-10-07.md`：
- 结论先行：P1 是否真上云、云端新码是否真跑。
- 表格分行：commit hash / deploy md5 对齐项 / 云端重启 PID / 云端探针三项 / Mac 重开。
- 红线单列：未 push origin、无 --force。

## 六、P1 收官后的下一步（供萧哥决策，非 R5 动作）
- P2 选项：显式边表（价值证据浮现后落成可解释边）/ lessons 注入（memory/manager.py，限最近 3 条+永久规则）/ 反思复用 item_history（reason 空=作废）。
- 已知局限（R3-QC NOTE）：FTS 填满 top_k 时邻座被截，若要"邻座必现"改配额逻辑。
- 自主（P3）：按江河设计 §八 已拍板——自由默认、关键时刻拦必留教；主动查史料佐证可开、须用本地史料库。
