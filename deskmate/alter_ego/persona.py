"""P2-6 个人 AI 分身——画像聚合 + 分身 prompt 生成。

把「写作风格画像」（P1-5 已有）+「决策偏好画像」（P2-6 新增）聚合成
「另一个我」的完整画像，生成分身 prompt，让 AI 能以用户的风格/思维方式输出，
而不是通用 AI 风格。

分身模式（可选开启，默认关闭，因为「另一个我」是高级功能）：
- 创作时：以用户的写作风格输出（句长/文言/对话占比等量化特征 + 定性描述）
- 建议时：以用户的决策偏好做建议（保守/激进、先A后B、重视X超过Y等）

设计原则：
- 零 LLM：画像聚合纯规则，不依赖外部 API
- 向后兼容：写作风格画像读取沿用现有 style_profile.json
- 可选注入：分身 prompt 是附加段，不影响原有 prompt 结构
- fail-open：画像缺失时返回空串，不阻断主流程
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .decision_profile import (
    DecisionProfile,
    format_decision_profile,
    load_profile as load_decision_profile,
)

DEFAULT_STYLE_PATH = (
    Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
    / "style_profile.json"
)


def load_style_profile(path: Path | None = None) -> dict[str, Any]:
    """加载写作风格画像（统一库 deskmate.db 为真相源；legacy 文件自动迁移）。

    委托 ``deskmate.style_profile_store.load_profile`` 保持单一来源，避免两套读取逻辑漂移。
    返回空 dict 如果不存在或损坏。
    """
    from deskmate.style_profile_store import load_profile as _load_style_profile

    return _load_style_profile(path or DEFAULT_STYLE_PATH)


def format_style_quantified(style: dict[str, Any]) -> str:
    """把量化风格指标格式化为可读文本（供分身 prompt 注入）。

    只输出有统计基础的指标（n >= 2 才认为有统计意义）。
    """
    metrics = style.get("metrics") or {}
    if not metrics or metrics.get("n", 0) < 2:
        return ""

    lines: list[str] = []

    # 句长
    sent = metrics.get("sentences") or {}
    if sent:
        avg = sent.get("avg_len")
        median = sent.get("median_len")
        short = sent.get("short_pct")
        long_ = sent.get("long_pct")
        parts = []
        if avg:
            parts.append(f"平均句长 {avg:.0f} 字")
        if median:
            parts.append(f"中位句长 {median:.0f} 字")
        if short is not None:
            parts.append(f"短句占比 {short:.0f}%")
        if long_ is not None:
            parts.append(f"长句占比 {long_:.0f}%")
        if parts:
            lines.append(f"- 句式：{'，'.join(parts)}")

    # 文言/白话
    classical = metrics.get("classical_per_1k")
    modern = metrics.get("modern_per_1k")
    if classical is not None or modern is not None:
        parts = []
        if classical is not None:
            parts.append(f"文言虚词 {classical:.1f}/千字")
        if modern is not None:
            parts.append(f"现代语气词 {modern:.1f}/千字")
        if parts:
            lines.append(f"- 语体：{'，'.join(parts)}")

    # 四字格
    four = metrics.get("four_char") or {}
    if four and four.get("pct_of_chunk") is not None:
        lines.append(f"- 四字格：{four['pct_of_chunk']:.1f}% 的词块为四字格")

    # 对话占比
    dialogue = metrics.get("dialogue") or {}
    if dialogue and dialogue.get("pct") is not None:
        lines.append(f"- 对话：{dialogue['pct']:.0f}% 文字为对话")

    # 段落
    para = metrics.get("paragraphs") or {}
    if para and para.get("avg_len"):
        lines.append(f"- 段落：平均段长 {para['avg_len']:.0f} 字")

    if not lines:
        return ""

    header = f"## 用户写作风格（量化指纹，基于 {metrics.get('chars', 0):.0f} 字样本）"
    return header + "\n" + "\n".join(lines)


def format_style_qualitative(style: dict[str, Any]) -> str:
    """把定性风格描述格式化为可读文本（summary + features）。"""
    summary = style.get("summary") or ""
    features = style.get("features") or []
    if not summary and not features:
        return ""

    lines: list[str] = ["## 用户写作风格（定性描述）"]
    if summary:
        lines.append(f"- 总评：{summary}")
    if features:
        for f in features[:5]:  # 最多 5 条
            if isinstance(f, str) and f.strip():
                lines.append(f"- {f.strip()}")
    return "\n".join(lines)


def build_alter_ego_prompt(
    style: dict[str, Any] | None = None,
    decision: DecisionProfile | None = None,
    mode: str = "full",
) -> str:
    """生成分身 prompt（「另一个我」画像注入段）。

    Args:
        style: 写作风格画像（None 时自动加载）
        decision: 决策偏好画像（None 时自动加载）
        mode: "full"（写作+决策）/ "writing"（仅写作）/ "decision"（仅决策）

    Returns:
        分身 prompt 文本（无信号时返回空串）
    """
    if style is None:
        style = load_style_profile()
    if decision is None:
        decision = load_decision_profile()

    sections: list[str] = []

    if mode in ("full", "writing"):
        quantified = format_style_quantified(style)
        if quantified:
            sections.append(quantified)
        qualitative = format_style_qualitative(style)
        if qualitative:
            sections.append(qualitative)

    if mode in ("full", "decision"):
        decision_text = format_decision_profile(decision)
        if decision_text:
            sections.append(decision_text)

    if not sections:
        return ""

    # 加分身引导语（根据 mode 动态生成）
    if mode == "full":
        topic = "写作风格与决策偏好"
    elif mode == "writing":
        topic = "写作风格"
    else:
        topic = "决策偏好"
    intro = (
        "## 「另一个我」分身模式\n"
        f"以下是从用户长期使用中学习到的{topic}。"
        "在创作或给建议时，尽量贴合用户的风格和思维方式，"
        "让输出更像「用户自己会写的/会选的」，而不是通用 AI 风格。\n"
    )
    return intro + "\n".join(sections)


def has_alter_ego_data() -> bool:
    """检查是否有足够的分身数据（写作风格 n>=2 或 决策信号>=1）。"""
    style = load_style_profile()
    decision = load_decision_profile()
    has_style = (style.get("metrics") or {}).get("n", 0) >= 2
    has_decision = decision.total_signals >= 1
    return has_style or has_decision
