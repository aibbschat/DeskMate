#!/usr/bin/env python3
"""清理 USER.md / MEMORY.md 里的 `## 自动复盘` 分节：一天一节 + 语义近重复合并。

## 为什么要有这个脚本

`after_action._append_to_global_memory` 的旧实现**每轮复盘都新建一个节头**，
且只做逐字去重 —— 实测 USER.md 三天攒出 **17 个节**（09-19×6 / 09-20×6 /
09-21×4）、占全文 60%，同一条规则被三天的不同措辞各写了一遍。

代码侧已改成「按日期 upsert + 语义近重复合并」（2026-09-21），
但**存量那 17 个节不会自己合上**，所以用这个脚本清一次。
它和运行时的合并走**同一个函数**（`after_action._compact_recap`），
因此结果一致、且天然幂等 —— 清完再跑一次只会打印"无需改动"。

## 安全契约（四步缺一不可）

1. **默认 dry-run**：不加 `--apply` 只打印计划（迁出/留下逐条原文），不写文件。
2. **备份**：`shutil.copy2` → `<file>.bak-<ts>`，路径打印出来。
3. **原子写**：同目录 `mkstemp` + `os.replace`，并显式还原原文件权限
   （mkstemp 默认 0600，会把 644 悄悄改成 600）。
4. **写后自校验**：手写分节**逐字节不变** / 复盘节数与条目数符合预期 /
   被合并掉的旧条目确实消失 / 保留的条目一条不少。任一条不过 → 从备份回滚。

手写分节（`## 基本信息` / `## 写作观` / `## 查资料铁律` …）是**画像本体**，
脚本按构造只切"复盘节所在的连续区间"，区间外逐字节拷贝。

用法::

    python scripts/cleanup_user_md_recap_sections.py            # dry-run
    python scripts/cleanup_user_md_recap_sections.py --apply
    python scripts/cleanup_user_md_recap_sections.py --file MEMORY.md --apply
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from deskmate.memory import after_action as A  # noqa: E402

DEFAULT_FILES = ("USER.md", "MEMORY.md")


def _base_dir() -> Path:
    return Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))


def _multiset_diff(before: list[tuple[str, str]], after: list[tuple[str, str]]):
    """按归一化文本做多重集差：返回 (被丢弃的, 保留的)。"""
    rest = list(after)
    dropped, kept = [], []
    for item in before:
        key = A._recap_norm(item[1])
        for i, cand in enumerate(rest):
            if A._recap_norm(cand[1]) == key:
                kept.append(item)
                rest.pop(i)
                break
        else:
            dropped.append(item)
    return dropped, kept


def _killer_of(entry: tuple[str, str], kept: list[tuple[str, str]]) -> str:
    """找出是哪一条（更新的说法）把这条顶掉的——只用于打印，便于人工复核。"""
    best, bs = None, -1.0
    for cand in kept:
        s = A._recap_sim(entry[1], cand[1])
        if s > bs:
            best, bs = cand, s
    return f"{best[0]}|{best[1][:60]}" if best and bs >= A._RECAP_DUP_SIM else "（无）"


def process(path: Path, apply: bool) -> int:
    print(f"\n{'=' * 78}\n{path}\n{'=' * 78}")
    if not path.is_file():
        print("  文件不存在，跳过。")
        return 0
    orig = path.read_text(encoding="utf-8")
    spans = A._recap_spans(orig)
    if not spans:
        print("  没有 `## 自动复盘` 分节，无需处理。")
        return 0

    new, stats = A._compact_recap(orig)
    before = A._recap_entries(orig)
    after = A._recap_entries(new)
    dropped, kept = _multiset_diff(before, after)

    print(
        f"  节: {stats['sections_before']} → {stats['sections_after']}   "
        f"条目: {len(before)} → {len(after)}（合并 {len(dropped)}）"
    )
    print(f"  字符: {len(orig)} → {len(new)}")

    # 手写前缀（第一个复盘节之前的一切）必须逐字节相同
    prefix_ok = orig[: spans[0][0]] == new[: A._recap_spans(new)[0][0]]
    print(f"  手写分节逐字节不变: {'✅' if prefix_ok else '❌'}")
    if not prefix_ok:
        print("  !! 手写部分被动过 —— 中止，不做任何写入。")
        return 2

    if not dropped:
        print("  无需改动（幂等：没有可合并的近重复条目）。")
        return 0

    print("\n  ── 将被合并掉的旧说法（保留较新的那条）──")
    for date, text in dropped:
        print(f"    [{date}] {text[:110]}")
        print(f"         ← 被更新的说法覆盖: {_killer_of((date, text), kept)}")

    if not apply:
        print("\n  （dry-run：未写入。加 --apply 执行）")
        return 0

    bak = path.with_name(path.name + ".bak-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    shutil.copy2(path, bak)
    print(f"\n  已备份: {bak}")

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.chmod(tmp_name, os.stat(path).st_mode & 0o7777)  # mkstemp 默认 0600
        os.replace(tmp_name, path)
    except OSError:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    # ---- 写后自校验 ----
    got = path.read_text(encoding="utf-8")
    problems: list[str] = []
    if got != new:
        problems.append("落盘内容与预期不一致")
    if got[: A._recap_spans(got)[0][0]] != orig[: spans[0][0]]:
        problems.append("手写分节被改动")
    if len(A._recap_spans(got)) != stats["sections_after"]:
        problems.append("复盘节数不符")
    got_entries = [A._recap_norm(e[1]) for e in A._recap_entries(got)]
    if sorted(got_entries) != sorted(A._recap_norm(e[1]) for e in after):
        problems.append("条目集合不符")
    for _d, t in dropped:
        if A._recap_norm(t) in got_entries:
            problems.append(f"该被合并的旧条目仍在: {t[:40]}")
    if (path.stat().st_mode & 0o7777) != (bak.stat().st_mode & 0o7777):
        problems.append("文件权限与备份不一致")

    if problems:
        shutil.copy2(bak, path)
        print("  ❌ 自校验未通过，已从备份回滚：")
        for p in problems:
            print("     -", p)
        return 3
    print("  ✅ 自校验通过（手写分节逐字节一致 / 节数·条目集合符合预期 / 权限一致）")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理全局记忆里的 `## 自动复盘` 分节")
    ap.add_argument("--file", action="append", default=None,
                    help="要处理的文件名（可重复；默认 USER.md 与 MEMORY.md）")
    ap.add_argument("--base-dir", default=None, help="记忆目录（默认 $DESKMATE_DIR 或 ~/.deskmate）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只 dry-run）")
    args = ap.parse_args(argv)

    base = Path(args.base_dir) if args.base_dir else _base_dir()
    files = args.file or list(DEFAULT_FILES)
    print(f"记忆目录: {base}   模式: {'APPLY' if args.apply else 'DRY-RUN'}")
    changed = 0
    for name in files:
        changed += process(base / name, args.apply)
    print("\n完成。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
