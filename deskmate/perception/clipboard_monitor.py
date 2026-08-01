"""剪贴板读取 — macOS 剪贴板内容获取。"""

from __future__ import annotations

import subprocess


def read_clipboard(max_chars: int = 4000) -> str:
    """读取当前剪贴板文本内容。超过 max_chars 截断。"""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            text = result.stdout
            if len(text) > max_chars:
                return text[:max_chars] + f"\n…（截断，共 {len(text)} 字符）"
            return text
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def clipboard_has_image() -> bool:
    """检查剪贴板是否包含图片。"""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return (the clipboard info) as text'],
            capture_output=True, text=True, timeout=3,
        )
        return "TIFF" in result.stdout or "PNG" in result.stdout or "JPEG" in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
