"""只读任务耗时报表：把 llm_usage 与 audit_events 按会话拼起来。

用途：回答「这次任务到底慢在哪」——模型轮数、工具轮数、工具墙钟、>20s
空档、scene 分布。全程 sqlite mode=ro，不写任何库，不发网络。

用法：
  .venv/bin/python scripts/task_latency_report.py --session 0a23e948-0ca
  .venv/bin/python scripts/task_latency_report.py --days 3 --top 10
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections import Counter
from contextlib import closing


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _usage_db() -> str:
    try:
        from deskmate.paths import deskmate_db_path

        return str(deskmate_db_path())
    except Exception:
        return os.path.expanduser("~/.deskmate/deskmate.db")


def _audit_db() -> str:
    return os.path.expanduser("~/.config/coworker/coworker.db")


def _top_sessions(audit_path: str, days: int, top: int) -> list[str]:
    if not os.path.exists(audit_path):
        return []
    with closing(_ro(audit_path)) as con:
        return [
            r[0]
            for r in con.execute(
                "select session_id, count(*) c from audit_events "
                "where timestamp>=datetime('now', ?) group by session_id order by c desc limit ?",
                (f"-{days} day", top),
            )
        ]


def _report(session: str, usage_path: str, audit_path: str) -> None:
    print(f"\n===== {session} =====")
    if os.path.exists(usage_path):
        with closing(_ro(usage_path)) as con:
            rows = list(
                con.execute(
                    "select ts, model, scene, status, input_tokens, completion_tokens, "
                    "first_chunk_ms, stream_ms from llm_usage "
                    "where cast(ts as integer)>=strftime('%s','now')-30*86400"
                )
            )
        # llm_usage 没有 session_id（v1 设计如此）——只在「单会话模式下」用时间窗近似，
        # 并明确标注口径，绝不当作精确 per-session 账。
        print("LLM 账本（llm_usage 无 session 列；下列为全库近 30 天汇总，非该会话精确值）：")
        if rows:
            scenes = Counter(r[2] or "(空)" for r in rows)
            models = Counter(r[1] for r in rows)
            print("  scene:", dict(scenes))
            print("  models:", dict(models.most_common(5)))
            print(f"  calls={len(rows)}  input={sum(r[4] or 0 for r in rows):,}")
        else:
            print("  (无记录)")

    if not os.path.exists(audit_path):
        print("audit_events 不存在:", audit_path)
        return
    with closing(_ro(audit_path)) as con:
        events = [
            dict(zip([d[0] for d in cur.description], row))
            for cur in [con.execute(
                "select timestamp, tool, stage, status, reason from audit_events "
                "where session_id like ? order by id", (session + "%",)
            )]
            for row in cur
        ]
    if not events:
        print("audit_events: (无记录)")
        return

    import datetime as dt

    tools = Counter(e["tool"] for e in events if e["stage"] == "proposed")
    starts = [e for e in events if e["stage"] == "started"]
    fins = [e for e in events if e["stage"] == "finished"]
    print(
        f"audit: events={len(events)} proposed={sum(tools.values())} "
        f"started={len(starts)} finished={len(fins)}"
    )
    print("  top tools:", dict(tools.most_common(8)))

    # finished -> 下一条 proposed 的间隔：>20s 通常意味着模型往返而非工具执行
    gaps: list[float] = []
    for i, e in enumerate(events):
        if e["stage"] != "finished":
            continue
        t0 = dt.datetime.fromisoformat(e["timestamp"])
        nxt = next(
            (x for x in events[i + 1 :] if x["stage"] == "proposed"), None
        )
        if nxt is None:
            continue
        gaps.append((dt.datetime.fromisoformat(nxt["timestamp"]) - t0).total_seconds())
    big = [g for g in gaps if g >= 20]
    print(
        f"  finished→next proposed: n={len(gaps)} "
        f">=20s={len(big)} sum={sum(big):.1f}s max={max(big) if big else 0:.1f}s"
    )
    if big:
        print("  top gaps(s):", [round(g, 1) for g in sorted(big, reverse=True)[:8]])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", help="会话 ID 前缀（如 0a23e948-0ca）")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    usage, audit = _usage_db(), _audit_db()
    sessions = (
        [args.session]
        if args.session
        else _top_sessions(audit, args.days, args.top)
    )
    if not sessions:
        print("没有找到会话；试 --session <id>")
    for s in sessions:
        # audit 里的 session_id 不少是完整 id，传前缀也能匹配（报告只是筛行）。
        _report(s, usage, audit)


if __name__ == "__main__":
    main()
