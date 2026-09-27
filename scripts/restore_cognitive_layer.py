#!/usr/bin/env python3
"""恢复 cognitive 层被测试冲掉的真实画像（2026-09-07 事故）。

事故：16:12-16:13 跑 pytest 时，部分测试用了全局 manager（指向真实库），
而 store.persist 是全量覆盖（DELETE WHERE layer=?），一次 save 把 cognitive
层 37 条真实画像清成 5 条测试数据。

恢复源：
  1) ~/.deskmate/deskmate.db.bak-validation-20260904125624  （cognitive 54 条）
  2) WAL 里挖出的 9-04 之后新增的真实条目（4 条）

用法（演练 / 真实执行）：
    python scripts/restore_cognitive_layer.py --dry-run    # 只在副本上跑，打印结果
    python scripts/restore_cognitive_layer.py --apply      # 写真实库（APP 必须先关）
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REAL_DB = HOME / ".deskmate" / "deskmate.db"
BAK_DB = HOME / ".deskmate" / "deskmate.db.bak-validation-20260904125624"

#: 测试污染留下的假条目（内容特征）
FAKE_MARKERS = (
    "原内容",
    "旧说法",
    "冲突时保真实",
    "分析历史先看结构",
    "习惯用正史对比演义来辨伪",
)

#: 9-04 之后新增、只存在于 WAL 里的真实条目（人工从 WAL 文本中核对挑出）
WAL_RECOVERED: list[tuple[str, str, float]] = [
    (
        "stance",
        "小人物可以搅动时代但必须付出代价，不给读者爽文式假希望",
        0.85,
    ),
    (
        "stance",
        "一本书在写作过程中是心灵的产物，但被出版或放到平台后，它就是商品，这一点我分的很清楚。"
        "因为我是一个靠写作而生的作者，怎么生？就是靠写出的文字卖给平台，这是一种很明显的商业模式。"
        "不同的是，这个商品是你脑子里产生的，结合了你的经验、思想，所以图书又被认为是特殊商品",
        0.8,
    ),
    (
        "stance",
        "他放弃的是不值得求的部分，但一生执着于写作，早年出版梦想已实现，"
        "现在梦想是看到自己的书被改编成影视剧播出，并希望大爆",
        0.8,
    ),
    (
        "stance",
        "写作的人是孤独的，往往活在自己创建和想像的世界里，但回到现实是孤独的，"
        "所以我喜欢被关怀，被温柔的对待，被理解",
        0.8,
    ),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _backup(db: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db.with_name(db.name + f".bak-恢复前-{ts}")
    shutil.copy2(db, bak)
    return bak


def _existing_contents(conn: sqlite3.Connection, layer: str) -> set[str]:
    return {
        _norm(r[0])
        for r in conn.execute(
            "SELECT content FROM profile_items WHERE layer=?", (layer,)
        ).fetchall()
    }


def restore(db: Path, dry_run: bool = False) -> dict:
    """在 db 上执行恢复。dry_run 时先复制到临时副本再跑。"""
    target = db
    if dry_run:
        tmp = Path(tempfile.mkdtemp()) / "dryrun.db"
        shutil.copy2(db, tmp)
        target = tmp

    stats = {"removed_fake": 0, "from_backup": 0, "from_wal": 0, "skipped_dup": 0}

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        # ---- 1) 清掉测试污染 ----
        for marker in FAKE_MARKERS:
            cur = conn.execute(
                "DELETE FROM profile_items WHERE layer='cognitive' AND content LIKE ?",
                (f"%{marker}%",),
            )
            stats["removed_fake"] += cur.rowcount or 0

        known = _existing_contents(conn, "cognitive")

        # ---- 2) 从 9-04 备份导入 ----
        bak = sqlite3.connect(str(BAK_DB))
        bak.row_factory = sqlite3.Row
        cols = [r[1] for r in conn.execute("PRAGMA table_info(profile_items)").fetchall()]
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        rows = bak.execute(
            "SELECT * FROM profile_items WHERE layer='cognitive'"
        ).fetchall()
        for r in rows:
            if _norm(r["content"]) in known:
                stats["skipped_dup"] += 1
                continue
            vals = [r[c] for c in cols]
            try:
                conn.execute(
                    f"INSERT INTO profile_items ({col_list}) VALUES ({placeholders})",
                    vals,
                )
                known.add(_norm(r["content"]))
                stats["from_backup"] += 1
            except sqlite3.IntegrityError:
                # id 冲突：换个新 id 再插
                import uuid

                vals = [
                    (str(uuid.uuid4()) if c == "id" else r[c]) for c in cols
                ]
                conn.execute(
                    f"INSERT INTO profile_items ({col_list}) VALUES ({placeholders})",
                    vals,
                )
                known.add(_norm(r["content"]))
                stats["from_backup"] += 1
        bak.close()

        # ---- 3) 补回 WAL 里挖到的真实新增 ----
        now = datetime.now().isoformat()
        for dim, content, conf in WAL_RECOVERED:
            if _norm(content) in known:
                stats["skipped_dup"] += 1
                continue
            if any(_norm(content) in k or k in _norm(content) for k in known):
                stats["skipped_dup"] += 1
                continue
            import uuid

            conn.execute(
                "INSERT INTO profile_items("
                "id, layer, dimension, topic, content, category, status, confidence,"
                " first_extracted, last_updated, extra_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    "cognitive",
                    dim,
                    "创作观",
                    content,
                    "立场",
                    "active",
                    conf,
                    now,
                    now,
                    '{"tags":["恢复"],"contexts":[]}',
                ),
            )
            known.add(_norm(content))
            stats["from_wal"] += 1

        # 同步 profile_items_fts（P1-2 范围化注入依赖，2026-09-26 质检补）。
        # 本脚本绕过 CognitiveStore.persist 直插 profile_items——不重建则新恢复
        # 的条目**搜不到**（范围化注入的长尾召回静默失效，只剩核心层）。
        # 表不存在则跳过：下次开 store 会建表并按数量不符自愈。
        try:
            from deskmate.cognitive.store import CognitiveStore

            has_fts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_items_fts'"
            ).fetchone()
            if has_fts:
                CognitiveStore._rebuild_fts(conn)
        except Exception:
            pass  # fail-open：FTS 失步不影响恢复本身

        conn.commit()

        # ---- 4) 结果 ----
        total = conn.execute(
            "SELECT COUNT(*) FROM profile_items WHERE layer='cognitive'"
        ).fetchone()[0]
        by_dim = conn.execute(
            "SELECT dimension, COUNT(*) n FROM profile_items WHERE layer='cognitive'"
            " GROUP BY dimension ORDER BY n DESC"
        ).fetchall()
        stats["final_total"] = total
        stats["by_dim"] = {r[0]: r[1] for r in by_dim}
        return stats
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写真实库（APP 必须先关）")
    ap.add_argument("--dry-run", action="store_true", help="在副本上演练，不动真实库")
    args = ap.parse_args()

    if not args.apply and not args.dry_run:
        ap.print_help()
        return 1

    if args.apply:
        if not BAK_DB.exists():
            print(f"❌ 备份不存在: {BAK_DB}")
            return 1
        bak = _backup(REAL_DB)
        print(f"✅ 已备份真实库 -> {bak.name}")
        stats = restore(REAL_DB, dry_run=False)
        print("\n=== 恢复完成（真实库） ===")
    else:
        stats = restore(REAL_DB, dry_run=True)
        print("\n=== 演练结果（未动真实库） ===")

    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
