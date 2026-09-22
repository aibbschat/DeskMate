"""deskmate-growth MCP 服务 — 成长系统工具。

通过 OpenWorker 的 MCP 机制暴露成长状态查询与事件上报。运行方式：
    <venv>/bin/python -m deskmate.growth.server

工具列表：
- growth_stats            成长状态快照（等级/亲密度/信任感/默契度/技能熟练度）
- growth_set_proactive    设置信任感是否调制 proactive 阈值
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .engine import GrowthEngine

mcp = FastMCP("deskmate-growth")

def _base_dir() -> Path:
    """**调用时**解析数据目录（禁模块级常量：import 期绑定会让隔离失效）。"""
    from deskmate.paths import deskmate_base_dir

    return deskmate_base_dir()


@mcp.tool()
def growth_stats() -> str:
    """获取成长系统状态快照（等级、亲密度、信任感、默契度、技能熟练度、事件时间线）。

    用于前端渲染成长面板，以及诊断「越用越懂你」的进展。
    """
    try:
        import json

        engine = GrowthEngine(base_dir=_base_dir())
        return json.dumps(engine.snapshot(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取成长状态失败：{e}"


@mcp.tool()
def growth_set_proactive(control: bool) -> str:
    """设置信任感是否调制 proactive 主动打扰阈值。

    Args:
        control: True=信任感影响主动打扰（默认），False=关闭该调制

    Returns:
        操作结果
    """
    try:
        engine = GrowthEngine(base_dir=_base_dir())
        engine.state.trust_controls_proactive = bool(control)
        engine.save()
        status = "开启" if control else "关闭"
        return f"信任感调制 proactive 阈值已{status}"
    except Exception as e:
        return f"设置失败：{e}"


@mcp.tool()
def growth_skill_proficiency(skill_name: str = "") -> str:
    """查询技能熟练度。

    Args:
        skill_name: 技能名；留空返回全部技能熟练度

    Returns:
        熟练度 JSON（proficiency 0-1、uses、success_rate、last_used）
    """
    try:
        import json

        engine = GrowthEngine(base_dir=_base_dir())
        profs = engine.state.skill_proficiency
        if skill_name:
            p = profs.get(skill_name)
            return json.dumps(p.to_dict() if p else {}, ensure_ascii=False, indent=2)
        return json.dumps(
            {name: p.to_dict() for name, p in profs.items()},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"查询熟练度失败：{e}"


def main() -> None:
    parser = argparse.ArgumentParser(description="deskmate-growth MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输模式 (default: stdio)")
    parser.add_argument("--port", type=int, default=8095, help="SSE/HTTP 端口 (default: 8095)")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"deskmate-growth MCP server listening on port {args.port} ({args.transport})")
        mcp.run(transport=args.transport, mount_path=f"/{args.transport}")


if __name__ == "__main__":
    main()
