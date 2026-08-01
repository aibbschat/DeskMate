"""无障碍文本读取 — 通过 macOS Accessibility API 读取前台应用的文本内容。

需要「辅助功能」权限（系统设置 → 隐私与安全性 → 辅助功能）。
"""

from __future__ import annotations

import subprocess
from typing import Optional


def read_frontmost_app_text(max_chars: int = 12000) -> str:
    """读取前台应用的文本内容。

    优先用 AXUIElement 直接读取（快、准确），失败则回退到 AppleScript。
    返回截断后的文本，或空字符串。
    """
    text = _read_via_axui(max_chars)
    if text and len(text) > 50:
        return text

    # 回退：AppleScript（兼容性更好，但只能读部分应用）
    text = _read_via_applescript(max_chars)
    return text or ""


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

    except Exception:
        pass
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

    text = "\n".join(parts)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n…（截断，共 {len(text)} 字符）"
    return text


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
    except Exception:
        pass
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
            text = result.stdout.strip()
            if len(text) > max_chars:
                return text[:max_chars] + f"\n\n…（截断，共 {len(text)} 字符）"
            return text
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
