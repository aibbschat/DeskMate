"""共享 JSON 解析工具（writing / report / app 统一使用）。

提供：
  - extract_json(raw)  — 从 LLM 返回中鲁棒地解析 JSON（兼容代码块、截断、多余文本）
  - call_json(system, user, ...) — 调用 LLM 并解析为 JSON，失败自动重试一次（加大 token）

合并自 writing.py 和 report.py 的两套实现，取两者之长：
  - report.py 的 _repair_json（修复被截断的不完整 JSON）
  - report.py 的重试 token 加大策略
"""

from __future__ import annotations

import json
import re
from typing import Any

from deskmate.memory.llm import chat


# --------------------------------------------------------------------------- #
# JSON 修复（处理被 max_tokens 截断的不完整 JSON）
# --------------------------------------------------------------------------- #

def _repair_json(s: str) -> str | None:
    """尝试修复被截断 / 不完整的 JSON 字符串。

    策略：扫描字符定位未闭合的定界符与未结束的字符串，补上缺失的右定界符，
    并裁掉末尾不完整的 token（如尾随逗号 / 冒号）。无法修复时返回 None。
    """
    if not s:
        return None
    s = s.rstrip()
    if not s:
        return None
    stack: list[str] = []
    open_to_close = {"{": "}", "[": "]"}
    in_str = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in open_to_close:
            stack.append(open_to_close[ch])
        elif stack and stack[-1] == ch:
            stack.pop()
    # 末尾若停留在未结束的字符串内，先闭合引号
    if in_str:
        s += '"'
    # 移除末尾可能残留的尾随逗号 / 冒号（不完整 token）
    s = s.rstrip()
    while s.endswith(",") or s.endswith(":"):
        s = s[:-1].rstrip()
    # 补上缺失的右定界符（逆序闭合）
    if stack:
        s += "".join(reversed(stack))
    return s


# --------------------------------------------------------------------------- #
# 核心解析
# --------------------------------------------------------------------------- #

_MISSING = object()


def _try_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _MISSING


def extract_json(raw: str) -> Any:
    """从 LLM 返回中鲁棒地解析 JSON。

    兼容：
    - ```json ... ``` 代码块包裹
    - 前后有多余文本 / 思考链污染
    - 被 max_tokens 截断的不完整 JSON（自动修复）
    """
    if raw is None:
        raise ValueError("empty response")
    raw = raw.strip()

    # 1) 优先剥离 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if m:
        body = m.group(1).strip()
        val = _try_json(body)
        if val is not _MISSING:
            return val
        repaired = _repair_json(body)
        if repaired is not None:
            val = _try_json(repaired)
            if val is not _MISSING:
                return val

    # 2) 整段直接尝试
    val = _try_json(raw)
    if val is not _MISSING:
        return val
    repaired = _repair_json(raw)
    if repaired is not None:
        val = _try_json(repaired)
        if val is not _MISSING:
            return val

    # 3) 取最外层 {...} / [...] 窗口（容错前后多余文本 / 思考链污染）
    for op, cl in (("{", "}"), ("[", "]")):
        s, e = raw.find(op), raw.rfind(cl)
        if s != -1 and e != -1 and e > s:
            window = raw[s : e + 1]
            val = _try_json(window)
            if val is not _MISSING:
                return val
            repaired = _repair_json(window)
            if repaired is not None:
                val = _try_json(repaired)
                if val is not _MISSING:
                    return val

    raise ValueError("无法从模型返回中解析 JSON")


# --------------------------------------------------------------------------- #
# LLM 调用 + JSON 解析（含重试）
# --------------------------------------------------------------------------- #

def call_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> Any:
    """调用 LLM 并解析为 JSON。失败自动重试一次（加大 token + 强制纯 JSON 指令）。"""
    raw = chat(system, user, temperature=temperature, max_tokens=max_tokens)
    try:
        return extract_json(raw)
    except ValueError:
        # 重试：加大 max_tokens 并强制纯 JSON 输出
        raw2 = chat(
            system + "\n重要：只输出可被 JSON.parse 解析的纯 JSON，不要任何解释、"
            "不要代码块包裹、不要 Markdown 标记。",
            user,
            temperature=temperature,
            max_tokens=min(max(max_tokens + 2000, 5000), 8000),
        )
        return extract_json(raw2)
