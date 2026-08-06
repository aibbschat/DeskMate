"""无障碍文本读取 — 通过 macOS Accessibility API 读取前台应用的文本内容。

需要「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）。
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def _cap_and_mark(text: str, max_chars: int) -> str:
    """截断到上限，并附「不完整 + 禁止编造」的明确提示。

    关键：截断时不仅告知全文长度，还要明确告诉模型「这只是片段、不得据此
    补全情节或编造正文中不存在的人名/设定」——否则模型会对有头无尾的文本
    做「合理补全」式脑补（这正是桌搭读长稿瞎编的根因之一）。
    """
    if len(text) <= max_chars:
        return text
    shown = max_chars
    return (
        text[:shown]
        + f"\n\n[⚠️ 以上仅为前台应用文档的「前 {shown:,} 字符」片段，"
        f"全文约 {len(text):,} 字符，并非完整内容。"
        f"不得据此对全文下结论、补全未出现的情节，或编造正文中不存在的人名/设定。"
        f"如需完整审阅，请让用户通过 read_file / read_chapter 工具读取对应文件。]"
    )


def is_truncated_text(text: str) -> bool:
    """判断文本是否由 _cap_and_mark 标记为截断片段。"""
    return "⚠️ 以上仅为前台应用文档" in (text or "")


def read_frontmost_app_text(max_chars: int = 12000) -> str:
    """读取前台应用的文本内容。

    优先用 AXUIElement 直接读取（快、准确），失败则回退到 AppleScript。
    超出上限会截断并明确标注「仅为片段、禁止编造」后返回，或空字符串。
    """
    text = _read_via_axui(max_chars)
    if text and len(text) > 50:
        return _cap_and_mark(text, max_chars)

    # 回退：AppleScript（兼容性更好，但只能读部分应用）
    text = _read_via_applescript(max_chars)
    if text and len(text) > 50:
        return _cap_and_mark(text, max_chars)
    return ""


def _read_via_axui(max_chars: int) -> str:
    """通过 AXUIElement 读取前台应用的文本内容。"""
    try:
        import ApplicationServices as AS
        import Cocoa

        # 获取前台应用的 PID
        workspace = Cocoa.NSWorkspace.sharedWorkspace()
        front_app = workspace.frontmostApplication()
        if not front_app:
            return ""
        pid = front_app.processIdentifier()

        # 创建 AXUIElement 引用
        app_ref = AS.AXUIElementCreateApplication(pid)

        # 获取 focused UI element
        focused = _ax_get_attribute(app_ref, "AXFocusedUIElement")
        if focused:
            text = _extract_text_from_element(focused, max_chars)
            if text and len(text) > 50:
                return text

        # 如果没有 focused element，尝试获取主窗口的文本
        windows = _ax_get_attribute(app_ref, "AXWindows")
        if windows and len(windows) > 0:
            main_window = windows[0]
            text = _extract_text_from_element(main_window, max_chars)
            if text and len(text) > 50:
                return text

    except Exception as e:
        logger.debug("accessibility API read failed: %s", e)
    return ""


def _extract_text_from_element(element, max_chars: int, depth: int = 0) -> str:
    """递归提取 AXUIElement 中的文本内容。"""
    if depth > 15:
        return ""

    parts: list[str] = []

    # 优先读取 AXValue（文本编辑器的完整内容）
    value = _ax_get_attribute(element, "AXValue")
    if isinstance(value, str) and value.strip():
        parts.append(value.strip())

    # 读取 AXSelectedText（用户选中的文本）
    selected = _ax_get_attribute(element, "AXSelectedText")
    if isinstance(selected, str) and selected.strip() and selected != value:
        parts.append(f"[选中: {selected.strip()}]")

    # 如果没有 AXValue，递归子元素
    if not parts:
        children = _ax_get_attribute(element, "AXChildren")
        if children:
            for child in children[:50]:  # 限制子元素数量
                child_text = _extract_text_from_element(child, max_chars, depth + 1)
                if child_text:
                    parts.append(child_text)
                total = sum(len(p) for p in parts)
                if total >= max_chars:
                    break

    # 不在递归内截断/标注；由公开函数 read_frontmost_app_text 统一截断并标注，
    # 避免嵌套调用时重复包裹提示。
    return "\n".join(parts)


def _ax_get_attribute(element, attribute: str):
    """安全获取 AXUIElement 属性。"""
    try:
        import ApplicationServices as AS
        err, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        if err == 0 and value is not None:
            # PyObjC wraps AXValue variants automatically
            if hasattr(value, '__iter__') and not isinstance(value, str):
                return list(value)
            return value
    except Exception as e:
        logger.debug("accessibility attribute read failed: %s", e)
    return None


def _read_via_applescript(max_chars: int) -> str:
    """通过 AppleScript 读取前台应用文本（回退方案）。"""
    script = '''
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
    end tell

    -- 特定应用的 AppleScript 读取
    if appName is "Google Chrome" or appName is "Safari" then
        tell application appName
            try
                set pageText to execute active tab of front window javascript "document.body.innerText"
                return pageText
            end try
        end tell
    end if

    -- 通用：通过 Accessibility 读取文本
    tell application "System Events"
        tell frontApp
            try
                set uiElems to every UI element of front window
                set allText to ""
                repeat with elem in uiElems
                    try
                        set elemValue to value of elem
                        if elemValue is not missing value and class of elemValue is text then
                            set allText to allText & elemValue & linefeed
                        end if
                    end try
                end repeat
                return allText
            end try
        end tell
    end tell
    return ""
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # 返回原始全文，由公开函数统一截断并标注
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def check_accessibility_permission() -> bool:
    """检查是否已授予辅助功能权限。"""
    try:
        import ApplicationServices as AS
        return AS.AXIsProcessTrusted()
    except Exception:
        return False
