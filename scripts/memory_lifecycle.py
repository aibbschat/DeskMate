"""记忆生命周期巡检（P2）：**只读 dry-run，绝不写库**。

设计文档 §6 要求 consolidation / decay / archive，但这三件事都会改
``learned_history``（而 ``status`` / ``valid_until`` 在同步白名单里，会经
``_sync_oplog`` 推到云端）。按项目红线，批量改数据必须先备份、先 dry-run、
由萧哥拍板，所以本脚本**只输出计划**，不提供 ``--apply``：

    python scripts/memory_lifecycle.py --base-dir ~/.deskmate
    python scripts/memory_lifecycle.py --base-dir ~/.deskmate --json plan.json

输出的两组候选：
* ``decay``：按来源分级 TTL 超期、却仍 ``status='active'`` 的条目
  （progress 30 天 / reasoning、roundtable 90 天；setting 不自动过期）。
  建议动作是 ``status='archived'``，不物理删除、不改 fact。
* ``consolidation``：同书 + 同锚点（entity）且归一正文等价的近重复组。
  建议动作是保留最新一条、其余 ``superseded_by`` 指向它（保留链、不删行）。

真正落库要另走显式迁移脚本：先 ``sqlite3 .backup`` 备份生产库，再按本计划
逐条 UPDATE，并核对 ``_sync_oplog``。这一步本脚本故意不做。
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_BASE = Path.home() / ".deskmate"


def _ro_connect(db: Path) -> sqlite3.Connection:
    """只读打开；绝不因脚本创建/修改库。"""
    if not db.is_file():
        raise FileNotFoundError(f"deskmate.db not found: {db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def build_plan(base_dir: str | Path | None = None, now: datetime.datetime | None = None) -> dict[str, Any]:
    """扫描活跃记忆，返回 dry-run 计划（不改任何数据）。"""
    from deskmate.memory.learned_store import LearnedStore, source_book, source_prefix
    from deskmate.memory.user_fact_context import _DECAY_TTL_DAYS, _norm_body

    base = Path(base_dir) if base_dir else DEFAULT_BASE
    now = now or datetime.datetime.now()
    db = LearnedStore(base_dir=base).db_path
    plan: dict[str, Any] = {
        "dry_run": True,
        "base_dir": str(base),
        "generated_at": now.isoformat(timespec="seconds"),
        "scanned": 0,
        "decay": [],
        "consolidation": [],
        "conflict_review": [],
        "note": (
            "只出计划，不写库；落库需先备份生产库并逐条人工确认。"
            "conflict_review 是 P2 人工确认队列的候选发现（同锚点多版本），"
            "机器不替人裁决；L2 提议卡动作类型扩展需前端 AgencyPanel "
            "与 agency/engine.resolve_proposal 同版本上线，本脚本不自动发卡。"
        ),
    }
    with _ro_connect(db) as con:
        rows = con.execute(
            "SELECT id, ts, fact, source, entity, valid_until FROM learned_history "
            "WHERE status='active' ORDER BY ts DESC, id DESC"
        ).fetchall()
    plan["scanned"] = len(rows)

    groups: dict[tuple[str, str], list[dict]] = {}
    for rid, ts, fact, source, entity, valid_until in rows:
        fact_s = str(fact or "").strip()
        src = str(source or "")
        prefix = source_prefix(src)
        try:
            age = (now - datetime.datetime.fromisoformat(str(ts))).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age = 0.0
        ttl = _DECAY_TTL_DAYS.get(prefix)
        if ttl and age > ttl:
            plan["decay"].append({
                "id": rid,
                "source": src,
                "age_days": round(age, 1),
                "ttl_days": ttl,
                "fact": fact_s[:80],
                "suggested_status": "archived",
                "valid_until": valid_until,
            })
        key = (source_book(src), str(entity or "").strip())
        if key[1]:
            groups.setdefault(key, []).append({
                "id": rid, "ts": str(ts or ""), "fact": fact_s, "source": src,
            })

    for (book, entity), items in groups.items():
        # P2 人工确认队列候选：同锚点 ≥2 条、文本骨架不同、跨度 ≥7 天。
        # 与召回侧弱冲突提示同口径，但这里只列候选、不发卡、不裁决。
        if len(items) >= 2:
            bodies = {_norm_body(it["fact"]) for it in items if it["fact"].strip()}
            try:
                dates = [
                    datetime.datetime.fromisoformat(str(it["ts"]))
                    for it in items
                ]
                span = (max(dates) - min(dates)).total_seconds() / 86400.0
            except (ValueError, TypeError):
                span = 0.0
            if len(bodies) >= 2 and span >= 7:
                plan["conflict_review"].append({
                    "book": book,
                    "entity": entity,
                    "span_days": round(span, 1),
                    "ids": [it["id"] for it in items],
                    "samples": [it["fact"][:80] for it in items],
                    "suggested_action": "人工选：以新为准 / 保留旧 / 都要（分层）",
                })
        by_body: dict[str, list[dict]] = {}
        for item in items:
            body = _norm_body(item["fact"])
            if body:
                by_body.setdefault(body, []).append(item)
        for body, dup in by_body.items():
            if len(dup) < 2:
                continue
            keep = max(dup, key=lambda r: (r["ts"], r["id"]))
            plan["consolidation"].append({
                "book": book,
                "entity": entity,
                "normalized": body[:80],
                "keep_id": keep["id"],
                "supersede_ids": [r["id"] for r in dup if r["id"] != keep["id"]],
                "samples": [r["fact"][:80] for r in dup],
                "suggested_action": "status=superseded + superseded_by=keep_id（不删行）",
            })
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆生命周期巡检（只读 dry-run）")
    ap.add_argument("--base-dir", default=str(DEFAULT_BASE), help="记忆目录，默认 ~/.deskmate")
    ap.add_argument("--json", dest="json_out", default="", help="把计划写到该 JSON 文件")
    args = ap.parse_args(argv)
    try:
        plan = build_plan(args.base_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"巡检失败（未改动任何数据）：{exc}", file=sys.stderr)
        return 1
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
        print(f"计划已写入 {args.json_out}（dry-run，未改库）")
    else:
        print(text)
    print(
        f"[dry-run] scanned={plan['scanned']} decay={len(plan['decay'])} "
        f"consolidation={len(plan['consolidation'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
