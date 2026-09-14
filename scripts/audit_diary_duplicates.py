"""只读诊断：小桔日记有没有「一条写成两条」（并发重入写出的重复）。

## 背景

写一则日记是**无锁的「读-改-写」**：数文件小节算 seq → 调 LLM 渲染（5~25 秒）
→ 追加落盘。三个入口（后台 tick / GUI「生成今日日记」按钮 / 独立 proactivity
MCP 进程）能在 LLM 那段窗口里重叠，各自读到同一份状态、算出**同一个 seq**、
拿**同一批素材**，各写一条。2026-09-14 一天撞了三对（10:04 / 10:48 / 18:31），
文件里 10 则而账本只记 9 则；历史 09-05 / 09-08 / 09-09 / 09-12 也都有。

**判据**：同一 HH:MM 出现两次以上、**且内容高度重叠**（前 24 字相同，或重合度
≥0.35）。三计数只是线索——观察日记只占 seq 不消费素材账本，也会让计数对不齐。

修复（2026-09-14）：写入统一走 ``diary_inspiration.write_entry_once``——跨进程
``fcntl.flock`` + 锁内重取 pending 与传入素材取交集（幂等）；临时文件名加
pid+线程号（``growth/_atomic.unique_tmp``）。

## 用法

    env -u PYTHONPATH .venv/bin/python scripts/audit_diary_duplicates.py
    env -u PYTHONPATH .venv/bin/python scripts/audit_diary_duplicates.py 2026-09-14

**只读**，不改任何文件。发现疑似重复后，人工判断留哪条，再用逐 eid 移除的方式
清理（先备份）。
"""

from __future__ import annotations

import collections
import difflib
import json
import re
import sys
from pathlib import Path

DIARY_DIR = Path.home() / ".deskmate" / "diary"
MATERIAL_DIR = DIARY_DIR / "materials"


def _entries(text: str) -> list[tuple[str, str, str]]:
    """→ [(HH:MM, eid, body)]，按文件顺序。"""
    out: list[tuple[str, str, str]] = []
    for seg in re.split(r"(?m)^(?=## )", text):
        m = re.match(r"## (\d{2}:\d{2})\s*(?:<!--xj:([^>]+)-->)?", seg)
        if not m:
            continue
        body = "\n".join(seg.splitlines()[1:]).strip()
        out.append((m.group(1), m.group(2) or "-", body))
    return out


def _overlap(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a[:400], b[:400]).ratio()


def report_counts(date: str) -> None:
    """三个计数：文件小节数 / 账本 entries / 素材消费序号集合。

    ⚠ 只能当**线索**、不能当判据：观察日记（``append_observation_natural``）
    只占 seq 而**不消费素材账本**，历史清理也会留差值——两者都造成不一致。
    真正的判据是 :func:`report_duplicates`。
    """
    md = DIARY_DIR / f"{date}.md"
    if not md.is_file():
        return
    n_entries = len(re.findall(r"(?m)^## ", md.read_text(encoding="utf-8")))

    meta_path = MATERIAL_DIR / f"{date}.meta.json"
    recorded = "-"
    if meta_path.is_file():
        try:
            recorded = json.loads(meta_path.read_text(encoding="utf-8")).get("entries", "-")
        except (OSError, ValueError):
            pass

    used: list[int] = []
    mpath = MATERIAL_DIR / f"{date}.jsonl"
    if mpath.is_file():
        for line in mpath.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line).get("entry")
            except ValueError:
                continue
            if e:
                used.append(int(e))

    consistent = (recorded == n_entries) if isinstance(recorded, int) else None
    mark = "" if consistent is not False else "   ← 有差值"
    print(
        f"[{date}] 文件 {n_entries} 则 / 账本 entries {recorded} / "
        f"素材消费序号 {sorted(set(used))}{mark}"
    )


def report_duplicates(date: str | None = None) -> int:
    """同 HH:MM 且内容高度重叠 → 疑似重复对。返回疑似对数。"""
    files = [DIARY_DIR / f"{date}.md"] if date else sorted(DIARY_DIR.glob("*.md"))
    total = 0
    for md in files:
        if not md.is_file():
            continue
        by_time: dict[str, list[tuple[str, str]]] = {}
        for hhmm, eid, body in _entries(md.read_text(encoding="utf-8")):
            by_time.setdefault(hhmm, []).append((eid, body))
        for hhmm, group in by_time.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    a, b = group[i], group[j]
                    ratio = _overlap(a[1], b[1])
                    same_head = a[1][:24] == b[1][:24]
                    if ratio >= 0.35 or same_head:
                        total += 1
                        print(f"  {md.name} {hhmm}  {a[0]} ↔ {b[0]}  重合度 {ratio:.2f}")
    return total


def main() -> int:
    date = sys.argv[1] if len(sys.argv) > 1 else None
    print("=== 参考信号：三计数 ===")
    print(
        "⚠ 不一致**不等于**并发，只能当线索：\n"
        "  - 观察日记（append_observation_natural）只占 seq、**不消费素材账本**，\n"
        "    天然会让「文件则数 / 账本 entries / 素材序号」三者对不齐；\n"
        "  - 历史清理（移除重复小节）之后也会留差值。\n"
        "  **判据在下一段：同一 HH:MM 且内容重叠才算重复。**"
    )
    print()
    if date:
        report_counts(date)
    else:
        for md in sorted(DIARY_DIR.glob("*.md")):
            report_counts(md.stem)

    print("\n=== 疑似重复对（同 HH:MM 且内容重叠）—— 这一段才是判据 ===")
    n = report_duplicates(date)
    print(f"\n合计疑似重复对: {n}" if n else "\n未发现疑似重复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
