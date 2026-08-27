"""圆桌 teams · MCP server（stdio）。

把「创作流水线 + 圆桌终审 + docx 落地 + 圆桌研讨」暴露为 MCP tools，
让外部 Agent（如 Hermes）经标准 MCP 协议调用 openworker 的创作能力。

实现基于官方 ``mcp`` 库的 FastMCP（与上游 ``coworker/teams/mcp_server.py`` 同一套），
零额外协议代码，Hermes 侧只需配一个 MCP connector 指向本进程即可。

启动（stdio）：
    python -m deskmate.mcp.roundtable_server
或经 venv：
    .venv/bin/python -m deskmate.mcp.roundtable_server

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

import asyncio
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def build_server() -> FastMCP:
    mcp = FastMCP(
        "openworker-roundtable",
        instructions=(
            "openworker 创作能力集：① 以原生 teams 流水线把概念写成大纲/剧本"
            "（create_creation_pipeline → 逐步推进 → 定稿）；② 把剧本/大纲送圆桌"
            "多专家终审（final_review_proposal 取四要素 → 用户知情批准 → 调用方"
            "用 run_roundtable 跑研讨）；③ 定稿落地 .docx（export_docx，基于本地"
            " officecli）。工具返回结构化数据，供 Agent 决策。"
        ),
    )

    # ---- 创作流水线 ----------------------------------------------------
    @mcp.tool()
    def create_creation_pipeline(
        kind: str,
        concept: str,
        executor: str = "stub",
        novel_ref: str = "",
    ) -> dict:
        """新建创作项目并返回 {pid, kind, team_id, ids}。

        kind: "outline"（仅大纲）| "screenplay"（含大纲+剧本）。
        executor: "stub"（演示·不烧 token）| "llm"（真实创作，需 provider 凭证）。
        返回的 pid 用于后续 get_pipeline / final_review_proposal。
        """
        from deskmate.creation import service as svc

        try:
            return svc.create_pipeline(kind, concept, executor_kind=executor, novel_ref=novel_ref)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @mcp.tool()
    def get_pipeline(pid: str) -> dict:
        """查询创作流水线当前状态：各 board 项 state、team_id、phase、docx 产物。"""
        from deskmate.creation import service as svc

        try:
            return svc.snapshot(pid)
        except KeyError:
            return {"error": "not found"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @mcp.tool()
    def final_review_proposal(pid: str, item_kind: str, genre_hint: str = "") -> dict:
        """生成终审提案四要素（送审片段 / 推荐专家 / 验收标准 / 前序意见）。

        不拉圆桌、不烧 token；Agent 把四要素呈现给用户知情批准，再调 run_roundtable。
        item_kind: "outline" | "screenplay"。
        """
        from deskmate.creation import service as svc

        try:
            return svc.final_review_proposal(pid, item_kind, genre_hint=genre_hint or None)
        except KeyError:
            return {"error": "not found"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    # ---- docx 落地 ------------------------------------------------------
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

    # ---- 圆桌研讨（真实 LLM） -------------------------------------------
    @mcp.tool()
    async def run_roundtable(
        topic: str,
        personas: str = "",
        context: str = "",
        manuscript: str = "",
    ) -> str:
        """运行圆桌多专家研讨，返回《综合研判报告》markdown 全文。

        personas: 逗号分隔的 persona id（如 "historian,lit-editor"）；留空则用默认圆桌阵容。
        context / manuscript: 注入研讨的上下文与送审文本。
        注意：此工具真实调用 LLM，会消耗 token。
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

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
