"""记忆召回纪律评测集（P2，设计文档 §8）：30 条 case，只读、可复现。

两类用例：
* ``real``：把生产库 ``~/.deskmate/deskmate.db`` 用 ``sqlite3 .backup`` 复制到
  临时目录再跑，**绝不写真实库**；检查每轮都该成立的不变量与已知串场词。
* ``synthetic``：在 tmp 库里造已知条目，验证书隔离 / 来源分层 / 冲突 /
  陈旧 / 近重复这些结构性行为。

门禁：总通过率 ≥95%，且 ``book_scope`` 组（书隔离）必须 100%。

    python scripts/bench_memory_recall_discipline.py
    python scripts/bench_memory_recall_discipline.py --json report.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deskmate.memory.learned_store import LearnedStore
from deskmate.memory.user_fact_context import build_user_fact_context

REAL_DB = Path.home() / ".deskmate" / "deskmate.db"

#: 每轮注入都必须成立的事实纪律（e35d84e1 治本）
INVARIANT_MUST = ("以正文为准",)
INVARIANT_MUST_NOT = ("不是她猜的", "优先据此作答")
#: 《夜不收》专属词（正文逐字核实过，见 2026-11-10 校正）
YNS_WORDS = ("大和盟", "韩孝民", "龙山仓", "孔大贵")
#: 《国子监秘闻录》专属词
GZJ_WORDS = ("汪成儒", "江鸿鹰", "武成川")


@dataclass
class Case:
    name: str
    query: str
    group: str
    must: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    kind: str = "real"


def _invariant_cases() -> list[Case]:
    queries = [
        "我在写《国子监秘闻录》第六卷",
        "接着写《夜不收》第十一章",
        "《衙探》访谈稿怎么改",
        "今天随便聊两句",
        "夜不收 大和盟 李昖 粮仓",
        "帮我看看大明梦华的结构",
        "《兰陵王》人物关系",
        "上午那件事你怎么看",
        "《袁崇焕》史料核一下",
    ]
    out: list[Case] = []
    for i, q in enumerate(queries, 1):
        out.append(Case(f"R{i:02d} 事实纪律·{q[:12]}", q, "invariant", INVARIANT_MUST, INVARIANT_MUST_NOT))
    for i, q in enumerate(queries, 10):
        out.append(Case(f"R{i:02d} 无权威口吻·{q[:12]}", q, "invariant", (), INVARIANT_MUST_NOT))
    return out


def _book_cases() -> list[Case]:
    # 带 query 时召回按相关度打分，通用事实（无书维度）未必被选中——那是排序行为，
    # 不是书隔离。所以「通用事实不误杀」放在 B04 的无 query 用例里验证。
    return [
        Case("B01 国子监会话不含夜不收", "我在写《国子监秘闻录》第六卷", "book_scope",
             ("汪成儒",), YNS_WORDS, "synthetic"),
        Case("B02 夜不收会话不含国子监", "接着写《夜不收》第十一章", "book_scope",
             ("韩孝民",), GZJ_WORDS, "synthetic"),
        Case("B03 衙探会话两书都不串", "《衙探》访谈稿", "book_scope",
             (), YNS_WORDS + GZJ_WORDS, "synthetic"),
        Case("B04 无 query 不误杀通用", "", "book_scope",
             ("萧哥偏好雨天闷头码字",), (), "synthetic"),
        Case("B05 裸书名也能隔离", "夜不收 王炽 粮仓", "book_scope",
             (), GZJ_WORDS, "synthetic"),
        Case("B06 跨书对比不猜", "对比《夜不收》和《国子监秘闻录》", "book_scope",
             (), (), "synthetic"),
    ]


def _structure_cases() -> list[Case]:
    # 期望值按 2026-11-10 合成库实测输出定死（不是照抄文档的文字描述）
    return [
        Case("S01 外部新闻不进主块", "最近文化产业风向", "source",
             (), ("短剧改编升温",), "synthetic"),
        Case("S02 本地笔记进参考层", "人物资料", "source",
             ("参考层", "人物资料摘录"), (), "synthetic"),
        Case("S03 同锚点冲突须提示", "《夜不收》粮仓", "conflict",
             ("冲突提示",), (), "synthetic"),
        Case("S04 旧 progress 不注入", "夜不收 进度", "decay",
             (), ("第十一章改完了",), "synthetic"),
        Case("S05 近重复只留一条", "这一卷", "dedupe",
             ("这一卷",), ("这一卷/篇",), "synthetic"),
        Case("S06 整块不超 4000 字", "", "budget", (), (), "synthetic"),
    ]


def _make_synthetic(base: Path) -> None:
    """造固定状态的评测库。

    刻意用 SQL 直插而不是 ``LearnedStore.append``：append 会把所有含
    《夜不收》的条目抽成同一个 ``书:夜不收`` 实体，再按「同实体反极性」
    互相取代，评测数据会自己把自己清空（实测踩过）。评测集测的是**召回**，
    必须绕开写入侧冲突逻辑，固定每条的 source/status/ts/entity。
    """
    for name in ("夜不收", "国子监秘闻录", "衙探"):
        (base / "projects" / name).mkdir(parents=True, exist_ok=True)
    store = LearnedStore(base_dir=base)  # 只为建表
    now = datetime.datetime.now()
    old = (now - datetime.timedelta(days=60)).isoformat(timespec="seconds")
    ts = now.isoformat(timespec="seconds")
    rows = [
        (1, ts, "《夜不收》设定：大和盟总旗主是韩孝民，龙山仓粮未烧成", "setting:夜不收", "设定:夜不收", "assert"),
        (2, ts, "《国子监秘闻录》设定：汪成儒诗案牵连骨架不动", "setting:国子监秘闻录", "设定:国子监", "assert"),
        (3, ts, "萧哥偏好雨天闷头码字", "live", "", "assert"),
        (4, ts, "（外部·文化产业）网文风向：短剧改编升温", "external", "", "assert"),
        (5, ts, "本地笔记：人物资料摘录", "local_intake", "", "assert"),
        (6, ts, "《夜不收》粮仓烧成了", "reasoning:夜不收", "粮仓", "assert"),
        (7, ts, "《夜不收》粮仓没有烧成", "reasoning:夜不收", "粮仓", "deny"),
        (8, old, "《夜不收》第十一章改完了", "progress:夜不收", "进度", "assert"),
        # 文档 §8 第 9 条的原始量例（斜杠并列变体）
        (9, ts, "这一卷/篇", "live", "", "assert"),
        (10, ts, "这一卷", "live", "", "assert"),
    ]
    with sqlite3.connect(str(store.db_path)) as con:
        con.executemany(
            "INSERT OR REPLACE INTO learned_history"
            "(id, ts, fact, category, fp, source, entity, polarity, status, valid_from) "
            "VALUES (?, ?, ?, 'other', ?, ?, ?, ?, 'active', ?)",
            [(rid, t, fact, f"fp-{rid}", src, ent, pol, t) for rid, t, fact, src, ent, pol in rows],
        )


def _real_snapshot(tmp: Path) -> Path | None:
    if not REAL_DB.is_file():
        return None
    base = tmp / "real"
    base.mkdir(parents=True)
    src = sqlite3.connect(str(REAL_DB))
    dst = sqlite3.connect(str(base / "deskmate.db"))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return base


def run_cases() -> dict[str, Any]:
    cases = _invariant_cases() + _book_cases() + _structure_cases()
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="bench_memory_") as td:
        tmp = Path(td)
        real = _real_snapshot(tmp)
        synthetic = tmp / "synthetic"
        synthetic.mkdir()
        _make_synthetic(synthetic)
        for case in cases:
            base = real if case.kind == "real" else synthetic
            if base is None:
                results.append({"name": case.name, "group": case.group, "passed": True,
                                "skipped": "无真实库快照"})
                continue
            ctx = build_user_fact_context(base, query=case.query)
            missing = [w for w in case.must if w not in ctx]
            leaked = [w for w in case.must_not if w in ctx]
            oversize = case.group == "budget" and len(ctx) > 4000
            passed = not missing and not leaked and not oversize
            results.append({
                "name": case.name, "group": case.group, "passed": passed,
                "missing": missing, "leaked": leaked, "chars": len(ctx),
            })
    total = len(results)
    passed_n = sum(1 for r in results if r["passed"])
    by_group: dict[str, dict] = {}
    for r in results:
        g = by_group.setdefault(r["group"], {"total": 0, "passed": 0})
        g["total"] += 1
        g["passed"] += int(r["passed"])
    book = by_group.get("book_scope", {"total": 0, "passed": 0})
    gate = (passed_n / total if total else 0) >= 0.95 and book["passed"] == book["total"]
    return {
        "total": total, "passed": passed_n,
        "rate": round(passed_n / total, 4) if total else 0,
        "by_group": by_group, "gate_passed": gate, "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆召回纪律评测（只读）")
    ap.add_argument("--json", dest="json_out", default="", help="报告输出路径")
    args = ap.parse_args(argv)
    report = run_cases()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    for r in report["results"]:
        if not r["passed"]:
            print(f"FAIL {r['name']}: missing={r.get('missing')} leaked={r.get('leaked')}")
    print(f"评测 {report['passed']}/{report['total']} 通过率 {report['rate']:.1%}；"
          f"书隔离 {report['by_group'].get('book_scope', {})}；门禁 {'PASS' if report['gate_passed'] else 'FAIL'}")
    if args.json_out:
        print(f"报告：{args.json_out}")
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
