"""deskmate-proactivity MCP 服务（stdio）— 主动推理工具。

通过 OpenWorker 的 MCP 机制暴露主动推理工具。运行方式：
    <venv>/bin/python -m deskmate.proactivity.server

工具列表：
- proactive_tick      执行一次主动推理 tick（感知→判断→决策）
- proactive_feedback   记录用户对建议的反馈
- proactive_silent     设置静默模式
- proactive_stats      获取防打扰统计
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .tick import ProactiveEngine

mcp = FastMCP("deskmate-proactivity")

def _base_dir() -> Path:
    """**调用时**解析数据目录。

    禁模块级常量（2026-09-22 收口）：import 期求值有两个害处——
    (a) 把真实 ``~/.deskmate`` 绑死，而 conftest 的 autouse fixture 在
        import **之后**才 setenv，隔离全线失效；
    (b) 只 import 本模块就产生「构造引擎 + 读盘」的副作用。
    """
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()


# 全局引擎单例（惰性：首次调用才构造并加载防打扰状态）
_engine: ProactiveEngine | None = None


def _get_engine() -> ProactiveEngine:
    global _engine
    if _engine is None:
        _engine = ProactiveEngine(base_dir=_base_dir())
        # 加载防打扰持久化数据
        _engine.disturb.persist(_base_dir() / "memory" / "disturb_state.json")
    return _engine


@mcp.tool()
def proactive_tick(
    user_message: str = "",
    workspace: str = "",
    memory_context: str = "",
    project_context: str = "",
) -> str:
    """执行一次主动推理 tick。

    收集当前桌面状态，判断是否需要主动提供帮助。
    建议在对话轮次之间定期调用（如每 30-60 秒一次）。

    Args:
        user_message: 当前用户消息（如有）
        workspace: 当前工作目录
        memory_context: 相关记忆上下文（由 build_memory_context 获取）
        project_context: 项目上下文

    Returns:
        推理结果 JSON（action/confidence/suggestion_content 等）
    """
    try:
        import json

        engine = _get_engine()

        if workspace:
            engine.workspace = workspace
        if memory_context:
            engine.set_memory_context(memory_context)
        if project_context:
            engine.set_project_context(project_context)

        result = engine.tick(user_message=user_message)
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)

    except Exception as e:
        import json
        return json.dumps(
            {"action": "no_action", "reason": f"tick 失败: {e}"},
            ensure_ascii=False,
        )


@mcp.tool()
def proactive_feedback(suggestion_key: str, accepted: bool) -> str:
    """记录用户对主动建议的反馈。

    Args:
        suggestion_key: 建议的标识（建议内容前 100 字符）
        accepted: 用户是否接受了建议

    Returns:
        操作结果
    """
    try:
        _get_engine().record_feedback(suggestion_key, accepted)
        status = "接受" if accepted else "忽略"
        return f"已记录反馈：{status}"
    except Exception as e:
        return f"记录反馈失败：{e}"


@mcp.tool()
def proactive_silent(silent: bool) -> str:
    """设置主动推理的静默模式。

    Args:
        silent: True=静默（不主动打扰），False=正常模式

    Returns:
        操作结果
    """
    try:
        _get_engine().set_silent(silent)
        if silent:
            return "已进入静默模式，不会主动打扰"
        return "已退出静默模式"
    except Exception as e:
        return f"设置失败：{e}"


@mcp.tool()
def proactive_stats() -> str:
    """获取主动推理的防打扰统计信息。

    Returns:
        统计信息
    """
    try:
        import json

        stats = _get_engine().disturb.get_stats()
        return json.dumps(stats, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取统计失败：{e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
