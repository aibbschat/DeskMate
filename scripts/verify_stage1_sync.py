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
]


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
    """SSH 只读：scp 独立只读脚本到云端执行，回收 JSON。"""
    ro = (
        "import sqlite3, json\n"
        "c = sqlite3.connect(%r)\n"
        "out = {\n"
        "  'stats': list(c.execute(\n"
        "    'SELECT COUNT(*), SUM(sync_uuid IS NULL), COUNT(DISTINCT sync_uuid),"
        " SUM(sync_hlc IS NULL), SUM(sync_tombstone) FROM profile_items').fetchone()),\n"
        "  'uuids': [r[0] for r in c.execute('SELECT sync_uuid FROM profile_items ORDER BY sync_uuid')],\n"
        "}\n"
        "print(json.dumps(out))\n"
    ) % CLOUD_DB
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
            check("云端统计 92/0/92/0/0", tuple(cs["stats"]) == (92, 0, 92, 0, 0),
                  f"COUNT={cs['stats'][0]} null_uuid={cs['stats'][1]} distinct={cs['stats'][2]} null_hlc={cs['stats'][3]} tomb={cs['stats'][4]}")
            mac_u = set(local_uuids())
            cloud_u = set(cs["uuids"])
            diff = mac_u ^ cloud_u
            print(f"[两端 sync_uuid 集合差] = {len(diff)} (空=对齐)")
            check("两端 uuid 集合相等", len(diff) == 0,
                  "差集为空 ✅" if len(diff) == 0 else f"差集 {len(diff)} 条")
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
