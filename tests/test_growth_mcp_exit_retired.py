"""deskmate-growth MCP 出口退役契约（2026-11-20 萧哥拍板：摘出口，数据本体留）。

三个 MCP 工具与 HTTP 端点重复、主 agent prompt 零引用；真消费者全走
GrowthEngine 直连与 HTTP/前端。只从 builtin 清单摘除，growth/server.py
与数据层不动。
"""

from coworker.mcp.builtin import BUILTIN_SERVERS


def test_growth_server_not_seeded_as_builtin():
    assert "deskmate-growth" not in BUILTIN_SERVERS


def test_growth_server_module_still_exists():
    """只摘出口，不连根拔：server.py 本体保留，需要时可手工复活。"""
    import importlib.util

    spec = importlib.util.find_spec("deskmate.growth.server")
    assert spec is not None
