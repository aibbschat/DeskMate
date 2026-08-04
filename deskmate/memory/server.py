"""deskmate-memory MCP 服务（stdio）- 记忆系统工具。

通过 OpenWorker 的 MCP 机制暴露记忆管理工具。运行方式：
    <venv>/bin/python -m deskmate.memory.server

工具列表：
- memory_read          读取记忆（SOUL.md / USER.md / MEMORY.md）
- memory_write         写入记忆
- memory_search        搜索记忆
- memory_reset         重置记忆
- set_active_project   设置活跃项目
- list_projects        列出所有项目
- build_context        组装完整记忆上下文（prefetch）
- vault_write          写入 Obsidian vault（带 hash 保护）
- vault_read           读取 vault 中的单个文件
- vault_search         搜索 vault（CJK 2-gram 分词）
- vault_list           列出 vault 文件
- vault_delete         删除 vault 文件
- diagnostics_health   全局诊断健康报告
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .manager import DeskMateMemoryManager
from ..diagnostics import diagnostics_health

mcp = FastMCP("deskmate-memory")

# 全局单例
_base_dir = Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
_mgr = DeskMateMemoryManager(base_dir=_base_dir)


def _get_mgr() -> DeskMateMemoryManager:
    return _mgr


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_read(section: str = "all") -> str:
    """读取 DeskMate 记忆文件。

    Args:
        section: 记忆类型 — 'soul'（人格运行时状态/演化层）| 'user'（用户画像）| 'memory'（全局记忆）| 'all'（全部）

    Returns:
        对应记忆文件的内容
    """
    try:
        return _get_mgr().read_memory(section)
    except Exception as e:
        return f"读取记忆失败：{e}"


@mcp.tool()
def memory_write(section: str, content: str) -> str:
    """写入或更新 DeskMate 记忆文件。

    Args:
        section: 记忆类型 — 'soul'（人格运行时状态/演化层）| 'user'（用户画像）| 'memory'（全局记忆）
        content: 新的完整内容（会覆盖旧内容）

    Returns:
        操作结果
    """
    try:
        return _get_mgr().write_memory(section, content)
    except Exception as e:
        return f"写入记忆失败：{e}"


@mcp.tool()
def memory_append(section: str, new_entry: str, target_section: str = "") -> str:
    """向记忆文件追加新内容（不覆盖现有内容），并按 § 分节自动归并。

    Args:
        section: 记忆类型 — 'user'（用户画像）| 'memory'（全局记忆）
        new_entry: 要追加的新条目文本（可带首行 #标签，如 "#correction 用户纠正…"）
        target_section: 可选，强制归入的 § 分节 id（如 "纠正" / "correction"）；留空则自动路由

    Returns:
        操作结果

    注意：自动写入只动 MEMORY/USER，绝不写 SOUL。
    """
    try:
        return _get_mgr().append_to_section(
            section, new_entry, target_section=target_section or None
        )
    except Exception as e:
        return f"追加记忆失败：{e}"


@mcp.tool()
def post_session_summary(user_message: str = "", assistant_message: str = "") -> str:
    """会话结束钩子：抽取本轮（及最近若干轮）对话中新学到的偏好 / 纠正 / 项目进展并归档。

    应在每轮对话结束前调用。自动把偏好写入 USER.md、纠正/进展写入 MEMORY.md 对应 §。

    Args:
        user_message: 当前轮用户消息
        assistant_message: 当前轮助手消息

    Returns:
        抽取结果摘要（JSON）

    注意：🛡️ 只写 MEMORY/USER，绝不写 SOUL；无模型密钥或异常时静默跳过。
    """
    try:
        import json

        result = _get_mgr().post_session_summary(user_message, assistant_message)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return f"会话归档失败（已跳过）：{e}"


@mcp.tool()
def memory_full_context(user_message: str = "") -> str:
    """构造每轮对话要注入的完整记忆上下文（相关记忆 + 完整用户画像）。

    用于「每轮自动注入记忆」的 opt-in 能力。区别于 build_memory_context（仅相关片段）。

    Args:
        user_message: 当前用户消息（用于 prefetch 相关片段）

    Returns:
        包裹在 <memory-context> 标签中的完整上下文
    """
    try:
        return _get_mgr().build_full_context_for_turn(user_message)
    except Exception as e:
        return f"构造完整上下文失败：{e}"


@mcp.tool()
def memory_search(query: str, scope: str = "all") -> str:
    """在记忆中搜索相关内容。

    Args:
        query: 搜索关键词或自然语言查询
        scope: 搜索范围 — 'global'（全局记忆）| 'project'（项目记忆）| 'all'（全部）

    Returns:
        匹配的记忆片段
    """
    try:
        mgr = _get_mgr()
        parts: list[str] = []
        if scope in ("global", "all"):
            result = mgr._search_global_memory(query)
            if result:
                parts.append(f"【全局记忆】\n{result}")
        if scope in ("project", "all") and mgr.active_project:
            result = mgr._search_project_memory(query, mgr.active_project)
            if result:
                parts.append(f"【项目记忆：{mgr.active_project}】\n{result}")
        if not parts:
            return "（未找到相关记忆）"
        combined = "\n\n".join(parts)
        if len(combined) > 6000:
            combined = combined[:6000] + "\n…"
        return combined
    except Exception as e:
        return f"搜索记忆失败：{e}"


@mcp.tool()
def memory_reset(section: str) -> str:
    """重置指定记忆文件为默认值。谨慎使用。

    Args:
        section: 记忆类型 - 'soul'（人格运行时状态/演化层）| 'user'（用户画像）

    Returns:
        操作结果
    """
    try:
        return _get_mgr().reset_memory(section)
    except Exception as e:
        return f"重置记忆失败：{e}"


@mcp.tool()
def memory_replace(section: str, old_text: str, new_text: str) -> str:
    """精确替换记忆中的某段文本（用于纠正过时信息）。

    当用户说「之前说的XX不对，应该是YY」时，用本工具把旧记忆替换为新记忆，
    而不是追加（追加会导致新旧并存、矛盾）。

    Args:
        section: 记忆类型 - 'user'（用户画像）| 'memory'（全局记忆）
        old_text: 要被替换的原文（必须是记忆中的精确子串）
        new_text: 替换后的新文本

    Returns:
        操作结果
    """
    try:
        return _get_mgr().replace_section_entry(section, old_text, new_text)
    except Exception as e:
        return f"替换记忆失败：{e}"


@mcp.tool()
def memory_remove(section: str, text_to_remove: str) -> str:
    """从记忆中精确删除某段文本（用于淘汰过时信息）。

    当用户说「忘掉XX」「这个不再适用了」时用本工具。删除后自动清理多余空行。

    Args:
        section: 记忆类型 - 'user'（用户画像）| 'memory'（全局记忆）
        text_to_remove: 要删除的文本（必须是记忆中的精确子串）

    Returns:
        操作结果
    """
    try:
        return _get_mgr().remove_section_entry(section, text_to_remove)
    except Exception as e:
        return f"删除记忆失败：{e}"


@mcp.tool()
def set_active_project(project_name: str = "") -> str:
    """设置当前活跃项目。设置后，该项目的记忆会自动注入每轮对话。

    Args:
        project_name: 项目名称（传空字符串取消活跃项目）

    Returns:
        操作结果
    """
    try:
        name = project_name if project_name else None
        return _get_mgr().set_active_project(name)
    except Exception as e:
        return f"设置活跃项目失败：{e}"


@mcp.tool()
def list_memory_projects() -> str:
    """列出所有有记忆的项目。

    Returns:
        项目列表
    """
    try:
        projects = _get_mgr().get_projects()
        if not projects:
            return "（暂无项目记忆）"
        lines = [f"共 {len(projects)} 个项目："]
        for p in projects:
            lines.append(f"  • {p}")
        return "\n".join(lines)
    except Exception as e:
        return f"列出项目失败：{e}"


@mcp.tool()
def vault_write(
    content: str,
    category: str = "note",
    tags: str = "",
    filename: str = "",
) -> str:
    """向 Obsidian vault（~/.deskmate/vault/）写入一条结构化记忆。

    走 VaultStore 写入，自动添加 YAML frontmatter（type/tags/created/updated/hash）。
    hash 用于检测用户是否在 Obsidian 中手动编辑过——若已编辑则不覆盖，创建 -v2 版本。

    适合写入富文本记忆（多段、标题、双向链接 [[...]]），而非 MEMORY.md 的一行摘要。

    Args:
        content: 记忆正文（支持 Markdown：标题、列表、[[双向链接]] 等）
        category: 分类 - 'preference'（偏好）| 'correction'（纠正）| 'work'（进展）| 'note'（知识）
        tags: 逗号分隔的标签（如 "writing, 萧盛, 风格"），留空则无标签
        filename: 自定义文件名（不含扩展名）；留空则从内容自动生成

    Returns:
        写入的文件路径（相对 vault 根），或跳过原因
    """
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        return _get_mgr().vault.write_entry(
            content=content,
            category=category,
            tags=tag_list,
            source="mcp-vault_write",
            filename=filename,
        )
    except Exception as e:
        return f"写入 vault 失败：{e}"


@mcp.tool()
def vault_read(filename: str) -> str:
    """读取 vault 中的单个记忆文件（含 YAML frontmatter）。

    Args:
        filename: 文件路径（相对 vault 根，如 "preferences/萧盛写作风格.md"），
                  或仅文件名（如 "萧盛写作风格"，自动搜索）

    Returns:
        文件完整内容（含 frontmatter），或未找到提示
    """
    try:
        from pathlib import Path

        vault = _get_mgr().vault
        vault_dir = vault.vault_dir

        # 直接路径
        target = vault_dir / filename
        if not target.suffix:
            target = target.with_suffix(".md")
        if target.exists():
            return target.read_text(encoding="utf-8")

        # 按文件名搜索
        stem = Path(filename).stem
        for md_file in vault_dir.rglob("*.md"):
            if md_file.stem == stem:
                return md_file.read_text(encoding="utf-8")

        return f"未找到 vault 文件：{filename}"
    except Exception as e:
        return f"读取 vault 失败：{e}"


@mcp.tool()
def vault_delete(path: str) -> str:
    """永久删除 vault 中的一个记忆文件。

    Args:
        path: 文件路径（相对 vault 根，如 "preferences/喜欢简洁回复.md"），
              或仅文件名（如 "喜欢简洁回复"，自动搜索）

    Returns:
        操作结果
    """
    try:
        return _get_mgr().vault.delete_entry(path)
    except Exception as e:
        return f"删除 vault 文件失败：{e}"


@mcp.tool()
def vault_search(query: str, max_results: int = 5) -> str:
    """在 vault 中搜索相关记忆（CJK 2-gram 分词，支持中文模糊匹配）。

    搜索范围：正文 + tags + 文件名（排除 archive/）。

    Args:
        query: 搜索关键词或自然语言查询
        max_results: 最多返回条数（默认 5）

    Returns:
        匹配的记忆片段（按相关度排序），或未找到提示
    """
    try:
        result = _get_mgr().vault.search(query, max_results=max_results)
        return result if result else "（vault 中未找到相关记忆）"
    except Exception as e:
        return f"搜索 vault 失败：{e}"


@mcp.tool()
def vault_list(category: str = "") -> str:
    """列出 vault 中的所有记忆文件（含分类、标签、创建日期、摘要）。

    Args:
        category: 按分类过滤 - 'preference' | 'correction' | 'work' | 'note' | 'knowledge'；
                  留空则列出全部

    Returns:
        文件列表（JSON 数组字符串）
    """
    try:
        import json

        vault = _get_mgr().vault
        subdir_map = {
            "preference": "preferences",
            "correction": "corrections",
            "work": "work",
            "note": "knowledge",
            "knowledge": "knowledge",
        }
        subdir = subdir_map.get(category.lower().strip(), None) if category else None
        entries = vault.list_entries(subdir)
        if not entries:
            return "（vault 为空）"
        return json.dumps(entries, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"列出 vault 失败：{e}"


@mcp.tool()
def build_memory_context(user_message: str) -> str:
    """根据用户消息，组装相关的记忆上下文（prefetch）。
    用于在对话开始前自动加载相关记忆。

    Args:
        user_message: 当前用户消息

    Returns:
        包裹在 <memory-context> 标签中的相关记忆
    """
    try:
        return _get_mgr().prefetch(user_message)
    except Exception as e:
        return f"组装记忆上下文失败：{e}"


@mcp.tool()
def decay_memory() -> str:
    """执行记忆衰减：按 TTL 规则淘汰过时条目。

    衰减规则：
    - #work 下超过 30 天的条目 → 归档到 vault（可恢复）
    - #note 下超过 7 天的条目 → 删除（一次性信息）
    - #preference / #correction → 永不衰减

    建议在每次对话结束时调用，或定期手动触发。
    """
    try:
        result = _get_mgr().decay_memory()
        return f"衰减完成：归档 {result['archived']} 条，删除 {result['deleted']} 条"
    except Exception as e:
        return f"记忆衰减失败：{e}"


@mcp.tool()
def memory_stats() -> str:
    """获取记忆统计信息：各 section 条目数、字符数、时间跨度、vault 文件数。"""
    try:
        import json
        stats = _get_mgr().get_memory_stats()
        return json.dumps(stats, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取统计失败：{e}"


@mcp.tool()
def diagnostics_health() -> str:
    """返回全局诊断健康报告：总错误数、按模块/工具统计、最近 10 条错误。

    用于快速排查 DeskMate 各模块运行状况。
    """
    try:
        import json
        from ..diagnostics import diagnostics_health as _health

        report = _health()
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取诊断报告失败：{e}"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="deskmate-memory MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输模式 (default: stdio)")
    parser.add_argument("--port", type=int, default=8091, help="SSE/HTTP 端口 (default: 8091)")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"deskmate-memory MCP server listening on port {args.port} ({args.transport})")
        mcp.run(transport=args.transport, mount_path=f"/{args.transport}")


if __name__ == "__main__":
    main()
