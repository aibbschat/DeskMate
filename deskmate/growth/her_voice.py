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

#: 系统渲染的固定句（``diary._opening`` 的兜底 + 空转句）。它们不是「她写的话」，
#: 拿它们当她的原句就是自己抄自己。**改 diary._opening 的固定句时必须同步这里。**
_TEMPLATE_LINES = {
    "今天我们一起做了些事，我都记在心里。",
    "这一阵没什么动静。我安静地待着，等你忙完。",
    "上午过后又有了些新动静，我接着记。",
    "下午又有了些新动静，我接着记。",
    "入夜了又添了几笔，我接着记。",
}

#: 结构行（徽章、落款、图片），是版式不是句子
_SKIP_LINE_RE = re.compile(
    r"(?:^|[-*]\s*)(?:默契\s|信任\s|今天解锁徽章|我更懂你了|累计对话)"
    r"|^>\s*累计对话|^!\["
)


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
    """取日记每个 ``## `` 小节里她写的**句子**（一行一句），不是整段。

    2026-11-17 修：原实现返回整段，于是 ``diary._opening(seq>1)`` 把上一则
    全文当成开场白——第二则开头变成第一则整段、落款还重复一遍
    （``test_append_two_entries`` 抓的就是这个，长期被当成「既有红」）。
    ``pick_her_line`` 那边同样受害：几百字的整段做 2-gram 匹配几乎什么都能命中，
    挑出来的「她的一句原句」其实是半篇日记。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for part in re.split(r"(?m)^##\s+", text)[1:]:
        # 第一行是 ``HH:MM <!--xj:...-->`` 标题，跳过
        for raw in part.splitlines()[1:]:
            s = raw.strip()
            if not s or _SKIP_LINE_RE.search(s):
                continue
            # 清单项（``- 我们一起整理了文稿。``）是渲染器按素材生成的描述，
            # 不是她写的话。取来当开场白就是「第二则复述第一则」——
            # ``test_second_entry_does_not_repeat_first`` 抓的就是这个。
            # 宁可回落固定兜底句，也不拿清单冒充她的话。
            if s.startswith(("- ", "* ")):
                continue
            s = re.sub(r"\[xj:[^\]]*\]", "", s).strip()
            for sent in re.split(r"(?<=[。！？…])", s):
                sent = sent.strip()
                if len(sent) >= 6 and sent not in _TEMPLATE_LINES:
                    out.append(sent)
    return out


def recent_her_lines(
    base_dir: Optional[Path] = None,
    limit: int = 8,
    skip_recent_hours: float = 0.0,
) -> list[str]:
    """收集她最近的第一人称原句，最新在前。

    - 情绪独白（emotion_log.description，去尾随「（原因）」meta）
    - 日记叙述句（近 7 天文件，正序 → 反转成最新在前）

    ``skip_recent_hours``：跳过最近 N 小时的独白。写日记时开场白要用「她过去
    说过的话」；同一则刚产生的独白属于当下，再引一次就是复述。

    fail-open：任何异常段被吞，返回已收集部分（至少 []）。
    """
    monologues: list[str] = []
    try:
        from deskmate.growth.emotion_engine import EmotionEngine

        eng = EmotionEngine(base_dir=base_dir)
        for d in eng.recent_log_descriptions(
            since_hours=24 * _MONOLOGUE_DAYS,
            limit=limit * 2,
            skip_recent_hours=skip_recent_hours,
        ):
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
