#!/usr/bin/env python3
"""每日进度跟踪：新增正文**确定性落盘** + 原文引文门禁。

背景（2026-11-05 事故）
------------------------
定时任务要求「把新增正文落成纯文本，剥掉 paraId，一个字不改写」，但执行时
LLM 用 ``write_file`` 凭上下文**复述**了 3518 字，产生 92 处漂移：
「第十一章：烧粮仓」→「第三章」、「连栋」→「连栎」、「地狱」→「地狑」、
「贩夫走卒」→「赀夫走卒」、「过道」→「甬道」，还有「倒也不是不行」被写成
「倒也是不行」（语义反了）。漂移稿随后喂进文风桶、写进四份圣典与简报。
而「乱码终检」只查 HTML 标签 / 英文残留 / 控制字符，**查不出合法汉字互换**，
所以一路报 CLEAN。

根治原则：**逐字保真这一环从模型手里拿走**。本脚本只做确定性字符串变换，
模型负责判断与写作，绝不负责誊抄。

用法
----
1) 工具输出直接落盘（模型不参与誊抄）::

     officecli view "x.docx" text --start 1385 --end 1438 > /tmp/yns_raw.txt
     python3 scripts/progress_feed.py extract \
       --src /tmp/yns_raw.txt \
       --out ".../style_feeds/新增正文_2026-11-05.txt"

   必须看到 ``OK extract: ... byte-identical`` 才允许继续；没有这行就当落盘失败。

2) 复核一份已经落盘的 txt（次日回查 / 事故排查）::

     python3 scripts/progress_feed.py verify-file \
       --src /tmp/yns_raw.txt --file ".../新增正文_2026-11-05.txt"

3) 简报 / 圣典落笔前，校验所有「原文」引文（章名、专名、器物、原句）::

     python3 scripts/progress_feed.py verify-quotes --src /tmp/yns_raw.txt \
       --quote "第十一章：烧粮仓" --quote "仓廒连栋成片" \
       --quotes-file /tmp/quotes.txt

   任何一条在原始输出里精确命中不了，就非零退出 —— 这条能直接挡住假章名。

所有失败路径均 exit != 0，便于任务指令用 ``&&`` 串成硬门禁。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# officecli view text 的段落行形如：
#   [/body/p[@paraId=55764EF1]] 第十一章：烧粮仓
# 兼容历史变体 [/word/body/p...] 与无 paraId 标记。
_PARA_RE = re.compile(
    r"^\[/(?:word/)?body/p(?:\[@paraId=[^\]]*\])?\]\s?(.*)$"
)

_OK_EXTRACT = "OK extract:"
_OK_FILE = "OK verify-file:"
_OK_QUOTES = "OK quotes:"


def strip_para_markers(raw: str) -> tuple[str, list[tuple[int, str]]]:
    """把 officecli 原始输出转成纯正文，返回 (文本, 未识别行)。

    只做前缀剥离，**不改写任何一个字符**；未识别的非空行一律报出来，
    避免工具输出格式变化时静默丢内容。
    """
    lines: list[str] = []
    unmatched: list[tuple[int, str]] = []
    for n, line in enumerate(raw.splitlines(), 1):
        m = _PARA_RE.match(line)
        if m:
            lines.append(m.group(1))
        elif not line.strip():
            # 原始输出里真正的空行（无标记）保留为空段
            lines.append("")
        else:
            unmatched.append((n, line[:120]))
    text = "\n".join(lines)
    if lines:
        text += "\n"
    return text, unmatched


def _read_src(path: str, start_line: int | None, end_line: int | None) -> str:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    if start_line is not None or end_line is not None:
        # 显式校验，别用 `x or 1` —— 0 会被当成缺省值悄悄放行（测试锁住）
        if start_line is not None and start_line < 1:
            raise ValueError(f"起始行必须 >= 1，收到 {start_line}")
        if end_line is not None and end_line < 1:
            raise ValueError(f"结束行必须 >= 1，收到 {end_line}")
        rows = raw.splitlines(keepends=True)
        a = (start_line if start_line is not None else 1) - 1
        b = end_line if end_line is not None else len(rows)
        if a < 0 or b > len(rows) or a >= b:
            raise ValueError(f"行号范围非法：{a + 1}..{b}（源共 {len(rows)} 行）")
        raw = "".join(rows[a:b])
    return raw


def _atomic_write(path: Path, text: str) -> None:
    """原子写：同目录唯一临时名 + os.replace，中途失败不留半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _first_diff(a: str, b: str) -> str:
    """给出首个差异的可读定位，便于人工排查。"""
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i] != b[i]:
            return (
                f"首个差异 @{i}: 源={a[max(0, i - 12):i + 12]!r} "
                f"目标={b[max(0, i - 12):i + 12]!r}"
            )
    return f"长度不同：源 {len(a)} 字 / 目标 {len(b)} 字（前缀一致）"


def _plain_text(src: str, start_line: int | None, end_line: int | None,
                already_plain: bool) -> tuple[str, list[tuple[int, str]]]:
    raw = _read_src(src, start_line, end_line)
    if already_plain:
        return raw, []
    return strip_para_markers(raw)


def cmd_extract(args: argparse.Namespace) -> int:
    text, unmatched = strip_para_markers(
        _read_src(args.src, args.start_line, args.end_line)
    )
    if unmatched:
        print(f"FAIL extract: {len(unmatched)} 行不符合 officecli 段落格式，拒绝落盘",
              file=sys.stderr)
        for n, sample in unmatched[:5]:
            print(f"  第{n}行: {sample}", file=sys.stderr)
        return 2
    if not text.strip():
        print("FAIL extract: 剥标记后正文为空，拒绝落盘", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser()
    _atomic_write(out, text)
    # 回读逐字自校验：写盘内容与期望文本必须完全相等
    written = out.read_text(encoding="utf-8")
    if written != text:
        print(f"FAIL extract: 回读不一致 {_first_diff(text, written)}", file=sys.stderr)
        return 3
    print(f"{_OK_EXTRACT} {len(text)} chars, {len(text.splitlines())} paragraphs, "
          f"byte-identical -> {out}")
    if len(text.strip()) < 500:
        print("NOTE extract: 正文不足 500 字，喂桶脚本会自动跳过（不影响落盘）")
    return 0


def cmd_verify_file(args: argparse.Namespace) -> int:
    text, unmatched = _plain_text(args.src, args.start_line, args.end_line,
                                  args.plain)
    if unmatched:
        print(f"FAIL verify-file: 源输出有 {len(unmatched)} 行格式异常", file=sys.stderr)
        return 2
    target = Path(args.file).expanduser().read_text(encoding="utf-8")
    if target == text:
        print(f"{_OK_FILE} {args.file} 与源输出逐字一致（{len(text)} chars）")
        return 0
    print(f"FAIL verify-file: 落盘文件与源输出不一致 —— {_first_diff(text, target)}",
          file=sys.stderr)
    return 3


def cmd_verify_quotes(args: argparse.Namespace) -> int:
    text, unmatched = _plain_text(args.src, args.start_line, args.end_line,
                                  args.plain)
    if unmatched:
        print(f"FAIL quotes: 源输出有 {len(unmatched)} 行格式异常", file=sys.stderr)
        return 2
    quotes: list[str] = [q for q in (args.quote or []) if q.strip()]
    if args.quotes_file:
        for line in Path(args.quotes_file).expanduser().read_text(
                encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                quotes.append(line)
    if not quotes:
        print("FAIL quotes: 没有给任何引文，门禁形同虚设", file=sys.stderr)
        return 2
    missed = [q for q in quotes if q not in text]
    if missed:
        print(f"FAIL quotes: {len(missed)}/{len(quotes)} 条引文在原始输出中命中不了",
              file=sys.stderr)
        for q in missed:
            print(f"  ✗ {q}", file=sys.stderr)
        print("  → 不许把命中不了的「原文」写进简报或圣典；回工具输出核对字形。",
              file=sys.stderr)
        return 3
    print(f"{_OK_QUOTES} {len(quotes)}/{len(quotes)} 条引文均在原始输出中精确命中")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--src", required=True,
                        help="officecli view text 的原始输出文件（- 表示 stdin）")
        sp.add_argument("--start-line", type=int, default=None,
                        help="从源输出第 N 行开始（1-based，闭区间），用于全稿导出截取")
        sp.add_argument("--end-line", type=int, default=None,
                        help="到源输出第 M 行结束（1-based，闭区间）")
        sp.add_argument("--plain", action="store_true",
                        help="源已经是剥好标记的纯文本，不再剥 paraId")

    sp = sub.add_parser("extract", help="确定性落盘（不经模型誊抄）+ 回读逐字自校验")
    add_common(sp)
    sp.add_argument("--out", required=True, help="落盘纯文本路径")
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("verify-file", help="复核已落盘 txt 是否与源输出逐字一致")
    add_common(sp)
    sp.add_argument("--file", required=True, help="待复核的 txt")
    sp.set_defaults(func=cmd_verify_file)

    sp = sub.add_parser("verify-quotes", help="简报/圣典落笔前校验所有「原文」引文")
    add_common(sp)
    sp.add_argument("--quote", action="append", default=[],
                    help="一条原文引文，可重复传")
    sp.add_argument("--quotes-file", default=None,
                    help="每行一条引文的文件（# 开头为注释）")
    sp.set_defaults(func=cmd_verify_quotes)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"FAIL {args.cmd}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
