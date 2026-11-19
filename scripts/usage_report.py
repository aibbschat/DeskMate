#!/usr/bin/env python3
"""LLM 用量与首字延迟报表（2026-11-19 PR0，只读）。

用途：给「先补基线，再谈优化」提供可复现的验收口径——按 scene/model 汇总
token、首字耗时 p50/p95、错误率；并从 traces/inject.jsonl 汇总每轮记忆上下文
的组装耗时与预算裁剪情况。

红线：
  - 只读。不写 deskmate.db、不写 trace、不联网、不发 LLM 请求。
  - 不打印用户原文 / 记忆正文：trace 里只取块名、耗时、字符数、预算统计。
  - 缺表 / 缺列 / 文件不存在一律降级为空报表，不报错（fail-open）。

用法：
  .venv/bin/python scripts/usage_report.py --days 7
  .venv/bin/python scripts/usage_report.py --days 1 --top 10
  DESKMATE_DIR=/path/to/deskmate .venv/bin/python scripts/usage_report.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def _base_dir() -> Path:
    explicit = os.environ.get("DESKMATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    try:
        from deskmate.paths import deskmate_base_dir

        return deskmate_base_dir()
    except Exception:
        return Path.home() / ".deskmate"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


def _usage_rows(db: Path, days: float) -> list[dict[str, Any]]:
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(llm_usage)").fetchall()}
            if not cols:
                return []
            first = "first_chunk_ms" if "first_chunk_ms" in cols else "NULL"
            stream = "stream_ms" if "stream_ms" in cols else "NULL"
            scene = "scene" if "scene" in cols else "NULL"
            rows = con.execute(
                f"SELECT model, provider, {scene} AS scene, status, "
                f"COALESCE(input_tokens,0) AS input_tokens, "
                f"COALESCE(completion_tokens,0) AS completion_tokens, "
                f"COALESCE(cache_read,0) AS cache_read, "
                f"COALESCE(cache_write,0) AS cache_write, "
                f"{first} AS first_chunk_ms, {stream} AS stream_ms "
                "FROM llm_usage WHERE ts >= ?",
                (time.time() - days * 86400,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def _trace_rows(base: Path, days: float) -> list[dict[str, Any]]:
    path = base / "traces" / "inject.jsonl"
    if not path.is_file():
        return []
    cutoff = time.time() - days * 86400
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts")
                # ts 是本地 ISO 秒串；缺失时算在窗口内，避免老 trace 全被丢。
                if ts:
                    try:
                        import datetime

                        epoch = datetime.datetime.fromisoformat(ts).timestamp()
                        if epoch < cutoff:
                            continue
                    except Exception:
                        pass
                out.append(rec)
    except Exception:
        return []
    return out


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(len(cell), 60))
    def line(cells: list[str]) -> str:
        return "  ".join(c[: widths[i]].ljust(widths[i]) for i, c in enumerate(cells))
    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=7.0, help="统计窗口（天，默认 7）")
    ap.add_argument("--top", type=int, default=10, help="最慢的注入块显示条数（默认 10）")
    ap.add_argument("--db", default="", help="覆盖 deskmate.db 路径（默认按 DESKMATE_DIR 解析）")
    ap.add_argument("--base", default="", help="覆盖 deskmate 根目录（trace 来源）")
    args = ap.parse_args()

    base = Path(args.base).expanduser() if args.base else _base_dir()
    db = Path(args.db).expanduser() if args.db else base / "deskmate.db"

    usage = _usage_rows(db, args.days)
    print(f"== LLM 用量（近 {args.days:g} 天，{len(usage)} 条记录）==")
    if not usage:
        print("（无记录：llm_usage 表为空或数据库不存在）")
    else:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for r in usage:
            grouped[(str(r.get("scene") or "-"), str(r.get("model") or "-"))].append(r)
        rows: list[list[str]] = []
        for (scene, model), items in sorted(grouped.items()):
            firsts = [float(i["first_chunk_ms"]) for i in items if i.get("first_chunk_ms") is not None]
            streams = [float(i["stream_ms"]) for i in items if i.get("stream_ms") is not None]
            tokens = sum(int(i.get("input_tokens") or 0) + int(i.get("completion_tokens") or 0) for i in items)
            errs = sum(1 for i in items if str(i.get("status")) != "ok")
            rows.append([
                scene[:18], model[:28], str(len(items)), str(tokens),
                f"{_percentile(firsts, .5):.0f} / {_percentile(firsts, .95):.0f}",
                f"{_percentile(streams, .5):.0f}",
                f"{errs / len(items) * 100:.1f}%",
            ])
        print(_fmt_table(
            ["scene", "model", "calls", "tokens", "首字p50/p95 ms", "流p50 ms", "err%"], rows
        ))

    traces = _trace_rows(base, args.days)
    print(f"\n== 记忆上下文组装（近 {args.days:g} 天，{len(traces)} 轮）==")
    if not traces:
        print("（无 trace：traces/inject.jsonl 不存在或窗口内为空）")
    else:
        totals = [float(t.get("total_ms") or 0) for t in traces]
        toks = [int((t.get("budget") or {}).get("tokens") or 0) for t in traces]
        lights = sum(1 for t in traces if (t.get("budget") or {}).get("light"))
        dropped: dict[str, int] = defaultdict(int)
        cost: dict[str, list[float]] = defaultdict(list)
        for t in traces:
            for name in (t.get("budget") or {}).get("dropped") or []:
                dropped[str(name)] += 1
            for b in t.get("blocks") or []:
                if isinstance(b, dict) and b.get("name"):
                    try:
                        cost[str(b["name"])].append(float(b.get("ms") or 0))
                    except Exception:
                        pass
        print(f"total_ms  p50={_percentile(totals, .5):.0f}  p95={_percentile(totals, .95):.0f}  max={max(totals):.0f}")
        print(f"注入 token  p50={_percentile(toks, .5):.0f}  p95={_percentile(toks, .95):.0f}  max={max(toks):.0f}")
        print(f"闲聊轮 = {lights} / {len(traces)}")
        if dropped:
            print("被裁剪块 TOP:", ", ".join(f"{k}={v}" for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])[:10]))
        slow = sorted(
            ((name, _percentile(ms, .5), _percentile(ms, .95), len(ms)) for name, ms in cost.items()),
            key=lambda x: -x[2],
        )[: args.top]
        if slow:
            print("\n最慢的注入块：")
            print(_fmt_table(
                ["block", "p50 ms", "p95 ms", "samples"],
                [[n, f"{p50:.1f}", f"{p95:.1f}", str(c)] for n, p50, p95, c in slow],
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
