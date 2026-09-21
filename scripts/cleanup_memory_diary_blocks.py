#!/usr/bin/env python3
"""清理 MEMORY.md 里的「§ 日记沉淀」重复块（一次性数据修复，幂等）。

背景（2026-09-21 诊断）
----------------------
``memory/manager._load_global_memory()`` 会把 MEMORY.md **拼接**上
``diary_memory.md``（DiaryStore 导出的检索视图）供 prefetch 检索，而
``_save_global_memory()`` 只写 MEMORY.md —— 任何
``load → 解析 → 渲染 → 保存`` 的路径（dream 每天跑一次、append_to_section、
衰减、压缩）都会把视图内容**固化**进 MEMORY.md，每次落笔 +1 份。
实测累积到 3 份，占文件 52%，且 dream 会把日记当普通事实合并（19→18 条）。

代码侧已断源（``_strip_diary_sections`` + dream ``_PROTECTED_SECTIONS``），
本脚本负责**清存量**。

处置策略（逐块数据驱动，不按位置硬编码）
----------------------------------------
对每个「§ 日记沉淀」块：

- 内容与 ``diary_memory.md`` 视图一致（且能在 SQLite loved_entries 找到）
  → **冗余副本，删除**（事实源在 SQLite，删后检索链路不受影响）
- 否则 → **唯一副本，改名保留**为 ``§ 日记主题 #diary-summary``
  （永不衰减、不受断源剥离影响，仍进全局检索）

用法
----
    python scripts/cleanup_memory_diary_blocks.py            # dry-run（默认）
    python scripts/cleanup_memory_diary_blocks.py --apply    # 执行（备份+原子写+自校验）
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

BASE = Path(os.path.expanduser("~/.deskmate"))
MEM = BASE / "MEMORY.md"
VIEW = BASE / "diary_memory.md"
DB = BASE / "deskmate.db"

# 日记沉淀块：从「§ 日记沉淀」行首起，吃到下一个 § 分节之前（含块尾空行）
_BLOCK_RE = re.compile(r"(?m)^§\s*日记沉淀\b[^\n]*(?:\n(?!§ ).*)*")
_HEADER_RE = re.compile(r"(?m)^§\s*日记沉淀\b[^\n]*$")

RENAME_TITLE = "日记主题"
# 用单一 `#diary`：`#(\w+)` 遇到 `-` 会截断，写 `#diary-summary` 会让分节 id
# 变成「日记主题 -summary」（脏 id，会进 traces/references 的 section 字段）。
RENAME_TAGS = "#diary"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def norm_lines(text: str) -> list[str]:
    """规范化正文为「非空行序列」，用于忽略空行差异的内容比对。"""
    return [l.strip() for l in text.splitlines() if l.strip()]


def block_body(block: str) -> str:
    """取块正文（去掉 header 行）。"""
    lines = block.split("\n", 1)
    return lines[1] if len(lines) > 1 else ""


def view_body() -> str:
    """diary_memory.md 视图正文（去掉 header 行）。"""
    if not VIEW.is_file():
        return ""
    txt = VIEW.read_text(encoding="utf-8")
    return txt.split("\n", 1)[1] if "\n" in txt else ""


def db_contents() -> set[str]:
    """loved_entries 里的日记原文集合（归一化后）。"""
    if not DB.is_file():
        return set()
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute("SELECT content FROM loved_entries").fetchall()
        con.close()
    except Exception:
        return set()

    def k(s: str) -> str:
        s = re.sub(r"^[-*]?\s*", "", s)
        s = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}\s*[:：]\s*", "", s)
        return s.replace("🩷", "").replace(" ", "").strip()[:40]

    return {k(r[0]) for r in rows if r and r[0]}


def parse_section_counts(text: str) -> dict[str, int]:
    """各分节（header_rest[:26] → 条目行数）。注意同名分节会被合并，此处只作概览。"""
    out: dict[str, int] = {}
    parts = re.split(r"(?m)^§\s*", text)
    for part in parts[1:]:
        if not part.strip():
            continue
        lines = part.split("\n", 1)
        head = lines[0].strip()
        if "日记沉淀" in head:
            continue
        body = lines[1] if len(lines) > 1 else ""
        n = len([l for l in body.splitlines() if l.strip()])
        key = head[:26]
        out[key] = out.get(key, 0) + n
    return out


def atomic_write(path: Path, text: str) -> None:
    """原子写并保留原文件权限（mkstemp 默认 600，会改掉 umask 结果）。"""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backup(path: Path) -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{ts}")
    bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return bak


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="清理 MEMORY.md 日记重复块")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认仅 dry-run）")
    args = ap.parse_args()

    if not MEM.is_file():
        print(f"✗ 找不到 {MEM}")
        return 1

    raw = MEM.read_text(encoding="utf-8")
    matches = list(_BLOCK_RE.finditer(raw))

    print(f"MEMORY.md : {len(raw)} 字符 / {raw.count('§ 日记沉淀')} 个日记块")
    print(f"视图      : {VIEW}  ({len(view_body())} 字符正文)")
    print(f"库        : {DB}  ({len(db_contents())} 条 loved_entries)\n")

    if not matches:
        print("✓ 无「§ 日记沉淀」块，无需处理（幂等）")
        return 0

    vlines = norm_lines(view_body())
    dbkeys = db_contents()

    # ---- 分类 ----
    plan: list[tuple[str, int, int, str, list[str]]] = []  # (action, start, end, text, 摘要)
    for i, m in enumerate(matches, 1):
        block = m.group(0)
        blines = norm_lines(block_body(block))
        same_as_view = bool(blines) and blines == vlines
        in_db = 0
        for l in blines:
            key = re.sub(r"^[-*]?\s*", "", l)
            key = re.sub(r"^\d{4}-\d{1,2}-\d{1,2}\s*[:：]\s*", "", key)
            key = key.replace("🩷", "").replace(" ", "").strip()[:40]
            if key in dbkeys:
                in_db += 1

        if same_as_view and in_db == len(blines):
            action = "delete"
        elif same_as_view:
            action = "delete"  # 内容与视图一致即可删；库覆盖仅作旁证
        else:
            action = "rename"
        plan.append((action, m.start(), m.end(), block, blines))

    print("=== 逐块判定 ===")
    for i, (act, s, e, block, blines) in enumerate(plan, 1):
        tag = "删除（与视图一致）" if act == "delete" else "改名保留（唯一副本）"
        print(f"  块#{i}  偏移 {s}-{e}  {len(blines)} 条  → {tag}")
        for l in blines[:2]:
            print(f"        {l[:88]}")
        if len(blines) > 2:
            print(f"        … 共 {len(blines)} 条")

    # ---- 生成新文本（从后往前替换，避免 span 失效）----
    text = raw
    renamed = 0
    deleted = 0
    for act, s, e, block, blines in sorted(plan, key=lambda x: -x[1]):
        if act == "delete":
            # 连同块尾空行一起删，避免留下多段空行
            end = e
            while end < len(text) and text[end] == "\n":
                end += 1
            text = text[:s] + text[end:]
            deleted += 1
        else:
            # 直接替换首行 header（不走 re.sub，避免新串里的反斜杠被当转义）
            parts = block.split("\n", 1)
            new_header = f"§ {RENAME_TITLE} {RENAME_TAGS}"
            new_block = new_header + ("\n" + parts[1] if len(parts) > 1 else "")
            text = text[:s] + new_block + text[e:]
            renamed += 1

    text = re.sub(r"\n{3,}", "\n\n", text)
    if text and not text.endswith("\n"):
        text += "\n"

    # ---- 自校验 ----
    print("\n=== 自校验 ===")
    ok = True

    n_diary = text.count("§ 日记沉淀")
    print(f"  残留日记沉淀块: {n_diary}  {'✓' if n_diary == 0 else '✗'}")
    ok &= n_diary == 0

    before, after = parse_section_counts(raw), parse_section_counts(text)
    changed = [k for k in before if before[k] != after.get(k)]
    print(f"  非日记分节条目数变化: {len(changed)} 处  {'✓' if not changed else '✗ ' + str(changed)}")
    ok &= not changed

    lost = [k for k in before if k not in after]
    print(f"  非日记分节丢失: {lost if lost else '无'}  {'✓' if not lost else '✗'}")
    ok &= not lost

    # 改名块内容逐行保留
    for act, s, e, block, blines in plan:
        if act != "rename":
            continue
        m2 = re.search(r"(?m)^§\s*日记主题\b[^\n]*(?:\n(?!§ ).*)*", text)
        if not m2:
            print(f"  ✗ 改名块未找到（{RENAME_TITLE}）")
            ok = False
            break
        kept = norm_lines(block_body(m2.group(0)))
        same = kept == blines
        print(f"  改名块条目 {len(blines)} → {len(kept)} 逐行一致: {same}  {'✓' if same else '✗'}")
        ok &= same

    for key in ("串书禁令", "托管站点 ai.xs91.com", "丁闻香口头禅", "人生里程碑"):
        hit = key in text
        print(f"  关键内容 {key}: {'✓' if hit else '✗ 丢失!!'}")
        ok &= hit

    print(f"\n  字符数 {len(raw)} → {len(text)}（净减 {len(raw)-len(text)}，"
          f"{(len(raw)-len(text))/len(raw)*100:.0f}%）")
    print(f"  删除 {deleted} 块 / 改名 {renamed} 块")
    print(f"  自校验总判定: {'✓ 通过' if ok else '✗ 未通过'}")

    if not args.apply:
        print("\n[dry-run] 未写盘。加 --apply 执行。")
        return 0 if ok else 2

    if not ok:
        print("\n✗ 自校验未通过，拒绝写盘。")
        return 2

    bak = backup(MEM)
    atomic_write(MEM, text)
    print(f"\n✓ 已写入（备份 {bak.name}，{os.path.getsize(MEM)} 字节，"
          f"权限 {oct(stat.S_IMODE(os.stat(MEM).st_mode))}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
