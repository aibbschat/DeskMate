"""创作落地 · MCP server（stdio）。

2026-11-20 按「有消费者才留」收敛：只暴露 `export_docx` 一个工具。

原先 13 个工具里，11 个创作流水线工具全部被 web.py/前端 HTTP-SSE 覆盖且强绑定
进程内 pid，主 agent 拿不到 pid、场景不成立；`run_roundtable` 是同步长任务，
直接暴露会重演 consult_experts 当年 240s 同步超时的覆辙，故保留实现但不注册，
等后台化改造（派发即返回 + consult_runs 回执）后再考虑放行。

Hermes / 其他 MCP client 配置示例（stdio）：
    {
      "mcpServers": {
        "openworker-roundtable": {
          "command": "/Users/xiaocheng/workspace/openworker/.venv/bin/python",
          "args": ["-m", "deskmate.mcp.roundtable_server"],
          "cwd": "/Users/xiaocheng/workspace/openworker"
        }
      }
    }
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP


def build_server() -> FastMCP:
    mcp = FastMCP(
        "openworker-roundtable",
        instructions=(
            "创作落地能力：把给定 Markdown 定稿导出为本地 .docx（基于 officecli，"
            "缺失时降级 .md）。纯本地、零 token、幂等。"
        ),
    )

    @mcp.tool()
    def export_docx(kind: str, title: str, markdown: str) -> dict:
        """把给定 Markdown 定稿导出为 .docx（基于本地 officecli）。

        kind: "outline" | "screenplay"。返回 {ok, path}；officecli 缺失时降级 .md。
        """
        from deskmate.creation.docx_export import export_creation_docx

        try:
            return export_creation_docx(kind, title or "未命名", markdown)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    return mcp


async def run_roundtable(
    topic: str,
    personas: str = "",
    context: str = "",
    manuscript: str = "",
) -> str:
    """[未注册·保留实现] 圆桌多专家研讨，返回《综合研判报告》全文。

    2026-11-20 起不再挂 @mcp.tool()：同步 await Roundtable(...).run() 会把主
    agent 卡住数分钟，违反「专家咨询＝后台任务，派发即返回」红线。
    复活前置：改成后台派发（consult_runs.json + claim_delivery + notifier）。
    """
    from deskmate.roundtable.coordinator import Roundtable
    from deskmate.roundtable.personas import get_persona, load_personas

    pids = [p.strip() for p in personas.split(",") if p.strip()]
    plist = [p for pid in pids if (p := get_persona(pid)) is not None] if pids else load_personas()
    result = await Roundtable(
        topic=topic,
        personas=plist,
        context=context,
        manuscript_text=manuscript,
    ).run()
    return result.synthesis


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
