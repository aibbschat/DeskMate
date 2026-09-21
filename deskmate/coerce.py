"""数值兜底转换：**只在真的缺省时**回退默认值。

## 为什么单独一个模块

``float(v or default)`` 是个反复踩的坑：``0`` / ``0.0`` 在 Python 里是 **falsy**，
于是"合法的零值"会被当成"没有值"而替换成默认值。

2026-09-21 现场（小桔记忆/成长层）：

- ``growth.state.trust`` 真实值是 **0.0**（连续纠正把它压到了下限）；
- 但 ``emotion_timeline.record_today`` 里写的是
  ``float(getattr(st, "trust", 0.5) or 0.5)`` → **0.0 被吞成 0.5**；
- ``growth/diary.py`` 的日记落款同样 → 日记里写「信任 0.5」；
- 结果：情感时间线 surface 出 ``0.44 → 0.34 → … → 0.02 → 0.5 ×17 天``，
  看起来像"指标在 09-05 冻结了 17 天"，其实一直是 **0.0**。

**判据：缺省与否看 ``is None`` / 空串 / 转换失败，不看真假。**
全仓统一走 ``as_float`` / ``as_int``，别再手写 ``or``。
"""
from __future__ import annotations

from typing import Any


def as_float(value: Any, default: float = 0.0) -> float:
    """转 float；仅 ``None`` / 空串 / 不可转换时回退 ``default``。0 是合法值。"""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    """转 int；仅 ``None`` / 空串 / 不可转换时回退 ``default``。0 是合法值。"""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default
