"""记忆质量评测集 runner —— 方向盘。

纯本地、无 LLM 依赖。给定自然语言提问，检查小桔记忆系统能否从五类记忆源
正确召回相关信息，量化**召回率**作为「越用越懂你」的硬指标。

每次改记忆逻辑（P0-5 去重 / P0-6 分层 / P2 提炼等）跑一遍即可防回归：
  python tests/memory_bench/runner.py

也可走 pytest 门禁：
  python -m pytest tests/memory_bench/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 让独立运行时也能 import deskmate 包
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deskmate.memory.manager import DeskMateMemoryManager  # noqa: E402
from deskmate.memory.user_profile import load_user  # noqa: E402

DATASET = Path(__file__).parent / "benchmark_dataset.json"

# 判定阈值（低于此召回率视为回归，pytest 会 fail）
RECALL_THRESHOLD = 0.85


def _retrieve(mgr: DeskMateMemoryManager, item: dict, base_dir: Path) -> str:
    """按 item['retrieval'] 调用对应记忆检索接口，返回检索到的文本。"""
    r = item["retrieval"]
    q = item.get("question", "")
    if r == "load_user":
        return load_user(base_dir)
    if r == "search_global":
        return mgr._search_global_memory(q)
    if r == "prefetch":
        return mgr.prefetch(q)
    if r == "search_project":
        return mgr._search_project_memory(q, item["project"])
    if r == "search_vault":
        return mgr._search_vault(q)
    if r == "novel_recall":
        from deskmate.novel_memory.core import keyword_recall

        return keyword_recall(q, str(base_dir / "bible" / item["project"]))
    raise ValueError(f"未知 retrieval: {r}")


def evaluate(base_dir: Path) -> dict:
    """跑完整题库，返回 {recall_rate, total, passed, by_category, details, text}。"""
    base_dir = Path(base_dir)
    mgr = DeskMateMemoryManager(base_dir=base_dir)
    # P0-5 方向盘附加指标：USER.md 近重复率（越低越好，目标趋近 0）
    try:
        user_dup_rate = mgr.user_dup_rate()
    except Exception:  # noqa: BLE001
        user_dup_rate = None
    # P0-6 方向盘附加指标：全局记忆陈旧率（越低越「新鲜」；>_STALE_DAYS 视为历史事实）
    try:
        memory_stale_rate = mgr.memory_stale_rate()
    except Exception:  # noqa: BLE001
        memory_stale_rate = None
    data = json.loads(DATASET.read_text(encoding="utf-8"))

    details: list[dict] = []
    by_category: dict[str, dict] = {}

    for item in data:
        cat = item["category"]
        by_category.setdefault(cat, {"total": 0, "passed": 0})
        by_category[cat]["total"] += 1

        try:
            text = _retrieve(mgr, item, base_dir)
            err = None
        except Exception as e:  # noqa: BLE001
            text = ""
            err = f"{type(e).__name__}: {e}"

        text = text or ""
        expect = item.get("expect_in", [])
        min_hit = item.get("min_hit", 1)
        hits = [kw for kw in expect if kw in text]
        passed = (not err) and bool(hits) and len(hits) >= min_hit

        if passed:
            by_category[cat]["passed"] += 1

        details.append({
            "id": item["id"],
            "category": cat,
            "question": item.get("question", ""),
            "retrieval": item["retrieval"],
            "passed": passed,
            "hits": hits,
            "expect_in": expect,
            "error": err,
        })

    total = len(details)
    passed_total = sum(1 for d in details if d["passed"])
    recall_rate = (passed_total / total) if total else 0.0

    # 组装可读报告
    lines = []
    lines.append("=" * 60)
    lines.append("记忆质量评测集（8.1 方向盘）")
    lines.append("=" * 60)
    lines.append(f"总题数: {total}   命中(召回): {passed_total}   "
                 f"召回率: {recall_rate:.1%}   (阈值 ≥ {RECALL_THRESHOLD:.0%})")
    if user_dup_rate is not None:
        lines.append(f"USER.md 近重复率: {user_dup_rate:.1%}   (P0-5 目标趋近 0)")
    if memory_stale_rate is not None:
        lines.append(f"全局记忆陈旧率: {memory_stale_rate:.1%}   (P0-6 时间感知：越低越新鲜)")
    lines.append("")
    lines.append("按类别:")
    for cat, stat in by_category.items():
        rate = (stat["passed"] / stat["total"]) if stat["total"] else 0
        lines.append(f"  {cat:<16} {stat['passed']}/{stat['total']}  {rate:.0%}")
    lines.append("")
    lines.append("逐题明细:")
    for d in details:
        mark = "✓" if d["passed"] else "✗"
        hit_str = "、".join(d["hits"]) if d["hits"] else "（无命中）"
        tail = "" if d["passed"] else f"  期望含: {d['expect_in']}"
        if d["error"]:
            tail = f"  异常: {d['error']}"
        lines.append(f"  [{mark}] {d['id']:<4} {d['category']:<14} "
                     f"命中[{hit_str}]{tail}")
    lines.append("=" * 60)

    return {
        "recall_rate": recall_rate,
        "user_dup_rate": user_dup_rate,
        "memory_stale_rate": memory_stale_rate,
        "total": total,
        "passed": passed_total,
        "by_category": by_category,
        "details": details,
        "text": "\n".join(lines),
    }


if __name__ == "__main__":
    import tempfile

    from fixture_xiaosheng import build_xiaosheng_fixture

    d = Path(tempfile.mkdtemp(prefix="membench_"))
    build_xiaosheng_fixture(d)
    rep = evaluate(d)
    print(rep["text"])
    if rep["recall_rate"] < RECALL_THRESHOLD:
        print(f"\n⚠️ 召回率 {rep['recall_rate']:.1%} 低于阈值 {RECALL_THRESHOLD:.0%}")
        sys.exit(1)
    print("\n✅ 通过基准")
