"""上下文组装 — 收集感知数据和记忆，组装为 LLM 推理所需的上下文。

感知数据统一从 deskmate.perception 子模块获取，避免与 MCP server 重复实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from deskmate.perception.accessibility_reader import read_frontmost_app_text
from deskmate.perception.calendar_reader import get_calendar_events
from deskmate.perception.clipboard_monitor import read_clipboard
from deskmate.perception.file_reader import extract_file_path_from_title, read_file_safe
from deskmate.perception.filesystem_watcher import FilesystemWatcher
from deskmate.perception.window_monitor import get_active_window


@dataclass
class PerceptionContext:
    """感知上下文。"""
    active_window: str          # 当前活动窗口描述
    active_file_content: str    # 当前活动文件内容（如有）
    recent_files: str           # 最近文件变更
    clipboard: str              # 剪贴板内容
    calendar: str               # 日历事件


@dataclass
class ProactiveContext:
    """完整的主动推理上下文。"""
    perception: PerceptionContext
    memory_context: str         # 相关记忆
    user_activity: str          # 用户活跃状态描述
    project_context: str        # 项目上下文


# ---------------------------------------------------------------------------
# 感知数据获取（复用 perception 模块，避免与 MCP server 代码重复）
# ---------------------------------------------------------------------------


def get_active_window_info() -> tuple[str, str]:
    """获取当前活动窗口信息。返回 (窗口描述, 文件内容)。"""
    try:
        w = get_active_window()
        parts = [f"应用：{w.app}"]
        if w.title:
            parts.append(f"标题：{w.title}")
        if w.bundle_id:
            parts.append(f"Bundle ID：{w.bundle_id}")
        if w.url:
            parts.append(f"URL：{w.url}")

        # 读取前台应用文本内容（两层策略）
        file_content = ""

        # 1. 优先：Accessibility API 直接读取（适用于所有应用）
        ax_text = read_frontmost_app_text(max_chars=12000)
        if ax_text and len(ax_text) > 100:
            file_content = ax_text

        # 2. 回退：从窗口标题提取文件路径读取（适用于 Word/编辑器）
        if not file_content:
            file_path = extract_file_path_from_title(w.title, w.app)
            if file_path:
                file_content = read_file_safe(file_path, max_chars=8000)

        return " | ".join(parts), file_content
    except Exception:
        return "", ""


def get_clipboard_preview(max_chars: int = 500) -> str:
    """获取剪贴板文本预览。复用 perception.clipboard_monitor。"""
    try:
        return read_clipboard(max_chars=max_chars)
    except Exception:
        return ""


def get_recent_file_changes(workspace: str | None = None, minutes: int = 5) -> str:
    """获取最近 N 分钟内的文件变更。复用 perception.filesystem_watcher。

    如果提供了 workspace，监控该目录；否则返回空。
    """
    if not workspace:
        return ""
    try:
        watcher = FilesystemWatcher(root=workspace)
        watcher.poll()
        changes = watcher.get_recent(minutes=minutes)
        if not changes:
            return f"（最近 {minutes} 分钟内无文件变更）"
        lines = [f"最近 {minutes} 分钟内 {len(changes)} 个文件变更："]
        for c in changes[:30]:
            icon = {"created": "+", "modified": "~", "deleted": "-"}[c.kind]
            lines.append(f"  {icon} {c.path}")
        if len(changes) > 30:
            lines.append(f"  …（共 {len(changes)} 条）")
        return "\n".join(lines)
    except Exception:
        return ""


def get_calendar_summary(days: int = 1, max_events: int = 5) -> str:
    """获取日历事件摘要。复用 perception.calendar_reader。"""
    try:
        events = get_calendar_events(days=days, max_events=max_events)
        if not events:
            return ""
        lines = [f"未来 {days} 天内 {len(events)} 个事件："]
        for evt in events:
            lines.append(f"  • {evt.summary}  ({evt.start} — {evt.end})")
            if evt.location:
                lines.append(f"    地点：{evt.location}")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 上下文组装
# ---------------------------------------------------------------------------


def build_perception_context(
    workspace: str | None = None,
) -> PerceptionContext:
    """组装感知上下文。"""
    window_info, file_content = get_active_window_info()
    return PerceptionContext(
        active_window=window_info,
        active_file_content=file_content,
        recent_files=get_recent_file_changes(workspace),
        clipboard=get_clipboard_preview(),
        calendar=get_calendar_summary(),
    )


def build_proactive_context(
    workspace: str | None = None,
    memory_context: str = "",
    user_activity: str = "",
    project_context: str = "",
) -> ProactiveContext:
    """组装完整的主动推理上下文。"""
    return ProactiveContext(
        perception=build_perception_context(workspace),
        memory_context=memory_context,
        user_activity=user_activity,
        project_context=project_context,
    )


def format_context_for_judge(ctx: ProactiveContext) -> str:
    """将上下文格式化为 LLM 推理用的文本。"""
    parts: list[str] = []

    parts.append("## 当前桌面状态")
    if ctx.perception.active_window:
        parts.append(f"- 活动窗口: {ctx.perception.active_window}")
    if ctx.perception.active_file_content:
        parts.append(f"- 当前文件内容:\n{ctx.perception.active_file_content}")
    if ctx.perception.recent_files:
        parts.append(f"- 最近文件变更:\n{ctx.perception.recent_files}")
    if ctx.perception.clipboard:
        parts.append(f"- 剪贴板内容: {ctx.perception.clipboard}")
    if ctx.perception.calendar:
        parts.append(f"- 今日日历: {ctx.perception.calendar}")

    if ctx.user_activity:
        parts.append(f"\n## 用户状态\n{ctx.user_activity}")

    if ctx.project_context:
        parts.append(f"\n## 项目上下文\n{ctx.project_context}")

    if ctx.memory_context:
        parts.append(f"\n## 相关记忆\n{ctx.memory_context}")

    return "\n".join(parts)
