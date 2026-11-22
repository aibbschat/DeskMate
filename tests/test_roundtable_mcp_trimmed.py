"""roundtable MCP 去留契约（2026-11-20 萧哥拍板：按需接除）。

- 只接 `export_docx`：唯一不依赖 GUI pid、零 token、其它 MCP 无替代的能力。
- 摘掉 11 个被 web/前端 HTTP 完全覆盖的工具（含同步长任务的 draft/review）。
- `run_roundtable` 保留实现但不注册：同步长任务会重演 consult_experts 超时，
  须先做后台化改造才能放行。
"""

from pathlib import Path

from coworker.mcp.builtin import BUILTIN_SERVERS

_REPO = Path(__file__).resolve().parents[1]


def test_roundtable_server_registered_as_builtin():
    assert "openworker-roundtable" in BUILTIN_SERVERS
    assert (
        BUILTIN_SERVERS["openworker-roundtable"]["module"]
        == "deskmate.mcp.roundtable_server"
    )


def test_persona_pins_only_export_docx():
    md = (_REPO / "coworker" / "personas" / "builtin" / "deskmate.md").read_text(
        encoding="utf-8"
    )
    assert "- openworker-roundtable" in md
    assert "mcp:openworker-roundtable:export_docx" in md


def test_server_exposes_exactly_one_tool():
    src = (_REPO / "deskmate" / "mcp" / "roundtable_server.py").read_text(
        encoding="utf-8"
    )
    registered = [ln for ln in src.splitlines() if ln.strip() == "@mcp.tool()"]
    assert len(registered) == 1
    assert "def export_docx(" in src
    assert "@mcp.tool()\n    async def run_roundtable" not in src


def test_run_roundtable_implementation_retained_for_future_async_work():
    src = (_REPO / "deskmate" / "mcp" / "roundtable_server.py").read_text(
        encoding="utf-8"
    )
    assert "async def run_roundtable(" in src
