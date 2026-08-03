"""perception MCP 服务（stdio）— macOS 桌面感知工具。

通过 OpenWorker 的 MCP 机制暴露桌面感知工具。运行方式：
    <venv>/bin/python -m deskmate.perception.server

工具列表：
- get_active_window    当前活动窗口（应用名、标题、URL）
- get_recent_file_changes  最近文件变更
- read_clipboard       剪贴板内容
- get_calendar_events  日历事件
- read_file            安全读取文件（超大文件会提示分页/按章读取，不静默截断）
- list_chapters        列出稿件章节目录与字数
- read_chapter         只读指定的一章（与全书长度无关，长稿不会顶爆上下文）
- list_directory       列出目录内容
- diff_files           对比文件差异
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .calendar_reader import get_calendar_events
from .clipboard_monitor import clipboard_has_image, read_clipboard
from .diff_changes import diff_files
from .file_reader import list_dir, read_chapter, list_chapters, read_file_safe
from .filesystem_watcher import FilesystemWatcher
from .window_monitor import get_active_window

mcp = FastMCP("perception")

# 每个监控目录一个 watcher 实例（惰性创建）
_watchers: dict[str, FilesystemWatcher] = {}


def _get_watcher(folder: str) -> FilesystemWatcher:
    if folder not in _watchers:
        _watchers[folder] = FilesystemWatcher(root=folder)
    return _watchers[folder]


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@mcp.tool()
def get_active_window() -> str:
    """获取当前活动窗口信息：正在使用的应用名、窗口标题、Bundle ID。
    如果是浏览器，还会获取当前标签页的 URL。

    Returns:
        格式化的窗口信息字符串
    """
    try:
        w = get_active_window()
        lines = [
            f"应用：{w.app}",
            f"标题：{w.title or '（无标题）'}",
            f"Bundle ID：{w.bundle_id}",
            f"PID：{w.pid}",
        ]
        if w.url:
            lines.append(f"URL：{w.url}")
        return "\n".join(lines)
    except Exception as e:
        return f"（获取活动窗口失败：{e}）"


@mcp.tool()
def get_recent_file_changes(folder: str, minutes: int = 5) -> str:
    """获取指定文件夹中最近 N 分钟内的文件变更（新增/修改/删除）。
    首次调用会建立基线快照，后续调用返回增量变更。

    Args:
        folder: 要监控的文件夹绝对路径
        minutes: 回溯分钟数（默认5）

    Returns:
        变更列表，每行一个文件变更
    """
    try:
        watcher = _get_watcher(folder)
        watcher.poll()  # 触发一次轮询，更新内部状态
        changes = watcher.get_recent(minutes=minutes)
        if not changes:
            return f"（最近 {minutes} 分钟内无文件变更）"
        lines = [f"最近 {minutes} 分钟内 {len(changes)} 个文件变更："]
        for c in changes[:50]:  # 最多显示50条
            icon = {"created": "+", "modified": "~", "deleted": "-"}[c.kind]
            lines.append(f"  {icon} {c.path}")
        if len(changes) > 50:
            lines.append(f"  …（共 {len(changes)} 条）")
        return "\n".join(lines)
    except Exception as e:
        return f"（获取文件变更失败：{e}）"


@mcp.tool()
def read_clipboard_text(max_chars: int = 4000) -> str:
    """读取当前剪贴板中的文本内容。

    Args:
        max_chars: 最大返回字符数（默认4000）

    Returns:
        剪贴板文本内容
    """
    try:
        text = read_clipboard(max_chars=max_chars)
        has_img = clipboard_has_image()
        if not text and has_img:
            return "（剪贴板中是图片，无文本内容）"
        if not text:
            return "（剪贴板为空）"
        return text
    except Exception as e:
        return f"（读取剪贴板失败：{e}）"


@mcp.tool()
def get_calendar_events_tool(days: int = 1, max_events: int = 20) -> str:
    """获取 macOS 日历中未来 N 天的事件。需要 Calendar.app 的辅助功能权限。

    Args:
        days: 查看未来几天（默认1，即今天）
        max_events: 最多返回多少条事件（默认20）

    Returns:
        格式化的事件列表
    """
    try:
        events = get_calendar_events(days=days, max_events=max_events)
        if not events:
            return f"（未来 {days} 天内无日历事件）"
        lines = [f"未来 {days} 天内 {len(events)} 个事件："]
        for evt in events:
            lines.append(f"  • {evt.summary}")
            lines.append(f"    时间：{evt.start} — {evt.end}")
            if evt.location:
                lines.append(f"    地点：{evt.location}")
            if evt.calendar:
                lines.append(f"    日历：{evt.calendar}")
        return "\n".join(lines)
    except Exception as e:
        return f"（获取日历事件失败：{e}）"


@mcp.tool()
def read_file(path: str, max_chars: int = 40000) -> str:
    """安全读取文件内容（支持文本/代码/配置/小说等常见格式）。

    超大文件**不会静默截断**：超出默认上限时会返回全文总字数、章节目录，
    并提示改用 list_chapters / read_chapter 按章读取，同时明确警告「不得声称读完了」。
    二进制文件会被跳过。

    Args:
        path: 文件绝对路径
        max_chars: 最大返回字符数（默认40000，仅作为分页窗口）

    Returns:
        文件内容或错误信息 / 分页引导
    """
    try:
        return read_file_safe(path, max_chars=max_chars)
    except Exception as e:
        return f"（读取文件失败：{e}）"


@mcp.tool()
def list_chapters(path: str) -> str:
    """列出稿件的章节目录与每章字数——读长稿前先看目录，再决定读哪一章。

    适用于 .docx / .txt / .md 等小说稿件。返回格式：
      《书名》共 N 节，全文 M 字：
        1. 楔子（xxx 字）
        2. 第一章：xxx（xxx 字）
      …

    Args:
        path: 稿件绝对路径

    Returns:
        章节目录（含每章字数）与读取建议；文件不存在/不支持时返回错误
    """
    try:
        return list_chapters(path)
    except Exception as e:
        return f"（读取章节目录失败：{e}）"


@mcp.tool()
def read_chapter(path: str, chapter: str, max_chars: int = 100000) -> str:
    """只读指定的一章——与全书长度无关，长稿也不会顶爆上下文。

    这是读长篇小说的**首选方式**：无论全书十几万字还是几十万字，每次只取
    一章（通常几千字），绝不会因整本读取触发上下文压缩而丢失内容、进而编造。

    chapter 可以是序号（从 1 起）、完整标题，或标题片段（如「第二章」「财当道」）。

    Args:
        path: 稿件绝对路径
        chapter: 章节序号 / 完整标题 / 标题片段
        max_chars: 单章上限（默认100000，正常章节远小于此）

    Returns:
        以【章节标题】（字数，本章完整）开头的正文；未找到/不支持时返回错误
    """
    try:
        return read_chapter(path, chapter, max_chars=max_chars)
    except Exception as e:
        return f"（读取章节失败：{e}）"


@mcp.tool()
def list_directory(path: str, max_items: int = 100) -> str:
    """列出目录内容（不递归），显示文件名和大小。

    Args:
        path: 目录绝对路径
        max_items: 最多显示多少项（默认100）

    Returns:
        格式化的目录列表
    """
    try:
        return list_dir(path, max_items=max_items)
    except Exception as e:
        return f"（列出目录失败：{e}）"


@mcp.tool()
def diff_two_files(old_path: str, new_path: str, context_lines: int = 3) -> str:
    """对比两个文件的内容差异，返回 unified diff 格式。

    Args:
        old_path: 旧版本文件路径
        new_path: 新版本文件路径
        context_lines: diff 上下文行数（默认3）

    Returns:
        unified diff 格式的差异文本
    """
    try:
        return diff_files(old_path, new_path, context_lines=context_lines)
    except Exception as e:
        return f"（文件对比失败：{e}）"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="deskmate-perception MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio",
                        help="传输模式 (default: stdio)")
    parser.add_argument("--port", type=int, default=8093, help="SSE/HTTP 端口 (default: 8093)")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"deskmate-perception MCP server listening on port {args.port} ({args.transport})")
        mcp.run(transport=args.transport, mount_path=f"/{args.transport}")


if __name__ == "__main__":
    main()
