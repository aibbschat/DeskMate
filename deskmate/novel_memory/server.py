"""novel-memory MCP 服务（stdio）。

通过 OpenWorker 的 MCP 机制暴露小说记忆工具。运行方式（由 mcp.json 调用）：
    <venv>/bin/python -m deskmate.novel_memory.server
要求 cwd 为 openworker 仓库根目录（保证 deskmate 包可导入）。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .core import (
    build_bible,
    check_consistency,
    check_consistency_enhanced,
    inject_bible,
    incremental_bible_update,
    novel_recall,
    novel_relationships,
    read_bible,
)
from . import core as _core

mcp = FastMCP("novel-memory")


@mcp.tool()
def analyze_novel(folder: str) -> str:
    """分析一本小说文件夹：通读全书正文，抽取权威「人物关系圣经」
    （敌/友/亲/暧昧/上下级及依据），生成大纲、时间线、设定备注，
    并写入该工作区的 OpenWorker 记忆（后续对话自动带上，根治人物关系错乱）。

    Args:
        folder: 小说文件夹的绝对路径（即当前 OpenWorker 工作区）。
    """
    try:
        docs = build_bible(folder)
        written = inject_bible(folder, docs)
        rel = str(len(docs)) + " 份文档"
        names = "、".join(docs.keys()) or "（无）"
        return (
            f"✅ 已完成《{folder.split('/')[-1]}》的人物圣经分析。\n"
            f"生成文档：{names}\n"
            f"已写入 OpenWorker workspace 记忆 {written} 条（随后续对话自动注入）。\n"
            f"文件同时存于：{folder.rstrip('/')}/.deskmate/\n"
            f"下次讨论人物前，agent 会自动参考这份圣经；写了新章节后可重跑本工具刷新。"
        )
    except Exception as e:  # 让 agent 看到明确错误，而不是崩溃
        return f"❌ analyze_novel 失败：{e}"


@mcp.tool()
def novel_recall(query: str, folder: str) -> str:
    """跨章检索：返回与 query 相关的人物圣经段落 + 正文中命中关键词的片段。
    用于「第三卷某处伏笔」「A 和 B 在哪几章同框」这类全书级问题。

    Args:
        query: 自然语言问题或关键词。
        folder: 小说文件夹绝对路径（当前工作区）。
    """
    try:
        return _core.novel_recall(query, folder)
    except Exception as e:
        return f"❌ novel_recall 失败：{e}"


@mcp.tool()
def novel_check_consistency(folder: str, new_text: str) -> str:
    """检测新写的章节是否与人物圣经中的关系存在矛盾。

    使用双层检测：正则快速检测 + LLM 语义分析（需配置 LLM provider）。
    能发现显式矛盾（敌/友关键词）和隐含矛盾（投靠、背叛、暗通、行为不一致等）。

    Args:
        folder: 小说文件夹的绝对路径。
        new_text: 新写的章节文本。
    """
    try:
        bible = read_bible(folder)
        if not bible:
            return "（尚未生成人物圣经，请先运行 analyze_novel）"
        result = check_consistency_enhanced(bible, new_text)
        if result["consistent"]:
            return (
                f"✅ 已检查 {result['checked_pairs']} 组人物关系，未发现矛盾。"
            )
        lines = [f"⚠️ 发现 {len(result['issues'])} 处潜在矛盾："]
        for i, issue in enumerate(result["issues"], 1):
            lines.append(f"{i}. {issue}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ novel_check_consistency 失败：{e}"


@mcp.tool()
def novel_relationships(character: str, folder: str) -> str:
    """查询某角色在人物圣经中的所有关系（敌/友/亲等）。

    Args:
        character: 角色名。
        folder: 小说文件夹绝对路径（当前工作区）。
    """
    try:
        return _core.novel_relationships(character, folder)
    except Exception as e:
        return f"❌ novel_relationships 失败：{e}"


@mcp.tool()
def novel_analyze_incremental(folder: str) -> str:
    """增量更新人物圣经：只扫描新增/修改过的章节，合并到现有圣经中。

    与 analyze_novel（全量重建）不同，本工具只处理自上次分析以来修改过的文件，
    大幅减少 token 消耗和时间。适合「写了新章节后刷新圣经」的场景。

    Args:
        folder: 小说文件夹的绝对路径。
    """
    try:
        docs = _core.incremental_bible_update(folder)
        if docs is None:
            return "（尚未生成人物圣经，请先运行 analyze_novel 进行全量分析）"
        if docs == _core.read_bible(folder):
            return "✅ 未发现新增/修改章节，圣经已是最新。"
        written = _core.inject_bible(folder, docs)
        names = "、".join(docs.keys()) or "（无）"
        return (
            f"✅ 增量更新完成。\n"
            f"更新文档：{names}\n"
            f"已写入 OpenWorker workspace 记忆 {written} 条。\n"
            f"新写的章节内容已合并到人物圣经中。"
        )
    except Exception as e:
        return f"❌ novel_analyze_incremental 失败：{e}"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="deskmate-novel-memory MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输模式 (default: stdio)")
    parser.add_argument("--port", type=int, default=8094, help="SSE/HTTP 端口 (default: 8094)")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"deskmate-novel-memory MCP server listening on port {args.port} ({args.transport})")
        mcp.run(transport=args.transport, mount_path=f"/{args.transport}")


if __name__ == "__main__":
    main()
