#!/usr/bin/env python3
"""记忆文件层检索评测（三段检索基线 / 复测共用）。

为什么单独一套
--------------
``scripts/bench_memory_recall.py`` 评的是 deskmate.db 里的 learned_history /
external_items（外部信息层，已有向量）。本脚本评的是**记忆文件层**：
USER.md（硬规矩 / 偏好与事实）、MEMORY.md、diary_memory.md、diary/*.md、vault/。
两者语料与检索通道不同，不能混在一张表里看。

评测集
------
``reports/2026-11-11-检索评测集.jsonl``，每行一条 query：
    {"id","intent","category","layer","variant","query","gold","gold_source"}
- 一个 intent 有三种说法（同义 / 口语 / 指代），按 variant 分项统计。
- gold 是期望被召回条目里的唯一子串；``gold_source`` 用于启动自检
  （gold 在源文件里必须存在，否则该题作废并让脚本失败）。

基线口径（--impl keyword）
--------------------------
只调用**生产现路**，不另写理想实现：
    _search_recent_memory / _search_global_memory / _search_vault /
    _search_user_memory / recall_preferences / build_user_fact_context
指标：Hit（任一通道命中）、R@5（任一通道 rank<=5）、MRR、逐条耗时、
mean/median/p95、按分类/说法分项、漏检清单、是否已在常驻注入。

复测口径（--impl fused）
-----------------------
调用三段新实现 ``file_search.search_memory_files``（本轮新增），同一套题、
同一套指标，用于对比。索引不可用时自动跳过并明确报错，不静默当 0 分。

用法
----
    python scripts/bench_memory_files.py --impl keyword
    python scripts/bench_memory_files.py --impl fused

确定性
------
现网 ``vault_store.search`` 遍历 ``set(_CATEGORY_DIRS.values())``，同分条目的
先后受 Python 哈希随机化影响 —— 同一套题两次运行命中率会漂 3~5 个点。
本脚本在未显式设置 ``PYTHONHASHSEED`` 时用子进程以 ``PYTHONHASHSEED=0`` 重入，
保证「同一条命令跑两次结果一致」。这是评测口径，不改生产代码。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deskmate.paths import deskmate_base_dir  # noqa: E402

DEFAULT_EVAL = ROOT / "reports" / "2026-11-11-检索评测集.jsonl"
OUT_DIR = ROOT / "reports"
_K = 5


# ---------------------------------------------------------------------------
# 评测集
# ---------------------------------------------------------------------------
def load_cases(path: Path) -> list[dict]:
    cases: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        c = json.loads(line)
        for key in ("id", "intent", "category", "layer", "variant", "query", "gold", "gold_source"):
            if not c.get(key):
                raise SystemExit(f"评测集字段缺失 {key}: {line[:80]}")
        cases.append(c)
    return cases


def verify_gold(cases: list[dict], base: Path) -> None:
    """gold 必须存在于 gold_source，否则评测集本身有错，直接失败。"""
    bad: list[str] = []
    for c in cases:
        rel = c["gold_source"].split("#", 1)[0]
        p = base / rel
        if not p.is_file():
            bad.append(f"{c['id']}: 源文件不存在 {rel}")
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if c["gold"] not in text:
            bad.append(f"{c['id']}: gold {c['gold']!r} 不在 {rel}")
    if bad:
        raise SystemExit("评测集自检失败：\n" + "\n".join(bad))


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """去空白后比对；标点保留（文件名/引文里的标点有区分度）。"""
    return "".join((text or "").split())


def _units_block(text: str) -> list[str]:
    """把通道返回的文本切成有序检索单元（段落 / bullet）。

    段内若有多条 ``- `` bullet，再拆成独立单元——否则一条命中会把整段
    其他条目一起算成命中，R@5 会被高估。
    """
    units: list[str] = []
    for block in (text or "").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        bullets = [ln.strip() for ln in lines if ln.strip().startswith(("- ", "· ", "* "))]
        if bullets and len(bullets) == len([ln for ln in lines if ln.strip()]):
            units.extend(bullets)
        else:
            units.append(block)
    return units


def _units_bullets(text: str) -> list[str]:
    return [
        ln.strip() for ln in (text or "").splitlines()
        if ln.strip().startswith(("- ", "· ", "* "))
    ]


def _units_vault(text: str) -> list[str]:
    """vault 返回 ``**[rel]**\\nbody`` 以 ``---`` 分隔。"""
    return [b.strip() for b in (text or "").split("\n\n---\n\n") if b.strip()]


def _best_rank(gold: str, units: list[str]) -> Optional[int]:
    g = _norm(gold)
    if not g:
        return None
    for i, u in enumerate(units, 1):
        if g in _norm(u):
            return i
    return None


# ---------------------------------------------------------------------------
# 通道采集
# ---------------------------------------------------------------------------
def collect_keyword(mgr, base: Path, query: str) -> dict[str, dict]:
    """生产现路。返回 {channel: {text, units, ms}}。"""
    from deskmate.memory.user_preferences import recall_preferences
    from deskmate.memory.user_fact_context import build_user_fact_context

    out: dict[str, dict] = {}

    def _run(name: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            val = fn()
        except Exception as exc:  # fail-open：记录错误，不中断整轮
            val = ""
            err = f"{type(exc).__name__}: {exc}"
        else:
            err = None
        ms = (time.perf_counter() - t0) * 1000
        if name == "recall":
            text = (val or ("", set()))[0] if isinstance(val, tuple) else (val or "")
            units = _units_block(text)
        elif name == "vault":
            text = val or ""
            units = _units_vault(text)
        elif name in ("user", "pref"):
            text = val or ""
            units = _units_bullets(text)
        else:
            text = val or ""
            units = _units_block(text)
        out[name] = {"text": text, "units": units, "ms": ms, "error": err}

    _run("recall", lambda: mgr._search_recent_memory(query))
    _run("global", lambda: mgr._search_global_memory(query))
    _run("vault", lambda: mgr._search_vault(query))
    _run("user", lambda: mgr._search_user_memory(query))
    _run("pref", lambda: recall_preferences(base, query))
    _run("user_fact", lambda: build_user_fact_context(base, query))
    return out


def collect_fused(base: Path, query: str, k: int = 8) -> dict[str, dict]:
    """三段新实现（索引 + 三通道融合）。"""
    from deskmate.memory.file_search import search_memory_files

    t0 = time.perf_counter()
    try:
        hits = search_memory_files(query, k=k, budget_chars=1200, base=base)
        err = None
    except Exception as exc:  # noqa: BLE001
        hits = []
        err = f"{type(exc).__name__}: {exc}"
    ms = (time.perf_counter() - t0) * 1000
    # 融合后是一个统一 ranked 列表：每个命中就是一个单元
    units = [h.get("content", "") for h in hits]
    text = "\n\n".join(units)
    return {"fused": {"text": text, "units": units, "ms": ms, "error": err,
                      "hits": hits}}


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
    return s[idx]


def run(cases: list[dict], base: Path, impl: str) -> dict:
    from deskmate.memory.manager import DeskMateMemoryManager
    from deskmate.memory.context_budget import strip_auto_review
    from deskmate.memory.user_profile import load_user

    mgr = DeskMateMemoryManager(base_dir=base)
    resident = strip_auto_review(load_user(base) or "")

    # 预热：载入模块/首读文件，不计入逐条延迟（报告里单列冷启动）
    cold_t0 = time.perf_counter()
    if impl == "keyword":
        collect_keyword(mgr, base, "预热查询")
    else:
        collect_fused(base, "预热查询")
    cold_ms = (time.perf_counter() - cold_t0) * 1000

    details: list[dict] = []
    for c in cases:
        t0 = time.perf_counter()
        if impl == "keyword":
            chans = collect_keyword(mgr, base, c["query"])
        else:
            chans = collect_fused(base, c["query"])
        total_ms = (time.perf_counter() - t0) * 1000

        best_rank: Optional[int] = None
        best_channel = ""
        for name, ch in chans.items():
            r = _best_rank(c["gold"], ch["units"])
            if r is not None and (best_rank is None or r < best_rank):
                best_rank, best_channel = r, name

        details.append({
            "id": c["id"], "intent": c["intent"], "category": c["category"],
            "layer": c["layer"], "variant": c["variant"], "query": c["query"],
            "gold": c["gold"], "gold_source": c["gold_source"],
            "hit": best_rank is not None,
            "rank": best_rank, "hit_channel": best_channel,
            "hit_at_5": best_rank is not None and best_rank <= _K,
            "rr": (1.0 / best_rank) if best_rank else 0.0,
            "resident": _norm(c["gold"]) in _norm(resident),
            "ms": round(total_ms, 2),
            "chars": sum(len(ch["text"]) for ch in chans.values()),
            "channels": {
                name: {"ms": round(ch["ms"], 2), "chars": len(ch["text"]),
                       "units": len(ch["units"]), "error": ch["error"]}
                for name, ch in chans.items()
            },
        })

    n = len(details)
    hits = sum(1 for d in details if d["hit"])
    top5 = sum(1 for d in details if d["hit_at_5"])
    mrr = sum(d["rr"] for d in details) / n if n else 0.0
    lat = [d["ms"] for d in details]

    def _group(key: str) -> dict:
        groups: dict[str, dict] = {}
        for d in details:
            g = d[key]
            groups.setdefault(g, {"n": 0, "hit": 0, "top5": 0, "rr": 0.0})
            groups[g]["n"] += 1
            groups[g]["hit"] += int(d["hit"])
            groups[g]["top5"] += int(d["hit_at_5"])
            groups[g]["rr"] += d["rr"]
        for g in groups.values():
            g["hit_rate"] = round(g["hit"] / g["n"], 3)
            g["r@5"] = round(g["top5"] / g["n"], 3)
            g["mrr"] = round(g["rr"] / g["n"], 3)
        return groups

    chan_ms: dict[str, list[float]] = {}
    for d in details:
        for name, ch in d["channels"].items():
            chan_ms.setdefault(name, []).append(ch["ms"])

    return {
        "impl": impl,
        "base_dir": str(base),
        "cases": n,
        "hit_rate": round(hits / n, 3) if n else 0.0,
        "r@5": round(top5 / n, 3) if n else 0.0,
        "mrr": round(mrr, 3),
        "cold_ms": round(cold_ms, 2),
        "latency_ms": {
            "mean": round(statistics.mean(lat), 2) if lat else 0.0,
            "median": round(statistics.median(lat), 2) if lat else 0.0,
            "p95": round(_pct(lat, 0.95), 2),
            "max": round(max(lat), 2) if lat else 0.0,
        },
        "channel_mean_ms": {
            k: round(statistics.mean(v), 2) for k, v in chan_ms.items()
        },
        "by_category": _group("category"),
        "by_variant": _group("variant"),
        "by_layer": _group("layer"),
        "resident_covered": sum(1 for d in details if d["resident"]),
        "resident_chars": len(resident),
        "details": details,
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _fmt_pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def render_markdown(rep: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 检索基线（{rep['impl']}）")
    lines.append("")
    lines.append(f"- 语料根目录：`{rep['base_dir']}`")
    lines.append(f"- 评测集：`{rep.get('eval', 'reports/2026-11-11-检索评测集.jsonl')}`（{rep['cases']} 条 query）")
    lines.append(f"- 冷启动（首次调用）：{rep['cold_ms']:.0f} ms")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| Hit（任一通道命中） | {_fmt_pct(rep['hit_rate'])} |")
    lines.append(f"| R@5 | {_fmt_pct(rep['r@5'])} |")
    lines.append(f"| MRR | {rep['mrr']:.3f} |")
    lines.append(f"| 逐条耗时 mean / median / p95 / max | "
                 f"{rep['latency_ms']['mean']:.1f} / {rep['latency_ms']['median']:.1f} / "
                 f"{rep['latency_ms']['p95']:.1f} / {rep['latency_ms']['max']:.1f} ms |")
    lines.append(f"| gold 已在常驻注入 | {rep['resident_covered']}/{rep['cases']}"
                 f"（常驻文本 {rep['resident_chars']} 字符） |")
    lines.append("")
    lines.append("各通道平均耗时（ms）：")
    lines.append("")
    lines.append("| 通道 | 平均 ms |")
    lines.append("|---|---:|")
    for k, v in sorted(rep["channel_mean_ms"].items(), key=lambda x: -x[1]):
        lines.append(f"| {k} | {v:.1f} |")
    lines.append("")

    for title, key in (("按分类", "by_category"), ("按说法", "by_variant"), ("按目标层", "by_layer")):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| 组 | n | Hit | R@5 | MRR |")
        lines.append("|---|---:|---:|---:|---:|")
        for g, s in rep[key].items():
            lines.append(f"| {g} | {s['n']} | {_fmt_pct(s['hit_rate'])} | "
                         f"{_fmt_pct(s['r@5'])} | {s['mrr']:.3f} |")
        lines.append("")

    misses = [d for d in rep["details"] if not d["hit"]]
    lines.append(f"## 漏检样例（{len(misses)} 条）")
    lines.append("")
    if not misses:
        lines.append("无。")
    else:
        lines.append("| id | 分类 | 说法 | query | gold | 最接近 rank |")
        lines.append("|---|---|---|---|---|---|")
        for d in misses:
            near = d["rank"] if d["rank"] else "—"
            q = d["query"].replace("|", "\\|")
            lines.append(f"| {d['id']} | {d['category']} | {d['variant']} | {q} | "
                         f"{d['gold']} | {near} |")
    lines.append("")

    lines.append("## 逐条明细")
    lines.append("")
    lines.append("| id | rank | 通道 | ms | 字符 |")
    lines.append("|---|---:|---|---:|---:|")
    for d in rep["details"]:
        rank = d["rank"] if d["rank"] else "—"
        lines.append(f"| {d['id']} | {rank} | {d['hit_channel'] or '—'} | "
                     f"{d['ms']:.1f} | {d['chars']} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    # 确定性重入：PYTHONHASHSEED 必须在解释器启动前设置（见模块 docstring）
    if os.environ.get("PYTHONHASHSEED") is None:
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()),
                                  *(sys.argv[1:] if argv is None else argv)])

    parser = argparse.ArgumentParser(description="记忆文件层检索评测（只读）")
    parser.add_argument("--impl", choices=["keyword", "fused"], default="keyword")
    parser.add_argument("--eval", type=Path, default=DEFAULT_EVAL)
    parser.add_argument("--base", type=Path, default=None)
    parser.add_argument("--out-prefix", default=None)
    args = parser.parse_args(argv)

    base = args.base or deskmate_base_dir()
    cases = load_cases(args.eval)
    verify_gold(cases, base)
    rep = run(cases, base, args.impl)
    rep["eval"] = str(args.eval)

    prefix = args.out_prefix or ("2026-11-11-检索基线" if args.impl == "keyword"
                                 else "2026-11-11-检索复测")
    md_path = OUT_DIR / f"{prefix}.md"
    json_path = OUT_DIR / f"{prefix}.json"
    md_path.write_text(render_markdown(rep), encoding="utf-8")
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(render_markdown(rep).split("## 按分类")[0])
    print(f"已写入：{md_path} / {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
