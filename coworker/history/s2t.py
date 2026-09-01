"""简繁变体扩展 —— 让简体查询也能命中繁体原文（反之亦然）。

为什么需要它
------------
史料语料是**简繁混排**的：正史原文（如《史記》《舊唐書》）多为繁体，而现代
整理本（如明史系列）多为简体。FTS5 按单字建索引，``项羽`` 与 ``項羽`` 是两组
完全不同的 token，只查原形会漏掉另一种字形的全部史料。

在真库上实测（``~/.deskmate/history``，2026-09-01）：

===================  ============  ============  ==========
查询词               duan-dai-shi  shibu-shi     说明
===================  ============  ============  ==========
项羽 / 項羽          35.5%          67.4%         繁体形占比
韩信 / 韓信          21.5%          65.4%
诸葛亮 / 諸葛亮       28.1%          50.5%
玄武门 / 玄武門       41.9%          58.8%
===================  ============  ============  ==========

即：**只查原形，史部子集会漏掉近四成史料**。

为什么必须带词组字典
--------------------
一简对多繁的字（历→歷/曆、发→發/髮、谷→谷/穀、后→后/後）按字转换会出错，
在史书里尤其致命：

  - 字级转换：``万历`` → ``萬歷``（错，年号应为 ``萬曆``）
  - 词组转换：``万历`` → ``萬曆``（对）
  - 词组转换：``山谷`` → ``山谷``（对，不会被误转为 ``山穀``）

因此这里用的是 OpenCC 的完整 s2t/t2s 配置（词组字典 + 字字典 + 最长匹配分词），
而不是简单的单字查表。

失败策略
--------
转换只是**召回增强**，不是正确性依赖。字典文件缺失（打包漏带）、opencc 内部报错
等任何异常都不得让检索崩掉 —— 一律静默退化为「只有原形」，行为等同未启用本模块。

可用 ``HISTORY_SCRIPT_VARIANTS=0`` 关闭变体扩展（A/B 对比召回率时用）。
"""

from __future__ import annotations

import os
import threading
from typing import Optional

_lock = threading.Lock()
_s2t = None  # Optional[OpenCC]
_t2s = None  # Optional[OpenCC]
_loaded = False


def _load() -> None:
    """懒加载两个方向的转换器；失败就永久关停（不重试，避免每次检索都抛异常）。"""
    global _s2t, _t2s, _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _loaded = True  # 无论成败都置位：失败即永久降级，不再反复尝试
        try:
            from .vendor.opencc import OpenCC

            _s2t = OpenCC("s2t")
            _t2s = OpenCC("t2s")
        except Exception:
            _s2t = None
            _t2s = None


def _enabled() -> bool:
    return os.environ.get("HISTORY_SCRIPT_VARIANTS", "1").strip() not in ("0", "false", "no")


def converters() -> tuple[Optional[object], Optional[object]]:
    """返回 ``(s2t, t2s)``；不可用时为 ``(None, None)``。"""
    if not _enabled():
        return (None, None)
    if not _loaded:
        _load()
    return (_s2t, _t2s)


def convert(text: str, direction: str) -> str:
    """单方向转换；不可用时原样返回。``direction`` 为 ``"s2t"`` 或 ``"t2s"``。"""
    if not text:
        return text
    s2t, t2s = converters()
    fn = s2t if direction == "s2t" else t2s
    if fn is None:
        return text
    try:
        return fn.convert(text)  # type: ignore[attr-defined]
    except Exception:
        return text


def to_traditional(text: str) -> str:
    return convert(text, "s2t")


def to_simplified(text: str) -> str:
    return convert(text, "t2s")


def variants(query: str) -> list[str]:
    """返回查询词的简繁变体，**原形排在第一位**（排序时原形命中最优先）。

    ``转繁`` 与 ``转简`` 两个方向都试一遍，因此无论用户输入简体还是繁体都能
    覆盖对侧。转换不可用时返回 ``[query]``，绝不抛异常。
    """
    query = query or ""
    if not query or not _enabled():
        return [query] if query else []
    s2t, t2s = converters()
    if s2t is None and t2s is None:
        return [query]

    out = [query]
    for fn in (s2t, t2s):
        if fn is None:
            continue
        try:
            v = fn.convert(query)  # type: ignore[attr-defined]
        except Exception:
            continue
        if v and v != query and v not in out:
            out.append(v)
    return out
