#!/usr/bin/env python3
"""一次性数据迁移（2026-09-21）：把 MEMORY.md「§ 工作 #work」里被 30 天 TTL
误判的**长效条目**迁到新增的「§ 长期事实 #stable」（永不衰减）。

背景
----
``MEMORY_DECAY_TTL`` 只把 ``work`` 定为 30 天，于是「§ 工作」里的运维配置、
协作模式、项目位置、硬规则、创作设定全都在 30 天后被归档/删除——而它们
既不是「进展」也不是「一次性信息」。其中 ``串书禁令（最高优先级）`` 一类
一旦丢失就是事故。

归属选择
--------
不迁到项目记忆 ``projects/<名>/``：项目记忆的注入依赖 ``active_project``，
而应用层从未调用 ``set_active_project``（只有 MCP memory server 能手动设），
迁过去等于**永久不可达**。故落在全局层的 ``#stable`` 分节。

安全保证
--------
- 默认 dry-run，只打印计划，不写任何文件
- ``--apply`` 先落 ``MEMORY.md.bak-<ts>`` 备份，再原子替换（tmp + os.replace）
- 写后自校验：迁移条目在目标分节恰好各出现一次、源分节不再包含、
  其余分节与原文件**字节完全一致**

用法
----
    env -u PYTHONPATH <venv>/bin/python scripts/migrate_memory_work_to_stable.py
    env -u PYTHONPATH <venv>/bin/python scripts/migrate_memory_work_to_stable.py --apply
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

MEM_PATH = Path(os.path.expanduser("~/.deskmate/MEMORY.md"))

SRC_HEADER = "§ 工作 #work #sensitive"
DST_HEADER = "§ 长期事实 #stable #sensitive"
SRC_PREFIX = "§ 工作"
DST_PREFIX = "§ 长期事实"
ANCHOR_PREFIX = "§ 系统"  # 新分节插到「§ 系统」之后

# 需要迁移的条目：唯一子串 → 目标行（None=原行原样搬）。
# 判读标准：价值是「长效事实 / 规则 / 设定 / 路径」才迁；只是「某天的进展
# 快照」的条目留在 #work 自然衰减。
MOVE_MARKERS: list[tuple[str, str | None]] = [
    ("短剧/剧本由专门公司负责", None),
    ("预设自动任务字数", "SPLIT_0814"),
    ("托管站点 ai.xs91.com", None),
    ("高付始终为御史台派驻监察御史", None),
    ("史料库约85个txt", None),
    ("《国子监秘闻录》项目位于", None),
    ("串书禁令（最高优先级）", None),
    ("写作营每日文章和素材补存到", None),
    ("小桔每天写一篇萧盛", None),
    ("太学人数2400（外舍2000", None),
    ("萧盛名下Agent：小桔", None),
    ("选题原则：主推伏笔设计", None),
    ("「视角半径」心法", None),
    ("萧盛写作背景素材：十五岁推倒重写", None),
    ("第四章结局定稿", None),
    ("弓箭手立法为元丰五年康识立法", None),
    ("第五章主题定为寒门压抑", None),
    ("每日读一本萧盛已出版作品", None),
    ("《大宋梦华》官职", None),
    ("第五章《忘川帖》反派设计", None),
    ("第五章设定：策论题=屯田", None),
    ("仿写《屯田策》骨架仿赵禼《营田议》", None),
    ("萧盛任命小桔为AI圈第1任敲钟人", None),
    ("AI圈技能包更新到 v0.22.0", None),
    ("MiniMax Code CLI 官方仓库", None),
    ("小桔记录经验：直连 GitHub/HF", None),
    ("萧盛推进第五章《忘川帖》", None),
]

SPLIT_MARKER = "预设自动任务字数"


def _split_0814(line: str) -> list[str]:
    """把 08-14 那条「自动任务字数 + 丁闻香口头禅」拆成两条独立事实。

    原条目把「运维配置」和「小说人物设定」塞在一行里，是分类错配的缩影。
    """
    m = re.match(r"^\s*[-*]?\s*(\d{4}-\d{1,2}-\d{1,2})[:：]\s*(.*)$", line)
    if not m:
        return [line]
    day, rest = m.group(1), m.group(2).strip()
    out: list[str] = []
    for part in [p.strip() for p in rest.split("；") if p.strip()]:
        if "口头禅" in part:
            part = part.rstrip("。") + "。（《国子监秘闻录》人物）"
        elif not part.endswith("。"):
            part = part + "。"
        out.append(f"- {day}: {part}")
    return out or [line]


def _section_span(text: str, prefix: str) -> tuple[int, int, int]:
    """定位以 prefix 开头的分节：(块起点, 正文起点, 正文终点)。

    正文终点 = 下一个 `^§ ` 的位置（即下一个分节头行的起始），
    因此 `text[:块起点]` 自带分隔空行、`text[正文终点:]` 直接以 `§` 开头。
    """
    pat = re.compile(r"(?m)^" + re.escape(prefix) + r"[^\n]*\n")
    m = pat.search(text)
    if not m:
        raise SystemExit(f"✗ 找不到分节：{prefix}")
    body_start = m.end()
    nxt = re.search(r"(?m)^§\s", text[body_start:])
    body_end = body_start + nxt.start() if nxt else len(text)
    return m.start(), body_start, body_end


def _entries(body: str) -> list[str]:
    """把分节正文拆成条目：每个非空行即一条（本文件条目均为单行）。"""
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _normalize(line: str) -> str:
    """统一成 `- ` 列表形式（源文件里有个别条目漏了前缀，顺手规范化，
    便于 `_ENTRY_DATE_RE` 逐行判定日期）。"""
    return line if re.match(r"^\s*[-*]\s", line) else "- " + line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()

    if not MEM_PATH.is_file():
        print(f"✗ 找不到 {MEM_PATH}")
        return 1

    original = MEM_PATH.read_text(encoding="utf-8")
    _s, work_body_start, work_body_end = _section_span(original, SRC_PREFIX)
    work_lines = _entries(original[work_body_start:work_body_end])

    dst_lines: list[str] = []
    keep: list[str] = []
    matched: set[str] = set()

    for line in work_lines:
        hit = next(
            (mk for mk, _ in MOVE_MARKERS if mk in line and mk not in matched), None
        )
        if hit is None:
            keep.append(line)
            continue
        matched.add(hit)
        rule = dict(MOVE_MARKERS)[hit]
        produced = _split_0814(line) if rule == "SPLIT_0814" else [line]
        dst_lines.extend(_normalize(t) for t in produced)

    missing = [mk for mk, _ in MOVE_MARKERS if mk not in matched]
    if missing:
        print("✗ 以下标记在「§ 工作」里没找到，已中止（避免误伤）:")
        for mk in missing:
            print(f"    - {mk}")
        return 2

    # ---- 组装新文件 ----------------------------------------------------
    # 注意顺序：先替换「工作」正文（位于锚点之后），再插入新分节（锚点处），
    # 锚点索引不受影响。
    new_work_body = "\n".join(keep) + "\n"
    text = (
        original[:work_body_start]
        + new_work_body
        + "\n"
        + original[work_body_end:]
    )

    _s2, _bs2, anchor_end = _section_span(text, ANCHOR_PREFIX)
    new_section = DST_HEADER + "\n" + "\n".join(dst_lines) + "\n"
    # text[:anchor_end] 以分隔空行结尾 → 直接接新分节；新分节后补一个空行再接 § 工作
    text = text[:anchor_end] + new_section + "\n" + text[anchor_end:]

    # ---- 计划预览 ------------------------------------------------------
    print(f"文件：{MEM_PATH}")
    print(f"原字符数 {len(original)} → 新 {len(text)}")
    print(f"\n=== 迁往「{DST_HEADER}」共 {len(dst_lines)} 条（永不衰减）===")
    for line in dst_lines:
        print("  →", line[:90])
    print(f"\n=== 留在「{SRC_HEADER}」共 {len(keep)} 条（继续 30 天 TTL）===")
    for line in keep:
        print("  ·", line[:90])

    if not args.apply:
        print("\n[dry-run] 未写入任何文件。加 --apply 执行。")
        return 0

    # ---- 备份 + 原子写 --------------------------------------------------
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = MEM_PATH.with_name(f"MEMORY.md.bak-{ts}")
    shutil.copy2(MEM_PATH, bak)
    print(f"\n备份：{bak}")

    fd, tmp_name = tempfile.mkstemp(dir=str(MEM_PATH.parent), prefix=".MEMORY.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # mkstemp 默认 0600，会把原文件的权限悄悄改掉 → 显式还原
        os.chmod(tmp_name, os.stat(MEM_PATH).st_mode & 0o7777)
        os.replace(tmp_name, MEM_PATH)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # ---- 写后自校验 ----------------------------------------------------
    after = MEM_PATH.read_text(encoding="utf-8")
    problems: list[str] = []

    _s3, ws3, we3 = _section_span(after, DST_PREFIX)
    after_dst = _entries(after[ws3:we3])
    _s4, ws4, we4 = _section_span(after, SRC_PREFIX)
    after_src = _entries(after[ws4:we4])

    for line in dst_lines:
        if after_dst.count(line) != 1:
            problems.append(f"目标分节该条出现 {after_dst.count(line)} 次（应 1）：{line[:60]}")
    for line in keep:
        if line not in after_src:
            problems.append(f"源分节丢了本该保留的条目：{line[:60]}")
    for mk, _ in MOVE_MARKERS:
        if any(mk in ln for ln in after_src):
            problems.append(f"源分节仍残留：{mk}")

    def blocks(txt: str) -> dict[str, str]:
        return {b.split("\n", 1)[0]: b for b in re.split(r"(?m)^(?=§\s)", txt)}

    before_blocks, after_blocks = blocks(original), blocks(after)
    for head, blk in before_blocks.items():
        if head in (SRC_HEADER, DST_HEADER):
            continue
        if after_blocks.get(head) != blk:
            problems.append(f"未迁移的分节被改动了：{head}")
    for head in after_blocks:
        if head not in before_blocks and head != DST_HEADER:
            problems.append(f"多出一个意料之外的分节：{head}")

    if problems:
        print("\n✗ 自校验失败：")
        for p in problems:
            print("   -", p)
        print(f"   备份仍在 {bak}，可人工回滚。")
        return 3

    print("✓ 自校验通过：迁移条目各出现一次、源分节已清空、其余分节字节未变、无多余分节")
    print(f"✓ 新字符数 {len(after)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
