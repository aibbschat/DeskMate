# 小桔逻辑脑多副本同步 · 阶段 2 生产部署方案

> 状态：代码完成、**60 个同步测试全绿（含生产真 DDL 对齐的 P0 回归 + 二蛋复验追出的触发器补侧表回归）**；**已于 2026-10-04 经萧哥批准完成云端实际部署**（冷备两端→commit/push backup→停两端 daemon→推码+归零游标→重启→verify 9/9 全绿；详 `.workbuddy/memory/2026-10-04.md`）。**唯一残留：Mac 端 daemon 常驻需萧哥在本机终端 `launchctl load` 接管**（Bash 沙箱管不了 GUI launchd，部署时后台实例已主动停掉释放 8765 端口）。
> 红线提醒：origin 绝不 push；只推 backup/fork/base 分支；改动目前全在本地工作树，待审查。

## 1. 阶段 2 范围
- 同步表从 `profile_items` 扩大到全部 B 类表：`profile_items` + `learned_history` + `loved_entries`。
- 捕获方式：DB 触发器（零侵入，覆盖 app 直写 LearnedStore 等绕过 store 的路径）。
- 首连基线：两端游标归零时逐表 `reconcile_baseline_oplog` 全量对账，随后纯 oplog 增量。
- oplog GC / 墓碑业务过滤：本期**不做**（沿用阶段 1 口径）。

## 2. 本轮修复的关键根因（trust-but-verify 实测定位）
1. **int 自增主键导致 sync_uuid 碰撞**：`learned_history`/`loved_entries` 用 `int id` 派生 sync_uuid，
   双端各自从 1 自增 → `fpA1(id=1)` 与 `fpB1(id=1)` 同 sync_uuid，基线对账误当冲突覆盖。
   → 改用业务唯一键 `sync_key` 派生：`learned_history.fp` / `loved_entries.moment_id`；`profile_items` 用 `id`。
2. **触发器补 sync_uuid 用随机值**：`randomblob(16)` 使跨端新建行 sync_uuid 不一致。
   → 改为确定性字符串 `deskmate:{table}:{sync_key值}`，与 migrate 回填格式统一。
3. **text-pk 表业务 id 被 sync_uuid 覆盖**：`profile_items.id` 是业务主键，upsert 把它写成 record_id（sync_uuid）。
   → upsert 的 `id` 列改取 `data['id']`（业务主键），sync_uuid 列才取 record_id。
4. **引擎写入经触发器回声**：`upsert/delete` 已统一 `suppress_triggers=True`，避免同 HLC 重复 op 致墓碑误复活。
5. **多表路由 bug**：`SyncEngine._apply_one_to` 误用 `self.store`（多表模式下为 None）→ 改为路由到的 store。
6. **触发器在 SQLite 3.50 下 `NEW.col=expr` 赋值失效**、`ALTER ... DEFAULT(random)` 被拒 → 改为 AFTER 触发器内 suppress 包裹的 UPDATE 补列。

## 2b. 二蛋 review 追出的 P0（生产必崩 + 永久卡死，已修）
> 二蛋用生产真 DDL 复现：45 测试全绿却测不出——**测试库 schema 比生产宽松（省略 NOT NULL）**。

7. **墓碑占位行撞 NOT NULL，生产一删就崩 + 同步永久卡死**（P0，阻断上线）：
   `delete()` 对「本端从未见过的删除墓碑」插只填 sync 三列、不填业务列的占位行；生产库
   `learned_history.ts/fact/fp`、`loved_entries.content/day/created_at`、`profile_items.dimension` 皆 `NOT NULL`
   → `IntegrityError`。且无 per-op try：一条 op 抛异常 → 整批中断 → `tick()` 在推游标前就炸 →
   游标原地不动 → 下一轮重拉同批再炸，无限循环。
   → **墓碑另表化 + 物理删除**：新增独立 `_sync_tombstones(table_name, record_id, hlc)`；`delete()`
   对业务表**物理删除**（无论是否见过），墓碑只落侧表（同名 record_id 只留最大 hlc）。业务表零污染
   → 不再撞 NOT NULL、app 直读不泄漏幽灵行、`apply_remote` 不再卡死。LWW 防复活改由 `_apply_one_to`
   查 `_sync_tombstones`（`has_tombstone`）实现。`reconcile_baseline_oplog` 对本地独有/胜出的墓碑改发
   `delete` op（非空数据 upsert）。`items()`/`get()` 墓碑感知（从侧表补回 tombstone 记录），使首连基线能
   传播删除。
8. **测试库 schema 必须对齐生产**：新增 `tests/fixtures/real_schema.sql`（逐字取自 `~/.deskmate/deskmate.db`
   三表真 DDL，含 NOT NULL），新增 `test_p0_unseen_delete_no_crash_with_real_schema` /
   `test_p0_real_schema_full_converge_with_unseen_delete` 等，确保在真实约束下删除不崩、不卡死、可收敛。

## 2c. 二蛋《P0 复验》追出的新缺口（同源「删除不防复活」类，已修）

9. **app 直写删除（AFTER DELETE 触发器路径）不写墓碑侧表** → 迟到旧 upsert 可复活（非阻断，但正是 P0 要根治的那类）：
   引擎路径 `store.delete()` 在 Python 内直写 `_sync_tombstones`，但 `trg_{t}_ad` 触发器只写 oplog、不写侧表
   → `has_tombstone()` 查不到 → 对端重放更旧副本时本端短暂复活（最终仍一致，低频）。
   → **触发器路径补齐**：`trg_{t}_ad` 在 oplog INSERT 后补一条侧表 INSERT
   （`SELECT ... WHERE OLD.sync_uuid IS NOT NULL` + `ON CONFLICT DO UPDATE WHERE excluded.hlc > {侧表}.hlc`），
   与引擎路径同口径；两路删除都落侧表，`has_tombstone` 判定统一。
   → **生产可落地**：`migrate()` 建触发器前先 `DROP TRIGGER IF EXISTS trg_{t}_ai/au/ad`
   （`sync_store_sqlite.py:307-315`），重跑 migrate 即生效，活库/云端不会卡旧触发器。
   → **回归测试**：`test_app_direct_delete_writes_tombstone_side_table`（生产真 DDL 建库→绕过 store 直删→
   断言侧表有墓碑 + `has_tombstone` 对远旧 hlc 拦下、对远新 hlc 放行）。
   → 二蛋次要提醒（`live_items()` 不过滤 `sync_tombstone`）：经核无碍，墓碑语义只在侧表+业务行缺失体现，
   基线删传播靠 `get()/items()` 墓碑感知 + `reconcile_baseline_oplog` 发 `delete` op（已测 B 端归零），不改动。

## 3. 改动文件
- `deskmate/memory/sync_store_sqlite.py`：`SYNC_TABLES` 加 `sync_key`/`sync_uuid_kind`；`migrate` 按 sync_key 派生；
  触发器统一写确定性 sync_uuid；`upsert`/`delete` 永远 suppress；text-pk upsert 取 `data['id']`；多表 migrate 跳过不存在的表；
  **新增 `_sync_tombstones` 侧表；`delete()` 物理删除 + 侧表墓碑；`has_tombstone` 方法；`get()`/`items()` 墓碑感知**。
- `deskmate/memory/sync_daemon.py`：多表引擎路由（`apply_remote`/`_apply_one_to`，含侧表防复活守卫）；新增 `reconcile_baseline_oplog`（墓碑发 delete op）。
- `deskmate/memory/sync_transport.py`：`/snapshot` 端点（改走 `live_items()`，与基线口径对齐）+ `pull_snapshot`。
- `scripts/sync_daemon_runner.py`：多表迁移 + 首连基线触发。
- `scripts/verify_stage1_sync.py`：扩展阶段2 表（learned_history/loved_entries）同步健康度检查（未迁移则 SKIP，不污染阶段1 验收）。
- `tests/test_sync_*.py`：扩展多表/触发器/基线/回符合收敛测试 + **生产真 DDL 对齐的 P0 回归测试**。
- `tests/fixtures/real_schema.sql`：生产真 DDL 快照（新增）。

## 4. 部署步骤（待批准）
1. **冷备两端库**（铁律，先备再动）：
   - 本地：`sqlite3 ~/.deskmate/deskmate.db ".backup '/tmp/deskmate_local_$(date +%Y%m%d_%H%M%S).db'"`
   - 云端（SSH 到 `81.69.163.170`，路径待萧哥确认）：同法 `.backup`。
2. **本地迁移**（幂等、可重跑）：
   `python -c "import sqlite3; from deskmate.memory.sync_store_sqlite import migrate; migrate(sqlite3.connect('~/.deskmate/deskmate.db'), 'mac')"`
   → 生成 sync_uuid / sync_hlc / sync_tombstone 三列 + 三表触发器。
3. **云端同迁移**（同样命令，node='cloud'）。
4. **首连基线**：runner 以默认（全表）启动，`pull_after_seq==0 and push_after_seq==0` 时自动跑
   `reconcile_baseline_oplog` 逐表全量对账，再切纯增量 tick。
5. **常驻 daemon**：Mac ↔ 云端双向 tick（SSH 反隧道 + resident daemon，沿用阶段 1 形态）。

## 5. 验收口径
- 两端 `learned_history` 的 `fp` 集合一致、`loved_entries` 的 `moment_id` 集合一致、`profile_items` 的 `id` 集合一致。
- 一端新建→对端出现；一端删除（墓碑）→对端不复活；离线双写→重连后 LWW 收敛、无重复 op。
- **新增硬门禁**：`tests/fixtures/real_schema.sql` 对齐生产真 DDL 的 P0 测试必须全绿（删除不崩、不卡死、可收敛）；
  每次改动后用 `scripts/verify_stage1_sync.py` 复核（阶段2 表未迁移时自动 SKIP，不假绿）。

## 6. 部署状态（2026-10-04 已执行）
- 云端地址/deskmate.db 路径：确认 `ubuntu@81.69.163.170:/var/lib/openworker/.deskmate/deskmate.db`（WAL，属主 openworker，`/var/lib/openworker` 700，ubuntu 须 `sudo -u openworker`）。
- 已推 `backup/fork/base` 分支：`96f4ac3`（阶段2 落地+P0 修复）、`22115ab`（验收脚本基线修正）；**origin 未碰**。
- 两端冷备已生成：本地 `/tmp/deskmate_local_stage2_20261004_134150.db`；云端 `deskmate.db.stage2_pre_20261004_134221`。
- 三表 sync_uuid 集合两端严格一致，本地 `verify_stage1_sync.py` → **ALL PASS 9/9**。
- **残留（待萧哥本机操作）**：Mac 端 daemon 目前未由 launchd 托管。请在本机终端执行：
  `launchctl load ~/Library/LaunchAgents/com.xiaosheng.xiaoju.sync.plist`
  （若报已加载先 `launchctl unload` 再 load；沙箱内无法管理 GUI launchd，故部署时后台实例已停，避免双实例抢 8765 端口）。
- SQLite 触发器路径仅覆盖 B 类表；C 类（不同步）与 A 类（Mac-only）不受影响；神圣稿件 `.deskmate/` 圣经子目录不在同步范围（不变）。

## 7. 已知 asymmetry（非缺陷，已二蛋复验确认）

- **`_sync_tombstones` 两端不对称**：云端 1 行 `(profile_items, e2e-probe-20261003-2007, ...mac)`、Mac 侧表为空（0 行）。
- **来源**：游标归零后两端重放历史 oplog 时，云端 apply 了昨天 e2e 测试 Mac 侧删 `e2e-probe-20261003-2007` 的 delete op → 写侧表；Mac 因 `origin=mac` 回声防护跳过该 op，且阶段 1 时代该删除早于侧表存在。故仅在云端留痕。
- **影响评估**：该探针在两端三张业务表里均不存在（已查 `profile_items/learned_history/loved_entries` 三表 has probe? = 0/0/0）；复活不变量成立（`has_tombstone` 会拦远旧 upsert）；**当前无业务风险，不动它**。
- **复现条件**：未来若重置游标全量重放 oplog，此不对称会复现，但同样只影响这条探针记录。
- 另：`e2e-probe-cloud-20261003-2011` 这条 `sync_uuid` 直等于 id（无 `deskmate:` 前缀），是昨天 e2e 直插残留，两端一致、正常同步，非缺陷，不动。
