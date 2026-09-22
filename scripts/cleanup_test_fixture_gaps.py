#!/usr/bin/env python3
"""清理「无据的 open gap」——主要是 pytest 夹具残留（默认 dry-run）。

## 为什么需要

2026-09-22 排查「访谈卡为什么没弹」时挖到的：真实库 ``cognitive_gaps`` 里长期躺着
两个 **pytest 夹具**（来自 ``deskmate/cognitive/test_interview.py``）：

| id | topic | reason | related_items |
|----|-------|--------|---------------|
| gap_0b65fa4a | 矛盾待澄清 | 矛盾 | ["stance_2921cb84"] ← **库里不存在** |
| gap_6eeb060b | 新领域 | 从未提及 | [] |

它们被 ``SocraticInterviewEngine.generate_questions`` 正常取用，于是 09-17 起
每一轮访谈问出的都是「关于『矛盾待澄清』，你之前留下过有点矛盾的说法（矛盾）」——
用户看到只会觉得功能坏了。

``_gap_is_askable`` 已在生成侧挡掉它们（下游兜底），本脚本负责**把已污染的
数据清出来**：不删除，只标 ``resolved``，保留可追溯。

## 用法

    python scripts/cleanup_test_fixture_gaps.py                  # 只看（dry-run）
    python scripts/cleanup_test_fixture_gaps.py --apply           # 落库
    python scripts/cleanup_test_fixture_gaps.py --base-dir /tmp/x --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def list_unaskable_gaps(base_dir: Path, layer: str | None = None) -> list[dict]:
    """挑出「不该被提问」的 open gap。

    三条判定（都可解释，不靠硬编码词表）：

    1. **悬空引用**：``related_items`` 指向画像里不存在的条目。
       这是最强的夹具指纹——pytest 造的 related_items 必然指向测试进程里的对象。
    2. **内容为空**：topic/reason 同时为空，问了也是空话。
    3. **库初始化瞬间写入**：created_at 与 ``profile_meta.created_at``（库初始化时刻）
       相差 < 1 秒。按秒对齐取证：真实库里 gap_0b65fa4a/6eeb060b 与 profile_meta
       只差 0.07 毫秒，而同库的真实 gap 都落在 09-04 / 09-08 的业务时刻。
       库刚建好那一瞬不可能积累出「他真正需要澄清的盲区」——那是夹具或自检数据。
    """
    from datetime import datetime

    from deskmate.cognitive.interview import _gap_is_askable
    from deskmate.cognitive.profile import resolve_profile_manager

    layers = [layer] if layer else ["cognitive", "emotional"]
    out: list[dict] = []
    for lyr in layers:
        try:
            pm = resolve_profile_manager(layer=lyr, base_dir=base_dir)
        except Exception as e:  # pragma: no cover - 环境问题
            print(f"  [skip] layer={lyr} 画像不可用: {e}")
            continue
        known = {
            getattr(it, "id", None)
            for it in (pm.profile.stances + pm.profile.value_rankings
                       + pm.profile.thinking_frameworks + pm.profile.argument_patterns)
        }
        try:
            bootstrap = pm.store.get_meta("created_at")
            bootstrap_dt = datetime.fromisoformat(str(bootstrap)) if bootstrap else None
        except Exception:
            bootstrap_dt = None

        for g in pm.profile.cognitive_gaps:
            if getattr(g, "status", "") != "open":
                continue
            refs = list(getattr(g, "related_items", None) or [])
            dangling = [r for r in refs if r not in known]

            if dangling:
                why = "悬空引用"
            elif not _gap_is_askable(g, pm.profile):
                why = "内容为空"
            elif bootstrap_dt is not None and _near(g.created_at, bootstrap_dt):
                why = "库初始化瞬间写入（夹具/自检数据）"
            else:
                continue

            out.append({
                "layer": lyr,
                "id": g.id,
                "topic": getattr(g, "topic", ""),
                "reason": getattr(g, "reason", ""),
                "refs": refs,
                "dangling": dangling,
                "created_at": getattr(g, "created_at", ""),
                "why": why,
            })
    return out


_BOOTSTRAP_WINDOW_SECONDS = 1.0


def _near(created_at: str, bootstrap_dt) -> bool:
    """created_at 是否落在库初始化瞬间（< 1 秒）。"""
    from datetime import datetime

    try:
        return abs((datetime.fromisoformat(str(created_at)) - bootstrap_dt).total_seconds()) < _BOOTSTRAP_WINDOW_SECONDS
    except Exception:
        return False


def apply_resolve(db_path: Path, rows: list[dict]) -> int:
    """把选中的 gap 标为 resolved（只动 status/resolved_at 两列）。"""
    from deskmate.cognitive.models import _now

    ts = _now()
    con = sqlite3.connect(db_path)
    try:
        n = 0
        for r in rows:
            cur = con.execute(
                "UPDATE cognitive_gaps SET status='resolved', resolved_at=? "
                "WHERE id=? AND status='open'",
                (ts, r["id"]),
            )
            n += cur.rowcount or 0
        con.commit()
        return n
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-dir", default=str(Path.home() / ".deskmate"),
                    help="deskmate 数据目录（默认 ~/.deskmate）")
    ap.add_argument("--layer", default=None, choices=["cognitive", "emotional"],
                    help="只处理某层（默认两层都看）")
    ap.add_argument("--apply", action="store_true", help="真正落库（默认只打印）")
    args = ap.parse_args()

    base = Path(args.base_dir).expanduser()
    db = base / "deskmate.db"
    if not db.exists():
        print(f"找不到库: {db}")
        return 1

    rows = list_unaskable_gaps(base, args.layer)
    print(f"库: {db}")
    if not rows:
        print("没有「无据的 open gap」——无需清理。")
        return 0

    print(f"发现 {len(rows)} 个不该被提问的 open gap：\n")
    for r in rows:
        print(f"  [{r['layer']}] {r['id']}  topic={r['topic']!r} reason={r['reason']!r}")
        print(f"      created_at={r.get('created_at','')}  related_items={r['refs']}  判定：{r['why']}"
              + (f"（不存在：{r['dangling']}）" if r["dangling"] else ""))

    if not args.apply:
        print("\n[dry-run] 未改动任何数据。确认无误后加 --apply 落库。")
        return 0

    n = apply_resolve(db, rows)
    print(f"\n已标记 {n} 个 gap 为 resolved（未删除，可追溯）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
