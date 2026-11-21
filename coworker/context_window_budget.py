"""出站上下文「总预算硬上限」——实验装置（默认关闭）。

背景（萧哥 2026-11-20）：一次「读原文理路线」的问答，prompt_chars 达 9–13 万、
9 分钟内 20 次模型往返，比 Hermes 慢得多。已有分级预算各管一路
（memory-context 13000 token / 工具 schema 48000 / compaction 102400 token），
但**全局出站没有总预算**。此模块在唯一 provider 出口
``engine._outbound_messages`` 处提供一个可选的汇总硬顶，用来做 A/B 实验：
验证「上下文总量 + 往返次数」到底是不是慢的根因。

设计红线（不可违背）：
1. **默认关闭**：``COWORKER_CONTEXT_TOTAL_BUDGET`` 缺省 / ``0`` → 逐字节原样返回，
   不动 prompt-cache 前缀契约（昨天 11-19 刚优化过缓存命中）。
2. **不影响答案质量**：以下消息无条件豁免，绝不裁——
   - system（人格底座「她是谁」）
   - 首条 user（原始任务）
   - 末条 user（当前问题 + 注入的 <system-context>）
   - 带 tool_calls 的 assistant（agent loop 的决策链）
   - tool result（读进来的小说原文＝答案依据；除非显式 opt-in）
3. **可观测**：返回 report，记录 before/after、各路字符、豁免路由、裁了几轮、
   裁了哪些路由；便于写进 trace 与人工核对。
4. **fail-open**：任何异常（畸形消息 / 环境变量非法）一律原样返回并记 error，
   绝不阻断一轮对话。
5. **底座无关**：只用标准库，不 import coworker / deskmate 任何东西。
   换底座时这个文件可原样搬走。

裁剪策略：只从「中间的 assistant 纯文本轮次」 oldest-first 移除；这是 agent
loop 里价值最低的部分（过程自述）。保质量的裁剪会破坏前缀缓存，故默认关闭，
由实验者权衡。显式 opt-in（COWORKER_CONTEXT_TRIM_TOOL_RESULTS=1）才允许裁
tool result——即便如此也只从最旧的一条开始，且保留每条尾部（答案常引用末尾）。
"""

from __future__ import annotations

import os
from typing import Any

_ENV_BUDGET = "COWORKER_CONTEXT_TOTAL_BUDGET"
_ENV_TRIM_TOOL = "COWORKER_CONTEXT_TRIM_TOOL_RESULTS"

_EXEMPT_ROUTES_DEFAULT = ("system", "tool", "first_user", "last_user", "assistant_toolcall")


def _content_chars(content: Any) -> int:
    """统计一条消息 content 的字符数；content-parts（list）逐 part 累加。"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                txt = part.get("text")
                if isinstance(txt, str):
                    total += len(txt)
        return total
    return 0


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return _env_int(_ENV_BUDGET, 0) > 0


def measure(messages: list[dict[str, Any]]) -> dict[str, int]:
    """按 role 分路统计出站消息字符数（与 llm_usage 总量口径对齐）。

    system / user / assistant / tool 分别累计，另给 total 与消息条数。
    不修改入参。畸形消息跳过（不抛）。
    """
    out = {"system": 0, "user": 0, "assistant": 0, "tool": 0, "total": 0, "n_messages": 0}
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        out["n_messages"] += 1
        n = _content_chars(m.get("content"))
        if role in out:
            out[role] += n
        out["total"] += n
    return out


def _is_toolcall_assistant(m: dict[str, Any]) -> bool:
    return bool(m.get("tool_calls")) or bool(m.get("function_call"))


def _is_exempt(m: dict[str, Any], idx: int, first_user_idx: int, last_user_idx: int) -> bool:
    role = m.get("role", "")
    if role == "system":
        return True
    if role == "tool":
        return True  # trim 与否由外层按 opt-in 决定，这里只标豁免路由
    if role == "user" and idx in (first_user_idx, last_user_idx):
        return True
    if role == "assistant" and _is_toolcall_assistant(m):
        return True
    return False


def apply(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按总预算裁剪出站消息，返回 (新列表, report)。不修改入参。

    关闭 / 异常 → 原样返回，report 标 disabled / error。
    """
    stats_in = measure(messages)
    report: dict[str, Any] = {
        "enabled": False,
        "before": stats_in["total"],
        "after": stats_in["total"],
        "budget": _env_int(_ENV_BUDGET, 0),
        "routes": stats_in,
        "exempt_routes": list(_EXEMPT_ROUTES_DEFAULT),
        "trimmed_routes": [],
        "removed_turns": 0,
        "at_exempt_floor": False,
    }
    try:
        budget = report["budget"]
        if budget <= 0:
            return messages, report
        report["enabled"] = True
        trim_tool = _env_int(_ENV_TRIM_TOOL, 0) > 0
        if trim_tool:
            report["exempt_routes"] = [r for r in report["exempt_routes"] if r != "tool"]

        # 找首条 / 末条 user 的下标
        user_idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
        if not user_idx:
            return messages, report
        first_u, last_u = user_idx[0], user_idx[-1]

        # 豁免总量（不可裁）——先算出来，预算低于它就整个放弃（宁可超也不伤核心）
        exempt_chars = 0
        for i, m in enumerate(messages):
            if not isinstance(m, dict):
                exempt_chars += 0
                continue
            if _is_exempt(m, i, first_u, last_u) and not (
                m.get("role") == "tool" and trim_tool
            ):
                exempt_chars += _content_chars(m.get("content"))
        if exempt_chars >= budget:
            report["at_exempt_floor"] = True
            report["after"] = stats_in["total"]
            return messages, report

        # 可裁候选：中间历史里的 assistant 纯文本轮次（oldest-first）。
        # 默认不含 tool result；opt-in 时 tool result 也进候选（同样 oldest-first，保尾部）。
        candidates: list[tuple[int, str]] = []
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or i in (first_u, last_u):
                continue
            role = m.get("role", "")
            if role == "assistant" and not _is_toolcall_assistant(m):
                candidates.append((i, "middle_text"))
            elif role == "tool" and trim_tool:
                candidates.append((i, "tool"))

        out = list(messages)
        current = stats_in["total"]
        removed = 0
        drop_idx: set[int] = set()
        for i, route in candidates:
            if current <= budget:
                break
            if i in drop_idx:
                continue
            m = out[i]
            n = _content_chars(m.get("content"))
            if route == "tool" and trim_tool:
                # opt-in 的 tool result：不整条删，保尾部 1/4（答案常引用末尾）
                content = m.get("content")
                if isinstance(content, str) and len(content) > 40:
                    keep = max(40, len(content) // 4)
                    tail = content[-keep:]
                    current -= len(content) - len(tail)
                    out[i] = {**m, "content": tail}
                    if "tool" not in report["trimmed_routes"]:
                        report["trimmed_routes"].append("tool")
            else:
                # 中间 assistant 过程轮次：整条移除（不是替换成长占位符——
                # 占位符比短文本还长，替换会反向膨胀，这是必须避免的）。
                drop_idx.add(i)
                current -= n
                removed += 1
                if "middle_text" not in report["trimmed_routes"]:
                    report["trimmed_routes"].append("middle_text")

        if drop_idx:
            out = [m for i, m in enumerate(out) if i not in drop_idx]
        report["removed_turns"] = removed
        report["after"] = current
        return out, report
    except Exception as exc:  # noqa: BLE001 — fail-open
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["after"] = stats_in["total"]
        return messages, report
