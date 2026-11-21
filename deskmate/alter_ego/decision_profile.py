"""P2-6 个人 AI 分身——决策偏好提取（零 LLM、纯规则）。

从用户对话中提取决策模式，聚合成「决策画像」，与写作风格画像共同构成
「另一个我」的完整画像。AI 了解用户的决策偏好后，能以用户的思维方式
做建议、做选择，而不是通用 AI 风格。

决策维度（全部从用户消息中规则匹配，高精确率保守提取）：
- risk        : 风险偏好（保守/稳健 vs 激进/大胆）
- sequence    : 决策顺序（先A后B 的模式）
- tradeoff    : 价值权衡（重视X超过Y）
- change      : 改动倾向（最小改动 vs 彻底重构）
- info_style  : 信息偏好（喜欢对比选项 vs 直接给结论）

设计原则：
- 零 LLM：纯正则 + 关键词，不依赖外部 API
- 高精确率：只匹配高置信度句式，宁可少抽不可错抽
- 增量更新：每次对话提取新信号，合并到决策画像（带计数 + 最近出现时间）
- fail-open：任何异常返回空，绝不阻断主对话
- 只从用户消息提取：忽略 AI 的话
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from deskmate.persistence import BlobStore, BlobWriteError

DEFAULT_PROFILE_PATH = (
    Path(os.environ.get("DESKMATE_DIR", str(Path.home() / ".deskmate")))
    / "decision_profile.json"
)


# ---------------------------------------------------------------------------
# 决策信号提取规则（高精确率保守匹配）
# ---------------------------------------------------------------------------

@dataclass
class DecisionSignal:
    """一条提取出的决策信号。"""
    dimension: str       # risk / sequence / tradeoff / change / info_style
    value: str           # 具体值（如 "保守"、"先调研再决定"）
    evidence: str        # 触发的原文片段（可追溯）
    ts: float = field(default_factory=time.time)


# 风险偏好：保守/稳健
_RISK_CONSERVATIVE = [
    r"保守[一点些]?",
    r"稳妥[一点些]?",
    r"谨慎[一点些]?",
    r"不要冒[风险险]?",
    r"求稳",
    r"稳[一点些]?",
    r"别太[激进冒]",
]
# 否定式激进（「别太激进」「不要激进」）已在保守表里，激进匹配必须避开它们：
# 否则同一句话同时命中两侧，按「靠后者胜」会得出相反的保守意图（2026-11-21 回归）。
_RISK_AGGRESSIVE_NEGATED = re.compile(r"(?:别太|不要|别)[^，。！？\n]{0,3}(?:激进|冒险)")
# 风险偏好：激进/大胆
_RISK_AGGRESSIVE = [
    r"大胆[一点些]?",
    r"激进[一点些]?",
    r"直接做",
    r"别犹豫",
    r"放手[一去]?做",
    r"冒[一下点]?险",
    r"试试[就]?行",
]

# 决策顺序：先A后B
_SEQUENCE_PATTERNS = [
    r"先([^，。！？\n]{2,20})再([^，。！？\n]{2,20})",
    r"首先([^，。！？\n]{2,20})然后([^，。！？\n]{2,20})",
    r"第一步([^，。！？\n]{2,20})第二步([^，。！？\n]{2,20})",
]

# 价值权衡：重视X超过Y
_TRADEOFF_PATTERNS = [
    r"比起([^，。！？\n]{2,15})我更(看重|重视|在意|关心|喜欢|倾向)([^，。！？\n]{0,15})",
    r"([^，。！？\n]{2,15})比([^，。！？\n]{2,15})重要",
    r"与其([^，。！？\n]{2,15})不如([^，。！？\n]{2,15})",
]

# 改动倾向：最小改动
_CHANGE_MINIMAL = [
    r"尽量不[改动]",
    r"小改",
    r"微调",
    r"保持[现原]状",
    r"不[要别]大改",
    r"最小[改动变化]",
]
# 改动倾向：彻底重构
_CHANGE_RADICAL = [
    r"重构",
    r"彻底[重做改]",
    r"推翻重来",
    r"大改",
    r"重新设计",
    r"从头[再来做]",
]

# 信息偏好：喜欢对比选项
_INFO_COMPARE = [
    r"对比[一下]?",
    r"给我[几两][个种]?选项",
    r"有[哪几]种方案",
    r"利弊",
    r"优缺点",
    r"权衡[一下]?",
]
# 信息偏好：直接给结论
_INFO_DIRECT = [
    r"直接告诉[我你]",
    r"别[绕弯子废话]",
    r"直接[说给]?",
    r"要[就]?结论",
    r"别[给我]?分析",
    r"简单[点一]?",
]


def _match_any(patterns: list[str], text: str) -> str | None:
    """匹配任一正则，返回命中的原文片段。"""
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(0).strip()
    return None


def _first_hit(patterns: list[str], text: str) -> tuple[str, int] | None:
    """匹配任一正则，返回 (命中片段, 起始位置)；多个命中取最靠前的。"""
    best: tuple[str, int] | None = None
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            pos = m.start()
            if best is None or pos < best[1]:
                best = (m.group(0).strip(), pos)
    return best


def extract_signals(user_text: str) -> list[DecisionSignal]:
    """从用户文本中提取决策信号。

    Args:
        user_text: 用户消息文本（应只含用户的话，不含 AI 回复）

    Returns:
        决策信号列表（可能为空）
    """
    if not user_text or not user_text.strip():
        return []

    signals: list[DecisionSignal] = []

    # 1. 风险偏好（同一 risk 维度互斥：保守/激进不可同时成立，取更靠后出现的表态）
    cons = _first_hit(_RISK_CONSERVATIVE, user_text)
    agg = _first_hit(_RISK_AGGRESSIVE, user_text)
    if agg and _RISK_AGGRESSIVE_NEGATED.search(user_text[max(0, agg[1] - 4):agg[1] + len(agg[0]) + 2]):
        agg = None
    if cons and agg:
        # 文本同时出现两种相反表态 → 自相矛盾，保留更靠后出现的（更代表当前意图）
        if agg[1] >= cons[1]:
            signals.append(DecisionSignal(dimension="risk", value="激进/大胆", evidence=agg[0]))
        else:
            signals.append(DecisionSignal(dimension="risk", value="保守/稳健", evidence=cons[0]))
    elif cons:
        signals.append(DecisionSignal(dimension="risk", value="保守/稳健", evidence=cons[0]))
    elif agg:
        signals.append(DecisionSignal(dimension="risk", value="激进/大胆", evidence=agg[0]))

    # 2. 决策顺序
    for pat in _SEQUENCE_PATTERNS:
        for m in re.finditer(pat, user_text):
            first = m.group(1).strip()
            then = m.group(2).strip()
            if first and then and len(first) < 20 and len(then) < 20:
                signals.append(DecisionSignal(
                    dimension="sequence",
                    value=f"先{first}再{then}",
                    evidence=m.group(0).strip(),
                ))

    # 3. 价值权衡
    for pat in _TRADEOFF_PATTERNS:
        for m in re.finditer(pat, user_text):
            try:
                if "比起" in pat:
                    a = m.group(1).strip()  # 被比较的对象
                    b = m.group(3).strip() if m.lastindex and m.lastindex >= 3 else ""  # 更看重的对象
                    if a and b:
                        signals.append(DecisionSignal(
                            dimension="tradeoff",
                            value=f"重视{b}超过{a}",
                            evidence=m.group(0).strip(),
                        ))
                elif "比" in pat and "重要" in pat:
                    a = m.group(1).strip()
                    b = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
                    if a and b:
                        signals.append(DecisionSignal(
                            dimension="tradeoff",
                            value=f"重视{b}超过{a}",
                            evidence=m.group(0).strip(),
                        ))
                elif "与其" in pat:
                    a = m.group(1).strip()
                    b = m.group(2).strip()
                    if a and b:
                        signals.append(DecisionSignal(
                            dimension="tradeoff",
                            value=f"倾向于{b}而非{a}",
                            evidence=m.group(0).strip(),
                        ))
            except (IndexError, AttributeError):
                continue

    # 4. 改动倾向
    minimal = _match_any(_CHANGE_MINIMAL, user_text)
    if minimal:
        signals.append(DecisionSignal(
            dimension="change", value="最小改动/保持现状", evidence=minimal
        ))
    radical = _match_any(_CHANGE_RADICAL, user_text)
    if radical:
        signals.append(DecisionSignal(
            dimension="change", value="彻底重构/大改", evidence=radical
        ))

    # 5. 信息偏好
    compare = _match_any(_INFO_COMPARE, user_text)
    if compare:
        signals.append(DecisionSignal(
            dimension="info_style", value="喜欢对比选项/利弊分析", evidence=compare
        ))
    direct = _match_any(_INFO_DIRECT, user_text)
    if direct:
        signals.append(DecisionSignal(
            dimension="info_style", value="直接给结论/不要废话", evidence=direct
        ))

    return signals


# ---------------------------------------------------------------------------
# 决策画像存储（增量合并）
# ---------------------------------------------------------------------------

@dataclass
class DecisionProfile:
    """用户决策画像（增量合并后的稳定偏好）。"""
    risk: dict[str, Any] = field(default_factory=dict)          # {value: {count, last_ts, evidence}}
    sequence: list[dict[str, Any]] = field(default_factory=list)  # [{value, count, last_ts, evidence}]
    tradeoff: list[dict[str, Any]] = field(default_factory=list)
    change: dict[str, Any] = field(default_factory=dict)
    info_style: dict[str, Any] = field(default_factory=dict)
    total_signals: int = 0
    first_updated_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DecisionProfile":
        if not isinstance(data, dict):
            return cls()
        return cls(
            risk=data.get("risk", {}),
            sequence=data.get("sequence", []),
            tradeoff=data.get("tradeoff", []),
            change=data.get("change", {}),
            info_style=data.get("info_style", {}),
            total_signals=data.get("total_signals", 0),
            first_updated_at=data.get("first_updated_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


def _merge_counted(profile_dict: dict, value: str, evidence: str, ts: float) -> None:
    """合并到计数字典（risk/change/info_style 用）。"""
    if value not in profile_dict:
        profile_dict[value] = {"count": 0, "last_ts": 0, "evidence": ""}
    entry = profile_dict[value]
    entry["count"] += 1
    entry["last_ts"] = ts
    entry["evidence"] = evidence  # 保留最近的证据


def _merge_list(profile_list: list, value: str, evidence: str, ts: float) -> None:
    """合并到列表（sequence/tradeoff 用，按 value 去重计数）。"""
    for entry in profile_list:
        if entry.get("value") == value:
            entry["count"] = entry.get("count", 0) + 1
            entry["last_ts"] = ts
            entry["evidence"] = evidence
            return
    profile_list.append({
        "value": value, "count": 1, "last_ts": ts, "evidence": evidence,
    })


def update_profile(profile: DecisionProfile, signals: list[DecisionSignal]) -> DecisionProfile:
    """用新信号增量更新决策画像。"""
    now = time.time()
    for sig in signals:
        if sig.dimension == "risk":
            _merge_counted(profile.risk, sig.value, sig.evidence, sig.ts)
        elif sig.dimension == "sequence":
            _merge_list(profile.sequence, sig.value, sig.evidence, sig.ts)
        elif sig.dimension == "tradeoff":
            _merge_list(profile.tradeoff, sig.value, sig.evidence, sig.ts)
        elif sig.dimension == "change":
            _merge_counted(profile.change, sig.value, sig.evidence, sig.ts)
        elif sig.dimension == "info_style":
            _merge_counted(profile.info_style, sig.value, sig.evidence, sig.ts)
        profile.total_signals += 1
    profile.updated_at = now
    return profile


def load_profile(path: Path | None = None) -> DecisionProfile:
    """加载决策画像；不存在或损坏返回空画像。

    默认（path=None）从统一库 deskmate.db 读（首次自动从 legacy decision_profile.json 迁移）；
    显式 path 仍走文件（测试 / 自定义路径兼容）。
    """
    if path is None:
        data = BlobStore().get(BlobStore.KEY_DECISION, legacy_path=DEFAULT_PROFILE_PATH)
        return DecisionProfile.from_dict(data) if isinstance(data, dict) else DecisionProfile()
    p = path
    if not p.is_file():
        return DecisionProfile()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return DecisionProfile.from_dict(data)
    except (OSError, ValueError, json.JSONDecodeError):
        return DecisionProfile()


def save_profile(profile: DecisionProfile, path: Path | None = None) -> None:
    """保存决策画像。默认（path=None）写入统一库 deskmate.db；显式 path 走文件（原子写）。"""
    if path is None:
        BlobStore().set(BlobStore.KEY_DECISION, profile.to_dict())
        return
    p = path
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def extract_and_update(user_text: str, path: Path | None = None) -> tuple[DecisionProfile, list[DecisionSignal]]:
    """从用户文本提取信号并增量更新画像（一步完成，并发安全）。

    默认（path=None）走统一库，通过 BlobStore.atomic_update 把 load→update→save
    包在 BEGIN IMMEDIATE 事务里，消除多进程/线程竞态。

    Returns:
        (更新后的画像, 本次提取的信号列表)
    """
    signals = extract_signals(user_text)
    if not signals:
        return load_profile(path), []

    def _do_update(old_data: dict) -> dict:
        profile = DecisionProfile.from_dict(old_data) if isinstance(old_data, dict) else DecisionProfile()
        update_profile(profile, signals)
        return profile.to_dict()

    if path is None:
        try:
            result = BlobStore().atomic_update(
                BlobStore.KEY_DECISION,
                _do_update,
                legacy_path=DEFAULT_PROFILE_PATH,
            )
        except BlobWriteError:
            # 决策画像属旁路增强：写库失败不阻断对话主流程（fail-open），但留痕。
            logging.warning("decision_profile 写入失败，本次信号未落库", exc_info=True)
            return load_profile(None), signals
        if result is None:
            return DecisionProfile(), signals
        return DecisionProfile.from_dict(result), signals
    else:
        # 显式路径（文件）：测试/自定义场景，无并发
        profile = load_profile(path)
        update_profile(profile, signals)
        save_profile(profile, path)
        return profile, signals


# ---------------------------------------------------------------------------
# 画像 → 可读描述（供分身 prompt 注入）
# ---------------------------------------------------------------------------

def _top_counted(d: dict, n: int = 2) -> list[str]:
    """取计数字典中 top N 的值。"""
    items = sorted(d.items(), key=lambda x: x[1].get("count", 0), reverse=True)
    return [k for k, _ in items[:n]]


def _top_list(lst: list, n: int = 2) -> list[str]:
    """取列表中 top N 的值。"""
    items = sorted(lst, key=lambda x: x.get("count", 0), reverse=True)
    return [x["value"] for x in items[:n] if x.get("value")]


def format_decision_profile(profile: DecisionProfile) -> str:
    """把决策画像格式化为可读文本（供注入 prompt）。

    无信号时返回空串。
    """
    if profile.total_signals == 0:
        return ""

    lines: list[str] = ["## 用户决策偏好（从对话中学习）"]

    risk = _top_counted(profile.risk)
    if risk:
        lines.append(f"- 风险偏好：{'、'.join(risk)}")

    sequences = _top_list(profile.sequence)
    if sequences:
        lines.append(f"- 决策顺序：{'；'.join(sequences)}")

    tradeoffs = _top_list(profile.tradeoff)
    if tradeoffs:
        lines.append(f"- 价值权衡：{'；'.join(tradeoffs)}")

    changes = _top_counted(profile.change)
    if changes:
        lines.append(f"- 改动倾向：{'、'.join(changes)}")

    info_styles = _top_counted(profile.info_style)
    if info_styles:
        lines.append(f"- 信息偏好：{'、'.join(info_styles)}")

    if len(lines) <= 1:
        return ""
    return "\n".join(lines)
