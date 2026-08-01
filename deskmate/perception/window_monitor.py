"""活动窗口监控 — 通过 AppleScript 获取 macOS 当前活动窗口信息。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class ActiveWindow:
    app: str          # 应用名（如 "Safari", "VS Code"）
    title: str        # 窗口标题
    bundle_id: str    # 应用 Bundle ID（如 "com.apple.Safari"）
    pid: int          # 进程 ID
    url: str | None   # 浏览器 URL（仅 Safari / Chrome）


def get_active_window() -> ActiveWindow:
    """获取当前活动窗口信息。仅 macOS。"""
    script = '''
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
        set appBundle to bundle identifier of frontApp
        set appPID to unix id of frontApp
        try
            set windowTitle to name of front window of frontApp
        on error
            set windowTitle to ""
        end try
    end tell
    return appName & "|||" & windowTitle & "|||" & appBundle & "|||" & (appPID as text)
    '''
    result = _run_osascript(script)
    if not result:
        return ActiveWindow(app="unknown", title="", bundle_id="", pid=0, url=None)

    parts = result.split("|||")
    app = parts[0].strip() if len(parts) > 0 else ""
    title = parts[1].strip() if len(parts) > 1 else ""
    bundle_id = parts[2].strip() if len(parts) > 2 else ""
    try:
        pid = int(parts[3].strip()) if len(parts) > 3 else 0
    except ValueError:
        pid = 0

    url = _get_browser_url(bundle_id, app)
    return ActiveWindow(app=app, title=title, bundle_id=bundle_id, pid=pid, url=url)


def _get_browser_url(bundle_id: str, app_name: str) -> str | None:
    """如果活动应用是浏览器，获取当前标签页 URL。"""
    if "safari" in bundle_id.lower():
        return _run_osascript('tell application "Safari" to return URL of current tab of front window')
    if "chrome" in bundle_id.lower():
        return _run_osascript('tell application "Google Chrome" to return URL of active tab of front window')
    if "edge" in bundle_id.lower():
        return _run_osascript('tell application "Microsoft Edge" to return URL of active tab of front window')
    return None


def _run_osascript(script: str) -> str | None:
    """运行 AppleScript 并返回结果。"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
