"""deskmate-skills MCP 服务（stdio）—— 技能系统工具。

通过 OpenWorker 的 MCP 机制暴露技能管理工具。运行方式：
    <venv>/bin/python -m deskmate.skills.server

工具列表：
- skill_search          搜索匹配技能
- skill_view            查看技能详情
- skill_create          创建新技能
- skill_patch           更新技能（踩坑后修补）
- skill_list            列出所有技能
- skill_archive         归档技能
- skill_stats           技能统计
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .manager import SkillManager

mcp = FastMCP("deskmate-skills")

# 全局单例
_base_dir = Path(os.environ.get("DESKMATE_DIR", os.path.expanduser("~/.deskmate")))
_mgr = SkillManager(base_dir=_base_dir)


def _get_mgr() -> SkillManager:
    return _mgr


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@mcp.tool()
def skill_search(query: str, limit: int = 5) -> str:
    """搜索匹配的技能。

    Args:
        query: 搜索关键词或自然语言描述
        limit: 最大返回数量

    Returns:
        JSON 格式的匹配技能列表
    """
    try:
        skills = _get_mgr().match_skills(query, limit=limit)
        return json.dumps(skills, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"搜索技能失败：{e}"


@mcp.tool()
def skill_view(name: str) -> str:
    """查看技能详情。

    Args:
        name: 技能名称或路径

    Returns:
        技能完整内容（Markdown）
    """
    try:
        skill = _get_mgr().store.read_skill(name)
        if not skill:
            return f"技能不存在：{name}"
        # 仅记录查看量（engagement），不算「成功使用」：浏览 ≠ 技能被真正应用，
        # 不计入 success_rate 与成长系统 XP（A2 修复：原实现把浏览当成功使用，
        # 导致 success_rate 恒为 1.0、成长 XP 被浏览量灌满）。
        _get_mgr().store.bump_view(name)
        # 返回完整内容
        meta_lines = [f"# {skill['meta'].get('name', name)}"]
        if skill["meta"].get("description"):
            meta_lines.append(f"> {skill['meta']['description']}")
        if skill["meta"].get("trigger"):
            meta_lines.append(f"**触发条件**：{skill['meta']['trigger']}")
        if skill["meta"].get("tags"):
            meta_lines.append(f"**标签**：{', '.join(skill['meta']['tags'])}")
        meta_lines.append(f"**使用次数**：{skill['meta'].get('uses', 0)}")
        meta_lines.append("")
        return "\n".join(meta_lines) + skill.get("body", "")
    except Exception as e:
        return f"读取技能失败：{e}"


@mcp.tool()
def skill_create(
    name: str,
    description: str,
    body: str,
    tags: str = "",
    trigger: str = "",
    category: str = "general",
) -> str:
    """创建新技能。

    Args:
        name: 技能名称
        description: 简短描述
        body: 技能正文（Markdown，包含步骤和踩坑记录）
        tags: 逗号分隔的标签
        trigger: 触发条件（用户说什么时该加载此技能）
        category: 分类（writing/coding/office/general）

    Returns:
        操作结果 JSON
    """
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        result = _get_mgr().store.create_skill(
            name=name,
            description=description,
            body=body,
            tags=tag_list,
            trigger=trigger,
            category=category,
        )
        # 成长系统：技能创建事件
        if result.get("status") == "已创建":
            try:
                from deskmate.growth.engine import GrowthEngine

                GrowthEngine(base_dir=_base_dir).record_skill_created(name)
            except Exception:
                pass  # 成长系统异常不阻断技能创建
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def skill_patch(name: str, old_string: str, new_string: str) -> str:
    """更新技能（踩坑后修补）。

    当你发现技能的某个步骤不对、少了踩坑记录、或需要补充新信息时使用。

    Args:
        name: 技能名称
        old_string: 要替换的原文（精确匹配）
        new_string: 替换后的文本

    Returns:
        操作结果
    """
    try:
        result = _get_mgr().store.patch_skill(name, old_string, new_string)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def skill_list(category: str = "") -> str:
    """列出所有技能。

    Args:
        category: 可选分类过滤（writing/coding/office/general）

    Returns:
        JSON 格式的技能列表
    """
    try:
        skills = _get_mgr().store.list_skills(
            category=category if category else None
        )
        return json.dumps(skills, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"列出技能失败：{e}"


@mcp.tool()
def skill_archive(name: str) -> str:
    """归档技能（移到 archive/ 目录，不会删除）。

    Args:
        name: 技能名称

    Returns:
        操作结果
    """
    try:
        result = _get_mgr().store.archive_skill(name)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def skill_restore(name: str) -> str:
    """从 archive/ 还原技能（一键还原，归档不会删除）。

    Args:
        name: 技能名称

    Returns:
        操作结果
    """
    try:
        result = _get_mgr().store.restore_skill(name)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)
    """技能统计信息。

    Returns:
        技能总数、分类分布、使用次数
    """
    try:
        stats = _get_mgr().stats()
        return json.dumps(stats, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取技能统计失败：{e}"


@mcp.tool()
def skill_match_and_inject(user_message: str) -> str:
    """根据用户消息匹配相关技能，返回格式化的技能上下文。

    用于每轮对话前自动注入技能指导。

    Args:
        user_message: 当前用户消息

    Returns:
        <skills-context> 包裹的技能内容，无匹配时返回空串
    """
    try:
        return _get_mgr().format_skills_context(user_message)
    except Exception as e:
        return f"（技能匹配失败：{e}）"


@mcp.tool()
def skill_effectiveness() -> str:
    """查看所有技能的效果评估：使用次数、成功率、最近使用时间。

    用于识别需要优化或归档的低效技能。
    """
    try:
        skills = _get_mgr().store.list_skills()
        # 按成功率排序（低→高）
        rated = [s for s in skills if s.get("uses", 0) > 0]
        unrated = [s for s in skills if s.get("uses", 0) == 0]
        rated.sort(key=lambda x: x.get("success_rate", 0))
        result = {
            "rated": [
                {
                    "name": s["name"],
                    "uses": s["uses"],
                    "success_rate": s.get("success_rate", 0),
                    "last_used": s.get("last_used", ""),
                }
                for s in rated
            ],
            "unrated": [{"name": s["name"]} for s in unrated],
            "low_success": _get_mgr().store.get_low_success_skills(),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"获取技能效果失败：{e}"


@mcp.tool()
def skill_record_use(skill_name: str, success: bool = True, context: str = "") -> str:
    """记录技能使用结果（成功/失败）。

    每次使用技能后调用，用于追踪技能效果。

    Args:
        skill_name: 技能名称
        success: 本次使用是否成功
        context: 使用场景描述（可选）

    Returns:
        操作结果
    """
    try:
        _get_mgr().store.record_use(skill_name, context=context, success=success)
        _get_mgr().store.increment_uses(skill_name, success=success)
        # 成长系统：技能使用事件
        try:
            from deskmate.growth.engine import GrowthEngine

            full = _get_mgr().store.read_skill(skill_name)
            meta = full.get("meta", {}) if full else {}
            GrowthEngine(base_dir=_base_dir).record_skill_use(
                skill_name,
                success=success,
                skill_store_uses=meta.get("uses", 1),
                skill_store_rate=meta.get("success_rate", 0.0),
            )
        except Exception:
            pass  # 成长系统异常不阻断
        return json.dumps({"status": "ok", "skill": skill_name, "success": success}, ensure_ascii=False)
    except Exception as e:
        return f"记录使用失败：{e}"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="deskmate-skills MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输模式 (default: stdio)")
    parser.add_argument("--port", type=int, default=8092, help="SSE/HTTP 端口 (default: 8092)")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"deskmate-skills MCP server listening on port {args.port} ({args.transport})")
        mcp.run(transport=args.transport, mount_path=f"/{args.transport}")


if __name__ == "__main__":
    main()