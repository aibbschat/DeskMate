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

# 忙碌判定（会议/通话类）—— 收紧，避免普通网页标题误伤。
# 1) 原生会议/通话 App：按「前端应用名」精确匹配（最可靠）。
#    注意：Slack / Discord 是即时通讯（不总是会议），从本集合移除，
#    仅当标题出现明确通话特征时才判忙碌（见下方标题特征），避免「只是看消息」就被判忙碌。
_MEETING_APP_NAMES = {
    "zoom", "zoom.us", "microsoft teams", "teams",
    "google meet", "facetime", "skype", "webex", "ringcentral",
    "腾讯会议", "飞书", "钉钉",
}

# 2) 浏览器内的会议网页：仅当窗口标题命中「较完整的会议特征短语」才判忙碌，
#    不再用 "meet"/"teams" 这种短词子串匹配（普通页面标题很容易带这些词）。
_MEETING_TITLE_MARKERS = (
    "google meet", "腾讯会议", "zoom meeting", "zoom 会议",
    "· microsoft teams", "microsoft teams meeting",
    "| teams", "- teams", " · meet", "meet ·",
    "语音通话", "视频会议", "slack huddle", "discord |",
)

# 常见浏览器（仅这些 App 才用上面的标题特征判定，普通 App 不按标题判）
_BROWSER_APP_NAMES = {
    "google chrome", "safari", "microsoft edge", "firefox",
    "arc", "brave browser", "opera",
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
    """检测当前是否在会议/通话应用中（收紧，减少误判）。

    判定优先级：
    1. 前端应用本身就是已知会议/通话 App（按应用名精确匹配）→ 直接判忙碌，最可靠。
    2. 前端是浏览器，且窗口标题命中较完整的会议特征短语 → 判忙碌
       （避免普通网页标题里碰巧带 "meet"/"teams" 等短词而误判）。
    3. 其余（普通 App、普通网页/文档）→ 不判忙碌。
    """
    app = (app_name or "").strip().lower()
    title = (window_title or "").strip().lower()

    # 1) 原生会议 App
    if app in _MEETING_APP_NAMES:
        return True

    # 2) 浏览器内的会议网页：仅当标题命中完整会议特征短语
    if app in _BROWSER_APP_NAMES:
        return any(marker in title for marker in _MEETING_TITLE_MARKERS)

    # 非浏览器、又不是已知会议 App → 不算忙碌
    return False


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
