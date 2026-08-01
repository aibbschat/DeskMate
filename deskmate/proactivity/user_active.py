"""用户活跃状态检测 — 判断用户是否在桌前、是否忙碌。"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum


class UserActivity(Enum):
    """用户活跃度等级。"""
    ACTIVE = "active"      # 在桌前，正常使用
    IDLE = "idle"          # 离开（键盘/鼠标无输入 > 5 分钟）
    BUSY = "busy"          # 在桌前但忙碌（会议、通话等）


@dataclass
class ActivityState:
    """用户活跃状态。"""
    level: UserActivity
    idle_seconds: int          # 系统空闲时间（秒）
    window_title: str          # 当前窗口标题
    app_name: str              # 当前应用名
    reason: str                # 判断理由


# 空闲阈值（秒）
_IDLE_THRESHOLD_SECONDS = 300  # 5 分钟

# 忙碌应用关键词（会议、通话类）
_BUSY_APP_KEYWORDS = {
    "zoom", "teams", "meet", "slack call", "discord",
    "facetime", "skype", "webex", "ringcentral",
    "腾讯会议", "飞书会议", "钉钉",
}


def get_system_idle_seconds() -> int:
    """获取系统空闲时间（秒）。仅 macOS，通过 IOHIDSystem 获取。"""
    try:
        out = subprocess.check_output(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            text=True,
            timeout=5,
        )
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        if m:
            # IOHIDSystem 返回纳秒，转为秒
            return int(m.group(1)) // 1_000_000_000
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return 0


def _is_meeting_app(app_name: str, window_title: str) -> bool:
    """检测当前是否在会议/通话应用中。"""
    combined = f"{app_name} {window_title}".lower()
    return any(kw in combined for kw in _BUSY_APP_KEYWORDS)


def detect_user_activity(
    app_name: str = "",
    window_title: str = "",
) -> ActivityState:
    """检测用户当前活跃状态。

    Args:
        app_name: 当前应用名（如已知，可直接传入避免重复查询）
        window_title: 当前窗口标题

    Returns:
        ActivityState
    """
    idle_sec = get_system_idle_seconds()

    # 判断忙碌
    if _is_meeting_app(app_name, window_title):
        return ActivityState(
            level=UserActivity.BUSY,
            idle_seconds=idle_sec,
            window_title=window_title,
            app_name=app_name,
            reason=f"会议/通话应用: {app_name}",
        )

    # 判断空闲
    if idle_sec >= _IDLE_THRESHOLD_SECONDS:
        return ActivityState(
            level=UserActivity.IDLE,
            idle_seconds=idle_sec,
            window_title=window_title,
            app_name=app_name,
            reason=f"系统空闲 {idle_sec // 60} 分钟",
        )

    # 活跃
    return ActivityState(
        level=UserActivity.ACTIVE,
        idle_seconds=idle_sec,
        window_title=window_title,
        app_name=app_name,
        reason="用户在桌前活跃",
    )
