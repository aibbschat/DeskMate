#!/usr/bin/env python3
"""阶段1 回填 · 只读预演（C 项，NOT FOR WRITING）。

只读取、只列出「将要回填」的内容，不写任一端数据库。

回填身份列的取值规则必须与 deskmate/memory/sync_store_sqlite.py::migrate 完全一致，
否则 Mac 与云端首次对账会对不齐：
    ns   = uuid5(NAMESPACE_URL, "deskmate:profile_items")
    uuid = uuid5(ns, rid)          # rid = profile_items.id
    hlc  = migrate() 在回填时按单调基线生成（本预演只给 uuid，hlc 留待真实回填）

用法：
    # 仅本地 Mac 活库预演（不需要密钥）
    python3 scripts/preview_stage1_backfill.py

    # 叠加云端 id 清单，标出「Mac/云端 id 不一致」的跨端风险（二蛋点名的 6 行）
    # cloud-id-file 每行一个 profile_items.id（由持钥方 SSH 只读导出）
    python3 scripts/preview_stage1_backfill.py --cloud-id-file /tmp/cloud_ids.txt

    # 写出 JSON 清单（仍只读，不写库）
    python3 scripts/preview_stage1_backfill.py --out /tmp/stage1_backfill_preview.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid

LOCAL_DB = os.path.expanduser("~/.deskmate/deskmate.db")
TABLE = "profile_items"
NODE = "mac"
_NS = uuid.uuid5(uuid.NAMESPACE_URL, f"deskmate:{TABLE}")


def candidate_uuid(rid: str) -> str:
    """与 migrate() 完全一致的确定性 uuid5 派生。"""
    return str(uuid.uuid5(_NS, rid))


def load_cloud_ids(path: str) -> set[str]:
    ids: set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                ids.add(s)
    return ids


def preview(cloud_ids: set[str] | None):
    if not os.path.exists(LOCAL_DB):
        raise SystemExit(f"本地活库不存在: {LOCAL_DB}")
    # 只读连接
    c = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    try:
        try:
            null_rows = c.execute(
                "SELECT id, layer, dimension, topic, content, first_extracted, last_updated "
                "FROM profile_items WHERE sync_uuid IS NULL ORDER BY layer, dimension, id"
            ).fetchall()
        except sqlite3.OperationalError as e:
            raise SystemExit(f"无法读取 profile_items（表/列缺失？）: {e}")

        total_null = len(null_rows)
        layers = {}
        manifest = []
        id_risk = []
        for (rid, layer, dim, topic, content, fe, lu) in null_rows:
            cand = candidate_uuid(rid)
            layers[layer] = layers.get(layer, 0) + 1
            snippet = (content or "")[:60].replace("\n", " ")
            manifest.append({
                "id": rid, "layer": layer, "dimension": dim, "topic": topic,
                "first_extracted": fe, "last_updated": lu,
                "content_snippet": snippet,
                "candidate_sync_uuid": cand,
                "candidate_hlc": "由 migrate() 回填时生成（单调基线）",
            })
            if cloud_ids is not None and rid not in cloud_ids:
                id_risk.append(rid)

        return {
            "total_null_identity": total_null,
            "by_layer": layers,
            "rows": manifest,
            "id_mismatch_vs_cloud": id_risk,
        }
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser(description="阶段1 回填只读预演（不写库）")
    ap.add_argument("--cloud-id-file", default="",
                    help="可选：云端 profile_items.id 清单（每行一个），用于标出跨端 id 不一致")
    ap.add_argument("--out", default="", help="可选：写出 JSON 清单路径（仍只读）")
    args = ap.parse_args()

    cloud_ids = load_cloud_ids(args.cloud_id_file) if args.cloud_id_file else None

    print("=" * 60)
    print("阶段1 回填 · 只读预演（本脚本不写入任何数据库）")
    print("=" * 60)
    rep = preview(cloud_ids)

    print(f"\n[待回填] sync_uuid 为 NULL 的行总数 = {rep['total_null_identity']}")
    print(f"[按层分布] {rep['by_layer']}")
    print("\n[清单（前 20 条样例）]")
    for m in rep["rows"][:20]:
        print(f"  - {m['layer']}/{m['dimension']} id={m['id']} topic={m['topic']!r}")
        print(f"      candidate_sync_uuid={m['candidate_sync_uuid']}")
        print(f"      content={m['content_snippet']!r}")

    if rep["id_mismatch_vs_cloud"]:
        print(f"\n[⚠ 跨端 id 不一致] Mac 有 {len(rep['id_mismatch_vs_cloud'])} 个 id 不在云端清单：")
        for rid in rep["id_mismatch_vs_cloud"][:20]:
            print(f"    - {rid}")
        print("  → 这些行若按 id 回填，云端会生成不同 uuid，可能与已有条目冲突；")
        print("    真实回填前必须先确认云端该 id 是否换了身份或已删除。")
    else:
        print("\n[跨端 id] 未提供云端清单，跳过不一致比对。")

    print("\n[幂等说明] candidate_sync_uuid 由 uuid5(id) 确定性派生，重跑结果一致；")
    print("           真实回填后请跑 verify_stage1_sync.py --cloud 确认两端 uuid 差集为空。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"\n[写出] JSON 清单 → {args.out}")

    print("\n" + "=" * 60)
    print("本预演只读，未写入任何数据。是否执行真实回填由逍遥/二蛋复核后决定。")
    print("=" * 60)


if __name__ == "__main__":
    main()
