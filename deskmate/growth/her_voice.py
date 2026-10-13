"""小桔自己的声音（活感方案 v2 §三.3 兜底/陪伴语来源）。

真实优先原则：LLM 不可用时，让她自己写过的第一人称原句替她说话，
而不是用客服模板。来源：
- ``emotion_log.description``：情绪引擎每轮生成的独白（第一人称、≤40字，最干净）
- 日记正文行：``~/.deskmate/diary/YYYY-MM-DD.md`` 中 ``## HH:MM`` 小节的正文

捞取**必须带相关性过滤**（中文 2-gram 重叠），不能裸捞最近一条——
不搭调的她自己的原句，比复读固定模板更尴尬（§三.3 明示）。

全部 fail-open：任何取数异常 → 返回 [] / None，由调用方回落固定池。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_DIARY_FILES = 7          # 近 N 天日记
_MONOLOGUE_DAYS = 30      # 独白回看窗口


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _overlap(query: str, line: str) -> tuple[float, int]:
    """返回 (Jaccard 比, 共享 2-gram 数)。"""
    gq, gl = _bigrams(query), _bigrams(line)
    if not gq or not gl:
        return 0.0, 0
    shared = gq & gl
    return len(shared) / min(len(gq), len(gl)), len(shared)


def _diary_section_bodies(path: Path) -> list[str]:
    """取日记文件中每个 ``## `` 小节的正文（去 header 行）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    bodies: list[str] = []
    for part in re.split(r"(?m)^##\s+", text)[1:]:
        body = "\n".join(part.splitlines()[1:]).strip()
        if body:
            bodies.append(body)
    return bodies


def recent_her_lines(base_dir: Optional[Path] = None, limit: int = 8) -> list[str]:
    """收集她最近的第一人称原句，最新在前。

    - 情绪独白（emotion_log.description，去尾随「（原因）」meta）
    - 日记正文行（近 7 天文件，正序 → 反转成最新在前）
    fail-open：任何异常段被吞，返回已收集部分（至少 []）。
    """
    monologues: list[str] = []
    try:
        from deskmate.growth.emotion_engine import EmotionEngine

        eng = EmotionEngine(base_dir=base_dir)
        for d in eng.recent_log_descriptions(since_hours=24 * _MONOLOGUE_DAYS, limit=limit * 2):
            d = re.sub(r"[（(][^（）()]*[）)]$", "", (d or "").strip())
            if d:
                monologues.append(d)
    except Exception:
        pass

    diary_lines: list[str] = []
    try:
        from deskmate.paths import deskmate_base_dir

        base = Path(base_dir) if base_dir else deskmate_base_dir()
        diary_dir = base / "diary"
        if diary_dir.is_dir():
            for f in sorted(diary_dir.glob("*.md"))[-_DIARY_FILES:]:
                diary_lines.extend(_diary_section_bodies(f))
    except Exception:
        pass

    ordered = monologues + list(reversed(diary_lines))  # 最新在前
    seen: set[str] = set()
    out: list[str] = []
    for l in ordered:
        l = (l or "").strip()
        if not l or l in seen:
            continue
        seen.add(l)
        out.append(l)
    return out[:limit]


def pick_her_line(
    query: str,
    lines: list[str],
    min_shared: int = 2,
    min_ratio: float = 0.0,
) -> Optional[str]:
    """从她的原句里挑与 query 最相关的一条；不够相关返回 None。

    ``min_shared``：至少共享几个 2-gram（防只因「今天」这类泛词命中）。
    ``min_ratio``：Jaccard 下限（可选）。都不达标 → None（调用方落固定池）。
    """
    if not lines or not (query or "").strip():
        return None
    best: Optional[str] = None
    best_shared = -1
    for l in lines:
        ratio, shared = _overlap(query, l)
        if shared >= min_shared and ratio >= min_ratio and shared > best_shared:
            best, best_shared = l, shared
    return best
