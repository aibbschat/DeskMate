"""意志层（成为我）认知/情感摘要层。

把认知画像（立场/盲区/价值观信号）与情感时间线（今日心情）**压缩成可供
行为调制消费的只读信号**。本身不做任何决策——只做"给调制器喂什么料"。

设计要点（详见 docs/意志层（成为我）落地规划_2026-09-05.md）：
- **只读**：不写画像、不写时间线、不写任何状态，纯消费者
- **轻分类**：真实库 ``dimension`` 已统一为 ``stance``，原"价值观/思维框架"
  细分丢失，故用 topic/内容关键词做隐式轻分类
- **TTL 缓存**：每次对话会 new 多个 GrowthEngine，若每个都读一次 SQLite
  太浪费；模块级 30min 缓存，today 情绪更新时显式 invalidate
- **零 LLM + fail-open**：画像空 / 读取失败 → 返回空 digest（available=False），
  所有调用方回落到今天原逻辑
- **kill switch**：``WillSettings.enabled=False`` 或 ``DESKMATE_WILL_ENABLED=0``
  → 空 digest
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .settings import load_will_settings

# 价值观/权衡信号关键词——命中即视为"有决断材料"
_VALUE_SIGNAL_KEYWORDS = (
    "权衡", "妥协", "优先级", "冲突", "不行", "必须", "绝不", "底线",
    "宁可", "宁可", "让位", "让路", "排序", "先后",
)

_CACHE_TTL_SEC = 30 * 60  # 30 min


@dataclass
class WillDigest:
    """调制器消费的信号包。available=False 时所有字段为空，调用方回落现状。"""

    available: bool = False
    # 高置信立场 top3：(topic, content, confidence)
    top_stances: list[tuple[str, str, float]] = field(default_factory=list)
    # ≥0.7 active 立场总数（影响 proactive_threshold 敢不敢主动）
    stance_count: int = 0
    # open gap（盲区）数（影响 proactive_threshold 该不该闭嘴）
    open_gap_count: int = 0
    # 是否有价值观权衡信号
    has_value_signal: bool = False
    # 权衡说明片段（≤2 条，供 memory_budget 加料）
    value_notes: list[str] = field(default_factory=list)
    # 最近一天五态之一（压力大/低落/顺利/忙碌/平静）
    recent_emotion: Optional[str] = None
    # 情绪证据（供 inspect 端点展示，不进调制）
    emotion_evidence: Optional[str] = None

    def empty(self) -> "WillDigest":
        return WillDigest()


_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def invalidate_digest() -> None:
    """today 情绪或画像更新后调用，强制下次重建。"""
    with _cache_lock:
        _cache.clear()


def _reset_cache_for_test() -> None:
    """测试用：清缓存强制重新构建。"""
    invalidate_digest()


def _build(base_dir: Optional[str]) -> WillDigest:
    """实际构建（无缓存）。任何异常 → 空 digest。"""
    try:
        settings = load_will_settings(base_dir=base_dir)
        if not settings.enabled:
            return WillDigest()  # kill switch

        # ---- 认知侧 ----
        from deskmate.cognitive.profile import CognitiveProfileManager

        # base_dir 兼容：传目录就拼 deskmate.db（避开 _default_db_path 不可重定向）
        if base_dir and not base_dir.endswith(".db"):
            db_for_digest = os.path.join(base_dir, "deskmate.db")
        else:
            db_for_digest = base_dir  # 已经是 .db 路径或 None（走默认 ~/.deskmate）
        pm = CognitiveProfileManager(profile_path=db_for_digest, layer="cognitive")
        profile = pm.get()

        active = [s for s in profile.stances if s.status == "active"]
        high_conf = [s for s in active if s.confidence >= 0.7]
        # top3 by confidence desc（并列时按 topic 稳定排序）
        top = sorted(high_conf, key=lambda s: (-s.confidence, s.topic))[:3]
        top_stances = [(s.topic, s.content, s.confidence) for s in top]

        # 轻分类：价值观信号
        value_notes: list[str] = []
        has_value = False
        for s in active:
            text = f"{s.topic} {s.content}"
            if any(kw in text for kw in _VALUE_SIGNAL_KEYWORDS):
                has_value = True
                # 取内容里含关键词的那一句（≤40 字）
                note = s.content.strip()
                if len(note) > 40:
                    note = note[:40] + "…"
                if note and note not in value_notes:
                    value_notes.append(note)
                if len(value_notes) >= 2:
                    break

        open_gaps = [g for g in profile.cognitive_gaps if g.status == "open"]

        # ---- 情感侧 ----
        from deskmate.growth.emotion_timeline import EmotionTimeline

        tl = EmotionTimeline(base_dir=base_dir)
        snaps = tl.snapshots(7)
        recent_emotion = snaps[-1].get("state") if snaps else None
        emotion_evidence = (
            snaps[-1].get("evidence") if (snaps and snaps[-1].get("evidence")) else None
        )

        # available=True 当且仅当至少有一类信号（非空画像 / 非空情感）
        any_signal = bool(top_stances or value_notes or recent_emotion or open_gaps)

        return WillDigest(
            available=any_signal,
            top_stances=top_stances,
            stance_count=len(high_conf),
            open_gap_count=len(open_gaps),
            has_value_signal=has_value,
            value_notes=value_notes,
            recent_emotion=recent_emotion,
            emotion_evidence=emotion_evidence,
        )
    except Exception:
        # fail-open：绝不抛给调用方
        return WillDigest()


def get_digest(base_dir: Optional[str] = None) -> WillDigest:
    """读摘要（带 TTL 缓存）。"""
    key = base_dir or ""
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and now - entry["ts"] < _CACHE_TTL_SEC:
            return entry["data"]
    d = _build(base_dir)
    with _cache_lock:
        _cache[key] = {"data": d, "ts": now}
    return d
