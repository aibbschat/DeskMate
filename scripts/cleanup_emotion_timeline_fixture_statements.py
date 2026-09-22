#!/usr/bin/env python3
"""清理情感时间线里由**测试直跑**留下的占位字面量（默认 dry-run）。

背景（2026-09-22 定位）
-----------------------
``deskmate/cognitive/answer_interview`` 末尾会把访谈回答回流当日心情：

    EmotionTimeline().record_statement(None, response)
    # 走 deskmate.paths.deskmate_base_dir() 读 DESKMATE_DIR

而 ``deskmate/cognitive/test_interview.py`` 的自带隔离 ``_hermetic()`` 只管
**认知层**（把 CognitiveStore / get_profile_manager / get_extractor 指到临时库），
**没有覆盖情感层**。于是：

- 走 pytest  → conftest 设了 DESKMATE_DIR → 隔离成立，不脏
- **直跑** ``python deskmate/cognitive/test_interview.py``（该文件末尾有
  ``unittest.main()``）→ conftest 不参与，DESKMATE_DIR 为空 → 情感层写**真实库**
  ⇒ ``json_blobs.emotion_timeline`` 的 statements 里混进测试字面量
  「我的回答」「访谈回答内容」。

指纹（很好认）：**情感层脏、``interview_qa`` 干净** —— 因为前者未被隔离、
后者走被隔离的 CognitiveStore。真实库 09-22 实测正是这个不对称。

闭环验证（克隆库，不动真实库）
------------------------------
1. 克隆库清掉字面量 → 0 条
2. 用修复前版本直跑 → 两条**重新出现**
3. 重新清白 + 用修复后版本直跑 → 仍 0 条

用法
----
    python scripts/cleanup_emotion_timeline_fixture_statements.py            # dry-run
    python scripts/cleanup_emotion_timeline_fixture_statements.py --apply    # 执行（先备份）

备份落在 ``/tmp/deskmate_backup_<ts>.db``（``sqlite3 .backup``，非 cp）。
幂等：反复跑结果一致；只删完全等于白名单的整条 statements，不做模糊匹配。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskmate.paths import deskmate_db_path  # noqa: E402

# 只删「完全等于」这些整条文本的 statement —— 它们是测试文件的字面量，
# 不可能是他的真实表达。宁少删不多删：模糊匹配会误伤真话。
FIXTURE_STATEMENTS = frozenset({
    "我的回答",
    "访谈回答内容",
})

KEY = "emotion_timeline"


def backup(db: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = Path(f"/tmp/deskmate_backup_{ts}.db")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只报告）")
    ap.add_argument("--db", default=None, help="指定库路径（默认统一库）")
    args = ap.parse_args()

    db = Path(args.db) if args.db else deskmate_db_path()
    if not db.exists():
        print(f"❌ 库不存在：{db}")
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("select data from json_blobs where key=?", (KEY,)).fetchone()
    if not row:
        print(f"（库里没有 {KEY} 记录，无需处理）")
        conn.close()
        return 0

    data = json.loads(row["data"])
    if not isinstance(data, dict):
        print(f"❌ {KEY} 不是按日期分桶的 dict，格式意外，放弃")
        conn.close()
        return 1

    total, hits = 0, []
    for day in sorted(data.keys()):
        snap = data.get(day)
        stmts = (snap or {}).get("statements") or []
        total += len(stmts)
        bad = [s for s in stmts if s in FIXTURE_STATEMENTS]
        if bad:
            hits.append((day, bad, len(stmts)))

    print(f"库        : {db}")
    print(f"statements: 共 {total} 条")
    if not hits:
        print("✅ 没有发现测试字面量（幂等：已清理过或本就干净）")
        conn.close()
        return 0

    print(f"\n发现 {len(hits)} 个日期桶被污染：")
    for day, bad, n in hits:
        print(f"  {day}  ({n} 条中 {len(bad)} 条是测试字面量)")
        for b in bad:
            print(f"      - {b}")

    if not args.apply:
        print("\n（dry-run，未写入。加 --apply 执行）")
        conn.close()
        return 0

    bak = backup(db)
    print(f"\n已备份 → {bak}")

    removed = 0
    for day, bad, _ in hits:
        snap = data.get(day)
        keep = [s for s in (snap.get("statements") or []) if s not in FIXTURE_STATEMENTS]
        removed += len(snap.get("statements") or []) - len(keep)
        snap["statements"] = keep

    conn.execute(
        "update json_blobs set data=? where key=?",
        (json.dumps(data, ensure_ascii=False), KEY),
    )
    conn.commit()
    conn.close()
    print(f"✅ 已删除 {removed} 条测试字面量（statements {total} → {total - removed}）")
    print("   回滚：sqlite3 %s \".restore %s\"" % (db, bak))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
