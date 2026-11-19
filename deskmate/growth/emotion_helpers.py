"""情绪引擎开关/谓词 helper（从 emotion_engine.py 机械抽取，PR16/#33）。

各层启用开关（emotion/chord/echo/pending/temper）+ 到期桶解析 + 同事判定 + 辱骂判定。
行为与抽取前一致；常量经父模块 emotion_engine 命名空间解析（_L）。
emotion_engine.py 通过 re-export 保持 from deskmate.growth.emotion_engine import X 不变。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

# _L 懒代理父模块（复检修复）：避免子模块被「首个直接导入」时与父模块 re-export 循环导入。
# 仅在函数调用访问 _L.X 时解析父模块（此时必已完整加载）；monkeypatch(父, X) 经 getattr 实时生效。
class _LazyParent:
    def __getattr__(self, name):
        from deskmate.growth import emotion_engine as _m
        return getattr(_m, name)
_L = _LazyParent()


def _is_enabled() -> bool:
    return os.getenv("DESKMATE_EMOTION_ENGINE", "1") != "0"


def _chord_enabled() -> bool:
    """情绪和弦开关（独立通道 · 步 7 回退闸）。默认开；=0 退回单值标签。"""
    return os.getenv("DESKMATE_EMOTION_CHORD", "1") != "0"


def _echo_enabled() -> bool:
    """回潮开关（二期回退闸）。默认开；=0 旧事不再泛微澜。"""
    return os.getenv("DESKMATE_EMOTION_ECHO", "1") != "0"


def _pending_enabled() -> bool:
    """等待开关（二期回退闸）。默认开；=0 悬事不立项不爬坡。"""
    return os.getenv("DESKMATE_EMOTION_PENDING", "1") != "0"


def _due_ts_from_bucket(bucket: str) -> str | None:
    """LLM 报的粗粒度兑期 → due_ts（规则映射，LLM 不碰数值）。fail-open → None。"""
    try:
        hours = _L.PENDING_DUE_BUCKET_HOURS.get((bucket or "").strip().lower())
        if not hours:
            return None
        return (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
    except Exception:
        return None


def _same_affair(title: str, other: str) -> bool:
    """两件事名是否算「同一摊事」：字符重合 ≥0.6 且两边都够长。

    短名（如「悬事A」）撞字概率高，不参与模糊合并；只留给 spoken 侦探转述
    同一件事时的变体（「等 review 核稿结果」↔「等主编的 review」）。
    """
    try:
        a = {ch.lower() for ch in (title or "") if ch.isalnum()}
        b = {ch.lower() for ch in (other or "") if ch.isalnum()}
        if len(a) < _L.PENDING_FUZZY_DEDUP_MIN_CHARS \
                or len(b) < _L.PENDING_FUZZY_DEDUP_MIN_CHARS:
            return False
        return len(a & b) / max(len(a), len(b)) >= _L.PENDING_FUZZY_DEDUP_RATIO
    except Exception:
        return False


def _temper_enabled() -> bool:
    """脾气开关（二期回退闸）。默认开；=0 注入块脾气段整段消失。"""
    return os.getenv("DESKMATE_EMOTION_TEMPER", "1") != "0"


def _is_abusive(text: str) -> bool:
    """脾气黑名单：大骂/脏字是另一性质，不是小刺。命中 → temper_foul 留痕。"""
    if not text:
        return False
    return any(w in text for w in _L._ABUSIVE_WORDS)


