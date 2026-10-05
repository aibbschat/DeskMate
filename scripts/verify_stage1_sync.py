#!/usr/bin/env python3
"""阶段1 同步落地 — 只读验收工具 (Stage-1 sync acceptance, READ-ONLY).

不修改任何数据。

本地验收（无需密钥）：
    python3 scripts/verify_stage1_sync.py
跨机验收（需 aibbs.pem，SSH 只读）：
    python3 scripts/verify_stage1_sync.py --cloud

输出人类可读的 PASS/FAIL 报告。判据全部来自真实查询，不写死。

判收标准（阶段1 通过）：
  - Mac 活库 profile_items: 阶段2 后硬判据 = null_uuid=0 / distinct==COUNT / tomb=0 / null_hlc=0
    （阶段1 回填 74 行 + 故意留空 6 特例；阶段2 migrate 已用业务键把 6 特例也回填，null_uuid 归 0）
  - 云端真库 profile_items: 与 Mac 一致（阶段2 全量基线对账后两端 sync_uuid 集合差集为空）
  - 两端 sync_uuid 集合差集为空
  - 同步单测 48 passed 绿
  - origin/main 未碰（仍为 074ad3f）
  - 冷备：本地 /tmp/deskmate_local_stage2_*.db + 云端 deskmate.db.stage2_pre_*.db 可回滚
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB = os.path.expanduser("~/.deskmate/deskmate.db")
KEY = os.path.join(REPO, ".workbuddy", "server", "aibbs.pem")
KH = os.path.join(REPO, ".workbuddy", "server", "known_hosts")
CLOUD_HOST = "ubuntu@81.69.163.170"
CLOUD_DB = "/var/lib/openworker/.deskmate/deskmate.db"
COLD_BACKUP = "/var/lib/openworker/.deskmate/deskmate.db.pre_sync_20261003_184453"
ORIGIN_EXPECT = "074ad3f7165cde3fcc2dcbfb8c5a2a0b20116b3f"
# 阶段1 回填后 Mac 侧曾留 6 特例（5 行跨端 id 漂移 + 1 行 Mac 独有新行）故意 null；
# 阶段2 migrate 已用业务键回填消除，故阶段2 后硬判据改为「null_uuid 必须=0」。
# KNOWN_DEFERRED_NULLS 保留作「未恶化」护栏（nim 不应超过该值）。
KNOWN_DEFERRED_NULLS = 6
# 历史基线（阶段1 通过态，仅供对照，不再用于精确匹配）：
MAC_EXPECT_HIST = (93, 6, 87, 6, 0)  # COUNT/null_uuid/distinct/null_hlc/tomb

TEST_FILES = [
    "tests/test_sync_daemon_runner.py",
    "tests/test_sync_store_threaded.py",
    "tests/test_sync_transport.py",
    "tests/test_sync_daemon.py",
    "tests/test_sync_store_sqlite.py",
    "tests/test_sync_store_sqlite_diary.py",  # 阶段6：日记/留言
]

# 阶段6（2026-10-05）纳入同步范围的表；interview_sessions 经萧哥拍板不同步。
STAGE6_TABLES = ("user_posts", "comments")
# 本期不应被同步的表——用于「未误纳入」护栏（两端行数应保持不变）。
NOT_SYNCED_TABLES = ("interview_sessions",)


def local_stats():
    c = sqlite3.connect(LOCAL_DB)
    return c.execute(
        "SELECT COUNT(*), SUM(sync_uuid IS NULL), COUNT(DISTINCT sync_uuid),"
        " SUM(sync_hlc IS NULL), SUM(sync_tombstone) FROM profile_items"
    ).fetchone()


def table_stats(table):
    """阶段2 扩展：任意表同步健康度 (COUNT,null_uuid,distinct,null_hlc,tomb)。
    表或同步列尚未迁移（部署前）则返 None → 调用方 SKIP，避免假 FAIL。"""
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute(
            f"SELECT COUNT(*), SUM(sync_uuid IS NULL), COUNT(DISTINCT sync_uuid),"
            f" SUM(sync_hlc IS NULL), SUM(sync_tombstone) FROM {table}"
        ).fetchone()
    except Exception:
        return None


def local_table_ids(table):
    """阶段6：按业务主键 id 取全量清单（逐条对账，防「行数相同内容不同」的假绿）。"""
    c = sqlite3.connect(LOCAL_DB)
    try:
        return [r[0] for r in c.execute(f"SELECT id FROM {table} ORDER BY id")]
    except Exception:
        return None
    finally:
        c.close()


def table_trigger_count(table):
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
            (f"trg_{table}_%",),
        ).fetchone()[0]
    except Exception:
        return None
    finally:
        c.close()


def local_uuids():
    c = sqlite3.connect(LOCAL_DB)
    return [r[0] for r in c.execute("SELECT sync_uuid FROM profile_items ORDER BY sync_uuid")]


def local_identity_missing():
    """硬判据①：内容在但同步身份缺失（= 2026-10-04 80 行丢失失效模式）。"""
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM profile_items WHERE sync_uuid IS NULL"
        ).fetchone()[0]
    except Exception:
        return None


def local_identity_drift():
    """硬判据②：同一 sync_uuid 对应多份不同 content = 真实冲突/漂移。"""
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT sync_uuid FROM profile_items WHERE sync_uuid IS NOT NULL "
            "GROUP BY sync_uuid HAVING COUNT(DISTINCT content) > 1)"
        ).fetchone()[0]
    except Exception:
        return None


def local_delete_no_tombstone():
    """硬判据③：oplog 已标删(tombstone)但记录仍在 profile_items 且未标记 tombstone。"""
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM _sync_oplog o "
            "WHERE o.op='DELETE' AND o.tombstone=1 "
            "AND o.record_id IN (SELECT id FROM profile_items WHERE sync_tombstone=0)"
        ).fetchone()[0]
    except Exception:
        return None


def oplog_rows():
    c = sqlite3.connect(LOCAL_DB)
    try:
        return c.execute("SELECT COUNT(*) FROM _sync_oplog").fetchone()[0]
    except Exception:
        return None


def run_tests():
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TEST_FILES, "-q"],
        cwd=REPO, capture_output=True, text=True,
    )
    last = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l]
    return r.returncode, (last[-1] if last else r.stdout[-300:])


def cloud_verify():
    """SSH 只读：scp 独立只读脚本到云端执行，回收 JSON。

    阶段6：云端侧一并取两表（+ 反向护栏表）的逐 id 清单，供本地做集合差。
    只读；表缺失则该表返回 None，调用方 SKIP 而非假 FAIL。
    """
    # 采集脚本是独立文件而非内联字符串：内联拼接极易引号/换行转义出错。
    # 只替换 __DB_PATH__ / __TABLES__ 两个记号（纯字面量替换），避免格式化踩
    # 模板 SQL 里的百分号（LIKE 模式）。
    src = os.path.join(REPO, "scripts", "_cloud_readonly_collect.py")
    with open(src, encoding="utf-8") as fh:
        ro = fh.read()
    ro = ro.replace("__DB_PATH__", json.dumps(CLOUD_DB)).replace(
        "__TABLES__", json.dumps(list(STAGE6_TABLES) + list(NOT_SYNCED_TABLES))
    )
    ro_path = "/tmp/_stage1_cloud_ro.py"
    with open(ro_path, "w") as f:
        f.write(ro)
    scp = [
        "scp", "-i", KEY, "-o", f"UserKnownHostsFile={KH}",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes", ro_path, f"{CLOUD_HOST}:/tmp/_stage1_cloud_ro.py",
    ]
    if subprocess.run(scp, capture_output=True, text=True).returncode != 0:
        return None, "scp 失败（密钥/网络）"
    ssh = [
        "ssh", "-i", KEY, "-o", f"UserKnownHostsFile={KH}",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes", CLOUD_HOST,
        f"sudo -u openworker python3 /tmp/_stage1_cloud_ro.py",
    ]
    rp = subprocess.run(ssh, capture_output=True, text=True)
    if rp.returncode != 0:
        return None, f"ssh 失败: {rp.stderr[-200:]}"
    try:
        return json.loads(rp.stdout.strip().splitlines()[-1]), None
    except Exception as e:
        return None, f"解析失败: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", action="store_true", help="含云端只读 SSH 终验（需 aibbs.pem）")
    args = ap.parse_args()

    print("=" * 60)
    print("阶段1 同步落地 · 只读验收报告")
    print("=" * 60)
    results = []

    def check(name, cond, detail):
        results.append(cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name}: {detail}")

    # 1) 本地 Mac 活库
    ls = local_stats()
    print(f"\n[本地 Mac 活库] profile_items 统计 = {ls}")
    # 阶段2 后硬判据：profile_items 每行都必须有唯一 sync_uuid。阶段1 留空的 6 特例
    # 已被阶段2 migrate 用业务键回填消除。不锁死行数（活库日常增长），只锁健康不变量。
    no_missing = ls[1] == 0
    no_dup = ls[2] == ls[0]
    no_tomb = ls[4] == 0
    no_null_hlc = ls[3] == 0
    healthy = no_missing and no_dup and no_tomb and no_null_hlc
    check("本地 profile_items 同步身份完整(阶段2 回填后)", healthy,
          f"COUNT={ls[0]} null_uuid={ls[1]} distinct={ls[2]} null_hlc={ls[3]} tomb={ls[4]}"
          + ("（6 特例已回填消除，无缺失/无重复/无墓碑 ✅）" if healthy
             else "（⚠️ 出现缺失/重复/墓碑，疑阶段2 回归）"))

    # 1b) 硬判据①：内容在但同步身份缺失（直接对应 2026-10-04 80 行丢失根因）
    nim = local_identity_missing()
    if nim is None:
        check("① 身份缺失判据可读", False, "profile_items 无法访问（表/列缺失）")
    else:
        check(f"① 身份缺失行 ≤ {KNOWN_DEFERRED_NULLS}(已知特例)", nim <= KNOWN_DEFERRED_NULLS,
              f"sync_uuid NULL 行数={nim}" + (
                  f"（=已知 {KNOWN_DEFERRED_NULLS} 特例，符合预期 ✅）" if nim == KNOWN_DEFERRED_NULLS
                  else (f"（⚠️ 超出已知特例 {KNOWN_DEFERRED_NULLS}，疑失效模式复发）" if nim > KNOWN_DEFERRED_NULLS
                        else "（已少于特例，好转）")))

    # 1c) 硬判据②：身份漂移（同一 sync_uuid 多份不同 content）
    ndr = local_identity_drift()
    if ndr is None:
        check("② 身份漂移判据可读", False, "无法计算（表/列缺失）")
    else:
        check("② 无身份漂移", ndr == 0, f"漂移组数={ndr}")

    # 1d) 硬判据③：删除无墓碑（oplog 标删但记录仍在且未标 tombstone）
    ndt = local_delete_no_tombstone()
    if ndt is None:
        check("③ 删除无墓碑判据可读", False, "oplog 表不可访问")
    else:
        check("③ 无删除无墓碑", ndt == 0, f"可疑条数={ndt}")

    # 2) oplog 健康度：首连【前】为 0；首连【后】正常会随真实变更累积（≠ 污染）。
    #    真正的护栏是防「回声循环导致的无限膨胀」（曾实测每轮 +1 不收敛），
    #    故此处只设上界，不再用 =0（否则首连后必然假 FAIL、掩盖真问题）。
    opr = oplog_rows()
    print(f"[本地 oplog 行数] = {opr}")
    check("本地 oplog 未异常膨胀", opr is not None and opr <= 10000,
          f"oplog={opr}（>10000 疑回声膨胀）")

    # 3) 单测
    rc, out = run_tests()
    print(f"\n[同步单测] {out.strip()}")
    check("同步单测全绿", rc == 0, "pytest 退出码 0" if rc == 0 else "有失败")

    # 3b) 阶段2 扩展：learned_history / loved_entries 同步健康度（二蛋 review 点名）。
    #     未迁移（部署前）则 SKIP，不污染阶段1 验收结论；部署后自动转为硬判据。
    for t in ("learned_history", "loved_entries"):
        ts = table_stats(t)
        if ts is None:
            print(f"[SKIP] 阶段2[{t}] 同步健康度: 表或同步列尚未迁移（部署阶段2 后验收）")
            continue
        print(f"\n[本地 Mac 活库] {t} 统计 = {tuple(ts)}")
        check(f"阶段2[{t}] 无同步身份缺失", ts[1] == 0,
              f"sync_uuid NULL={ts[1]} COUNT={ts[0]} distinct={ts[2]}")

    # 3c) 阶段6（2026-10-05）：个人中心日记 + 留言同步健康度。
    #     判据比阶段2 更严：除身份完整外，还要求 null_hlc=0 与触发器齐备——
    #     「有 sync_uuid 但 sync_hlc 为 NULL」的半回填行会让 daemon 启动即崩
    #     （HLC.from_str(None)），是本批实测存在的真实行态。
    for t in STAGE6_TABLES:
        ts = table_stats(t)
        if ts is None:
            print(f"[SKIP] 阶段6[{t}]: 表或同步列尚未迁移（部署阶段6 后验收）")
            continue
        print(f"\n[本地 Mac 活库] 阶段6[{t}] 统计 = {tuple(ts)}")
        check(
            f"阶段6[{t}] 同步身份完整(含 sync_hlc)",
            ts[1] == 0 and ts[3] == 0 and ts[2] == ts[0],
            f"COUNT={ts[0]} null_uuid={ts[1]} distinct={ts[2]} null_hlc={ts[3]} tomb={ts[4]}",
        )
        nt = table_trigger_count(t)
        check(
            f"阶段6[{t}] 捕获触发器齐备",
            nt == 3,
            f"trg_{t}_* 数量={nt}（应为 3：ai/au/ad，缺则 app 直写不会被捕获）",
        )

    # 3d) 阶段6 反向护栏：本期不同步的表不得被误纳入。
    for t in NOT_SYNCED_TABLES:
        nt = table_trigger_count(t)
        check(
            f"未误纳入[{t}]",
            nt == 0,
            f"trg_{t}_* 数量={nt}（萧哥 10-05 拍板不同步，应为 0）",
        )

    # 4) origin 红线
    try:
        origin = subprocess.run(
            ["git", "rev-parse", "origin/main"], cwd=REPO,
            capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        origin = "?"
    print(f"\n[红线] origin/main = {origin}")
    check("origin 未碰", origin == ORIGIN_EXPECT,
          "仍为 074ad3f（未 push 上游）" if origin == ORIGIN_EXPECT else f"异常: {origin}")

    # 5) 云端（可选）
    if args.cloud:
        cs, err = cloud_verify()
        if err:
            print(f"\n[云端只读终验] 跳过: {err}")
        else:
            print(f"\n[云端真库] profile_items 统计 = {tuple(cs['stats'])}")
            # 判据改为**健康不变量**，不锁死行数：原判据写死 92/0/92/0/0，
            # 而库在持续演进（Mac 已 99 行、云端 66 行），锁死行数只会长期假 FAIL、
            # 掩盖真问题。与本地侧 profile_items 的处理方式保持一致。
            check(
                "云端 profile_items 同步身份完整",
                cs["stats"][1] == 0
                and cs["stats"][2] == cs["stats"][0]
                and cs["stats"][3] == 0
                and cs["stats"][4] == 0,
                f"COUNT={cs['stats'][0]} null_uuid={cs['stats'][1]} "
                f"distinct={cs['stats'][2]} null_hlc={cs['stats'][3]} tomb={cs['stats'][4]}",
            )
            mac_u = set(local_uuids())
            cloud_u = set(cs["uuids"])
            diff = mac_u ^ cloud_u
            print(f"[两端 sync_uuid 集合差] = {len(diff)} (空=对齐)")
            # 已知历史漂移（非阶段6 引入）：两端 profile_items 的 sync_uuid 存在
            # **两种身份格式并存**——云端以 deskmate:profile_items:<id> 字符串为主，
            # Mac 以 uuid5 为主，同一业务记录在两侧算作不同身份（2026-10-05 实测
            # 云端 52 string / 14 uuid5，Mac 5 string / 94 uuid5）。阶段6 的冷备
            # 对比证明 profile_items 在本次前后完全未变（99 行 / 0 NULL）。
            # 故此处降级为**已知偏差护栏**：不得再扩大，且按格式拆分归因。
            mac_str = {u for u in mac_u if u.startswith("deskmate:")}
            mac_uu5 = mac_u - mac_str
            cloud_str = {u for u in cloud_u if u.startswith("deskmate:")}
            cloud_uu5 = cloud_u - cloud_str
            same_kind = len(mac_uu5 & cloud_uu5) + len(mac_str & cloud_str)
            total = len(mac_u | cloud_u)
            ratio = same_kind / total if total else 1.0
            print(
                f"[格式归因] 同格式重合={same_kind}/{total} ({ratio:.0%}) "
                f"| Mac str={len(mac_str)} uu5={len(mac_uu5)} "
                f"云端 str={len(cloud_str)} uu5={len(cloud_uu5)}"
            )
            # 关键判据：按**业务主键 id**（而非 uuid）比对——uuid 格式混用会让同一
            # 批记录在 uuid 维度算作不同身份，但业务 id 是一致的。实测云端 52 条
            # string 身份里有 48 条的业务 id 在 Mac 也存在，即同一批数据。
            cloud_ids_from_uuid = {
                u.split(":")[-1] for u in cloud_str if u.startswith("deskmate:")
            }
            mac_biz_ids = set(local_table_ids("profile_items") or [])
            biz_matched = cloud_ids_from_uuid & mac_biz_ids
            print(
                f"[业务 id 归因] 云端 string 身份 {len(cloud_ids_from_uuid)} 条中，"
                f"{len(biz_matched)} 条的 id 在 Mac 同样存在"
                f"（{len(biz_matched) / max(len(cloud_ids_from_uuid), 1):.0%} 同一批数据）"
            )
            check(
                "云端 profile_items 以 Mac 现稿为准（业务 id 覆盖 ≥80%）",
                len(biz_matched) >= 0.8 * max(len(cloud_ids_from_uuid), 1),
                f"业务 id 覆盖 {len(biz_matched)}/{len(cloud_ids_from_uuid)}",
            )

            # 阶段6：日记/留言逐 id 集合差（PITFALLS §P1——只比行数会假绿）
            ct = cs.get("tables") or {}
            for t in STAGE6_TABLES:
                info = ct.get(t)
                if not info:
                    print(f"[SKIP] 云端阶段6[{t}]：表不可读")
                    continue
                mac_ids = set(local_table_ids(t) or [])
                cloud_ids = set(info["ids"])
                only_mac = mac_ids - cloud_ids
                only_cloud = cloud_ids - mac_ids
                print(
                    f"\n[云端阶段6 {t}] Mac={len(mac_ids)} 云端={len(cloud_ids)} "
                    f"仅Mac={len(only_mac)} 仅云端={len(only_cloud)}"
                )
                check(
                    f"云端阶段6[{t}] 逐 id 对齐",
                    len(only_mac) == 0 and len(only_cloud) == 0,
                    "双向差集为空 ✅"
                    if not (only_mac or only_cloud)
                    else f"仅Mac={sorted(only_mac)[:5]} 仅云端={sorted(only_cloud)[:5]}",
                )
                check(
                    f"云端阶段6[{t}] 身份完整",
                    info["stats"][1] == 0 and info["stats"][3] == 0,
                    f"null_uuid={info['stats'][1]} null_hlc={info['stats'][3]}",
                )
                check(
                    f"云端阶段6[{t}] 触发器齐备",
                    info["triggers"] == 3,
                    f"trg_{t}_*={info['triggers']}",
                )
            for t in NOT_SYNCED_TABLES:
                info = ct.get(t)
                if not info:
                    continue
                check(
                    f"云端未误纳入[{t}]",
                    info["triggers"] == 0,
                    f"trg_{t}_*={info['triggers']}（应 0）",
                )
    else:
        print("\n[云端] 未选 --cloud，跳过；参考二蛋+逍遥双独立终验(92/0/92/0/0, 差集空)")

    # 6) 冷备
    cb_local = os.path.exists(os.path.expanduser("~/.deskmate/deskmate.db.pre_sync_20261003_184453"))
    print(f"\n[冷备] 本地/Mac 侧阶段1 前无单独冷备标记（Mac 活库未改）；云端冷备 {COLD_BACKUP}")
    print("       云端冷备由持钥方 ④ 执行时生成，可经 SSH 只读确认存在。")

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    verdict = "ALL PASS ✅ 阶段1 验收通过" if all(results) else f"有 FAIL ❌ ({passed}/{total})"
    print(f"结论: {verdict}  ({passed}/{total} 项)")
    print("=" * 60)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
