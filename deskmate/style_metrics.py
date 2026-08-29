"""墨灵写作风格量化指标 — 纯统计、零 LLM。

从作者文本中计算可量化的风格信号，供「风格画像增量更新」使用。
所有指标只做统计，不引入 LLM 调用：成本可忽略、可单测、可复现。

设计目标（对应 docs/越用越懂你-打磨建议.md P1-5）：
把「一次性 LLM 定性画像」升级为「随作品演进的量化风格指纹」，
让『越写越懂你的文风』成为可度量、可对比、可回看的东西。

指标一览（全部按中文语境设计）：
- sentences   : 句长分布（平均/中位/短句占比/长句占比/超长句占比）
- paragraphs  : 段长分布（平均/短段/长段占比）
- four_char   : 四字格倾向（恰好四字的词块占比，反映「四字格/成语」密度）
- classical   : 文言虚词密度（之乎者也矣焉哉…，反映「半文半白」程度）
- modern      : 现代语气词密度（了的吧呢吗…，与 classical 对照）
- dialogue    : 对话占比（引号内文字比例）
- punct       : 标点节奏（句号/逗号/顿号/分号 每千字密度）
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

_CJK = re.compile(r"[\u4e00-\u9fff]")

# 句子结尾标点（中文常见句末）
_SENT_END = re.compile(r"[。！？；…]+")

# 词块：连续中文字符（不含标点），≥2 字才纳入
_CHUNK = re.compile(r"[\u4e00-\u9fff]{2,}")

# 文言虚词（半文半白 / 古雅语体的近似信号）
_CLASSICAL_PARTICLES = set("之乎者也矣焉哉兮乃其以于为而则耳夫盖欤邪")

# 现代语气词（与文言虚词对照，反映语体的现代白话程度）
_MODERN_PARTICLES = set("了的吧呢吗嘛呀啊哦嗯呗啦")

# 成对引号（中英文混用）
_QUOTE_PAIRS = [("“", "”"), ("『", "』"), ("「", "」"), ("\"", "\""), ("'", "'"), ("《", "》")]


@dataclass
class StyleMetrics:
    """一组可序列化的风格量化指标。"""

    chars: int = 0                      # 中文字符总数
    sentences: dict = field(default_factory=dict)
    paragraphs: dict = field(default_factory=dict)
    four_char: dict = field(default_factory=dict)
    classical_per_1k: float = 0.0       # 文言虚词 / 千字
    modern_per_1k: float = 0.0          # 现代语气词 / 千字
    dialogue_pct: float = 0.0           # 对话（引号内）占比 %
    punct: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _percent(vals: list[float], threshold: float, direction: str) -> float:
    """统计满足阈值条件的值占比（%）。direction: 'le'（≤）或 'ge'（≥）。"""
    if not vals:
        return 0.0
    if direction == "le":
        hit = sum(1 for v in vals if v <= threshold)
    else:
        hit = sum(1 for v in vals if v >= threshold)
    return round(100.0 * hit / len(vals), 2)


def _quoted_chars(text: str) -> int:
    """统计成对引号内的字符数（近似对话量）。"""
    total = 0
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            start = text.find(open_q, i)
            if start < 0:
                break
            end = text.find(close_q, start + 1)
            if end < 0:
                break
            total += len(_CJK.findall(text[start + 1 : end]))
            i = end + 1
    return total


def compute_style_metrics(text: str) -> dict:
    """从文本计算全部风格量化指标，返回可序列化 dict。

    Args:
        text: 作者文本（不限制长度；内部会控制计算量）。

    Returns:
        StyleMetrics.to_dict() 的 dict。
    """
    # 文本过长时截断以控制开销（统计口径不变，只牺牲样本量）
    if len(text) > 200_000:
        text = text[:200_000]

    cjk_chars = _CJK.findall(text)
    chars = len(cjk_chars)
    if chars == 0:
        return StyleMetrics().to_dict()

    # ---- 句子 ----
    # 按句末标点切分为「句子片段」（保留标点前的内容）；
    # 原文无任何句末标点（如纯排版/无标点文本）时退化为按词块计。
    if _SENT_END.search(text):
        raw_sents = [s for s in re.split(_SENT_END, text) if s.strip()]
        sent_lens = [len(_CJK.findall(s)) for s in raw_sents]
    else:
        chunks_for_sent = _CHUNK.findall(text)
        sent_lens = [len(c) for c in chunks_for_sent]
    sentences = {
        "count": len(sent_lens),
        "avg_len": round(sum(sent_lens) / max(1, len(sent_lens)), 2),
        "median_len": _median([float(v) for v in sent_lens]),
        "short_pct": _percent(sent_lens, 12, "le"),   # ≤12 字短句占比
        "long_pct": _percent(sent_lens, 40, "ge"),    # ≥40 字长句占比
        "overlong_pct": _percent(sent_lens, 60, "ge"),  # ≥60 字超长句占比
    }

    # ---- 段落 ----
    paras = [p.strip() for p in text.splitlines() if _CJK.search(p)]
    para_lens = [len(_CJK.findall(p)) for p in paras]
    paragraphs = {
        "count": len(para_lens),
        "avg_len": round(sum(para_lens) / max(1, len(para_lens)), 2),
        "short_pct": _percent(para_lens, 40, "le"),   # ≤40 字短段占比
        "long_pct": _percent(para_lens, 200, "ge"),   # ≥200 字长段占比
    }

    # ---- 四字格倾向 ----
    chunks = _CHUNK.findall(text)
    total_chunks = len(chunks)
    four_count = sum(1 for c in chunks if len(c) == 4)
    four_char = {
        "count": four_count,
        "pct_of_chunks": round(100.0 * four_count / max(1, total_chunks), 2),
    }

    # ---- 文言 / 现代语气词密度（每千字） ----
    classical_per_1k = sum(1 for c in cjk_chars if c in _CLASSICAL_PARTICLES) / chars * 1000
    modern_per_1k = sum(1 for c in cjk_chars if c in _MODERN_PARTICLES) / chars * 1000

    # ---- 对话占比 ----
    dialogue_pct = round(100.0 * _quoted_chars(text) / chars, 2)

    # ---- 标点节奏 ----
    punct = {
        "period_per_1k": round(text.count("。") / chars * 1000, 2),
        "comma_per_1k": round(text.count("，") / chars * 1000, 2),
        "dunhao_per_1k": round(text.count("、") / chars * 1000, 2),
        "semicolon_per_1k": round(text.count("；") / chars * 1000, 2),
    }

    m = StyleMetrics(
        chars=chars,
        sentences=sentences,
        paragraphs=paragraphs,
        four_char=four_char,
        classical_per_1k=round(classical_per_1k, 2),
        modern_per_1k=round(modern_per_1k, 2),
        dialogue_pct=dialogue_pct,
        punct=punct,
    )
    return m.to_dict()


def _recursive_merge(a, b, na: int, nb: int, key: Optional[str] = None):
    """两个指标树的合并（dict 递归；数值字段加权平均；`count` 字段累加）。

    a 为旧值（样本数 na），b 为新值（样本数 nb）。
    返回合并后的值。
    """
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        out = {}
        for k in keys:
            if k in a and k in b:
                out[k] = _recursive_merge(a[k], b[k], na, nb, key=k)
            else:
                out[k] = a.get(k, b.get(k))
        return out
    if (
        isinstance(a, (int, float))
        and isinstance(b, (int, float))
        and not isinstance(a, bool)
        and not isinstance(b, bool)
    ):
        # count 是样本累计量，累加；其余数值指标做样本数加权平均
        if key == "count":
            return a + b
        return round((a * na + b * nb) / max(1, na + nb), 2)
    return b if b is not None else a


def merge_metrics(old: Optional[dict], new: dict, old_n: int = 0) -> dict:
    """把新样本指标合并进旧指标树。

    Args:
        old: 旧指标 dict（可为 None，表示首次）。
        new: 新样本指标 dict（compute_style_metrics 的返回）。
        old_n: 旧指标已累计的样本数（用于加权平均）。

    Returns:
        合并后的指标 dict（样本数为 old_n + 1）。
    """
    if not old:
        return new
    return _recursive_merge(old, new, old_n, 1)
