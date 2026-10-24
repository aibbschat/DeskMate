"""回归测试：主对话是否真的把 proactivity MCP 的 3 个阶段六工具暴露给 function-calling。

复刻主对话启动时的真实加载路径：
  run.py:_seed_builtin_mcp_servers()  -> ensure_builtin_servers()
  load_mcp_servers()                  -> 解析 mcp.json 得到 MCPServerDef 列表
  MCPManager.ensure(server).tools     -> stdio 拉起 server 并 list_tools()
若 3 个新工具出现在 tools 列表里，说明主对话的 function-calling schema 一定能拿到它们。

直接对应 docs/小桔主动性改造技术文档_2026-10-24.md 阶段六「任务直达与跟进状态」：
补上「主对话此前调不到状态流转、只能靠前端按钮」的关键缺口。

注意：本测试会 stdio 拉起 proactivity MCP server 子进程，需在关闭沙箱
（dangerouslyDisableSandbox）且剥离 PYTHONPATH/NODE_OPTIONS shim 的环境下跑，
与项目其它 proactivity 测试一致。
"""
from __future__ import annotations

import asyncio

from coworker.mcp.builtin import ensure_builtin_servers
from coworker.mcp.config import load_mcp_servers
from coworker.mcp.client import MCPManager
from coworker.mcp.tools import tool_name

EXPECTED = {
    "proactive_next_steps",
    "proactive_start_task",
    "proactive_finish_task",
}


def test_proactivity_mcp_tools_exposed_to_main_chat():
    # 1) 与主对话同款：先 seed builtin（run.py 启动时会做，幂等非破坏）
    ensure_builtin_servers()

    # 2) 解析 mcp.json 得到 server 定义列表
    servers = load_mcp_servers()
    names = [s.name for s in servers]
    assert "deskmate-proactivity" in names, (
        "mcp.json 里没有 'deskmate-proactivity' —— 主对话根本不会加载它"
    )

    # 3) 复刻主对话：用 MCPManager 起连接并 list_tools()
    srv = next(s for s in servers if s.name == "deskmate-proactivity")
    mgr = MCPManager()
    conn = asyncio.run(mgr.ensure(srv))
    try:
        got = {t.name for t in conn.tools}
        missing = EXPECTED - got
        assert not missing, f"以下阶段六工具未暴露给主对话: {sorted(missing)}"

        # 4) 顺带确认主对话 function-calling 真实工具名可正确构造
        for t in sorted(EXPECTED):
            assert tool_name("deskmate-proactivity", t).startswith(
                "mcp__deskmate-proactivity__"
            )
    finally:
        # 尽力关掉子进程，避免测试进程退出前残留
        closer = getattr(conn, "aclose", None) or getattr(conn, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
