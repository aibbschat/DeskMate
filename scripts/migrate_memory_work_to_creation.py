#!/usr/bin/env python3
"""存量归位（2026-09-26）：把 MEMORY.md「§ 工作 #work」里的**书稿记忆**迁到
「§ 创作 #stable」（永不衰减）。

背景
----
「§ 工作」的标签是 `#work` → `MEMORY_DECAY_TTL["work"] = 30`，超过 30 天归档到
vault、不再进常驻上下文。而这一节里堆的几乎全是《国子监秘闻录》的书稿记忆
（章节进度、人物设定、伏笔状态）——它们是**创作事实**，不是「一时的工作进展」。

为什么不用内容判据自动归类
--------------------------
2026-09-26 用真实语料定标，结论是做不到：

- 「按书名分节」取《》出现最多者 → 取到「屯田策」「忘川帖」（章节名比书名出现
  频繁），整批归错书；
- 39 条里 **33 条压根没写书名**（「章太聪革职被捕」「越民授权狱中逼供」这类
  纯散文），任何内容判据都抓不住；
- 运行时的 `_is_creation_note()` 只能认出 8 条。

所以存量改走**分节级 + 显式豁免**，而不是逐条猜：

    迁 = 「§ 工作」的全部条目 − 命中运营信号的条目

「运营信号」（`_OPERATION_SIGNAL_RE`：写作营/日报/文章/广场/敲钟/技能包/擂台/
辩题/发帖/选题/知识库/目录树/MCP/AI圈）是这套归位里唯一可靠的判据——**因为
运营流水账有专属词表，而书稿内容是开放散文**。豁免项会在 dry-run 里逐条列出，
供人工过目。

安全保证
--------
- 默认 dry-run，只打印计划，不写任何文件
- ``--apply`` 先落 ``MEMORY.md.bak-<ts>`` 备份，再原子替换（tmp + os.replace）
- 写后自校验：迁移条目在目标分节恰好各出现一次、源分节不再包含、
  其余分节与原文件**字节完全一致**、无多余分节

用法
----
    env -u PYTHONPATH <venv>/bin/python scripts/migrate_memory_work_to_creation.py
    env -u PYTHONPATH <venv>/bin/python scripts/migrate_memory_work_to_creation.py --apply
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from deskmate.memory.manager import (  # noqa: E402
    CREATION_SECTION,
    _OPERATION_SIGNAL_RE,
)

MEM_PATH = Path(os.path.expanduser("~/.deskmate/MEMORY.md"))

SRC_PREFIX = "§ 工作"
DST_HEADER = f"§ {CREATION_SECTION} #stable #sensitive"
DST_PREFIX = f"§ {CREATION_SECTION}"
ANCHOR_PREFIX = "§ 长期事实"  # 新分节插到「§ 长期事实」之后


def _section_span(text: str, prefix: str) -> tuple[int, int, int]:
    """定位以 prefix 开头的分节：(块起点, 正文起点, 正文终点)。"""
    pat = re.compile(r"(?m)^" + re.escape(prefix) + r"[^\n]*\n")
    m = pat.search(text)
    if not m:
        raise SystemExit(f"✗ 找不到分节：{prefix}")
    body_start = m.end()
    nxt = re.search(r"(?m)^§\s", text[body_start:])
    body_end = body_start + nxt.start() if nxt else len(text)
    return m.start(), body_start, body_end


def _has_section(text: str, prefix: str) -> bool:
    return re.search(r"(?m)^" + re.escape(prefix) + r"[^\n]*\n", text) is not None


def _entries(body: str) -> list[str]:
    """分节正文 → 条目列表（本文件条目均为单行，空行分隔）。"""
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _normalize(line: str) -> str:
    """统一成 `- ` 列表形式（源里有的漏了前缀，顺手规范化便于逐行判日期）。"""
    return line if re.match(r"^\s*[-*]\s", line) else "- " + line


def plan(mem_text: str) -> tuple[list[str], list[str]]:
    """返回 (要迁的条目, 豁免留原地的条目)。纯函数，便于测试。"""
    _s, bs, be = _section_span(mem_text, SRC_PREFIX)
    move: list[str] = []
    keep: list[str] = []
    for line in _entries(mem_text[bs:be]):
        (keep if _OPERATION_SIGNAL_RE.search(line) else move).append(line)
    return move, keep


def build(mem_text: str) -> tuple[str, list[str], list[str]]:
    """按「§ 工作 全部条目 − 运营信号条目」构造新全文。

    返回 (新全文, 迁移条目, 豁免条目)。纯函数，便于测试。
    """
    move_raw, keep = plan(mem_text)
    move = [_normalize(t) for t in move_raw]

    _s, bs, be = _section_span(mem_text, SRC_PREFIX)
    text = mem_text[:bs] + ("\n".join(keep) + "\n" if keep else "") + "\n" + mem_text[be:]

    if _has_section(text, DST_PREFIX):
        # 已存在 § 创作 → 追加到该分节末尾，不新建
        _s2, bs2, be2 = _section_span(text, DST_PREFIX)
        existing = text[bs2:be2].rstrip("\n")
        text = text[:bs2] + existing + "\n\n" + "\n".join(move) + "\n\n" + text[be2:]
    else:
        _s2, _bs2, anchor_end = _section_span(text, ANCHOR_PREFIX)
        new_section = DST_HEADER + "\n" + "\n".join(move) + "\n"
        text = text[:anchor_end] + new_section + "\n" + text[anchor_end:]
    return text, move, keep


def _report(move: list[str], keep: list[str], before: int, after: int) -> None:
    print(f"文件：{MEM_PATH}")
    print(f"原字符数 {before} → 新 {after}")
    print(f"\n=== 迁往「{DST_HEADER}」共 {len(move)} 条（永不衰减）===")
    for line in move:
        print("  →", line[:96])
    print(f"\n=== 豁免留在「{SRC_PREFIX}」共 {len(keep)} 条（继续 30 天 TTL）===")
    for line in keep:
        print("  ·", line[:96])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()

    if not MEM_PATH.is_file():
        print(f"✗ 找不到 {MEM_PATH}")
        return 1

    if not args.apply:
        original = MEM_PATH.read_text(encoding="utf-8")
        move_raw, keep = plan(original)
        if not move_raw:
            print("没有可迁移的条目（§ 工作 为空，或全部命中运营信号）。")
            return 0
        text, move, keep = build(original)
        _report(move, keep, len(original), len(text))
        print("\n[dry-run] 未写入任何文件。加 --apply 执行。")
        return 0

    # ⚠ 读-改-写必须整段在锁内：MEMORY.md 有多个并发写入口
    #   （对话轮内管理器方法 / after_action 后台线程 / 定时 run_dream），
    #   锁外读、锁内写会把并发期间的新记忆覆盖掉。
    #   与 MemoryManager._save_global_memory 用同一把锁（同目录同 stem 的 .wlock）。
    from deskmate.memory._fs_lock import file_write_lock

    with file_write_lock(MEM_PATH):
        original = MEM_PATH.read_text(encoding="utf-8")
        move_raw, keep = plan(original)
        if not move_raw:
            print("没有可迁移的条目（§ 工作 为空，或全部命中运营信号）。")
            return 0
        text, move, keep = build(original)

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = MEM_PATH.with_name(f"MEMORY.md.bak-{ts}")
        shutil.copy2(MEM_PATH, bak)

        fd, tmp_name = tempfile.mkstemp(dir=str(MEM_PATH.parent),
                                        prefix=".MEMORY.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            # mkstemp 默认 0600，会把原文件权限悄悄改掉 → 显式还原
            os.chmod(tmp_name, os.stat(MEM_PATH).st_mode & 0o7777)
            os.replace(tmp_name, MEM_PATH)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    _report(move, keep, len(original), len(text))
    print(f"\n备份：{bak}")

    # ---- 写后自校验 ----------------------------------------------------
    after = MEM_PATH.read_text(encoding="utf-8")
    problems: list[str] = []

    _s3, ws3, we3 = _section_span(after, DST_PREFIX)
    after_dst = _entries(after[ws3:we3])
    _s4, ws4, we4 = _section_span(after, SRC_PREFIX)
    after_src = _entries(after[ws4:we4])

    for line in move:
        if after_dst.count(line) != 1:
            problems.append(f"目标分节该条出现 {after_dst.count(line)} 次（应 1）：{line[:60]}")
    for line in keep:
        if line not in after_src:
            problems.append(f"源分节丢了本该保留的条目：{line[:60]}")
    for line in move:
        if line in after_src:
            problems.append(f"源分节仍残留已迁移条目：{line[:60]}")

    def blocks(txt: str) -> dict[str, str]:
        return {b.split("\n", 1)[0]: b for b in re.split(r"(?m)^(?=§\s)", txt)}

    before_blocks, after_blocks = blocks(original), blocks(after)
    for head, blk in before_blocks.items():
        if head.startswith(SRC_PREFIX) or head.startswith(DST_PREFIX):
            continue
        if after_blocks.get(head) != blk:
            problems.append(f"未迁移的分节被改动了：{head}")
    for head in after_blocks:
        if head not in before_blocks and not head.startswith(DST_PREFIX):
            problems.append(f"多出一个意料之外的分节：{head}")

    if problems:
        print("\n✗ 自校验失败：")
        for p in problems:
            print("   -", p)
        print(f"   备份仍在 {bak}，可人工回滚。")
        return 3

    print("✓ 自校验通过：迁移条目各出现一次、源分节已清空对应条目、"
          "其余分节字节未变、无多余分节")
    print(f"✓ 新字符数 {len(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
