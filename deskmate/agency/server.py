"""L2 治理层 MCP 服务（stdio）— 自主层工具。

通过 OpenWorker 的 MCP 机制暴露 L2 工具。运行方式：
    <venv>/bin/python -m deskmate.agency.server

工具列表：
- agency_status          查看总控开关 / 白名单 / 最近任务 / 待裁决提议卡
- agency_set_enabled     开/关 L2 自主层（写 l2_settings.json）
- agency_tick            强制跑一次 L2 tick
- agency_propose_resolve 裁决高风险提议卡（执行 / 不用 / 以后别提）
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .engine import AgencyEngine
from .settings import load_agency_settings, save_agency_settings, AgencySettings
from .task_log import TaskStatus

mcp = FastMCP("deskmate-agency")

_base_dir = Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
_engine = AgencyEngine(base_dir=_base_dir)


def _get_engine() -> AgencyEngine:
    return _engine


@mcp.tool()
def agency_status() -> str:
    """查看 L2 自主层状态：总控开关、白名单、最近自主任务、待裁决提议卡。"""
    try:
        import json
        return json.dumps(_get_engine().status(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取状态失败：{e}"


@mcp.tool()
def agency_set_enabled(enabled: bool) -> str:
    """开/关 L2 自主层（小桔替你行动的开关）。

    Args:
        enabled: True=开启自主层，False=关闭（等同总开关拨到关）

    Returns:
        操作结果
    """
    try:
        settings = load_agency_settings(str(_base_dir))
        settings.enabled = bool(enabled)
        ok = save_agency_settings(settings, base_dir=str(_base_dir))
        return "已开启 L2 自主层" if enabled and ok else ("已关闭 L2 自主层" if ok else "保存失败")
    except Exception as e:
        return f"设置失败：{e}"


@mcp.tool()
def agency_tick() -> str:
    """强制跑一次 L2 tick（自主执行低风险动作 / 生成高风险提议卡）。"""
    try:
        import json
        return json.dumps(_get_engine().run_now(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"tick 失败：{e}"


@mcp.tool()
def agency_propose_resolve(record_id: str, decision: str) -> str:
    """裁决一条高风险提议卡。

    Args:
        record_id: 提议卡 id（来自 agency_status 的 pending_proposals）
        decision:   executed=执行 / rejected=不用 / never=以后别提

    Returns:
        操作结果
    """
    try:
        if decision not in (TaskStatus.EXECUTED, TaskStatus.REJECTED, TaskStatus.NEVER):
            return "decision 必须是 executed / rejected / never"
        ok = _get_engine().resolve_proposal(record_id, decision)
        label = {"executed": "已执行", "rejected": "已忽略", "never": "以后别提"}.get(decision, decision)
        return f"{label}" if ok else "未找到该提议卡或已裁决"
    except Exception as e:
        return f"裁决失败：{e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
